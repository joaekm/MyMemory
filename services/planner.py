"""
Planner - Pipeline v8.2 "Pivot or Persevere"

Ansvar:
- ReAct-loop: Reason + Act tills mission_goal uppfyllt
- Pivot or Persevere: Plannern avgör kontextrelevans
- Rolling Hypothesis: Bygger current_synthesis (Tornet) iterativt
- Identifiera gaps och learnings

v8.2 Changes:
- Pivot or Persevere: Tar emot befintligt Torn + Facts från SessionEngine
- MIN_ITERATIONS_BEFORE_COMPLETE: Tvingar tornbyggande först
- Returnerar state för SessionEngine att spara

Princip: HARDFAIL > Silent Fallback
"""

import os
import json
import yaml
import logging
from google import genai

# Import utilities
try:
    from services.utils.json_parser import parse_llm_json
    from services.session_engine import PlannerState
except ImportError as _import_err:
    try:
        from utils.json_parser import parse_llm_json
        from session_engine import PlannerState
    except ImportError as e:
        raise ImportError(f"HARDFAIL: Kan inte importera utilities: {e}") from e

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
LOGGER = logging.getLogger('Planner')

API_KEY = CONFIG['ai_engine']['api_key']
MODEL_LITE = CONFIG['ai_engine']['models']['model_lite']

# Konstanter
MAX_ITERATIONS = 30     # Säkerhetsspärr (exhaustion avslutar tidigare)
GAIN_THRESHOLD = 0.05   # Under detta = "vi hittade inget nytt"
MAX_PATIENCE = 2        # Antal stagnerade loopar innan exit

# AI Client (lazy init)
_AI_CLIENT = None

def _get_ai_client():
    global _AI_CLIENT
    if _AI_CLIENT is None:
        _AI_CLIENT = genai.Client(api_key=API_KEY)
    return _AI_CLIENT


def _format_past_queries(past_queries: list) -> str:
    """Formatera tidigare sökningar för prompten."""
    if not past_queries:
        return "(Inga tidigare sökningar)"
    return ", ".join([f"'{q}'" for q in past_queries[-5:]])  # Senaste 5


def _is_too_similar(new_query: str, past_queries: list, threshold: float = 0.7) -> bool:
    """
    Kontrollera om en ny sökning är för lik tidigare sökningar.
    Enkel ordöverlappningskontroll.
    """
    if not past_queries or not new_query:
        return False
    
    new_words = set(new_query.lower().split())
    
    for past in past_queries[-3:]:  # Jämför med senaste 3
        past_words = set(past.lower().split())
        if not past_words:
            continue
        
        overlap = len(new_words & past_words) / max(len(new_words), len(past_words))
        if overlap >= threshold:
            LOGGER.debug(f"Sökning '{new_query}' för lik '{past}' (overlap={overlap:.2f})")
            return True
    
    return False


def _format_existing_facts(facts: list) -> str:
    """Formatera befintliga fakta för prompten."""
    if not facts:
        return "(Inga fakta samlade ännu)"
    return "\n".join([f"- {fact}" for fact in facts])


def _evaluate_state(state: PlannerState, candidates_formatted: str) -> dict:
    """
    Evaluera aktuellt state mot mission_goal.
    v8.1 Rolling Hypothesis: Jämför ny info med befintlig syntes (Tornet).
    
    Args:
        state: PlannerState med current_synthesis, facts och past_queries
        candidates_formatted: Formaterade kandidater (topp 3 med fulltext)
    
    Returns:
        dict med:
            - status: "SEARCH" | "COMPLETE" | "ABORT"
            - updated_synthesis: Uppdaterad arbetshypotes (Tornet)
            - new_evidence: Nya bevis (läggs till i facts-listan)
            - next_search_query: Ny sökning om SEARCH
            - gaps: Vad som fortfarande saknas
    """
    prompt_template = PROMPTS.get('planner_evaluate', {}).get('instruction', '')
    
    if not prompt_template:
        LOGGER.error("HARDFAIL: planner_evaluate prompt saknas i chat_prompts.yaml")
        raise ValueError("HARDFAIL: planner_evaluate prompt saknas i chat_prompts.yaml")
    
    past_queries = _format_past_queries(state.past_queries)
    existing_facts = _format_existing_facts(state.facts)
    
    # v8.1: Rolling Hypothesis - inkludera current_synthesis
    current_synthesis = state.current_synthesis or "(Ingen analys ännu - börja från noll)"
    
    try:
        full_prompt = prompt_template.format(
            mission_goal=state.mission_goal,
            current_synthesis=current_synthesis,
            existing_facts=existing_facts,
            candidates=candidates_formatted,
            past_queries=past_queries,
            iteration=state.iteration + 1,
            max_iterations=MAX_ITERATIONS
        )
    except KeyError as e:
        # Fallback för legacy prompt
        LOGGER.warning(f"Legacy prompt format (saknar {e}), använder fallback")
        full_prompt = prompt_template.format(
            mission_goal=state.mission_goal,
            working_findings=current_synthesis if current_synthesis else existing_facts,
            candidates=candidates_formatted,
            past_queries=past_queries,
            iteration=state.iteration + 1,
            max_iterations=MAX_ITERATIONS
        )
    
    try:
        client = _get_ai_client()
        response = client.models.generate_content(
            model=MODEL_LITE,
            contents=full_prompt
        )
        
        text = response.text
        LOGGER.debug(f"Planner evaluate LLM-svar: {text[:500]}...")
        result = parse_llm_json(text, context="planner_evaluate")
        
        return {
            "status": result.get('status', 'ABORT'),
            # v8.1: Rolling Hypothesis
            "updated_synthesis": result.get('updated_synthesis', ''),
            "new_evidence": result.get('new_evidence', []),
            # Legacy fallbacks
            "new_facts": result.get('new_facts', []),
            "refined_findings": result.get('refined_findings', ''),
            "next_search_query": result.get('next_search_query'),
            "gaps": result.get('gaps', []),
            "llm_raw": text
        }
        
    except ValueError as e:
        # parse_llm_json kastar ValueError vid fel
        LOGGER.error(f"HARDFAIL: Planner evaluate: {e}")
        raise


def run_planner_loop(
    mission_goal: str,
    query: str,
    initial_candidates: list,
    candidates_formatted: str,
    session_id: str,
    initial_synthesis: str = "",
    initial_facts: list = None,
    search_fn=None,
    debug_trace: dict = None
) -> dict:
    """
    ReAct-loop med Pivot or Persevere (v8.2).
    
    Arkitektur:
    1. Tar emot befintligt Torn + Facts från SessionEngine (Pivot or Persevere)
    2. Plannern avgör själv om kontexten är relevant
    3. Tvingar minst MIN_ITERATIONS_BEFORE_COMPLETE innan COMPLETE tillåts
    4. Returnerar state för SessionEngine att spara
    
    Args:
        mission_goal: Uppdrag från IntentRouter
        query: Original användarfråga
        initial_candidates: Kandidater från ContextBuilder (full metadata)
        candidates_formatted: Formaterad sträng med topp 3 fulltext
        session_id: Unikt session-ID
        initial_synthesis: Befintligt Torn från SessionEngine (Pivot or Persevere)
        initial_facts: Befintliga Facts från SessionEngine (Pivot or Persevere)
        search_fn: Funktion för extra sökningar (optional)
        debug_trace: Dict för debug-info (optional)
    
    Returns:
        dict med:
            - status: "COMPLETE" | "ABORT" | "PARTIAL"
            - report: Rapport baserad på Tornet
            - current_synthesis: Uppdaterat Torn
            - facts: Uppdaterade Facts
            - state: PlannerState för SessionEngine att spara
            - sources_used: Lista med använda filnamn
            - gaps: Vad som saknas
    """
    # Import format_candidates_for_planner för nya sökningar
    try:
        from services.context_builder import format_candidates_for_planner, TOP_N_FULLTEXT
    except ImportError as _import_err:
        LOGGER.debug(f"Fallback-import context_builder: {_import_err}")
        from context_builder import format_candidates_for_planner, TOP_N_FULLTEXT
    
    # v8.2: Skapa state med befintligt Torn + Facts (Pivot or Persevere)
    state = PlannerState(
        session_id=session_id,
        mission_goal=mission_goal,
        query=query,
        candidates=initial_candidates,
        facts=initial_facts or [],
        current_synthesis=initial_synthesis,
        past_queries=[]
    )
    
    if initial_synthesis:
        LOGGER.info(f"Pivot or Persevere: Startar med befintligt Torn ({len(initial_synthesis)} chars)")
    if initial_facts:
        LOGGER.info(f"Pivot or Persevere: Startar med {len(initial_facts)} befintliga Facts")
    
    # Nuvarande kandidater formaterade för prompt
    current_candidates_formatted = candidates_formatted
    
    # Context Gain Delta - "Nöjd eller Utmattad"
    patience = 0
    
    while state.iteration < MAX_ITERATIONS:
        LOGGER.info(f"Planner iteration {state.iteration + 1}/{MAX_ITERATIONS}")
        
        # Evaluate: LLM läser dokument och uppdaterar hypotes
        eval_result = _evaluate_state(state, current_candidates_formatted)
        
        # v8.1: Uppdatera TORNET (current_synthesis)
        updated_synthesis = eval_result.get('updated_synthesis', '')
        if updated_synthesis:
            state.current_synthesis = updated_synthesis
            LOGGER.info(f"Tornet uppdaterat: {updated_synthesis[:100]}...")
        
        # v8.1: APPENDA nya bevis med deduplicering
        new_evidence = eval_result.get('new_evidence', []) or eval_result.get('new_facts', [])
        if new_evidence:
            existing_lower = {f.lower().strip() for f in state.facts}
            added = 0
            for evidence in new_evidence:
                if evidence and evidence.lower().strip() not in existing_lower:
                    state.facts.append(evidence)
                    existing_lower.add(evidence.lower().strip())
                    added += 1
            LOGGER.info(f"Lade till {added} nya bevis (totalt: {len(state.facts)})")
        
        # Legacy fallback: Om prompten returnerar refined_findings
        if not updated_synthesis and eval_result.get('refined_findings'):
            state.working_findings = eval_result.get('refined_findings', state.working_findings)
        
        state.gaps = eval_result.get('gaps', [])
        
        # --- CONTEXT GAIN DELTA ---
        context_gain = eval_result.get('context_gain', 0.5)
        LOGGER.info(f"Context gain: {context_gain:.2f}")
        
        if context_gain < GAIN_THRESHOLD:
            patience += 1
            LOGGER.info(f"Lågt gain ({context_gain:.2f}), patience={patience}/{MAX_PATIENCE}")
            
            if patience >= MAX_PATIENCE:
                LOGGER.info(f"EXHAUSTED efter {state.iteration + 1} iterationer")
                sources = [c.get('filename', 'unknown') for c in state.candidates[:10]]
                report = state.current_synthesis if state.current_synthesis else "\n".join([f"- {f}" for f in state.facts])
                return {
                    "status": "COMPLETE",
                    "reason": "Exhausted - no new context found",
                    "report": report,
                    "current_synthesis": state.current_synthesis,
                    "facts": state.facts,
                    "state": state,
                    "sources_used": sources,
                    "gaps": state.gaps
                }
        else:
            patience = 0  # Reset om vi hittade nytt
        
        # Spara till debug_trace
        if debug_trace is not None:
            debug_trace[f'planner_iter_{state.iteration}'] = {
                "status": eval_result['status'],
                "context_gain": context_gain,
                "patience": patience,
                "synthesis_preview": state.current_synthesis[:200] if state.current_synthesis else "(tom)",
                "facts_count": len(state.facts),
                "facts_preview": state.facts[:3] if state.facts else [],
                "new_evidence_added": len(new_evidence) if new_evidence else 0,
                "gaps": state.gaps,
                "next_search": eval_result.get('next_search_query')
            }
        
        # Check för COMPLETE
        if eval_result['status'] == 'COMPLETE':
            LOGGER.info(f"Planner COMPLETE efter {state.iteration + 1} iterationer")
            
            sources = [c.get('filename', 'unknown') for c in state.candidates[:10]]
            
            if state.current_synthesis:
                report = state.current_synthesis
            elif state.facts:
                report = "\n".join([f"- {fact}" for fact in state.facts])
            else:
                report = ""
            
            return {
                "status": "COMPLETE",
                "report": report,
                "current_synthesis": state.current_synthesis,
                "facts": state.facts,
                "state": state,
                "sources_used": sources,
                "gaps": state.gaps
            }
        
        # Check för ABORT
        if eval_result['status'] == 'ABORT':
            LOGGER.warning(f"Planner ABORT: {state.gaps}")
            
            # Returnera PARTIAL om vi har NÅGOT
            if state.current_synthesis or state.facts:
                sources = [c.get('filename', 'unknown') for c in state.candidates[:5]]
                if state.current_synthesis:
                    report = state.current_synthesis
                elif state.facts:
                    report = "\n".join([f"- {fact}" for fact in state.facts])
                else:
                    report = ""
                return {
                    "status": "PARTIAL",
                    "report": report,
                    "current_synthesis": state.current_synthesis,
                    "facts": state.facts,
                    "state": state,  # v8.2
                    "sources_used": sources,
                    "gaps": state.gaps
                }
            
            return {
                "status": "ABORT",
                "reason": "Inga relevanta dokument hittades",
                "report": "",
                "current_synthesis": "",
                "facts": [],
                "state": state,  # v8.2
                "sources_used": [],
                "gaps": state.gaps
            }
        
        # SEARCH: Kör ny sökning
        if eval_result['status'] == 'SEARCH':
            next_query = eval_result.get('next_search_query')
            
            if not next_query:
                LOGGER.warning("SEARCH utan next_search_query, avbryter")
                state.iteration += 1
                continue
            
            # Divergens-kontroll
            if _is_too_similar(next_query, state.past_queries):
                LOGGER.warning(f"Sökning '{next_query}' för lik tidigare, skippar")
                state.iteration += 1
                continue
            
            if not search_fn:
                LOGGER.warning("Ingen search_fn tillgänglig")
                state.iteration += 1
                continue
            
            LOGGER.info(f"Planner söker: '{next_query}'")
            state.past_queries.append(next_query)
            
            # Kör sökning
            search_result = search_fn(next_query)
            new_candidates = search_result.get('candidates_full', [])
            
            # Lägg till nya kandidater (undvik dubbletter)
            existing_ids = {c['id'] for c in state.candidates}
            added = 0
            for c in new_candidates:
                if c['id'] not in existing_ids:
                    state.candidates.append(c)
                    existing_ids.add(c['id'])
                    added += 1
            
            LOGGER.info(f"Lade till {added} nya kandidater")
            
            # Formatera nya kandidater för nästa iteration
            # v7.5: Använder TOP_N_FULLTEXT (3) från config
            current_candidates_formatted = format_candidates_for_planner(
                state.candidates, top_n_fulltext=TOP_N_FULLTEXT
            )
            
            # Logga sökning
            state.search_history.append({
                "query": next_query,
                "hits": len(new_candidates),
                "added": added,
                "iteration": state.iteration
            })
        
        state.iteration += 1
    
    # Max iterations nådd
    LOGGER.warning(f"Planner: Max iterations ({MAX_ITERATIONS}) nådd")
    
    # Returnera vad vi har
    sources = [c.get('filename', 'unknown') for c in state.candidates[:10]]
    
    # v8.2: Rapport baseras på Tornet
    if state.current_synthesis:
        report = state.current_synthesis
    elif state.facts:
        report = "\n".join([f"- {fact}" for fact in state.facts])
    else:
        report = ""
    
    has_content = state.current_synthesis or state.facts
    
    return {
        "status": "PARTIAL" if has_content else "ABORT",
        "report": report,
        "current_synthesis": state.current_synthesis,
        "facts": state.facts,
        "state": state,  # v8.2: För SessionEngine
        "sources_used": sources,
        "gaps": state.gaps
    }


# --- TEST ---
if __name__ == "__main__":
    print("Planner modul laddad.")
    print("Kräver ContextBuilder-output för att testa.")
