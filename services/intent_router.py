"""
IntentRouter - Pipeline v6.0 Fas 1

Ansvar:
- Klassificera intent: STRICT (specifik fakta) vs RELAXED (breda idéer)
- Parsa tidsreferenser till absoluta datum
- Upplösa kontextreferenser från chatthistorik
- Extrahera sökord och semantisk söksträng
"""

import os
import json
import yaml
import datetime
import logging
from google import genai

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
    raise FileNotFoundError("Config not found")

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
    raise FileNotFoundError("Prompts not found")

CONFIG = _load_config()
PROMPTS = _load_prompts()
LOGGER = logging.getLogger('IntentRouter')

API_KEY = CONFIG['ai_engine']['api_key']
MODEL_LITE = CONFIG['ai_engine']['models']['model_lite']
TAXONOMY_FILE = os.path.expanduser(CONFIG['paths'].get('taxonomy_file', '~/MyMemory/Index/my_mem_taxonomy.json'))


def _load_taxonomy_nodes() -> list:
    """Ladda huvudnoder från taxonomin."""
    try:
        if os.path.exists(TAXONOMY_FILE):
            with open(TAXONOMY_FILE, 'r', encoding='utf-8') as f:
                taxonomy = json.load(f)
            return list(taxonomy.keys())
    except Exception as e:
        LOGGER.warning(f"Kunde inte ladda taxonomi: {e}")
    
    # Fallback om taxonomin inte finns
    return ["Okategoriserat", "Händelser", "Projekt", "Administration", "Person", "Aktör"]


TAXONOMY_NODES = _load_taxonomy_nodes()

# AI Client (lazy init)
_AI_CLIENT = None

def _get_ai_client():
    global _AI_CLIENT
    if _AI_CLIENT is None:
        _AI_CLIENT = genai.Client(api_key=API_KEY)
    return _AI_CLIENT


def _get_swedish_weekday():
    """Returnera veckodagen på svenska."""
    days = ['måndag', 'tisdag', 'onsdag', 'torsdag', 'fredag', 'lördag', 'söndag']
    return days[datetime.date.today().weekday()]


def _format_history(chat_history: list) -> str:
    """Formatera chatthistorik för prompten."""
    if not chat_history:
        return "(Ingen tidigare historik)"
    
    lines = []
    for msg in chat_history[-10:]:  # Max 10 senaste
        role = msg.get('role', 'user').upper()
        content = msg.get('content', '')[:500]  # Trunkera långa meddelanden
        lines.append(f"{role}: {content}")
    
    return "\n".join(lines)


def route_intent(query: str, chat_history: list = None, debug_trace: dict = None) -> dict:
    """
    Analysera användarfråga och returnera sökparametrar.
    
    Args:
        query: Användarens fråga
        chat_history: Lista med tidigare meddelanden [{"role": "user/assistant", "content": "..."}]
        debug_trace: Dict för att samla debug-info (optional)
    
    Returns:
        dict med:
            - intent: "STRICT" eller "RELAXED"
            - keywords: Lista med sökord
            - vector_query: Semantisk söksträng
            - time_filter: {"after": "YYYY-MM-DD", "before": "YYYY-MM-DD"} eller None
            - context_resolved: Dict med upplösta referenser
            - reasoning: Kort motivering
    """
    
    # Hämta prompt-template
    prompt_template = PROMPTS.get('intent_router', {}).get('instruction', '')
    if not prompt_template:
        LOGGER.error("HARDFAIL: intent_router prompt saknas i chat_prompts.yaml")
        return {
            "status": "ERROR",
            "reason": "intent_router prompt saknas i chat_prompts.yaml",
            "intent": "RELAXED",
            "keywords": [],
            "vector_query": query,
            "time_filter": None,
            "context_resolved": {},
            "graph_paths": [],
            "reasoning": "Fallback pga saknad prompt"
        }
    
    # Bygg prompt
    today = datetime.date.today()
    weekday = _get_swedish_weekday()
    history_text = _format_history(chat_history)
    taxonomy_text = ", ".join(TAXONOMY_NODES)
    
    full_prompt = prompt_template.format(
        date=today,
        weekday=weekday,
        query=query,
        history=history_text,
        taxonomy_nodes=taxonomy_text
    )
    
    try:
        client = _get_ai_client()
        response = client.models.generate_content(
            model=MODEL_LITE,
            contents=full_prompt
        )
        
        # Parsa JSON-svar
        text = response.text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        
        # Validera och normalisera
        intent = result.get('intent', 'RELAXED').upper()
        if intent not in ['STRICT', 'RELAXED']:
            intent = 'RELAXED'
        
        # Validera graph_paths mot kända huvudnoder
        graph_paths = result.get('graph_paths', [])
        valid_paths = [p for p in graph_paths if p in TAXONOMY_NODES]
        if len(valid_paths) != len(graph_paths):
            invalid = set(graph_paths) - set(valid_paths)
            LOGGER.warning(f"IntentRouter angav ogiltiga graph_paths: {invalid}")
        
        output = {
            "status": "OK",
            "intent": intent,
            "keywords": result.get('keywords', []),
            "vector_query": result.get('vector_query', query),
            "time_filter": result.get('time_filter'),
            "context_resolved": result.get('context_resolved', {}),
            "graph_paths": valid_paths,
            "reasoning": result.get('reasoning', '')
        }
        
        # Spara till debug_trace om tillgänglig
        if debug_trace is not None:
            debug_trace['intent_router'] = output
            debug_trace['intent_router_raw'] = text
        
        LOGGER.info(f"IntentRouter: {intent} - keywords={output['keywords']}")
        return output
        
    except json.JSONDecodeError as e:
        LOGGER.error(f"HARDFAIL: IntentRouter JSON parse error: {e}")
        return {
            "status": "ERROR",
            "reason": f"JSON parse error: {e}",
            "intent": "RELAXED",
            "keywords": [],
            "vector_query": query,
            "time_filter": None,
            "context_resolved": {},
            "graph_paths": [],
            "reasoning": "Fallback pga parse-fel"
        }
        
    except Exception as e:
        LOGGER.error(f"HARDFAIL: IntentRouter error: {e}")
        return {
            "status": "ERROR",
            "reason": str(e),
            "intent": "RELAXED",
            "keywords": [],
            "vector_query": query,
            "time_filter": None,
            "context_resolved": {},
            "graph_paths": [],
            "reasoning": "Fallback pga fel"
        }


# --- TEST ---
if __name__ == "__main__":
    # Enkel test
    test_query = "Vad diskuterade vi om projektet igår?"
    test_history = [
        {"role": "user", "content": "Berätta om Adda-projektet"},
        {"role": "assistant", "content": "Adda är ett AI PoC för avropsstöd..."}
    ]
    
    result = route_intent(test_query, test_history)
    print(json.dumps(result, indent=2, ensure_ascii=False))

