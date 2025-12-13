"""
State Manager - Persistent state för Planner ReAct-loopen.

Sparar state till fil för debuggbarhet och återupptag.
"""

import os
import json
import yaml
import logging
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional

LOGGER = logging.getLogger('StateManager')


# --- CONFIG LOADER ---
def _load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, 'config', 'my_mem_config.yaml'),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f:
                return yaml.safe_load(f)
    raise FileNotFoundError("HARDFAIL: Config not found")


CONFIG = _load_config()
SESSIONS_PATH = os.path.expanduser(CONFIG['paths'].get('asset_sessions', '~/MyMemory/Assets/Sessions'))


@dataclass
class PlannerState:
    """
    State för Planner ReAct-loopen (v7.0 Knowledge Refinement).
    
    Key fields:
    - working_findings: Den förädlade kunskapsbanken (ersätts, inte appendas)
    - past_queries: För att tvinga divergens i sökningar
    - candidates: Alla kandidat-dokument (för referens)
    """
    session_id: str
    mission_goal: str
    query: str
    iteration: int = 0
    candidates: List[Dict] = field(default_factory=list)
    working_findings: str = ""  # Förädlad kunskapsbank
    past_queries: List[str] = field(default_factory=list)  # För divergens-kontroll
    gaps: List[str] = field(default_factory=list)
    search_history: List[Dict] = field(default_factory=list)
    status: str = "IN_PROGRESS"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> dict:
        """Konvertera till dict för JSON-serialisering."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'PlannerState':
        """Skapa PlannerState från dict (hanterar legacy fält)."""
        # Filtrera bort fält som inte finns i dataclass
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered_data)


def get_state_path(session_id: str) -> str:
    """Returnera sökväg till state-fil."""
    return os.path.join(SESSIONS_PATH, f"{session_id}_state.json")


def save_state(state: PlannerState) -> str:
    """
    Spara PlannerState till fil.
    
    Args:
        state: PlannerState att spara
    
    Returns:
        str: Sökväg till sparad fil
    
    Raises:
        RuntimeError: Om sparning misslyckas
    """
    # Säkerställ att mappen finns
    os.makedirs(SESSIONS_PATH, exist_ok=True)
    
    # Uppdatera timestamp
    state.updated_at = datetime.now().isoformat()
    
    filepath = get_state_path(state.session_id)
    
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(state.to_dict(), f, indent=2, ensure_ascii=False)
        LOGGER.debug(f"State sparad: {filepath}")
        return filepath
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Kunde inte spara state: {e}")
        raise RuntimeError(f"HARDFAIL: Kunde inte spara state till {filepath}: {e}") from e


def load_state(session_id: str) -> Optional[PlannerState]:
    """
    Ladda PlannerState från fil.
    
    Args:
        session_id: Session-ID att ladda
    
    Returns:
        PlannerState om filen finns, annars None
    """
    filepath = get_state_path(session_id)
    
    if not os.path.exists(filepath):
        LOGGER.debug(f"Ingen state-fil hittades: {filepath}")
        return None
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        state = PlannerState.from_dict(data)
        LOGGER.debug(f"State laddad: {filepath}")
        return state
    except Exception as e:
        LOGGER.error(f"Kunde inte ladda state från {filepath}: {e}")
        return None


def delete_state(session_id: str) -> bool:
    """
    Ta bort state-fil.
    
    Args:
        session_id: Session-ID att ta bort
    
    Returns:
        True om borttagning lyckades, False annars
    """
    filepath = get_state_path(session_id)
    
    if not os.path.exists(filepath):
        return False
    
    try:
        os.remove(filepath)
        LOGGER.debug(f"State borttagen: {filepath}")
        return True
    except Exception as e:
        LOGGER.error(f"Kunde inte ta bort state {filepath}: {e}")
        return False


# --- TEST ---
if __name__ == "__main__":
    # Test: Skapa, spara, ladda, ta bort
    test_state = PlannerState(
        session_id="test_session_123",
        mission_goal="Hitta alla möten förra veckan",
        query="Vad hände förra veckan?"
    )
    
    # Spara
    path = save_state(test_state)
    print(f"Sparad till: {path}")
    
    # Uppdatera och spara igen
    test_state.iteration = 1
    test_state.working_findings = "Hittat: Möte med Cenk på måndag, diskuterade Adda-projektet."
    test_state.past_queries.append("möten förra veckan")
    test_state.gaps = ["Saknar mötesanteckningar från tisdag"]
    save_state(test_state)
    
    # Ladda
    loaded = load_state("test_session_123")
    print(f"Laddad: iteration={loaded.iteration}")
    print(f"Working findings: {loaded.working_findings[:50]}...")
    
    # Ta bort
    deleted = delete_state("test_session_123")
    print(f"Borttagen: {deleted}")
    
    print("Alla tester passerade!")

