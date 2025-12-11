import os
import sys
import time
import yaml
import json
import logging
import chromadb
import datetime
import argparse
from google import genai
from google.genai import types
from chromadb.utils import embedding_functions
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.table import Table
from rich import box
from rich.live import Live

# Pipeline v6.0 imports
try:
    from services.intent_router import route_intent
    from services.context_builder import build_context
    from services.planner import create_report
except ImportError:
    # Direkt körning - försök importera utan services-prefix
    try:
        from intent_router import route_intent
        from context_builder import build_context
        from planner import create_report
    except ImportError as e:
        raise ImportError(f"HARDFAIL: Kan inte importera pipeline-moduler: {e}") from e

# Entity Register (OBJEKT-44) - Använder graph_builder direkt
try:
    from services.my_mem_graph_builder import (
        add_entity_alias as add_alias,
        get_canonical_from_graph as get_canonical,
        get_all_entities as get_known_entities
    )
except ImportError:
    try:
        from my_mem_graph_builder import (
            add_entity_alias as add_alias,
            get_canonical_from_graph as get_canonical,
            get_all_entities as get_known_entities
        )
    except ImportError as e:
        raise ImportError(
            "HARDFAIL: my_mem_graph_builder.py saknas eller har fel."
        ) from e

# Session Logger (OBJEKT-48) - ännu ej implementerat
try:
    from services.session_logger import (
        start_session, end_session, log_search, log_feedback, log_abort
    )
except ImportError:
    try:
        from session_logger import (
            start_session, end_session, log_search, log_feedback, log_abort
        )
    except ImportError as e:
        raise ImportError(
            "HARDFAIL: session_logger.py saknas. "
            "Skapa modulen enligt OBJEKT-48 i backloggen."
        ) from e

import re

# --- ARGUMENT PARSER (endast vid direkt körning) ---
def parse_args():
    parser = argparse.ArgumentParser(description="MyMem Chat Client")
    parser.add_argument("--debug", action="store_true", help="Aktivera debug-läge (skvaller)")
    return parser.parse_args()

# --- CONFIG LOADER ---
def hitta_och_ladda_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths_to_check = [
        os.path.join(script_dir, 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, '..', 'my_mem_config.yaml')
    ]
    config_path = None
    for p in paths_to_check:
        if os.path.exists(p):
            config_path = p
            break
    if not config_path:
        print("[Chat] CRITICAL: Config not found.")
        exit(1)
    
    with open(config_path, 'r') as f: config = yaml.safe_load(f)
    for k, v in config['paths'].items():
        config['paths'][k] = os.path.expanduser(v)
    return config

CONFIG = hitta_och_ladda_config()

# --- PROMPT LOADER ---
def ladda_chat_prompts():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', 'config', 'chat_prompts.yaml'),
        os.path.join(script_dir, 'config', 'chat_prompts.yaml'),
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f)
            except Exception as e:
                raise RuntimeError(f"HARDFAIL: Kunde inte ladda prompts från {p}: {e}") from e
    raise FileNotFoundError("HARDFAIL: chat_prompts.yaml hittades inte")

PROMPTS = ladda_chat_prompts()

# --- SETUP ---
CHROMA_PATH = CONFIG['paths']['chroma_db']
LAKE_PATH = CONFIG['paths'].get('lake_dir', os.path.expanduser("~/MyMemory/Lake"))
LOG_FILE = CONFIG['logging']['log_file_path'] 

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - CHAT - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('MyMem_Chat')

API_KEY = CONFIG['ai_engine']['api_key']
MODEL_LITE = CONFIG['ai_engine']['models']['model_lite'] 
MODEL_PRO = CONFIG['ai_engine']['models']['model_pro']   

# UI SETUP: Tvinga vänsterjustering och rimlig bredd
console = Console(width=100) 

# AI Client (global, initieras lazy)
_AI_CLIENT = None

def get_ai_client():
    global _AI_CLIENT
    if _AI_CLIENT is None:
        try:
            _AI_CLIENT = genai.Client(api_key=API_KEY)
        except Exception as e:
            console.print(f"[bold red]Kunde inte initiera AI: {e}[/bold red]")
            raise
    return _AI_CLIENT

# --- DEBUG HELPER ---
def debug_panel(title, content, style="yellow", debug_mode=False, debug_trace=None, trace_key=None):
    """
    Visar debug-panel i terminalen OCH samlar info till debug_trace om tillgänglig.
    
    Args:
        title: Titel för panelen
        content: Innehåll att visa
        style: Färgstil
        debug_mode: Om True, printa till konsolen
        debug_trace: Dict att samla debug-info till (optional)
        trace_key: Nyckel att spara under i debug_trace (optional)
    """
    if debug_mode:
        console.print(Panel(content, title=f"[bold {style}]DEBUG: {title}[/bold {style}]", border_style=style, expand=False, padding=(0, 1)))
    
    # Samla till trace om tillgänglig
    if debug_trace is not None and trace_key is not None:
        # Rensa Rich-formatering för trace
        clean_content = content.replace("[bold]", "").replace("[/bold]", "")
        clean_content = clean_content.replace("[cyan]", "").replace("[/cyan]", "")
        clean_content = clean_content.replace("[green]", "").replace("[/green]", "")
        clean_content = clean_content.replace("[magenta]", "").replace("[/magenta]", "")
        debug_trace[trace_key] = clean_content

# --- FEEDBACK HANDLING ---

def _parse_learn_command(text: str) -> tuple:
    """
    Parsa /learn kommandot.
    
    Format: /learn X = Y (person|projekt|koncept)
    
    Returns:
        (canonical, alias, entity_type) eller (None, None, None)
    """
    # Matcha: /learn X = Y eller /learn X = Y (typ)
    pattern = r'^/learn\s+(.+?)\s*=\s*(.+?)(?:\s*\((\w+)\))?\s*$'
    match = re.match(pattern, text.strip(), re.IGNORECASE)
    
    if not match:
        return None, None, None
    
    canonical = match.group(1).strip()
    alias = match.group(2).strip()
    entity_type = match.group(3) if match.group(3) else "Person"
    
    # Normalisera entity_type till taxonomins kategorier
    # Användaren kan skriva svenska eller engelska
    type_map = {
        # Svenska (taxonomin)
        "person": "Person",
        "aktör": "Aktör",
        "organisation": "Aktör",
        "projekt": "Projekt",
        "teknologi": "Teknologier",
        "produkt": "Teknologier",
        "metodik": "Metodik",
        "koncept": "Metodik",
        # Engelska alternativ
        "organization": "Aktör",
        "project": "Projekt",
        "product": "Teknologier",
        "concept": "Metodik"
    }
    entity_type = type_map.get(entity_type.lower(), entity_type)
    
    return canonical, alias, entity_type


def _interpret_feedback(text: str) -> dict:
    """
    Tolka naturligt språk som potentiell feedback.
    
    Returns:
        dict med is_feedback, type, canonical, alias, entity_type, confidence
    """
    # Kolla om det finns en feedback_interpreter prompt
    prompt_template = PROMPTS.get('feedback_interpreter', {}).get('instruction', '')
    if not prompt_template:
        return {"is_feedback": False}
    
    full_prompt = prompt_template.format(user_input=text)
    
    try:
        ai_client = get_ai_client()
        response = ai_client.models.generate_content(model=MODEL_LITE, contents=full_prompt)
        result_text = response.text.replace("```json", "").replace("```", "").strip()
        result = json.loads(result_text)
        return result
    except Exception as e:
        LOGGER.debug(f"Feedback interpretation failed: {e}")
        return {"is_feedback": False}


def _handle_feedback(canonical: str, alias: str, entity_type: str) -> str:
    """
    Hantera bekräftad feedback genom att spara alias.
    
    Returns:
        Bekräftelsemeddelande till användaren
    """
    try:
        add_alias(canonical, alias, entity_type)
        
        # Logga feedback för Dreaming
        log_feedback(canonical, alias, entity_type, source="user")
        
        # Mappa taxonomins kategorier till läsbara svenska namn
        type_names = {
            "Person": "person",
            "Aktör": "organisation",
            "Projekt": "projekt",
            "Teknologier": "produkt",
            "Metodik": "koncept"
        }
        type_name = type_names.get(entity_type, "entitet")
        
        return f"✓ Noterat! Nästa gång jag hör '{alias}' skriver jag '{canonical}' ({type_name})."
    except Exception as e:
        LOGGER.error(f"Kunde inte spara alias: {e}")
        return f"✗ Kunde inte spara aliaset: {e}"


# --- STEP 2a: THE HUNTER ---
def search_lake(keywords):
    if not keywords: return {}
    hits = {}
    if not os.path.exists(LAKE_PATH): return hits
    
    try:
        files = [f for f in os.listdir(LAKE_PATH) if f.endswith('.md')]
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Kunde inte lista filer i Lake: {e}")
        raise RuntimeError(f"HARDFAIL: Kunde inte lista filer i Lake: {e}") from e
    
    for filename in files:
        filepath = os.path.join(LAKE_PATH, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                content_lower = content.lower()
                
                summary = "Ingen sammanfattning"
                if "summary:" in content:
                    try:
                        summary_part = content.split("summary:")[1].split("\n")[0].strip()
                        if len(summary_part) > 5: summary = summary_part
                    except Exception as e:
                        LOGGER.error(f"HARDFAIL: Kunde inte parsa summary i {filename}: {e}")
                        raise RuntimeError(f"HARDFAIL: Kunde inte parsa summary i {filename}") from e

                for kw in keywords:
                    if kw.lower() in content_lower:
                        file_id = filename
                        if "_" in filename:
                             parts = filename.split('_')
                             if len(parts[-1]) > 10: file_id = parts[-1].replace('.md', '')
                        
                        hits[file_id] = {
                            "id": file_id,
                            "filename": filename,
                            "summary": summary,
                            "content": content,
                            "source": f"SEARCH_LAKE ({kw})",
                            "score": 1.0
                        }
                        break 
        except Exception as e:
            LOGGER.error(f"HARDFAIL: Kunde inte läsa fil {filename}: {e}")
            raise RuntimeError(f"HARDFAIL: Kunde inte läsa fil {filename}") from e
    return hits

# --- STEP 1: PLANERING ---
def plan_query(user_query, chat_history, debug_mode=False, debug_trace=None):
    history_text = ""
    if chat_history:
        history_text = "\nKONTEXT (Tidigare dialog):\n" + "\n".join([f"{m['role'].upper()}: {m['content']}" for m in chat_history])

    instruction_template = PROMPTS['planner']['instruction']
    prompt_content = instruction_template.format(date=datetime.date.today())
    
    full_prompt = f"FRÅGA: \"{user_query}\"\n{history_text}\n\n{prompt_content}"

    try:
        ai_client = get_ai_client()
        resp = ai_client.models.generate_content(model=MODEL_LITE, contents=full_prompt)
        text = resp.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        
        # Visa resonemanget!
        debug_content = f"[bold]Resonemang:[/bold] {data.get('reasoning', 'Inget resonemang')}\n\n"
        debug_content += f"[cyan]Jägaren:[/cyan] {data.get('hunter_keywords')}\n"
        debug_content += f"[green]Vektorn:[/green] {data.get('vector_query')}\n"
        debug_content += f"[magenta]Kriterier:[/magenta] {data.get('ranking_criteria')}"
        
        debug_panel("1. PLANERING", debug_content, style="magenta", debug_mode=debug_mode, debug_trace=debug_trace, trace_key="plan")
        
        # Spara strukturerad plan-data till trace (utökad)
        if debug_trace is not None:
            debug_trace["plan_data"] = data
            debug_trace["hunter_keywords"] = data.get('hunter_keywords', [])
            debug_trace["vector_query"] = data.get('vector_query', '')
            debug_trace["ranking_criteria"] = data.get('ranking_criteria', '')
            debug_trace["plan_reasoning"] = data.get('reasoning', '')
        
        return data
    except Exception as e:
        LOGGER.error(f"Plan error: {e}")
        return {"ranking_criteria": "Relevans", "hunter_keywords": [], "vector_query": user_query}

# --- STEP 4: RE-RANKING ---
def rerank_candidates(candidates, criteria, debug_mode=False, debug_trace=None):
    if not candidates: return [], None
    
    docs_metadata = []
    for doc in candidates.values():
        snippet = doc.get('summary', '')
        if len(snippet) < 10: snippet = doc.get('content', '')[:300].replace("\n", " ")
        docs_metadata.append({
            "id": doc['id'],
            "filename": doc['filename'],
            "source": doc['source'],
            "summary_snippet": snippet
        })
    
    # Visa vad domaren får jobba med
    if debug_mode:
        input_table = Table(title="Dokument till Domaren", show_header=True, header_style="bold magenta", box=box.SIMPLE, width=90)
        input_table.add_column("Källa", style="cyan", width=15)
        input_table.add_column("Filnamn", style="green")
        for d in docs_metadata[:10]:
            input_table.add_row(d['source'], d['filename'])
        console.print(input_table)

    instruction_template = PROMPTS['judge']['instruction']
    full_prompt = instruction_template.format(
        criteria=criteria,
        documents=json.dumps(docs_metadata, indent=2)
    )
    
    try:
        ai_client = get_ai_client()
        resp = ai_client.models.generate_content(model=MODEL_LITE, contents=full_prompt)
        text = resp.text.replace("```json", "").replace("```", "").strip()
        rank_data = json.loads(text)
        ranked_ids = rank_data.get('ranked_ids', [])
        
        debug_content = f"[bold]Resonemang:[/bold] {rank_data.get('reasoning', 'Inget resonemang')}\n\n"
        debug_content += f"[bold]Valda IDn:[/bold] {ranked_ids[:5]}..."
        debug_panel("4. DOMAREN (Beslut)", debug_content, style="cyan", debug_mode=debug_mode, debug_trace=debug_trace, trace_key="judge")
        
        # Spara judge-data till trace (utökad)
        if debug_trace is not None:
            debug_trace["judge_data"] = rank_data
            debug_trace["judge_input_count"] = len(docs_metadata)
            debug_trace["judge_input_files"] = [d['filename'] for d in docs_metadata]
            debug_trace["judge_reasoning"] = rank_data.get('reasoning', '')
        
        if not ranked_ids:
            if debug_mode: console.print("[dim yellow]   ⚠️ Domaren returnerade 0 dokument. Använder fallback.[/dim yellow]")
            return list(candidates.keys()), rank_data
            
        return ranked_ids, rank_data
    except Exception as e:
        LOGGER.error(f"Rerank error: {e}")
        return list(candidates.keys()), None

# --- MAIN SEARCH PIPELINE ---
def execute_pipeline(query, chat_history, debug_mode=False, debug_trace=None):
    """
    Kör sökpipelinen och returnerar context_docs.
    
    Args:
        query: Användarens fråga
        chat_history: Lista med tidigare meddelanden
        debug_mode: Om True, visa debug-paneler
        debug_trace: Dict att samla debug-info till (optional)
    
    Returns:
        list: Lista med kontext-dokument för syntes
    """
    plan = plan_query(query, chat_history, debug_mode=debug_mode, debug_trace=debug_trace)
    candidates = {}
    hunter_hits_count = 0
    vector_hits_count = 0

    # 2. GREP SÖK (Jägaren)
    hunter_files = []
    if plan.get('hunter_keywords'):
        hunter_hits = search_lake(plan['hunter_keywords'])
        if hunter_hits:
            candidates.update(hunter_hits)
            hunter_hits_count = len(hunter_hits)
            hunter_files = [{"filename": h['filename'], "matched_keyword": h['source']} for h in hunter_hits.values()]
            
            if debug_mode:
                msg = "\n".join([f"- {h['filename']}" for h in list(hunter_hits.values())[:5]])
                if len(hunter_hits) > 5: msg += f"\n... (+{len(hunter_hits)-5} till)"
                debug_panel("2. JÄGAREN (Träffar)", msg, style="green", debug_mode=debug_mode, debug_trace=debug_trace, trace_key="hunter")
    
    # Spara hunter-resultat till trace
    if debug_trace is not None:
        debug_trace["hunter_files"] = hunter_files

    # 3. VEKTORN
    vector_files = []
    try:
        chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
        emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        coll = chroma_client.get_collection(name="dfm_knowledge_base", embedding_function=emb_fn)
        
        vec_res = coll.query(query_texts=[plan['vector_query']], n_results=20)
        
        vector_hits_debug = []
        for i, uid in enumerate(vec_res['ids'][0]):
            distance = vec_res['distances'][0][i]
            meta = vec_res['metadatas'][0][i]
            filename = meta.get('filename')
            
            # Spara alla vektorträffar till trace (även duplicates)
            vector_files.append({
                "filename": filename,
                "distance": round(distance, 3),
                "already_in_candidates": uid in candidates
            })
            
            if uid in candidates: continue 
            
            text = vec_res['documents'][0][i]
            
            candidates[uid] = {
                "id": uid,
                "filename": filename,
                "summary": meta.get('summary', 'Ingen sammanfattning'),
                "content": text,
                "source": "VEKTOR",
                "score": 0.5
            }
            vector_hits_debug.append(f"{filename} ({distance:.2f})")
        
        vector_hits_count = len(vector_hits_debug)
            
        if debug_mode and vector_hits_debug:
             msg = "\n".join(vector_hits_debug[:5])
             if len(vector_hits_debug) > 5: msg += f"\n... (+{len(vector_hits_debug)-5} till)"
             debug_panel("3. VEKTORN", msg, style="blue", debug_mode=debug_mode, debug_trace=debug_trace, trace_key="vector")

    except Exception as e:
        if debug_mode:
            console.print(f"[red]Vektor fel: {e}[/red]")
        LOGGER.error(f"Vector search error: {e}")
    
    # Spara vektor-resultat till trace
    if debug_trace is not None:
        debug_trace["vector_files"] = vector_files

    # Spara träffstatistik till trace
    if debug_trace is not None:
        debug_trace["hits_hunter"] = hunter_hits_count
        debug_trace["hits_vector"] = vector_hits_count

    # 4. RE-RANKING
    ranked_ids, judge_data = rerank_candidates(candidates, plan['ranking_criteria'], debug_mode=debug_mode, debug_trace=debug_trace)
    
    # 5. CONTEXT ASSEMBLY
    final_context = []
    sources = []
    total_chars = 0
    MAX_CHARS = 100000
    
    for uid in ranked_ids:
        if uid not in candidates: continue
        doc = candidates[uid]
        
        content = doc['content']
        if len(content) < 1000 and os.path.exists(os.path.join(LAKE_PATH, doc['filename'])):
             try:
                 with open(os.path.join(LAKE_PATH, doc['filename']), 'r') as f:
                     content = f.read()
             except Exception as e:
                 LOGGER.error(f"HARDFAIL: Kunde inte läsa fil {doc['filename']}: {e}")
                 raise RuntimeError(f"HARDFAIL: Kunde inte läsa fil {doc['filename']}") from e
        
        entry = f"--- DOKUMENT ({doc['source']}) ---\nID: {uid}\nFIL: {doc['filename']}\nINNEHÅLL:\n{content}\n"
        
        if total_chars + len(entry) > MAX_CHARS:
            break
            
        final_context.append(entry)
        sources.append(doc['filename'])
        total_chars += len(entry)

    debug_panel("5. SYNTES", f"Valde {len(final_context)} dokument.\nTotalt: {total_chars} tecken.", style="red", debug_mode=debug_mode, debug_trace=debug_trace, trace_key="synthesis")
    
    # Spara utökad info till trace
    if debug_trace is not None:
        debug_trace["sources"] = sources
        debug_trace["docs_selected"] = len(final_context)
        debug_trace["total_chars"] = total_chars
        debug_trace["total_candidates"] = len(candidates)
        debug_trace["ranked_order"] = ranked_ids[:10] if ranked_ids else []
        # Sammanfattning av flödet
        debug_trace["pipeline_summary"] = {
            "keywords_used": plan.get('hunter_keywords', []),
            "vector_query_used": plan.get('vector_query', ''),
            "hunter_found": hunter_hits_count,
            "vector_found": vector_hits_count,
            "total_candidates": len(candidates),
            "docs_to_synthesis": len(final_context),
            "chars_to_synthesis": total_chars
        }
    
    return final_context


# === PIPELINE v6.0 ===
def execute_pipeline_v6(query, chat_history, debug_mode=False, debug_trace=None):
    """
    Pipeline v6.0: IntentRouter → ContextBuilder → Planner → Synthesizer
    
    Args:
        query: Användarens fråga
        chat_history: Lista med tidigare meddelanden
        debug_mode: Om True, visa debug-paneler
        debug_trace: Dict att samla debug-info till (optional)
    
    Returns:
        dict: {
            "status": "OK" | "NO_RESULTS" | "ERROR",
            "report": Markdown-rapport för Synthesizer,
            "sources": Lista med filnamn,
            "gaps": Lista med identifierade luckor
        }
    """
    start_time = time.time()
    
    # Fas 1: IntentRouter (AI)
    if debug_mode:
        console.print("[dim]→ IntentRouter...[/dim]")
    
    intent_data = route_intent(query, chat_history, debug_trace=debug_trace)
    
    if intent_data.get('status') == 'ERROR':
        LOGGER.error(f"HARDFAIL: IntentRouter misslyckades: {intent_data.get('reason')}")
        raise RuntimeError(f"HARDFAIL: IntentRouter misslyckades: {intent_data.get('reason')}")
    
    if debug_mode:
        debug_content = f"[bold]Intent:[/bold] {intent_data.get('intent')}\n"
        debug_content += f"[cyan]Keywords:[/cyan] {intent_data.get('keywords')}\n"
        debug_content += f"[green]Graf-paths:[/green] {intent_data.get('graph_paths')}\n"
        debug_content += f"[magenta]Tidsfilter:[/magenta] {intent_data.get('time_filter')}"
        debug_panel("1. INTENT", debug_content, style="magenta", debug_mode=debug_mode)
    
    # Fas 2: ContextBuilder (Kod)
    if debug_mode:
        console.print("[dim]→ ContextBuilder...[/dim]")
    
    context_result = build_context(intent_data, debug_trace=debug_trace)
    
    # Logga sökning för Dreaming
    stats = context_result.get('stats', {})
    total_hits = stats.get('lake_hits', 0) + stats.get('vector_hits', 0)
    log_search(
        query=query,
        keywords=intent_data.get('keywords', []),
        hits=total_hits,
        intent=intent_data.get('intent', 'RELAXED')
    )
    
    if context_result.get('status') == 'NO_RESULTS':
        if debug_mode:
            console.print(f"[yellow]HARDFAIL: {context_result.get('reason')}[/yellow]")
        return {
            "status": "NO_RESULTS",
            "reason": context_result.get('reason'),
            "suggestion": context_result.get('suggestion'),
            "report": "",
            "sources": [],
            "gaps": ["Inga dokument hittades"]
        }
    
    if debug_mode:
        debug_content = f"[bold]Lake:[/bold] {stats.get('lake_hits', 0)} träffar\n"
        debug_content += f"[bold]Vektor:[/bold] {stats.get('vector_hits', 0)} träffar\n"
        debug_content += f"[bold]Efter dedup:[/bold] {stats.get('after_dedup', 0)} kandidater"
        debug_panel("2. CONTEXT", debug_content, style="green", debug_mode=debug_mode)
    
    # Fas 3: Planner (AI)
    if debug_mode:
        console.print("[dim]→ Planner...[/dim]")
    
    report_result = create_report(context_result, intent_data, debug_trace=debug_trace)
    
    if report_result.get('status') in ['ERROR', 'NO_CANDIDATES']:
        if debug_mode:
            console.print(f"[yellow]Planner-fel: {report_result.get('reason')}[/yellow]")
        return {
            "status": "ERROR",
            "reason": report_result.get('reason'),
            "report": "",
            "sources": [],
            "gaps": report_result.get('gaps', [])
        }
    
    if debug_mode:
        debug_content = f"[bold]Använda källor:[/bold] {len(report_result.get('sources_used', []))}\n"
        debug_content += f"[bold]Luckor:[/bold] {report_result.get('gaps', [])}\n"
        debug_content += f"[bold]Confidence:[/bold] {report_result.get('confidence', 0)}"
        debug_panel("3. PLANNER", debug_content, style="blue", debug_mode=debug_mode)
    
    # Spara timing
    if debug_trace is not None:
        debug_trace['pipeline_duration'] = round(time.time() - start_time, 2)
    
    return {
        "status": "OK",
        "report": report_result.get('report', ''),
        "sources": report_result.get('sources_used', []),
        "gaps": report_result.get('gaps', []),
        "confidence": report_result.get('confidence', 0)
    }


# --- PROCESS QUERY (API) ---
def process_query(query: str, chat_history: list = None, collect_debug: bool = False, use_v6: bool = True) -> dict:
    """
    Bearbetar en fråga och returnerar ett strukturerat svar.
    
    Denna funktion är avsedd för programmatisk användning (API).
    Den kör hela pipelinen och returnerar svaret som en dictionary.
    
    Args:
        query: Användarens fråga
        chat_history: Lista med tidigare meddelanden [{"role": "user/assistant", "content": "..."}]
        collect_debug: Om True, samla debug-information i svaret
        use_v6: Om True, använd Pipeline v6.0 (default). Om False, använd legacy v5.2.
    
    Returns:
        dict: {
            "answer": "Svaret från Gemini...",
            "sources": ["fil1.md", "fil2.md"],
            "debug_trace": {...}
        }
    """
    if chat_history is None:
        chat_history = []
    
    debug_trace = {} if collect_debug else None
    start_time = time.time()
    
    if use_v6:
        # === PIPELINE v6.0 ===
        pipeline_result = execute_pipeline_v6(query, chat_history, debug_mode=False, debug_trace=debug_trace)
        
        if pipeline_result.get('status') == 'NO_RESULTS':
            return {
                "answer": f"Hittade ingen relevant information. {pipeline_result.get('suggestion', '')}",
                "sources": [],
                "debug_trace": debug_trace if collect_debug else {}
            }
        
        if pipeline_result.get('status') == 'ERROR':
            return {
                "answer": f"Ett fel uppstod: {pipeline_result.get('reason', 'Okänt fel')}",
                "sources": [],
                "debug_trace": debug_trace if collect_debug else {}
            }
        
        # Syntes med rapport istället för råa dokument
        report = pipeline_result.get('report', '')
        sources = pipeline_result.get('sources', [])
        gaps = pipeline_result.get('gaps', [])
        
        # Syntes med prompt från config
        synth_template = PROMPTS.get('synthesizer_v6', {}).get('instruction', '')
        if not synth_template:
            LOGGER.error("HARDFAIL: synthesizer_v6 prompt saknas i chat_prompts.yaml")
            return {
                "answer": "Konfigurationsfel: synthesizer_v6 prompt saknas",
                "sources": sources,
                "debug_trace": debug_trace if collect_debug else {}
            }
        
        synth_prompt = synth_template.format(
            report=report,
            gaps=gaps if gaps else "Inga kända luckor",
            query=query
        )
        
        contents = []
        for msg in chat_history:
            role = "model" if msg['role'] == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg['content'])]))
        
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=synth_prompt)]))
        
    else:
        # === LEGACY PIPELINE v5.2 ===
        context_docs = execute_pipeline(query, chat_history, debug_mode=False, debug_trace=debug_trace)
        
        if not context_docs:
            return {
                "answer": "Hittade ingen relevant information för att besvara frågan.",
                "sources": [],
                "debug_trace": debug_trace if collect_debug else {}
            }
        
        synthesizer_template = PROMPTS['synthesizer']['instruction']
        system_prompt = synthesizer_template.format(context_docs="\n".join(context_docs))
        
        contents = []
        for msg in chat_history:
            role = "model" if msg['role'] == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg['content'])]))
            
        full_message = f"{system_prompt}\n\nFRÅGA: {query}"
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=full_message)]))
        
        sources = debug_trace.get("sources", []) if debug_trace else []

    # Syntes (gemensam för båda pipelines)
    try:
        ai_client = get_ai_client()
        # Synthesizer använder LITE - rapporten är redan kurerad av Planner (PRO)
        response = ai_client.models.generate_content(model=MODEL_LITE, contents=contents)
        answer = response.text
    except Exception as e:
        LOGGER.error(f"Synthesis error: {e}")
        answer = f"Fel vid generering av svar: {e}"
    
    # Spara timing
    if debug_trace is not None:
        debug_trace['total_duration'] = round(time.time() - start_time, 2)
        debug_trace['pipeline_version'] = 'v6.0' if use_v6 else 'v5.2'
    
    return {
        "answer": answer,
        "sources": sources if not use_v6 else pipeline_result.get('sources', []),
        "debug_trace": debug_trace if collect_debug else {}
    }


# --- SESSION TILL ASSETS (Fas 1: DocConverter hanterar Lake) ---
def _save_session_to_assets(chat_history: list, reason: str = "normal") -> str:
    """
    Spara session som Markdown i Assets för DocConverter att processa.
    
    Args:
        chat_history: Lista med meddelanden
        reason: "normal", "interrupted", etc.
    
    Returns:
        Sökväg till sparad fil
    """
    if not chat_history:
        return None
    
    # Skapa sessions-mapp i Assets (läs från config)
    assets_base = CONFIG['paths'].get('asset_store', os.path.expanduser("~/MyMemory/Assets"))
    assets_path = os.path.join(assets_base, "sessions")
    os.makedirs(assets_path, exist_ok=True)
    
    # Filnamn med timestamp
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"session_{timestamp}.md"
    filepath = os.path.join(assets_path, filename)
    
    # Bygg meddelanden
    messages_text = ""
    for msg in chat_history:
        role = "**User:**" if msg['role'] == 'user' else "**MyMem:**"
        messages_text += f"{role} {msg['content']}\n\n"
    
    # Hämta mall från config
    template = PROMPTS.get('session_export', {}).get('markdown_template', '')
    if not template:
        raise ValueError("HARDFAIL: session_export.markdown_template saknas i chat_prompts.yaml")
    
    date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    content = template.format(date=date_str, reason=reason, messages=messages_text)
    
    # Spara filen
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    
    return filepath


# --- CHAT LOOP (CLI) ---
def chat_loop(debug_mode=False, use_v6=True):
    """
    Interaktiv chattloop för CLI-användning.
    
    Args:
        debug_mode: Om True, visa debug-paneler under körning
        use_v6: Om True, använd Pipeline v6.0 (default)
    """
    console.clear()
    version = "v6.0" if use_v6 else "v5.2"
    product_name = CONFIG.get('system', {}).get('product_name', 'MyMemory')
    console.print(Panel(f"[bold white]{product_name} {version}[/bold white]", style="on blue", box=box.DOUBLE, expand=False))
    if debug_mode:
        console.print("[dim]Debug Mode: ON[/dim]")
        
    chat_history = [] 

    try:
        while True:
            query = Prompt.ask("\n[bold green]Du[/bold green]")
            if query.lower() in ['exit', 'quit', 'sluta']:
                # Spara session till Assets (DocConverter hanterar Lake)
                filepath = _save_session_to_assets(chat_history, "normal")
                if filepath:
                    console.print(f"[dim]Session sparad: {os.path.basename(filepath)}[/dim]")
                break
            if not query.strip(): continue

            # === HANTERA /learn KOMMANDO ===
            if query.startswith('/learn'):
                canonical, alias, entity_type = _parse_learn_command(query)
                if canonical and alias:
                    result = _handle_feedback(canonical, alias, entity_type)
                    console.print(f"[bold purple]MyMem:[/bold purple] {result}")
                    continue
                else:
                    console.print("[yellow]Format: /learn NAMN = ALIAS (Person|Aktör|Projekt)[/yellow]")
                    console.print("[dim]Exempel: /learn Cenk Bisgen = Sänk[/dim]")
                    continue
            
            # === KOLLA OM DET ÄR NATURLIGT SPRÅK FEEDBACK ===
            # (Endast om det börjar med typiska feedback-fraser)
            feedback_phrases = ["är samma", "heter egentligen", "kallas också", "är alias för"]
            if any(phrase in query.lower() for phrase in feedback_phrases):
                feedback = _interpret_feedback(query)
                if feedback.get('is_feedback') and feedback.get('confidence', 0) > 0.7:
                    canonical = feedback.get('canonical')
                    alias = feedback.get('alias')
                    entity_type = feedback.get('entity_type', 'Person')
                    if canonical and alias:
                        result = _handle_feedback(canonical, alias, entity_type)
                        console.print(f"[bold purple]MyMem:[/bold purple] {result}")
                        continue

            # Debug trace för CLI
            debug_trace = {} if debug_mode else None
            
            if use_v6:
                # === PIPELINE v6.0 ===
                pipeline_result = execute_pipeline_v6(query, chat_history, debug_mode=debug_mode, debug_trace=debug_trace)
                
                if pipeline_result.get('status') in ['NO_RESULTS', 'ERROR']:
                    console.print(f"[red]{pipeline_result.get('reason', 'Ingen information hittades.')}[/red]")
                    continue
                
                report = pipeline_result.get('report', '')
                sources = pipeline_result.get('sources', [])
                gaps = pipeline_result.get('gaps', [])
                
                # Syntes med prompt från config
                synth_template = PROMPTS.get('synthesizer_v6', {}).get('instruction', '')
                if not synth_template:
                    console.print("[red]HARDFAIL: synthesizer_v6 prompt saknas i config[/red]")
                    continue
                
                synth_prompt = synth_template.format(
                    report=report,
                    gaps=gaps if gaps else "Inga kända luckor",
                    query=query
                )
                
                contents = []
                for msg in chat_history:
                    role = "model" if msg['role'] == "assistant" else "user"
                    contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg['content'])]))
                
                contents.append(types.Content(role="user", parts=[types.Part.from_text(text=synth_prompt)]))
                
            else:
                # === LEGACY PIPELINE v5.2 ===
                context_docs = execute_pipeline(query, chat_history, debug_mode=debug_mode, debug_trace=debug_trace)
                
                if not context_docs:
                    console.print("[red]Hittade ingen information.[/red]")
                    continue
                
                synthesizer_template = PROMPTS['synthesizer']['instruction']
                system_prompt = synthesizer_template.format(context_docs="\n".join(context_docs))
                
                contents = []
                for msg in chat_history:
                    role = "model" if msg['role'] == "assistant" else "user"
                    contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg['content'])]))
                    
                full_message = f"{system_prompt}\n\nFRÅGA: {query}"
                contents.append(types.Content(role="user", parts=[types.Part.from_text(text=full_message)]))

            console.print("\n[bold purple]MyMem:[/bold purple]")
            
            collected_text = ""
            try:
                ai_client = get_ai_client()
                # Synthesizer använder LITE - rapporten är redan kurerad av Planner (PRO)
                response_stream = ai_client.models.generate_content_stream(model=MODEL_LITE, contents=contents)
                with Live(Markdown(collected_text), console=console, refresh_per_second=10) as live:
                    for chunk in response_stream:
                        if chunk.text:
                            collected_text += chunk.text
                            live.update(Markdown(collected_text))
                chat_history.append({"role": "user", "content": query})
                chat_history.append({"role": "assistant", "content": collected_text})
            except Exception as e:
                LOGGER.error(f"HARDFAIL: AI-syntes misslyckades: {e}")
                console.print(f"[red]AI-fel: {e}[/red]")
                raise RuntimeError(f"HARDFAIL: AI-syntes misslyckades: {e}") from e
    
    except KeyboardInterrupt:
        # Spara session till Assets (DocConverter hanterar Lake)
        filepath = _save_session_to_assets(chat_history, "interrupted")
        if filepath:
            console.print(f"\n[dim]Session sparad: {os.path.basename(filepath)}[/dim]")
        console.print("Hejdå!")


if __name__ == "__main__":
    args = parse_args()
    DEBUG_MODE = args.debug or CONFIG.get('debug', False)
    USE_V6 = CONFIG.get('pipeline_version', 'v6') == 'v6'  # Default till v6.0
    
    chat_loop(debug_mode=DEBUG_MODE, use_v6=USE_V6)
