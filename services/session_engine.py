"""
Session Engine - Pipeline v8.2 "Pivot or Persevere"

EN manager som hanterar:
1. Session state (chat_history + planner_state)
2. Pipeline orchestration (IntentRouter → ContextBuilder → Planner → Synthesizer)
3. Pivot or Persevere: Plannern avgör kontextrelevans

Princip: HARDFAIL > Silent Fallback
"""

import os
import time
import uuid
import yaml
import logging
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional

LOGGER = logging.getLogger('SessionEngine')


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
                return yaml.safe_load(f)
    raise FileNotFoundError("HARDFAIL: Config not found")


CONFIG = _load_config()


# === PLANNER STATE ===

@dataclass
class PlannerState:
    """
    State för Planner ReAct-loopen (v8.2 Pivot or Persevere).
    
    Key fields:
    - facts: Lista av extraherade fakta (Bevisen) - append-only
    - current_synthesis: Aktuell arbetshypotes (Tornet) - uppdateras varje loop
    - past_queries: För att tvinga divergens i sökningar
    """
    session_id: str
    mission_goal: str
    query: str
    iteration: int = 0
    candidates: List[Dict] = field(default_factory=list)
    facts: List[str] = field(default_factory=list)
    current_synthesis: str = ""
    past_queries: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    search_history: List[Dict] = field(default_factory=list)
    status: str = "IN_PROGRESS"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> dict:
        """Konvertera till dict för serialisering."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'PlannerState':
        """Skapa PlannerState från dict."""
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered_data)


# === SESSION ENGINE ===

class SessionEngine:
    """
    Orchestrator för hela pipelinen.
    
    Hanterar:
    - chat_history: Kontext för IntentRouter
    - planner_state: Tornet + Bevisen (Pivot or Persevere)
    - run_query(): Kör hela pipelinen
    """
    
    def __init__(self):
        self.chat_history: List[dict] = []
        self.planner_state: Optional[PlannerState] = None
        self._session_id = str(uuid.uuid4())[:8]
        LOGGER.info(f"SessionEngine initierad: {self._session_id}")
    
    def run_query(self, query: str, debug_mode: bool = False, debug_trace: dict = None, on_iteration=None) -> dict:
        """
        Kör hela pipelinen: IntentRouter → ContextBuilder → Planner → Synthesizer
        
        Pivot or Persevere: Skickar ALLTID Tornet + Facts till Planner.
        Plannern avgör själv vad som är relevant.
        
        Args:
            query: Användarens fråga
            debug_mode: Om True, logga debug-info
            debug_trace: Dict att samla debug-info till
            on_iteration: Callback för live-output av Tornbygget
        
        Returns:
            dict med answer, sources, status, etc.
        """
        # Lazy imports för att undvika cirkulära beroenden
        try:
            from services.intent_router import route_intent
            from services.context_builder import build_context, search
            from services.planner import run_planner_loop
            from services.synthesizer import synthesize
            from services.session_logger import log_search
        except ImportError as _import_err:
            LOGGER.debug(f"Fallback import: {_import_err}")
            from intent_router import route_intent
            from context_builder import build_context, search
            from planner import run_planner_loop
            from synthesizer import synthesize
            from session_logger import log_search
        
        if debug_trace is None:
            debug_trace = {}
        
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
        mission_goal = intent_data.get('mission_goal', query)
        
        LOGGER.info(f"Intent: op_mode={op_mode}, format={delivery_format}")
        
        # --- FAS 2: CONTEXT BUILDER ---
        LOGGER.debug("ContextBuilder...")
        
        context_result = build_context(
            keywords=intent_data.get('keywords', []),
            entities=intent_data.get('entities', []),
            time_filter=intent_data.get('time_filter'),
            debug_trace=debug_trace
        )
        
        # Logga sökning
        stats = context_result.get('stats', {})
        total_hits = stats.get('lake_hits', 0) + stats.get('vector_hits', 0)
        log_search(
            query=query,
            keywords=intent_data.get('keywords', []),
            hits=total_hits,
            intent="v8.2"
        )
        
        if context_result.get('status') == 'NO_RESULTS':
            LOGGER.warning(f"Inga träffar: {context_result.get('reason')}")
            return {
                "status": "NO_RESULTS",
                "answer": f"Hittade ingen relevant information. {context_result.get('suggestion', '')}",
                "sources": [],
                "gaps": ["Inga dokument hittades"]
            }
        
        LOGGER.info(f"Context: {stats.get('after_dedup', 0)} kandidater")
        
        # --- FAS 3: PLANNER (Pivot or Persevere) ---
        LOGGER.debug("Planner Loop...")
        
        # PIVOT OR PERSEVERE: Skicka ALLTID befintligt Torn + Facts
        initial_synthesis = ""
        initial_facts = []
        
        if self.planner_state:
            initial_synthesis = self.planner_state.current_synthesis
            initial_facts = self.planner_state.facts
            LOGGER.info(f"Pivot or Persevere: Torn={len(initial_synthesis)} chars, Facts={len(initial_facts)}")
        
        # Hämta graf-kontext för kreativa sökspår
        graph_context = context_result.get('graph_context', '')
        
        planner_result = run_planner_loop(
            mission_goal=mission_goal,
            query=query,
            initial_candidates=context_result.get('candidates_full', []),
            candidates_formatted=context_result.get('candidates_formatted', ''),
            session_id=self._session_id,
            initial_synthesis=initial_synthesis,
            initial_facts=initial_facts,
            search_fn=search,
            debug_trace=debug_trace,
            on_iteration=on_iteration,
            graph_context=graph_context
        )
        
        # Spara nytt state (Plannern har redan gjort Pivot or Persevere)
        if planner_result.get('state'):
            self.planner_state = planner_result['state']
            LOGGER.info(f"State uppdaterat: Torn={len(self.planner_state.current_synthesis)} chars")
        
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
        if self.planner_state:
            return self.planner_state.current_synthesis
        return ""
    
    def get_facts(self) -> List[str]:
        """Hämta aktuella Bevis."""
        if self.planner_state:
            return self.planner_state.facts
        return []
    
    def clear(self):
        """Nollställ sessionen."""
        self.chat_history = []
        self.planner_state = None
        self._session_id = str(uuid.uuid4())[:8]
        LOGGER.info(f"Session nollställd: {self._session_id}")
    
    def summary(self) -> dict:
        """Returnera session-statistik."""
        return {
            "session_id": self._session_id,
            "history_count": len(self.chat_history),
            "has_planner_state": self.planner_state is not None,
            "facts_count": len(self.planner_state.facts) if self.planner_state else 0,
            "synthesis_length": len(self.planner_state.current_synthesis) if self.planner_state else 0
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
    
    # Simulera att vi har ett tidigare Torn
    engine.planner_state = PlannerState(
        session_id="test",
        mission_goal="Testmission",
        query="Testfråga",
        facts=["Fakta 1", "Fakta 2"],
        current_synthesis="Detta är ett test-torn."
    )
    
    print(f"Summary efter state: {engine.summary()}")
    print(f"Torn: {engine.get_synthesis()}")
    print(f"Facts: {engine.get_facts()}")
    
    engine.clear()
    print(f"Summary efter clear: {engine.summary()}")
    
    print("\n✓ SessionEngine test OK")

