"""
Planner - Pipeline v7.5 "Facts List"

Ansvar:
- ReAct-loop: Reason + Act tills mission_goal uppfyllt
- Evaluate state: Jämför insamlad data med mission_goal
- Identifiera gaps och learnings
- Konvergens/stagnation-detection

v7.5 Changes:
- Facts List: Appendar fakta istället för att skriva om working_findings
- Förhindrar "Telephone Game" där detaljer försvinner vid omskrivning

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
    from services.utils.state_manager import PlannerState, save_state, load_state
except ImportError as _import_err:
    try:
        from utils.json_parser import parse_llm_json
        from utils.state_manager import PlannerState, save_state, load_state
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
MAX_ITERATIONS = 5

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
    v7.5 Fact Extractor: Extraherar fakta från dokument (appendar, ersätter inte).
    
    Args:
        state: PlannerState med facts och past_queries
        candidates_formatted: Formaterade kandidater (topp 3 med fulltext)
    
    Returns:
        dict med:
            - status: "SEARCH" | "COMPLETE" | "ABORT"
            - new_facts: Nya extraherade fakta (läggs till i facts-listan)
            - next_search_query: Ny sökning om SEARCH
            - gaps: Vad som fortfarande saknas
    """
    prompt_template = PROMPTS.get('planner_evaluate', {}).get('instruction', '')
    
    if not prompt_template:
        LOGGER.error("HARDFAIL: planner_evaluate prompt saknas i chat_prompts.yaml")
        raise ValueError("HARDFAIL: planner_evaluate prompt saknas i chat_prompts.yaml")
    
    past_queries = _format_past_queries(state.past_queries)
    existing_facts = _format_existing_facts(state.facts)
    
    # v7.5: Använd nya placeholders om de finns, annars fallback till legacy
    try:
        full_prompt = prompt_template.format(
            mission_goal=state.mission_goal,
            existing_facts=existing_facts,
            candidates=candidates_formatted,
            past_queries=past_queries,
            iteration=state.iteration + 1,
            max_iterations=MAX_ITERATIONS
        )
    except KeyError:
        # Fallback för legacy prompt (working_findings)
        LOGGER.warning("Legacy prompt format detekterat, använder working_findings")
        full_prompt = prompt_template.format(
            mission_goal=state.mission_goal,
            working_findings=_format_existing_facts(state.facts) if state.facts else state.working_findings or "(Tom)",
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
            "new_facts": result.get('new_facts', []),  # v7.5: nya fakta
            "refined_findings": result.get('refined_findings', ''),  # Legacy fallback
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
    search_fn=None,
    debug_trace: dict = None
) -> dict:
    """
    ReAct-loop med Facts List (v7.5).
    
    Arkitektur:
    1. Topp 3 dokument levereras med FULLTEXT direkt (Lost in the Middle fix)
    2. LLM extraherar FAKTA från dokument (appendar till lista, skriver INTE om)
    3. SEARCH, COMPLETE eller ABORT
    4. Rapport byggs från facts-lista först i slutet
    
    Args:
        mission_goal: Uppdrag från IntentRouter
        query: Original användarfråga
        initial_candidates: Kandidater från ContextBuilder (full metadata)
        candidates_formatted: Formaterad sträng med topp 3 fulltext
        session_id: Unikt session-ID för state persistence
        search_fn: Funktion för extra sökningar (optional)
        debug_trace: Dict för debug-info (optional)
    
    Returns:
        dict med:
            - status: "COMPLETE" | "ABORT" | "PARTIAL"
            - report: Fakta-lista formaterad som rapport
            - sources_used: Lista med använda filnamn
            - gaps: Vad som saknas
    """
    # Import format_candidates_for_planner för nya sökningar
    try:
        from services.context_builder import format_candidates_for_planner, TOP_N_FULLTEXT
    except ImportError as _import_err:
        LOGGER.debug(f"Fallback-import context_builder: {_import_err}")
        from context_builder import format_candidates_for_planner, TOP_N_FULLTEXT
    
    # Ladda eller skapa state
    state = load_state(session_id)
    if state is None:
        state = PlannerState(
            session_id=session_id,
            mission_goal=mission_goal,
            query=query,
            candidates=initial_candidates,
            facts=[],  # v7.5: Tom lista
            working_findings="",  # Legacy
            past_queries=[]
        )
    
    # Nuvarande kandidater formaterade för prompt
    current_candidates_formatted = candidates_formatted
    
    while state.iteration < MAX_ITERATIONS:
        LOGGER.info(f"Planner iteration {state.iteration + 1}/{MAX_ITERATIONS}")
        
        # Evaluate: LLM läser dokument och extraherar fakta
        eval_result = _evaluate_state(state, current_candidates_formatted)
        
        # v7.5: APPENDA nya fakta med deduplicering
        new_facts = eval_result.get('new_facts', [])
        if new_facts:
            # FACT DEDUPLICATION: Undvik att lägga till samma fakta flera gånger
            existing_lower = {f.lower().strip() for f in state.facts}
            for fact in new_facts:
                if fact and fact.lower().strip() not in existing_lower:
                    state.facts.append(fact)
                    existing_lower.add(fact.lower().strip())
            LOGGER.info(f"Lade till {len(new_facts)} nya fakta (totalt: {len(state.facts)})")
        
        # Legacy fallback: Om prompten returnerar refined_findings istället för new_facts
        if not new_facts and eval_result.get('refined_findings'):
            state.working_findings = eval_result.get('refined_findings', state.working_findings)
        
        state.gaps = eval_result.get('gaps', [])
        
        # Spara state för debuggbarhet
        save_state(state)
        
        # Spara till debug_trace
        if debug_trace is not None:
            # v7.5: Visa facts count och preview
            facts_preview = state.facts[:3] if state.facts else []
            debug_trace[f'planner_iter_{state.iteration}'] = {
                "status": eval_result['status'],
                "facts_count": len(state.facts),
                "facts_preview": facts_preview,
                "new_facts_added": len(new_facts) if new_facts else 0,
                "gaps": state.gaps,
                "next_search": eval_result.get('next_search_query')
            }
        
        # Check för COMPLETE
        if eval_result['status'] == 'COMPLETE':
            LOGGER.info(f"Planner COMPLETE efter {state.iteration + 1} iterationer")
            
            # Extrahera källor från candidates
            sources = [c.get('filename', 'unknown') for c in state.candidates[:10]]
            
            # v7.5: Bygg rapport från facts-lista (om den finns)
            if state.facts:
                report = "\n".join([f"- {fact}" for fact in state.facts])
            else:
                report = state.working_findings  # Legacy fallback
            
            return {
                "status": "COMPLETE",
                "report": report,
                "sources_used": sources,
                "gaps": state.gaps
            }
        
        # Check för ABORT
        if eval_result['status'] == 'ABORT':
            LOGGER.warning(f"Planner ABORT: {state.gaps}")
            
            # Returnera PARTIAL om vi har NÅGOT (facts eller working_findings)
            if state.facts or state.working_findings:
                sources = [c.get('filename', 'unknown') for c in state.candidates[:5]]
                # v7.5: Bygg rapport från facts-lista
                if state.facts:
                    report = "\n".join([f"- {fact}" for fact in state.facts])
                else:
                    report = state.working_findings
                return {
                    "status": "PARTIAL",
                    "report": report,
                    "sources_used": sources,
                    "gaps": state.gaps
                }
            
            return {
                "status": "ABORT",
                "reason": "Inga relevanta dokument hittades",
                "report": "",
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
    
    # v7.5: Bygg rapport från facts-lista
    if state.facts:
        report = "\n".join([f"- {fact}" for fact in state.facts])
    else:
        report = state.working_findings
    
    return {
        "status": "PARTIAL" if (state.facts or state.working_findings) else "ABORT",
        "report": report,
        "sources_used": sources,
        "gaps": state.gaps
    }


# --- TEST ---
if __name__ == "__main__":
    print("Planner modul laddad.")
    print("Kräver ContextBuilder-output för att testa.")
