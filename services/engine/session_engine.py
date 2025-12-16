"""
Session Engine - Pipeline v8.3 "Bygglaget"

EN manager som hanterar:
1. Session state (chat_history + synthesis/facts som dict)
2. Pipeline orchestration (IntentRouter → Planner → Synthesizer)
3. Pivot or Persevere: Plannern avgör kontextrelevans

v8.3 Changes:
- Planner anropar ContextBuilder internt (Engine är förenklad)
- Sparar resultat som dict, inte PlannerState

Princip: HARDFAIL > Silent Fallback
"""

import os
import time
import uuid
import yaml
import logging
from typing import List, Dict, Optional

LOGGER = logging.getLogger('SessionEngine')


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
                return yaml.safe_load(f)
    raise FileNotFoundError("HARDFAIL: Config not found")


CONFIG = _load_config()


# === SESSION ENGINE ===

class SessionEngine:
    """
    Orchestrator för hela pipelinen (v8.3 Bygglaget).
    
    Hanterar:
    - chat_history: Kontext för IntentRouter
    - Tornet + Bevisen (sparas som dict, inte PlannerState)
    - run_query(): Kör hela pipelinen
    """
    
    def __init__(self):
        self.chat_history: List[dict] = []
        # v8.3: Sparar resultat som dict istället för PlannerState
        self._last_synthesis: str = ""
        self._last_facts: List[str] = []
        self.last_candidates: List[Dict] = []  # För /show och /export kommandon
        self._session_id = str(uuid.uuid4())[:8]
        LOGGER.info(f"SessionEngine initierad: {self._session_id}")
    
    def run_query(self, query: str, debug_mode: bool = False, debug_trace: dict = None, 
                  on_iteration=None, on_scan=None) -> dict:
        """
        Kör hela pipelinen: IntentRouter → ContextBuilder → Planner → Synthesizer
        
        Pivot or Persevere: Skickar ALLTID Tornet + Facts till Planner.
        Plannern avgör själv vad som är relevant.
        
        Args:
            query: Användarens fråga
            debug_mode: Om True, logga debug-info
            debug_trace: Dict att samla debug-info till
            on_iteration: Callback för live-output av Tornbygget
            on_scan: Callback för Librarian Loop reasoning display
        
        Returns:
            dict med answer, sources, status, etc.
        """
        # Lazy imports för att undvika cirkulära beroenden
        try:
            from services.pipeline.intent_router import route_intent
            from services.pipeline.planner import run_planner_loop
            from services.pipeline.synthesizer import synthesize
            from services.engine.session_logger import log_search
        except ImportError as _import_err:
            LOGGER.debug(f"Fallback import: {_import_err}")
            from intent_router import route_intent
            from planner import run_planner_loop
            from synthesizer import synthesize
            from session_logger import log_search
        
        start_time = time.time()
        
        # --- FAS 1: INTENT ROUTER ---
        LOGGER.debug(f"IntentRouter: {query[:50]}...")
        
        # Formatera historik för router
        history_formatted = self._format_history_for_router()
        
        intent_data = route_intent(query, history_formatted, debug_trace=debug_trace)
        
        if intent_data.get('status') == 'ERROR':
            LOGGER.error(f"HARDFAIL: IntentRouter: {intent_data.get('reason')}")
            raise RuntimeError(f"HARDFAIL: IntentRouter: {intent_data.get('reason')}")
        
        op_mode = intent_data.get('op_mode', 'query')
        delivery_format = intent_data.get('delivery_format', 'Narrativ Analys')
        
        LOGGER.info(f"Intent: op_mode={op_mode}, format={delivery_format}")
        
        # --- FAS 2: PLANNER (v8.3 Bygglaget - anropar ContextBuilder internt) ---
        LOGGER.debug("Planner Loop...")
        
        # PIVOT OR PERSEVERE: Skicka befintligt Torn + Facts
        initial_synthesis = self._last_synthesis
        initial_facts = self._last_facts
        
        if initial_synthesis:
            LOGGER.info(f"Pivot or Persevere: Torn={len(initial_synthesis)} chars, Facts={len(initial_facts)}")
        
        planner_result = run_planner_loop(
            intent_data=intent_data,
            query=query,
            session_id=self._session_id,
            initial_synthesis=initial_synthesis,
            initial_facts=initial_facts,
            debug_trace=debug_trace,
            on_iteration=on_iteration,
            on_scan=on_scan,
        )
        
        # Logga sökning (keywords från intent)
        log_search(
            query=query,
            keywords=intent_data.get('keywords', []),
            hits=len(planner_result.get('candidates', [])),
            intent="v8.3"
        )
        
        # v8.3: Spara resultat som dict (inte PlannerState)
        self._last_synthesis = planner_result.get('current_synthesis', '')
        self._last_facts = planner_result.get('facts', [])
        self.last_candidates = planner_result.get('candidates', [])
        
        LOGGER.info(f"Planner: {planner_result.get('status')}, Torn={len(self._last_synthesis)} chars")
        
        LOGGER.info(f"Planner: {planner_result.get('status')}")
        
        # --- FAS 4: SYNTHESIZER ---
        LOGGER.debug("Synthesizer...")
        
        synth_result = synthesize(
            query=query,
            report=planner_result.get('report', ''),
            gaps=planner_result.get('gaps', []),
            status=planner_result.get('status', 'COMPLETE'),
            reason=planner_result.get('reason'),
            chat_history=self.chat_history,
            debug_trace=debug_trace,
            op_mode=op_mode,
            delivery_format=delivery_format,
            current_synthesis=planner_result.get('current_synthesis', ''),
            facts=planner_result.get('facts', [])
        )
        
        answer = synth_result.get('answer', 'Fel vid syntes')
        
        # Uppdatera historik
        self.chat_history.append({"role": "user", "content": query})
        self.chat_history.append({"role": "assistant", "content": answer})
        
        # Timing
        duration = round(time.time() - start_time, 2)
        if debug_trace is not None:
            debug_trace['total_duration'] = duration
            debug_trace['pipeline_version'] = 'v8.2'
        
        LOGGER.info(f"Pipeline klar: {duration}s")
        
        return {
            "status": planner_result.get('status', 'COMPLETE'),
            "answer": answer,
            "sources": planner_result.get('sources_used', []),
            "gaps": planner_result.get('gaps', []),
            "op_mode": op_mode,
            "delivery_format": delivery_format,
            "debug_trace": debug_trace
        }
    
    def _format_history_for_router(self) -> list:
        """Formatera chat_history för IntentRouter."""
        return self.chat_history[-6:]  # Senaste 6 meddelanden
    
    def get_synthesis(self) -> str:
        """Hämta aktuellt Torn."""
        return self._last_synthesis
    
    def get_facts(self) -> List[str]:
        """Hämta aktuella Bevis."""
        return self._last_facts
    
    def get_last_candidates(self) -> List[Dict]:
        """Hämta senaste sökningens kandidater (för /show och /export)."""
        return self.last_candidates

    def clear(self):
        """Nollställ sessionen."""
        self.chat_history = []
        self._last_synthesis = ""
        self._last_facts = []
        self.last_candidates = []
        self._session_id = str(uuid.uuid4())[:8]
        LOGGER.info(f"Session nollställd: {self._session_id}")
    
    def summary(self) -> dict:
        """Returnera session-statistik."""
        return {
            "session_id": self._session_id,
            "history_count": len(self.chat_history),
            "has_synthesis": bool(self._last_synthesis),
            "facts_count": len(self._last_facts),
            "synthesis_length": len(self._last_synthesis)
        }


# === SINGLETON ===

_ENGINE: Optional[SessionEngine] = None


def get_engine() -> SessionEngine:
    """Hämta singleton engine."""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = SessionEngine()
    return _ENGINE


def reset_engine():
    """Återställ singleton engine."""
    global _ENGINE
    if _ENGINE:
        _ENGINE.clear()
    _ENGINE = None


# --- TEST ---
if __name__ == "__main__":
    print("=== Test SessionEngine ===")
    
    engine = SessionEngine()
    print(f"Summary: {engine.summary()}")
    
    # Simulera att vi har ett tidigare Torn (v8.3: direkt på dict)
    engine._last_synthesis = "Detta är ett test-torn."
    engine._last_facts = ["Fakta 1", "Fakta 2"]
    
    print(f"Summary efter state: {engine.summary()}")
    print(f"Torn: {engine.get_synthesis()}")
    print(f"Facts: {engine.get_facts()}")
    
    engine.clear()
    print(f"Summary efter clear: {engine.summary()}")
    
    print("\n✓ SessionEngine test OK")

