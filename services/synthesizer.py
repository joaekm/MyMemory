"""
Synthesizer - Pipeline v7.0

Ansvar:
- Ta emot kurerad rapport från Planner
- Hantera både COMPLETE och ABORT status
- Generera naturligt svar baserat på rapporten
- Anpassa tonalitet och längd efter frågetyp
"""

import os
import yaml
import logging
from google import genai
from google.genai import types

# --- CONFIG LOADER ---
def _load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, 'config', 'my_mem_config.yaml'),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f:
                config = yaml.safe_load(f)
            return config
    raise FileNotFoundError("HARDFAIL: Config not found")

def _load_prompts():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', 'config', 'chat_prompts.yaml'),
        os.path.join(script_dir, 'config', 'chat_prompts.yaml'),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
    raise FileNotFoundError("HARDFAIL: Prompts not found")

CONFIG = _load_config()
PROMPTS = _load_prompts()
LOGGER = logging.getLogger('Synthesizer')

API_KEY = CONFIG['ai_engine']['api_key']
MODEL_LITE = CONFIG['ai_engine']['models']['model_lite']

# AI Client (lazy init)
_AI_CLIENT = None

def _get_ai_client():
    global _AI_CLIENT
    if _AI_CLIENT is None:
        _AI_CLIENT = genai.Client(api_key=API_KEY)
    return _AI_CLIENT


def _synthesize_abort(query: str, gaps: list, reason: str, chat_history: list = None, debug_trace: dict = None) -> dict:
    """
    Hantera ABORT-status: Förklara ärligt vad som saknas.
    """
    prompt_template = PROMPTS.get('synthesizer_abort', {}).get('instruction', '')
    
    if not prompt_template:
        LOGGER.error("HARDFAIL: synthesizer_abort prompt saknas i chat_prompts.yaml")
        raise ValueError("HARDFAIL: synthesizer_abort prompt saknas i chat_prompts.yaml")
    
    gaps_text = "\n".join(f"- {g}" for g in gaps) if gaps else "Ingen specifik information"
    
    synth_prompt = prompt_template.format(
        query=query,
        reason=reason,
        gaps=gaps_text
    )
    
    # Bygg contents med chatthistorik
    contents = []
    if chat_history:
        for msg in chat_history:
            role = "model" if msg['role'] == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg['content'])]))
    
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=synth_prompt)]))
    
    try:
        client = _get_ai_client()
        response = client.models.generate_content(model=MODEL_LITE, contents=contents)
        answer = response.text
        
        LOGGER.debug(f"Synthesizer ABORT LLM-svar: {answer[:500]}...")
        LOGGER.info(f"Synthesizer ABORT: svarslängd={len(answer)} tecken")
        
        if debug_trace is not None:
            debug_trace['synthesizer_llm_raw'] = answer
            debug_trace['synthesizer_status'] = 'ABORT'
        
        return {
            "status": "ABORT",
            "answer": answer
        }
        
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Synthesizer ABORT error: {e}")
        # Ge ett ärligt default-svar vid fel
        return {
            "status": "ERROR",
            "answer": f"Jag kunde inte hitta tillräcklig information för att svara på din fråga. "
                     f"Saknas: {', '.join(gaps) if gaps else reason}"
        }


def synthesize(
    query: str, 
    report: str, 
    gaps: list, 
    status: str = "COMPLETE",
    reason: str = None,
    chat_history: list = None, 
    debug_trace: dict = None
) -> dict:
    """
    Generera svar baserat på Planner-resultat.
    
    Pipeline v7.0: Hanterar både COMPLETE och ABORT status.
    
    Args:
        query: Användarens ursprungliga fråga
        report: Kurerad rapport från Planner
        gaps: Lista med identifierade luckor
        status: "COMPLETE" eller "ABORT"
        reason: Anledning vid ABORT
        chat_history: Tidigare konversation för kontext
        debug_trace: Dict för att samla debug-info (optional)
    
    Returns:
        dict med:
            - status: "OK", "ABORT" eller "ERROR"
            - answer: Genererat svar
    """
    # Hantera ABORT-status
    if status == "ABORT":
        return _synthesize_abort(
            query=query,
            gaps=gaps,
            reason=reason or "Kunde inte hitta tillräcklig information",
            chat_history=chat_history,
            debug_trace=debug_trace
        )
    
    # COMPLETE: Generera svar från rapport
    prompt_template = PROMPTS.get('synthesizer', {}).get('instruction', '')
    if not prompt_template:
        LOGGER.error("HARDFAIL: synthesizer prompt saknas i chat_prompts.yaml")
        raise ValueError("HARDFAIL: synthesizer prompt saknas i chat_prompts.yaml")
    
    # Bygg prompt
    synth_prompt = prompt_template.format(
        report=report,
        gaps=gaps if gaps else "Inga kända luckor",
        query=query
    )
    
    # Bygg contents med chatthistorik
    contents = []
    if chat_history:
        for msg in chat_history:
            role = "model" if msg['role'] == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg['content'])]))
    
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=synth_prompt)]))
    
    try:
        client = _get_ai_client()
        response = client.models.generate_content(model=MODEL_LITE, contents=contents)
        answer = response.text
        
        LOGGER.debug(f"Synthesizer LLM-svar: {answer[:500]}...")
        LOGGER.info(f"Synthesizer COMPLETE: svarslängd={len(answer)} tecken")
        
        # Spara till debug_trace
        if debug_trace is not None:
            debug_trace['synthesizer_llm_raw'] = answer
            debug_trace['synthesizer_status'] = 'COMPLETE'
        
        return {
            "status": "OK",
            "answer": answer
        }
        
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Synthesizer error: {e}")
        raise RuntimeError(f"HARDFAIL: Synthesizer misslyckades: {e}") from e


# --- TEST ---
if __name__ == "__main__":
    # Test COMPLETE
    test_report = """
    ## Sammanfattning
    Mötet den 2025-12-01 handlade om Adda PoC.
    
    ## Detaljer
    - Deltagare: Joakim, Cenk
    - Beslut: Gå vidare med fas 2
    """
    
    print("=== Test COMPLETE ===")
    result = synthesize(
        query="Vad hände på mötet?",
        report=test_report,
        gaps=["Budgetdiskussion saknas"],
        status="COMPLETE",
        chat_history=[]
    )
    print(f"Status: {result['status']}")
    print(f"Svar: {result['answer'][:200]}...")
    
    # Test ABORT
    print("\n=== Test ABORT ===")
    result = synthesize(
        query="Vad kostar projektet?",
        report="",
        gaps=["Budget", "Kostnadskalkyl", "Offert"],
        status="ABORT",
        reason="Ingen budgetinformation hittades",
        chat_history=[]
    )
    print(f"Status: {result['status']}")
    print(f"Svar: {result['answer'][:200]}...")
