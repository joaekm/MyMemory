"""
Session Manager - Minne över turer (Pipeline v8.1)

Hanterar två typer av persistens:
1. chat_history: För IntentRouter (kontextuell uppföljning)
2. planner_state: För Synthesizer (Tornet + Bevisen)

Princip: HARDFAIL > Silent Fallback
"""

import logging
from typing import Optional, List

# Import PlannerState
try:
    from services.utils.state_manager import PlannerState
except ImportError:
    from state_manager import PlannerState

LOGGER = logging.getLogger('SessionManager')


class SessionManager:
    """
    Hanterar session state mellan turer i en konversation.
    
    Två typer av minne:
    - chat_history: Lista av user/AI-utbyten (för IntentRouter)
    - planner_state: PlannerState objekt (för Tornet + Bevisen)
    """
    
    def __init__(self, max_history_turns: int = 6):
        """
        Initiera en ny session.
        
        Args:
            max_history_turns: Max antal turer att behålla i historiken
        """
        # Minne för IntentRouter (vad har vi sagt?)
        self.chat_history: List[str] = []
        
        # Minne för Planner (vad har vi hittat?)
        self.planner_state: Optional[PlannerState] = None
        
        # Konfiguration
        self.max_history_turns = max_history_turns
        
        LOGGER.debug("SessionManager initierad")
    
    def update_history(self, user_msg: str, ai_msg: str) -> None:
        """
        Lägg till en tur i chatthistoriken.
        
        Args:
            user_msg: Användarens fråga
            ai_msg: AI:ns svar
        """
        self.chat_history.append(f"User: {user_msg}")
        self.chat_history.append(f"AI: {ai_msg}")
        LOGGER.debug(f"Historik uppdaterad: {len(self.chat_history)} meddelanden")
    
    def get_history_string(self, max_turns: int = None) -> str:
        """
        Returnera senaste N turerna som sträng.
        
        Args:
            max_turns: Max antal meddelanden (default: self.max_history_turns)
        
        Returns:
            Formaterad sträng med chatthistorik
        """
        limit = max_turns or self.max_history_turns
        recent = self.chat_history[-limit:] if self.chat_history else []
        return "\n".join(recent)
    
    def get_history_list(self) -> List[dict]:
        """
        Returnera historiken som lista av dicts (för LLM-contents).
        
        Returns:
            Lista med {"role": "user"|"assistant", "content": "..."}
        """
        result = []
        for msg in self.chat_history:
            if msg.startswith("User: "):
                result.append({"role": "user", "content": msg[6:]})
            elif msg.startswith("AI: "):
                result.append({"role": "assistant", "content": msg[4:]})
        return result
    
    def save_planner_state(self, state: PlannerState) -> None:
        """
        Spara PlannerState för nästa tur.
        
        Args:
            state: PlannerState att spara
        """
        self.planner_state = state
        LOGGER.debug(f"PlannerState sparad: facts={len(state.facts)}, "
                    f"synthesis_len={len(state.current_synthesis) if state.current_synthesis else 0}")
    
    def load_planner_state(self) -> Optional[PlannerState]:
        """
        Ladda sparad PlannerState.
        
        Returns:
            PlannerState om den finns, annars None
        """
        if self.planner_state:
            LOGGER.debug(f"PlannerState laddad: facts={len(self.planner_state.facts)}")
        return self.planner_state
    
    def get_current_synthesis(self) -> str:
        """
        Hämta current_synthesis (Tornet) om det finns.
        
        Returns:
            Tornet som sträng, eller tom sträng
        """
        if self.planner_state and self.planner_state.current_synthesis:
            return self.planner_state.current_synthesis
        return ""
    
    def get_facts(self) -> List[str]:
        """
        Hämta facts (Bevisen) om de finns.
        
        Returns:
            Lista av fakta, eller tom lista
        """
        if self.planner_state and self.planner_state.facts:
            return self.planner_state.facts
        return []
    
    def clear(self) -> None:
        """
        Nollställ sessionen (ny konversation).
        """
        self.chat_history = []
        self.planner_state = None
        LOGGER.info("Session nollställd")
    
    def has_context(self) -> bool:
        """
        Kontrollera om sessionen har kontext att använda.
        
        Returns:
            True om vi har historik eller planner_state
        """
        return bool(self.chat_history or self.planner_state)
    
    def summary(self) -> dict:
        """
        Returnera en sammanfattning av session state.
        
        Returns:
            Dict med session-statistik
        """
        return {
            "history_count": len(self.chat_history),
            "has_planner_state": self.planner_state is not None,
            "facts_count": len(self.planner_state.facts) if self.planner_state else 0,
            "synthesis_length": len(self.planner_state.current_synthesis) if self.planner_state and self.planner_state.current_synthesis else 0
        }


# Singleton-instans för enkel import
_DEFAULT_SESSION: Optional[SessionManager] = None


def get_session() -> SessionManager:
    """
    Hämta default session (singleton).
    
    Returns:
        SessionManager instans
    """
    global _DEFAULT_SESSION
    if _DEFAULT_SESSION is None:
        _DEFAULT_SESSION = SessionManager()
    return _DEFAULT_SESSION


def reset_session() -> None:
    """
    Återställ default session.
    """
    global _DEFAULT_SESSION
    if _DEFAULT_SESSION:
        _DEFAULT_SESSION.clear()
    _DEFAULT_SESSION = None


# --- TEST ---
if __name__ == "__main__":
    print("=== Test SessionManager ===")
    
    session = SessionManager()
    
    # Test historik
    session.update_history("Vad sa Cenk?", "Cenk sa 500k på mötet.")
    session.update_history("När var det?", "Det var den 21 november.")
    
    print(f"Historik:\n{session.get_history_string()}")
    print(f"Har kontext: {session.has_context()}")
    
    # Test planner state
    from state_manager import PlannerState
    test_state = PlannerState(
        session_id="test",
        mission_goal="Hitta info om budget",
        query="Budget?",
        facts=["Budget 500k", "Ändrad till 600k"],
        current_synthesis="Budgeten startade på 500k men höjdes till 600k i november."
    )
    
    session.save_planner_state(test_state)
    print(f"\nSummary: {session.summary()}")
    print(f"Tornet: {session.get_current_synthesis()[:50]}...")
    print(f"Bevis: {session.get_facts()}")
    
    # Test clear
    session.clear()
    print(f"\nEfter clear: {session.summary()}")
    
    print("\n✓ Alla tester passerade!")

