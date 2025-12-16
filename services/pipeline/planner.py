"""
Planner - Pipeline v8.3 "Bygglaget"

Ansvar:
- ReAct-loop: Reason + Act tills mission_goal uppfyllt
- Pivot or Persevere: Plannern avgör kontextrelevans
- Rolling Hypothesis: Bygger current_synthesis (Tornet) iterativt
- Identifiera gaps och learnings
- Äger PlannerState (självständig från SessionEngine)

v8.3 Changes:
- PlannerState flyttad hit från session_engine.py
- Planner är nu självständig modul

Princip: HARDFAIL > Silent Fallback
"""

import os
import json
import yaml
import logging
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import List, Dict
from google import genai
from rich.console import Console

# Console för "Thinking Out Loud" output
_CONSOLE = Console()

# Import utilities
try:
    from services.utils.json_parser import parse_llm_json
except ImportError as _import_err:
    try:
        from utils.json_parser import parse_llm_json
    except ImportError as e:
        raise ImportError(f"HARDFAIL: Kan inte importera utilities: {e}") from e

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
LOGGER = logging.getLogger('Planner')

API_KEY = CONFIG['ai_engine']['api_key']
MODEL_LITE = CONFIG['ai_engine']['models']['model_lite']
TAXONOMY_FILE = os.path.expanduser(CONFIG['paths'].get('taxonomy_file', '~/MyMemory/Index/my_mem_taxonomy.json'))


# === KNOWLEDGE FRAGMENT (v8.3 Bygglaget) ===

@dataclass
class KnowledgeFragment:
    """
    En "bit" av kunskap extraherad av en agent.
    
    Ersätter råtext med strukturerad, spårbar information.
    """
    content: str                    # "Budgeten sattes till 500k"
    source_doc_id: str              # UUID till källdokument
    fragment_type: str              # "temporal", "financial", "action", "fact"
    confidence: float               # 0.0 - 1.0
    metadata: Dict = field(default_factory=dict)  # Extra info (datum, valuta, etc.)
    
    def to_dict(self) -> dict:
        return asdict(self)


# === PLANNER STATE ===

@dataclass
class PlannerState:
    """
    State för Planner ReAct-loopen (v8.3 Bygglaget).
    
    Key fields:
    - facts: Lista av extraherade fakta (Bevisen) - append-only
    - current_synthesis: Aktuell arbetshypotes (Tornet) - uppdateras varje loop
    - fragments: Lista av KnowledgeFragments från agenter
    - agents_run: Vilka agenter som körts denna session
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
    # Librarian Loop: Track documents that have been "Deep Read" this session
    read_document_ids: set = field(default_factory=set)
    # v8.3: Fragments och agenter
    fragments: List[KnowledgeFragment] = field(default_factory=list)
    agents_run: List[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        """Konvertera till dict för serialisering."""
        result = asdict(self)
        # Convert set to list for JSON serialization
        result['read_document_ids'] = list(self.read_document_ids)
        # Convert fragments to list of dicts
        result['fragments'] = [f.to_dict() if hasattr(f, 'to_dict') else f for f in self.fragments]
        return result
    
    @classmethod
    def from_dict(cls, data: dict) -> 'PlannerState':
        """Skapa PlannerState från dict."""
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in data.items() if k in valid_fields}
        # Convert list back to set for read_document_ids
        if 'read_document_ids' in filtered_data and isinstance(filtered_data['read_document_ids'], list):
            filtered_data['read_document_ids'] = set(filtered_data['read_document_ids'])
        return cls(**filtered_data)


def _load_taxonomy_context() -> str:
    """Ladda taxonomi som kontext för smarta sökningar."""
    if not os.path.exists(TAXONOMY_FILE):
        LOGGER.warning(f"Taxonomi-fil saknas: {TAXONOMY_FILE}")
        return "(Ingen taxonomi tillgänglig)"
    
    try:
        with open(TAXONOMY_FILE, 'r', encoding='utf-8') as f:
            taxonomy = json.load(f)
        
        lines = []
        for node, data in taxonomy.items():
            desc = data.get('description', '')
            subs = data.get('sub_nodes', [])
            if subs:
                lines.append(f"- {node}: {', '.join(subs[:10])}")
            else:
                lines.append(f"- {node}: {desc[:50]}")
        return "\n".join(lines[:15])  # Max 15 noder för att inte spränga context
    except Exception as e:
        LOGGER.error(f"Kunde inte ladda taxonomi: {e}")
        return "(Fel vid laddning av taxonomi)"


TAXONOMY_CONTEXT = _load_taxonomy_context()

# Konstanter
MAX_ITERATIONS = 30     # Säkerhetsspärr (exhaustion avslutar tidigare)
GAIN_THRESHOLD = 0.05   # Under detta = "vi hittade inget nytt"
HIGH_GAIN_THRESHOLD = 0.3  # Över detta = "vi hittar fortfarande mycket, fortsätt!"
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


def _print_scan_summary(query: str, total_candidates: int, kept_docs: list):
    """
    Visar Librarian-arbetet i terminalen ("Thinking Out Loud").
    Sparsmakat format utan AI-cringe.
    """
    kept_count = len(kept_docs)
    discarded_count = total_candidates - kept_count
    
    # Hämta filnamn (max 3)
    filenames = []
    for d in kept_docs[:3]:
        name = d.get('filename') or d.get('title') or d.get('id', 'Okänd')[:20]
        filenames.append(name)
    
    files_str = ", ".join(filenames)
    if len(kept_docs) > 3:
        files_str += f" (+{len(kept_docs) - 3} till)"
    
    # Skriv ut
    _CONSOLE.print(f"\n[bold cyan]Undersöker:[/bold cyan] {query}")
    _CONSOLE.print(f"[dim]Scannade:[/dim] {total_candidates} kandidater -> [green]Behåller {kept_count}[/green], [red]Kastar {discarded_count}[/red]")
    if filenames:
        _CONSOLE.print(f"[dim]Läste:[/dim] {files_str}")


# === LIBRARIAN LOOP HELPERS ===

def _format_candidate_for_scan(doc: dict) -> str:
    """
    Format a candidate for the Librarian scan.
    Extract only: id, date, title/filename, summary.
    Strip all other metadata to reduce noise.
    """
    doc_id = doc.get('id', 'unknown')
    date = doc.get('timestamp_created', doc.get('date', 'N/A'))
    title = doc.get('title', doc.get('filename', 'Untitled'))
    summary = doc.get('summary', '')[:300]
    return f"[ID: {doc_id}] {date} | {title}\nSummary: {summary}"


def _scan_candidates(candidates: list, mission_goal: str, current_query: str, debug_mode: bool = False) -> list:
    """
    Librarian Scan: Filter candidates using LLM-based summary review.
    
    Active Retrieval: Uses BOTH global context (mission_goal) and 
    local context (current_query/sub-goal) to prevent tunnel vision.
    
    Args:
        candidates: List of candidate documents to scan
        mission_goal: The overall goal (global context)
        current_query: The current search query (local context/sub-goal)
        debug_mode: If True, print raw LLM response
    
    Returns:
        List of document IDs to keep for deep reading
    """
    if not candidates:
        return []
    
    prompt_template = PROMPTS.get('planner_scan', {}).get('instruction', '')
    if not prompt_template:
        LOGGER.warning("planner_scan prompt saknas, behåller alla kandidater")
        return [c['id'] for c in candidates]
    
    # Format candidates for scan
    candidates_list = "\n\n".join([
        f"DOK {i+1}: {_format_candidate_for_scan(c)}"
        for i, c in enumerate(candidates)
    ])
    
    try:
        full_prompt = prompt_template.format(
            mission_goal=mission_goal,
            current_query=current_query,
            candidates_list=candidates_list
        )
    except KeyError as e:
        LOGGER.error(f"Prompt formatting failed: {e}")
        return [c['id'] for c in candidates]
    
    try:
        client = _get_ai_client()
        response = client.models.generate_content(
            model=MODEL_LITE,
            contents=full_prompt
        )
        
        text = response.text
        
        if debug_mode:
            print(f"\n[DEBUG RAW LIBRARIAN SCAN]:\n{text}\n[END RAW]\n")
        
        LOGGER.debug(f"Librarian scan response: {text[:300]}...")
        result = parse_llm_json(text, context="planner_scan")
        
        keep_ids = result.get('keep_ids', [])
        discard_ids = result.get('discard_ids', [])
        
        LOGGER.info(f"Librarian scan: {len(keep_ids)} behålls, {len(discard_ids)} kastas")
        
        return keep_ids
        
    except Exception as e:
        LOGGER.error(f"Librarian scan failed: {e}, behåller alla kandidater")
        return [c['id'] for c in candidates]


def _evaluate_state(state: PlannerState, candidates_formatted: str, graph_context: str = "", debug_mode: bool = False) -> dict:
    """
    Evaluera aktuellt state mot mission_goal.
    v8.1 Rolling Hypothesis: Jämför ny info med befintlig syntes (Tornet).
    
    Args:
        state: PlannerState med current_synthesis, facts och past_queries
        candidates_formatted: Formaterade kandidater (topp 3 med fulltext)
        graph_context: Graf-relationer för kreativa sökspår
    
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
            max_iterations=MAX_ITERATIONS,
            taxonomy_context=TAXONOMY_CONTEXT,
            entity_relations=graph_context if graph_context else "(Inga grafkopplingar)"
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
        
        if debug_mode:
            print(f"\n[DEBUG RAW LLM RESPONSE]:\n{text}\n[END RAW]\n")
        
        LOGGER.debug(f"Planner evaluate LLM-svar: {text[:500]}...")
        result = parse_llm_json(text, context="planner_evaluate")
        
        return {
            "status": result.get('status', 'ABORT'),
            "context_gain": result.get('context_gain'),  # Skicka vidare LLM:ens bedömning
            # v8.1: Rolling Hypothesis
            "updated_synthesis": result.get('updated_synthesis', ''),
            "new_evidence": result.get('new_evidence', []),
            # Legacy fallbacks
            "new_facts": result.get('new_facts', []),
            "refined_findings": result.get('refined_findings', ''),
            "next_search_query": result.get('next_search_query'),
            "gaps": result.get('gaps', []),
            "llm_raw": text,
            # v8.4: LLM-driven delegation
            # interface_reasoning: ENDAST för UX (Thinking Out Loud), används ALDRIG för logik
            "interface_reasoning": result.get('interface_reasoning', ''),
            "agent_tasks": result.get('agent_tasks', [])
        }
        
    except ValueError as e:
        # parse_llm_json kastar ValueError vid fel
        LOGGER.error(f"HARDFAIL: Planner evaluate: {e}")
        raise


def run_planner_loop(
    intent_data: dict,
    query: str,
    session_id: str,
    initial_synthesis: str = "",
    initial_facts: list = None,
    debug_trace: dict = None,
    on_iteration=None,  # Callback för live-output
    on_scan=None,  # Callback för Librarian Loop reasoning display
) -> dict:
    """
    ReAct-loop med Bygglaget (v8.3).
    
    Arkitektur:
    1. Tar emot intent_data och anropar ContextBuilder själv
    2. Tar emot befintligt Torn + Facts (Pivot or Persevere)
    3. Plannern avgör själv om kontexten är relevant
    4. Returnerar resultat som dict
    
    Args:
        intent_data: Output från IntentRouter (mission_goal, keywords, etc.)
        query: Original användarfråga
        session_id: Unikt session-ID
        initial_synthesis: Befintligt Torn (Pivot or Persevere)
        initial_facts: Befintliga Facts (Pivot or Persevere)
        debug_trace: Dict för debug-info (optional)
    
    Returns:
        dict med:
            - status: "COMPLETE" | "ABORT" | "PARTIAL"
            - report: Rapport baserad på Tornet
            - current_synthesis: Uppdaterat Torn
            - facts: Uppdaterade Facts
            - candidates: Lista med kandidater
            - sources_used: Lista med använda filnamn
            - gaps: Vad som saknas
    """
    # Import ContextBuilder (Planner äger nu kontextbygget)
    try:
        from services.pipeline.context_builder import build_context, search, format_candidates_for_planner, TOP_N_FULLTEXT
    except ImportError as _import_err:
        LOGGER.debug(f"Fallback-import context_builder: {_import_err}")
        from context_builder import build_context, search, format_candidates_for_planner, TOP_N_FULLTEXT
    
    # --- FAS 1: BYGG KONTEXT ---
    mission_goal = intent_data.get('mission_goal', query)
    graph_context = ""
    
    LOGGER.debug("Planner: Anropar ContextBuilder...")
    context_result = build_context(
        intent_data=intent_data,
        debug_trace=debug_trace
    )
    
    initial_candidates = context_result.get('candidates_full', [])
    candidates_formatted = context_result.get('candidates_formatted', '')
    graph_context = context_result.get('graph_context', '')
    
    if context_result.get('status') == 'NO_RESULTS':
        LOGGER.warning(f"Planner: Inga träffar från ContextBuilder")
        return {
            "status": "ABORT",
            "reason": context_result.get('reason', 'Inga dokument hittades'),
            "report": "",
            "current_synthesis": initial_synthesis,
            "facts": initial_facts or [],
            "candidates": [],
            "sources_used": [],
            "gaps": ["Inga dokument hittades"]
        }
    
    LOGGER.info(f"Planner: Fick {len(initial_candidates)} kandidater från ContextBuilder")
    
    # --- FAS 2: SKAPA STATE ---
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
    
    # search_fn för extra sökningar i loopen
    search_fn = search
    
    # Debug mode: aktiveras om debug_trace finns
    debug_mode = debug_trace is not None
    
    # Librarian Loop: Track discarded documents for this query (query-scoped)
    local_discarded_ids = set()
    
    # Context Gain Delta - "Nöjd eller Utmattad"
    patience = 0
    
    while state.iteration < MAX_ITERATIONS:
        LOGGER.info(f"Planner iteration {state.iteration + 1}/{MAX_ITERATIONS}")
        
        # Evaluate: LLM läser dokument och uppdaterar hypotes
        eval_result = _evaluate_state(state, current_candidates_formatted, graph_context, debug_mode=debug_mode)
        
        # v8.1: Uppdatera TORNET (current_synthesis)
        # SPARA GAMLA LÄNGDEN FÖRST (för context_gain fallback)
        old_synthesis_len = len(state.current_synthesis) if state.current_synthesis else 0
        
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
        
        # --- v8.4: AGENT DELEGATION (Bygglaget) ---
        # LLM:en bestämmer vilka agenter som ska köras baserat på sitt resonemang
        agent_tasks = eval_result.get('agent_tasks', [])
        if agent_tasks:
            try:
                from services.agents.chronologist import extract_temporal
            except ImportError as e:
                LOGGER.debug(f"Fallback import agents: {e}")
                from agents.chronologist import extract_temporal
            
            for task in agent_tasks:
                agent_name = task.get('agent')
                agent_task = task.get('task', '')
                
                if agent_name == 'chronologist' and 'chronologist' not in state.agents_run:
                    LOGGER.info(f"Planner delegerar till Kronologen: {agent_task}")
                    try:
                        time_filter = intent_data.get('time_filter')
                        fragments = extract_temporal(
                            docs=state.candidates,
                            anchor_date=datetime.now().strftime("%Y-%m-%d"),
                            time_filter=time_filter
                        )
                        if fragments:
                            state.fragments.extend(fragments)
                            state.agents_run.append("chronologist")
                            LOGGER.info(f"Kronologen extraherade {len(fragments)} temporal fragments")
                            # Lägg till fragments som fakta i Tornet
                            for f in fragments:
                                if f.content not in state.facts:
                                    state.facts.append(f.content)
                    except Exception as e:
                        LOGGER.error(f"Kronologen misslyckades: {e}")
                
                # Framtida agenter kan läggas till här:
                # elif agent_name == 'economist' and 'economist' not in state.agents_run:
                #     ...
        
        # --- CONTEXT GAIN DELTA ---
        context_gain = eval_result.get('context_gain')
        
        # Smart Fallback om LLM missar context_gain
        if context_gain is None:
            new_len = len(state.current_synthesis) if state.current_synthesis else 0
            
            if old_synthesis_len == 0 and new_len > 0:
                # Tornet gick från tomt till något -> MAX GAIN
                LOGGER.warning(f"LLM missade context_gain. Fallback: Syntes skapad (0->{new_len}) -> 1.0")
                context_gain = 1.0
            elif new_len > old_synthesis_len * 1.2:
                # Texten växte med mer än 20% -> Bra gain
                LOGGER.warning(f"LLM missade context_gain. Fallback: Syntes växte ({old_synthesis_len}->{new_len}) -> 0.5")
                context_gain = 0.5
            else:
                # Marginell skillnad
                LOGGER.warning(f"LLM missade context_gain. Fallback: Marginell ändring ({old_synthesis_len}->{new_len}) -> 0.1")
                context_gain = 0.1
        
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
                    "candidates": state.candidates,
                    "sources_used": sources,
                    "gaps": state.gaps
                }
        else:
            patience = 0  # Reset om vi hittade nytt
        
        # Spara till debug_trace
        iter_data = {
            "iteration": state.iteration + 1,
            "status": eval_result['status'],
            "context_gain": context_gain,
            "patience": patience,
            "synthesis_preview": state.current_synthesis[:200] if state.current_synthesis else "(tom)",
            "facts_count": len(state.facts),
            "facts_preview": state.facts[:3] if state.facts else [],
            "new_evidence_added": len(new_evidence) if new_evidence else 0,
            "gaps": state.gaps,
            "next_search": eval_result.get('next_search_query'),
            # v8.4: Thinking Out Loud - visas i chatten, används INTE för logik
            "interface_reasoning": eval_result.get('interface_reasoning', ''),
            "agents_dispatched": [t.get('agent') for t in agent_tasks] if agent_tasks else []
        }
        
        if debug_trace is not None:
            debug_trace[f'planner_iter_{state.iteration}'] = iter_data
        
        # LIVE OUTPUT
        if on_iteration:
            on_iteration(iter_data)
        
        # Check för COMPLETE
        if eval_result['status'] == 'COMPLETE':
            # Om vi fortfarande hittar mycket nytt OCH har en sökning, fortsätt!
            if context_gain >= HIGH_GAIN_THRESHOLD and eval_result.get('next_search_query'):
                LOGGER.info(f"Hög gain ({context_gain:.2f} >= {HIGH_GAIN_THRESHOLD}), fortsätter med ny sökning")
                eval_result['status'] = 'SEARCH'
            else:
                # Acceptera COMPLETE
                if context_gain >= HIGH_GAIN_THRESHOLD:
                    LOGGER.info(f"Hög gain men ingen sökning given - accepterar COMPLETE")
                
                LOGGER.info(f"Planner COMPLETE efter {state.iteration + 1} iterationer (gain={context_gain:.2f})")
                
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
                    "candidates": state.candidates,
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
                    "candidates": state.candidates,
                    "sources_used": sources,
                    "gaps": state.gaps
                }
            
            return {
                "status": "ABORT",
                "reason": "Inga relevanta dokument hittades",
                "report": "",
                "current_synthesis": "",
                "facts": [],
                "candidates": [],
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
            
            # === LIBRARIAN LOOP: Two-Stage Retrieval ===
            
            # 1. Hard Filter (Dedup) - Remove already read or discarded documents
            candidates_to_scan = [c for c in new_candidates 
                                  if c['id'] not in state.read_document_ids
                                  and c['id'] not in local_discarded_ids]
            
            # 2. Exhaustion Check
            if not candidates_to_scan:
                LOGGER.info("Librarian: No new candidates after dedup - exhausted")
                state.iteration += 1
                continue
            
            # 3. Determine current_query (sub-goal for Active Retrieval)
            current_query = state.past_queries[-1] if state.past_queries else state.mission_goal
            
            # 4. Scan (Soft Filter) - with BOTH global and local context
            kept_ids = _scan_candidates(
                candidates=candidates_to_scan,
                mission_goal=state.mission_goal,
                current_query=current_query,
                debug_mode=debug_mode
            )
            
            # 5. Update discarded
            for c in candidates_to_scan:
                if c['id'] not in kept_ids:
                    local_discarded_ids.add(c['id'])
            
            # 6. Select for Deep Read
            final_candidates = [c for c in candidates_to_scan if c['id'] in kept_ids]
            
            # 7. "Thinking Out Loud" - visa vad vi gör i terminalen
            _print_scan_summary(current_query, len(candidates_to_scan), final_candidates)
            
            # 8. Commit to session memory
            for c in final_candidates:
                state.read_document_ids.add(c['id'])
            
            # 9. Call on_scan callback (för debug_pipeline_trace.py)
            if on_scan:
                on_scan({
                    "current_query": current_query,
                    "mission_goal": state.mission_goal,
                    "scanned": len(candidates_to_scan),
                    "kept": len(kept_ids),
                    "discarded": len(candidates_to_scan) - len(kept_ids),
                    "kept_titles": [c.get('filename', c['id'][:12]) for c in final_candidates]
                })
            
            # === END LIBRARIAN LOOP ===
            
            # Lägg till nya kandidater till state (undvik dubbletter)
            existing_ids = {c['id'] for c in state.candidates}
            added = 0
            for c in final_candidates:
                if c['id'] not in existing_ids:
                    state.candidates.append(c)
                    existing_ids.add(c['id'])
                    added += 1
            
            LOGGER.info(f"Librarian: Scannade {len(candidates_to_scan)}, behöll {len(kept_ids)}, lade till {added} nya kandidater")
            
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
        "candidates": state.candidates,
        "sources_used": sources,
        "gaps": state.gaps
    }


# --- TEST ---
if __name__ == "__main__":
    print("Planner modul laddad.")
    print("Kräver ContextBuilder-output för att testa.")
