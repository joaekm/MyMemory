"""
Session Logger - Signal-loggning för Dreaming (OBJEKT-48)

Loggar signaler under användning för att möjliggöra lärande:
- Sökningar (vad sökte användaren, hur många träffar)
- Feedback (explicit alias-mapping)
- Avbrott (användaren gav upp)
- Sessioner (start/stopp)

Signaler sparas till: ~/MyMemory/Index/session_signals.json
Consolidator processar dessa vid "drömfasen"
"""

import os
import json
import uuid
import logging
import datetime
from typing import Optional

LOGGER = logging.getLogger('SessionLogger')

# Default path för signaler
_SIGNALS_FILE = os.path.expanduser("~/MyMemory/Index/session_signals.json")

# Aktuell session
_CURRENT_SESSION = None


def _ensure_file_exists():
    """Säkerställ att signal-filen finns."""
    os.makedirs(os.path.dirname(_SIGNALS_FILE), exist_ok=True)
    if not os.path.exists(_SIGNALS_FILE):
        with open(_SIGNALS_FILE, 'w', encoding='utf-8') as f:
            json.dump({"sessions": []}, f)


def _load_signals() -> dict:
    """Ladda alla signaler från fil."""
    _ensure_file_exists()
    try:
        with open(_SIGNALS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        LOGGER.error(f"Kunde inte ladda signaler: {e}")
        return {"sessions": []}


def _save_signals(data: dict):
    """Spara signaler till fil."""
    _ensure_file_exists()
    try:
        with open(_SIGNALS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        LOGGER.error(f"Kunde inte spara signaler: {e}")


def start_session() -> str:
    """
    Starta en ny session.
    
    Returns:
        Session ID
    """
    global _CURRENT_SESSION
    
    session_id = str(uuid.uuid4())[:8]  # Kort ID
    _CURRENT_SESSION = {
        "session_id": session_id,
        "started": datetime.datetime.now().isoformat(),
        "signals": []
    }
    
    LOGGER.info(f"Session startad: {session_id}")
    return session_id


def end_session(reason: str = "normal"):
    """
    Avsluta aktuell session och spara.
    
    Args:
        reason: "normal" eller "abort"
    """
    global _CURRENT_SESSION
    
    if _CURRENT_SESSION is None:
        return
    
    _CURRENT_SESSION["ended"] = datetime.datetime.now().isoformat()
    _CURRENT_SESSION["end_reason"] = reason
    
    # Lägg till i fil
    data = _load_signals()
    data["sessions"].append(_CURRENT_SESSION)
    _save_signals(data)
    
    LOGGER.info(f"Session avslutad: {_CURRENT_SESSION['session_id']} ({reason})")
    _CURRENT_SESSION = None


def log_search(query: str, keywords: list, hits: int, intent: str = "RELAXED"):
    """
    Logga en sökningssignal.
    
    Args:
        query: Användarens fråga
        keywords: Använda sökord
        hits: Antal träffar
        intent: STRICT eller RELAXED
    """
    global _CURRENT_SESSION
    
    if _CURRENT_SESSION is None:
        start_session()
    
    signal = {
        "type": "search",
        "timestamp": datetime.datetime.now().isoformat(),
        "query": query[:200],  # Trunkera långa frågor
        "keywords": keywords[:10],
        "hits": hits,
        "intent": intent
    }
    
    _CURRENT_SESSION["signals"].append(signal)
    
    # Om 0 träffar, det är en viktig signal
    if hits == 0:
        LOGGER.debug(f"Zero-hit search: keywords={keywords}")


def log_feedback(canonical: str, alias: str, entity_type: str, source: str = "user"):
    """
    Logga explicit feedback (alias-mapping).
    
    Args:
        canonical: Kanoniskt namn
        alias: Aliaset som lades till
        entity_type: persons/projects/concepts
        source: user/consolidator
    """
    global _CURRENT_SESSION
    
    if _CURRENT_SESSION is None:
        start_session()
    
    signal = {
        "type": "feedback",
        "timestamp": datetime.datetime.now().isoformat(),
        "action": "alias",
        "canonical": canonical,
        "alias": alias,
        "entity_type": entity_type,
        "source": source
    }
    
    _CURRENT_SESSION["signals"].append(signal)
    LOGGER.info(f"Feedback logged: {alias} -> {canonical}")


def log_abort(rounds: int, reason: str = "frustration"):
    """
    Logga att användaren avbröt sessionen.
    
    Args:
        rounds: Antal rundor innan avbrott
        reason: Anledning till avbrott
    """
    global _CURRENT_SESSION
    
    if _CURRENT_SESSION is None:
        return
    
    signal = {
        "type": "abort",
        "timestamp": datetime.datetime.now().isoformat(),
        "rounds": rounds,
        "reason": reason
    }
    
    _CURRENT_SESSION["signals"].append(signal)
    LOGGER.warning(f"Session aborted after {rounds} rounds: {reason}")


def log_custom(signal_type: str, data: dict):
    """
    Logga en custom signal.
    
    Args:
        signal_type: Typ av signal
        data: Signaldata
    """
    global _CURRENT_SESSION
    
    if _CURRENT_SESSION is None:
        start_session()
    
    signal = {
        "type": signal_type,
        "timestamp": datetime.datetime.now().isoformat(),
        **data
    }
    
    _CURRENT_SESSION["signals"].append(signal)


def get_unprocessed_sessions() -> list:
    """
    Hämta alla sessioner som inte har processats av Consolidator.
    
    Returns:
        Lista med sessioner
    """
    data = _load_signals()
    return [s for s in data.get("sessions", []) if not s.get("processed")]


def mark_sessions_processed(session_ids: list):
    """
    Markera sessioner som processade.
    
    Args:
        session_ids: Lista med session-IDs att markera
    """
    data = _load_signals()
    
    for session in data.get("sessions", []):
        if session.get("session_id") in session_ids:
            session["processed"] = True
            session["processed_at"] = datetime.datetime.now().isoformat()
    
    _save_signals(data)
    LOGGER.info(f"Markerade {len(session_ids)} sessioner som processade")


def get_signal_statistics() -> dict:
    """
    Hämta statistik över signaler.
    
    Returns:
        dict med statistik
    """
    data = _load_signals()
    sessions = data.get("sessions", [])
    
    total_searches = 0
    zero_hit_searches = 0
    total_feedback = 0
    aborts = 0
    
    for session in sessions:
        for signal in session.get("signals", []):
            if signal.get("type") == "search":
                total_searches += 1
                if signal.get("hits", 0) == 0:
                    zero_hit_searches += 1
            elif signal.get("type") == "feedback":
                total_feedback += 1
            elif signal.get("type") == "abort":
                aborts += 1
    
    return {
        "total_sessions": len(sessions),
        "unprocessed_sessions": len([s for s in sessions if not s.get("processed")]),
        "total_searches": total_searches,
        "zero_hit_searches": zero_hit_searches,
        "total_feedback": total_feedback,
        "aborts": aborts
    }


# --- TEST ---
if __name__ == "__main__":
    print("=== Session Logger Test ===\n")
    
    # Starta session
    session_id = start_session()
    print(f"Session: {session_id}")
    
    # Logga sökningar
    log_search("Vad sa Cenk?", ["Cenk", "möte"], hits=3, intent="STRICT")
    log_search("Sänk", ["Sänk"], hits=0, intent="STRICT")
    log_search("AI-strategi", ["AI", "strategi"], hits=5, intent="RELAXED")
    
    # Logga feedback
    log_feedback("Cenk Bisgen", "Sänk", "persons")
    
    # Avsluta session
    end_session("normal")
    
    # Visa statistik
    print(f"\nStatistik: {json.dumps(get_signal_statistics(), indent=2)}")
    
    # Visa unprocessade sessioner
    unprocessed = get_unprocessed_sessions()
    print(f"\nOprocessade sessioner: {len(unprocessed)}")

