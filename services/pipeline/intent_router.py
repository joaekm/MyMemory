"""
IntentRouter - Pipeline v7.0 "The System-Aware Router"

Ansvar:
- Skapa ett Kontextuellt Mission Goal
- Använda Taxonomin som "Grafens Karta"
- Extrahera keywords, entities och time_filter

OBS: Profil har tagits bort från IntentRouter. Endast Synthesizer vet vem användaren är.
"""

import os
import json
import yaml
import datetime
import logging
from google import genai

# Import robust JSON parser
try:
    from services.utils.json_parser import parse_llm_json
except ImportError as _import_err:
    # Direkt körning - försök utan services-prefix
    try:
        from utils.json_parser import parse_llm_json
    except ImportError as e:
        raise ImportError(f"HARDFAIL: Kan inte importera json_parser: {e}") from e

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
LOGGER = logging.getLogger('IntentRouter')

API_KEY = CONFIG['ai_engine']['api_key']
MODEL_LITE = CONFIG['ai_engine']['models']['model_lite']
TAXONOMY_FILE = os.path.expanduser(CONFIG['paths'].get('taxonomy_file', '~/MyMemory/Index/my_mem_taxonomy.json'))


def _load_taxonomy_nodes() -> list:
    """Ladda huvudnoder från taxonomin. HARDFAIL om det misslyckas."""
    if not os.path.exists(TAXONOMY_FILE):
        raise FileNotFoundError(
            f"HARDFAIL: Taxonomifil saknas: {TAXONOMY_FILE}. "
            "Skapa filen enligt Princip 8 i projektreglerna."
        )
    
    try:
        with open(TAXONOMY_FILE, 'r', encoding='utf-8') as f:
            taxonomy = json.load(f)
        nodes = list(taxonomy.keys())
        if not nodes:
            raise ValueError("HARDFAIL: Taxonomin är tom")
        return nodes
    except json.JSONDecodeError as e:
        raise ValueError(f"HARDFAIL: Kunde inte parsa taxonomi-JSON: {e}") from e
    except Exception as e:
        raise RuntimeError(f"HARDFAIL: Kunde inte ladda taxonomi: {e}") from e


def _load_taxonomy_str() -> str:
    """Formatera taxonomin som läsbar kontext för LLM (Grafens Karta)."""
    if not os.path.exists(TAXONOMY_FILE):
        return ""
    try:
        with open(TAXONOMY_FILE, 'r', encoding='utf-8') as f:
            taxonomy = json.load(f)
        
        lines = []
        for node, data in taxonomy.items():
            desc = data.get('description', '')
            subs = data.get('sub_nodes', [])
            lines.append(f"{node}: {desc}")
            if subs:
                lines.append(f"  Innehåller: {', '.join(subs[:20])}")
        return "\n".join(lines)
    except Exception as e:
        LOGGER.warning(f"Kunde inte formatera taxonomi: {e}")
        return ""


# Ladda vid uppstart
TAXONOMY_NODES = _load_taxonomy_nodes()
TAXONOMY_CONTEXT = _load_taxonomy_str()

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
    Skapa Mission Goal baserat på användarfråga.
    
    Pipeline v7.0: Ersätter STRICT/RELAXED med mission_goal.
    
    Args:
        query: Användarens fråga
        chat_history: Lista med tidigare meddelanden
        debug_trace: Dict för att samla debug-info (optional)
    
    Returns:
        dict med:
            - status: "OK" eller "ERROR"
            - keywords: Lista med sökord
            - entities: Lista med entiteter (t.ex. "Person: Cenk")
            - time_filter: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} eller None
            - mission_goal: Detaljerat uppdrag för Planner
    """
    
    # Hämta prompt-template
    prompt_template = PROMPTS.get('intent_router', {}).get('instruction', '')
    if not prompt_template:
        LOGGER.error("HARDFAIL: intent_router prompt saknas i chat_prompts.yaml")
        raise ValueError("HARDFAIL: intent_router prompt saknas i chat_prompts.yaml")
    
    # Bygg prompt
    now = datetime.datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M")
    weekday = _get_swedish_weekday()
    history_text = _format_history(chat_history)
    
    full_prompt = prompt_template.format(
        timestamp=timestamp,
        weekday=weekday,
        query=query,
        history=history_text,
        taxonomy_context=TAXONOMY_CONTEXT
    )
    
    try:
        client = _get_ai_client()
        response = client.models.generate_content(
            model=MODEL_LITE,
            contents=full_prompt
        )
        
        # Parsa JSON-svar med robust parser
        text = response.text
        LOGGER.info(f"IntentRouter RAW: {text}")
        result = parse_llm_json(text, context="intent_router")
        LOGGER.info(f"IntentRouter PARSED: {result}")
        
        # Validera entities mot taxonomi-noder
        entities = result.get('entities', [])
        
        output = {
            "status": "OK",
            "keywords": result.get('keywords', []),
            "entities": entities,
            "time_filter": result.get('time_filter'),
            "mission_goal": result.get('mission_goal', query)
        }
        
        # Spara till debug_trace om tillgänglig
        if debug_trace is not None:
            debug_trace['intent_router'] = output
            debug_trace['intent_router_raw'] = text
        
        LOGGER.info(f"IntentRouter: keywords={output['keywords']}, entities={output['entities']}")
        return output
        
    except ValueError as e:
        # parse_llm_json kastar ValueError vid fel
        LOGGER.error(f"HARDFAIL: IntentRouter: {e}")
        raise
        
    except Exception as e:
        LOGGER.error(f"HARDFAIL: IntentRouter error: {e}")
        raise RuntimeError(f"HARDFAIL: IntentRouter misslyckades: {e}") from e


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
