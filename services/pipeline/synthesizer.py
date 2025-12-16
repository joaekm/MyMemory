"""
Synthesizer - Pipeline v8.1 "Chameleon Delivery"

Ansvar:
- Ta emot kurerad rapport från Planner (current_synthesis + facts)
- Hantera både COMPLETE och ABORT status
- Chameleon Delivery: Anpassa format baserat på delivery_format
- Modellval: FAST för struktur, PRO för analys
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
        os.path.join(script_dir, '..', '..', 'config', 'my_mem_config.yaml'),
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
        os.path.join(script_dir, '..', '..', 'config', 'chat_prompts.yaml'),
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
MODEL_PRO = CONFIG['ai_engine']['models'].get('model_pro', MODEL_LITE)

# Ägarens namn för Synthesizer-prompten
OWNER_NAME = CONFIG.get('owner', {}).get('profile', {}).get('full_name', 'användaren')

# v8.1: Format-baserat modellval
# Struktur-format (bara formatering - FAST model)
FAST_FORMATS = [
    "Formellt Mötesprotokoll",
    "Professionellt Email-utkast",
    "Kondenserad Punktlista",
    "Teknisk Rapport"
]

# Analys-format (kräver resonemang - PRO model)
PRO_FORMATS = ["Narrativ Analys"]

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
    synth_config = PROMPTS.get('synthesizer_abort', {})
    role_template = synth_config.get('role', '')
    prompt_template = synth_config.get('instruction', '')
    
    if not prompt_template:
        LOGGER.error("HARDFAIL: synthesizer_abort prompt saknas i chat_prompts.yaml")
        raise ValueError("HARDFAIL: synthesizer_abort prompt saknas i chat_prompts.yaml")
    
    # Formatera role med owner_name
    role_text = role_template.format(owner_name=OWNER_NAME) if role_template else ""
    full_template = f"{role_text}\n\n{prompt_template}" if role_text else prompt_template
    
    gaps_text = "\n".join(f"- {g}" for g in gaps) if gaps else "Ingen specifik information"
    
    synth_prompt = full_template.format(
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
    debug_trace: dict = None,
    # v8.1: Chameleon Delivery
    op_mode: str = "query",
    delivery_format: str = "Narrativ Analys",
    current_synthesis: str = "",
    facts: list = None,
    # Debug mode
    debug_mode: bool = False
) -> dict:
    """
    Generera svar baserat på Planner-resultat.
    
    Pipeline v8.1: Chameleon Delivery - dynamiskt format och modellval.
    
    Args:
        query: Användarens ursprungliga fråga
        report: Kurerad rapport från Planner (legacy, används om current_synthesis saknas)
        gaps: Lista med identifierade luckor
        status: "COMPLETE" eller "ABORT"
        reason: Anledning vid ABORT
        chat_history: Tidigare konversation för kontext
        debug_trace: Dict för att samla debug-info (optional)
        op_mode: "query" eller "deliver" (v8.1)
        delivery_format: Format för leverans (v8.1)
        current_synthesis: Tornet från Planner (v8.1)
        facts: Bevislistan från Planner (v8.1)
    
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
    
    # v8.1: Välj modell baserat på format (INTE op_mode)
    if delivery_format in PRO_FORMATS:
        target_model = MODEL_PRO
        mode_label = "ANALYS"
    else:
        target_model = MODEL_LITE
        mode_label = "STRUKTUR" if op_mode == "deliver" else "QUERY"
    
    LOGGER.info(f"Synthesizer {mode_label} MODE ({delivery_format}) - {target_model}")
    
    # v8.1: Välj prompt baserat på op_mode
    if op_mode == "deliver":
        # DELIVER: Använd synthesizer_report med format
        prompt_template = PROMPTS.get('synthesizer_report', {}).get('instruction', '')
        if not prompt_template:
            # Fallback till standard synthesizer om report-prompt saknas
            LOGGER.warning("synthesizer_report prompt saknas, använder standard")
            prompt_template = PROMPTS.get('synthesizer', {}).get('instruction', '')
        
        # Bygg underlag från Tornet + bevis
        facts_text = "\n".join([f"- {f}" for f in (facts or [])]) if facts else "(Inga bevis)"
        synthesis_text = current_synthesis or report or "(Ingen analys)"
        
        try:
            synth_prompt = prompt_template.format(
                format=delivery_format,
                current_synthesis=synthesis_text,
                facts=facts_text,
                query=query,
                # Legacy fallbacks
                report=synthesis_text,
                gaps=gaps if gaps else "Inga kända luckor"
            )
        except KeyError as e:
            LOGGER.warning(f"Prompt-formatering misslyckades ({e}), använder fallback")
            synth_prompt = prompt_template.format(
                report=synthesis_text,
                gaps=gaps if gaps else "Inga kända luckor",
                query=query
            )
    else:
        # QUERY: Standard synthesizer prompt
        synth_config = PROMPTS.get('synthesizer', {})
        role_template = synth_config.get('role', '')
        prompt_template = synth_config.get('instruction', '')
        if not prompt_template:
            LOGGER.error("HARDFAIL: synthesizer prompt saknas i chat_prompts.yaml")
            raise ValueError("HARDFAIL: synthesizer prompt saknas i chat_prompts.yaml")
        
        # Formatera role med owner_name och prependa till instruction
        role_text = role_template.format(owner_name=OWNER_NAME) if role_template else ""
        full_template = f"{role_text}\n\n{prompt_template}" if role_text else prompt_template
        
        # Använd current_synthesis om tillgängligt, annars report
        synth_prompt = full_template.format(
            report=current_synthesis or report,
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
        response = client.models.generate_content(model=target_model, contents=contents)
        answer = response.text
        
        if debug_mode:
            print(f"\n[DEBUG RAW SYNTHESIZER RESPONSE]:\n{answer}\n[END RAW]\n")
        
        LOGGER.debug(f"Synthesizer LLM-svar: {answer[:500]}...")
        LOGGER.info(f"Synthesizer {status}: svarslängd={len(answer)} tecken")
        
        # Spara till debug_trace
        if debug_trace is not None:
            debug_trace['synthesizer_llm_raw'] = answer
            debug_trace['synthesizer_status'] = status
            debug_trace['synthesizer_model'] = target_model
            debug_trace['synthesizer_format'] = delivery_format
        
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
