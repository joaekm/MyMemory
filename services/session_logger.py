"""
session_logger.py - Sessionsloggning med Learnings-extraktion

Loggar händelser under en chatt-session och extraherar lärdomar (alias-kopplingar)
vid sessionsavslut. Skriver direkt till grafen via graph_builder.

Del av OBJEKT-48: Sessioner som Lärdomar.
"""

import os
import json
import yaml
import logging
import datetime
import zoneinfo
from typing import Optional

# --- CONFIG LOADER ---
def _ladda_yaml(filnamn, strict=True):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, 'config', filnamn),
        os.path.join(script_dir, '..', 'config', filnamn)
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
    if strict:
        raise FileNotFoundError(f"HARDFAIL: Kunde inte hitta {filnamn}")
    return {}

CONFIG = _ladda_yaml('my_mem_config.yaml', strict=True)
PROMPTS = _ladda_yaml('chat_prompts.yaml', strict=True)

# --- PATHS & SETTINGS ---
TAXONOMY_FILE = os.path.expanduser(CONFIG['paths'].get('taxonomy_file', '~/MyMemory/Index/my_mem_taxonomy.json'))
LOG_FILE = os.path.expanduser(CONFIG['logging']['log_file_path'])

TZ_NAME = CONFIG.get('system', {}).get('timezone', 'UTC')
try:
    SYSTEM_TZ = zoneinfo.ZoneInfo(TZ_NAME)
except Exception as e:
    raise ValueError(f"HARDFAIL: Ogiltig timezone '{TZ_NAME}': {e}") from e

# --- LOGGING ---
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE, 
    level=logging.INFO, 
    format='%(asctime)s - SESSION - %(levelname)s - %(message)s'
)
LOGGER = logging.getLogger('SessionLogger')

# --- AI CLIENT ---
API_KEY = CONFIG.get('ai_engine', {}).get('api_key', '')
MODEL_LITE = CONFIG.get('ai_engine', {}).get('models', {}).get('model_lite')

_AI_CLIENT = None

def _get_ai_client():
    """Lazy-load AI client."""
    global _AI_CLIENT
    if _AI_CLIENT is None:
        try:
            from google import genai
            _AI_CLIENT = genai.Client(api_key=API_KEY)
        except Exception as e:
            LOGGER.error(f"HARDFAIL: Kunde inte initiera AI-klient: {e}")
            raise RuntimeError(f"HARDFAIL: Kunde inte initiera AI-klient: {e}") from e
    return _AI_CLIENT

# --- GRAPH BUILDER IMPORT ---
try:
    from services.my_mem_graph_builder import add_entity_alias
except ImportError:
    try:
        from my_mem_graph_builder import add_entity_alias
    except ImportError as e:
        raise ImportError("HARDFAIL: my_mem_graph_builder.py saknas") from e

# --- TAXONOMY ---
def _load_taxonomy_types() -> list:
    """Läs giltiga entity types (huvudnoder) från taxonomin."""
    if not os.path.exists(TAXONOMY_FILE):
        raise FileNotFoundError(f"HARDFAIL: Taxonomifil saknas: {TAXONOMY_FILE}")
    try:
        with open(TAXONOMY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return list(data.keys())
    except Exception as e:
        raise RuntimeError(f"HARDFAIL: Kunde inte läsa taxonomi: {e}") from e


# === SESSION STATE ===

class SessionState:
    """Håller tillstånd för en aktiv session."""
    
    def __init__(self):
        self.session_id: Optional[str] = None
        self.started_at: Optional[datetime.datetime] = None
        self.searches: list = []
        self.feedback: list = []
        self.messages: list = []  # För learnings-extraktion
    
    def reset(self):
        self.session_id = None
        self.started_at = None
        self.searches = []
        self.feedback = []
        self.messages = []


# Global session state
_SESSION = SessionState()


# === PUBLIC API ===

def start_session() -> str:
    """
    Starta en ny session.
    
    Returns:
        Session ID (timestamp-baserad)
    """
    _SESSION.reset()
    _SESSION.session_id = datetime.datetime.now(SYSTEM_TZ).strftime("%Y%m%d_%H%M%S")
    _SESSION.started_at = datetime.datetime.now(SYSTEM_TZ)
    LOGGER.info(f"Session startad: {_SESSION.session_id}")
    return _SESSION.session_id


def end_session(chat_history: list = None) -> dict:
    """
    Avsluta sessionen och extrahera lärdomar.
    
    Args:
        chat_history: Lista med meddelanden [{"role": "user/assistant", "content": "..."}]
    
    Returns:
        dict med extraherade lärdomar
    """
    if not _SESSION.session_id:
        LOGGER.warning("end_session anropades utan aktiv session")
        return {"status": "NO_SESSION", "learnings": []}
    
    session_id = _SESSION.session_id
    duration = (datetime.datetime.now(SYSTEM_TZ) - _SESSION.started_at).seconds if _SESSION.started_at else 0
    
    # Samla session-data
    session_data = {
        "session_id": session_id,
        "duration_seconds": duration,
        "searches": _SESSION.searches,
        "feedback": _SESSION.feedback
    }
    
    # Extrahera lärdomar från chat_history om tillgänglig
    learnings = []
    if chat_history and len(chat_history) > 0:
        learnings = _extract_learnings(chat_history)
        
        # Skriv lärdomar direkt till graf
        for learning in learnings:
            try:
                success = add_entity_alias(
                    learning['canonical'],
                    learning['alias'],
                    learning['type']
                )
                if success:
                    LOGGER.info(f"Lärdom sparad: {learning['alias']} -> {learning['canonical']}")
            except Exception as e:
                LOGGER.error(f"Kunde inte spara lärdom: {e}")
    
    LOGGER.info(f"Session avslutad: {session_id} ({duration}s, {len(learnings)} lärdomar)")
    
    # Reset session
    _SESSION.reset()
    
    return {
        "status": "OK",
        "session_id": session_id,
        "duration": duration,
        "learnings": learnings,
        "searches_count": len(session_data['searches']),
        "feedback_count": len(session_data['feedback'])
    }


def log_search(query: str, keywords: list, hits: int, intent: str) -> None:
    """
    Logga en sökning.
    
    Args:
        query: Användarens fråga
        keywords: Extraherade nyckelord
        hits: Antal träffar
        intent: STRICT/RELAXED/FACT/INSPIRATION
    """
    if not _SESSION.session_id:
        # Starta implicit session om ingen finns
        start_session()
    
    search_entry = {
        "timestamp": datetime.datetime.now(SYSTEM_TZ).isoformat(),
        "query": query,
        "keywords": keywords,
        "hits": hits,
        "intent": intent
    }
    _SESSION.searches.append(search_entry)
    LOGGER.debug(f"Sökning loggad: {query[:50]}... ({hits} träffar)")


def log_feedback(canonical: str, alias: str, entity_type: str, source: str = "user") -> None:
    """
    Logga användar-feedback (explicit alias-koppling).
    
    Args:
        canonical: Kanoniskt namn
        alias: Alias/smeknamn
        entity_type: Typ från taxonomin
        source: Källa (user, system, etc.)
    """
    if not _SESSION.session_id:
        start_session()
    
    feedback_entry = {
        "timestamp": datetime.datetime.now(SYSTEM_TZ).isoformat(),
        "canonical": canonical,
        "alias": alias,
        "entity_type": entity_type,
        "source": source
    }
    _SESSION.feedback.append(feedback_entry)
    LOGGER.info(f"Feedback loggad: {alias} -> {canonical} ({entity_type})")


def log_abort() -> None:
    """
    Logga att sessionen avbröts (t.ex. KeyboardInterrupt).
    """
    if _SESSION.session_id:
        LOGGER.info(f"Session avbruten: {_SESSION.session_id}")
    _SESSION.reset()


# === LEARNINGS EXTRACTION ===

def _extract_learnings(chat_history: list) -> list:
    """
    Extrahera lärdomar (alias-kopplingar) från chatt-historik via LLM.
    
    Args:
        chat_history: Lista med meddelanden
    
    Returns:
        Lista med lärdomar [{canonical, alias, type, confidence}]
    """
    # Hämta prompt från config
    prompt_config = PROMPTS.get('session_learnings_extractor', {})
    prompt_template = prompt_config.get('instruction', '')
    
    if not prompt_template:
        LOGGER.warning("session_learnings_extractor prompt saknas - hoppar över learnings")
        return []
    
    # Läs giltiga entity types från taxonomin
    try:
        valid_types = _load_taxonomy_types()
    except Exception as e:
        LOGGER.error(f"Kunde inte läsa taxonomi: {e}")
        return []
    
    # Bygg konversationssträng
    conversation = "\n".join([
        f"{msg['role'].upper()}: {msg['content']}" 
        for msg in chat_history
    ])
    
    # Injicera valid_types i prompt
    prompt = prompt_template.format(valid_types=valid_types)
    full_prompt = f"{prompt}\n\nKONVERSATION ATT ANALYSERA:\n{conversation}"
    
    try:
        ai_client = _get_ai_client()
        response = ai_client.models.generate_content(
            model=MODEL_LITE,
            contents=full_prompt
        )
        
        # Parsa JSON-svar
        result_text = response.text.replace("```json", "").replace("```", "").strip()
        result = json.loads(result_text)
        
        learnings = result.get('learned_aliases', [])
        
        # Validera att type finns i taxonomin
        validated = []
        for learning in learnings:
            if learning.get('type') in valid_types:
                validated.append(learning)
            else:
                LOGGER.warning(f"Ogiltig entity type: {learning.get('type')}")
        
        return validated
        
    except json.JSONDecodeError as e:
        LOGGER.error(f"Kunde inte parsa learnings JSON: {e}")
        return []
    except Exception as e:
        LOGGER.error(f"Learnings-extraktion misslyckades: {e}")
        return []


# === EXPORT FOR TESTING ===

def get_session_state() -> dict:
    """Returnera nuvarande session-tillstånd (för debugging)."""
    return {
        "session_id": _SESSION.session_id,
        "started_at": _SESSION.started_at.isoformat() if _SESSION.started_at else None,
        "searches": _SESSION.searches,
        "feedback": _SESSION.feedback
    }
