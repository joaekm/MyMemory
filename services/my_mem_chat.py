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
    user_path = os.path.expanduser("/Users/jekman/Projects/MyMemory/config/chat_prompts.yaml")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        user_path,
        os.path.join(script_dir, 'config', 'chat_prompts.yaml'),
        os.path.join(script_dir, '..', 'config', 'chat_prompts.yaml')
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f)
            except Exception as e:
                print(f"[Chat] ERROR loading prompts from {p}: {e}")
                break
    print("[Chat] CRITICAL: Could not load chat_prompts.yaml")
    exit(1)

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

# --- STEP 2a: THE HUNTER ---
def search_lake(keywords):
    if not keywords: return {}
    hits = {}
    if not os.path.exists(LAKE_PATH): return hits
    
    try:
        files = [f for f in os.listdir(LAKE_PATH) if f.endswith('.md')]
    except: return {}
    
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
                    except: pass

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
        except: continue
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
             except: pass
        
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


# --- PROCESS QUERY (API) ---
def process_query(query: str, chat_history: list = None, collect_debug: bool = False) -> dict:
    """
    Bearbetar en fråga och returnerar ett strukturerat svar.
    
    Denna funktion är avsedd för programmatisk användning (API).
    Den kör hela pipelinen och returnerar svaret som en dictionary.
    
    Args:
        query: Användarens fråga
        chat_history: Lista med tidigare meddelanden [{"role": "user/assistant", "content": "..."}]
        collect_debug: Om True, samla debug-information i svaret
    
    Returns:
        dict: {
            "answer": "Svaret från Gemini...",
            "sources": ["fil1.md", "fil2.md"],
            "debug_trace": {
                "plan_data": {...},
                "hits_hunter": 2,
                "hits_vector": 5,
                "judge_data": {...},
                ...
            }
        }
    """
    if chat_history is None:
        chat_history = []
    
    debug_trace = {} if collect_debug else None
    
    # Kör sökpipelinen
    context_docs = execute_pipeline(query, chat_history, debug_mode=False, debug_trace=debug_trace)
    
    if not context_docs:
        return {
            "answer": "Hittade ingen relevant information för att besvara frågan.",
            "sources": [],
            "debug_trace": debug_trace if collect_debug else {}
        }
    
    # Syntes
    synthesizer_template = PROMPTS['synthesizer']['instruction']
    system_prompt = synthesizer_template.format(context_docs="\n".join(context_docs))
    
    contents = []
    for msg in chat_history:
        role = "model" if msg['role'] == "assistant" else "user"
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg['content'])]))
        
    full_message = f"{system_prompt}\n\nFRÅGA: {query}"
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=full_message)]))

    try:
        ai_client = get_ai_client()
        response = ai_client.models.generate_content(model=MODEL_PRO, contents=contents)
        answer = response.text
    except Exception as e:
        LOGGER.error(f"Synthesis error: {e}")
        answer = f"Fel vid generering av svar: {e}"
    
    # Extrahera källor från debug_trace eller kontext
    sources = debug_trace.get("sources", []) if debug_trace else []
    
    return {
        "answer": answer,
        "sources": sources,
        "debug_trace": debug_trace if collect_debug else {}
    }


# --- CHAT LOOP (CLI) ---
def chat_loop(debug_mode=False):
    """
    Interaktiv chattloop för CLI-användning.
    
    Args:
        debug_mode: Om True, visa debug-paneler under körning
    """
    console.clear()
    console.print(Panel("[bold white]Digitalist Företagsminne v5.2[/bold white]", style="on blue", box=box.DOUBLE, expand=False))
    if debug_mode:
        console.print("[dim]Debug Mode: ON[/dim]")
        
    chat_history = [] 

    while True:
        query = Prompt.ask("\n[bold green]Du[/bold green]")
        if query.lower() in ['exit', 'quit', 'sluta']: break
        if not query.strip(): continue

        # Debug trace för CLI
        debug_trace = {} if debug_mode else None
        
        # Kör pipelinen (med debug-output om aktiverat)
        context_docs = execute_pipeline(query, chat_history, debug_mode=debug_mode, debug_trace=debug_trace)
        
        if not context_docs:
            console.print("[red]Hittade ingen information.[/red]")
            continue

        console.print("\n[bold purple]MyMem:[/bold purple]")
        
        synthesizer_template = PROMPTS['synthesizer']['instruction']
        system_prompt = synthesizer_template.format(context_docs="\n".join(context_docs))
        
        contents = []
        for msg in chat_history:
            role = "model" if msg['role'] == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg['content'])]))
            
        full_message = f"{system_prompt}\n\nFRÅGA: {query}"
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=full_message)]))

        collected_text = ""
        try:
            ai_client = get_ai_client()
            response_stream = ai_client.models.generate_content_stream(model=MODEL_PRO, contents=contents)
            with Live(Markdown(collected_text), console=console, refresh_per_second=10) as live:
                for chunk in response_stream:
                    if chunk.text:
                        collected_text += chunk.text
                        live.update(Markdown(collected_text))
            chat_history.append({"role": "user", "content": query})
            chat_history.append({"role": "assistant", "content": collected_text})
        except Exception as e:
            console.print(f"[red]AI-fel: {e}[/red]")


if __name__ == "__main__":
    args = parse_args()
    DEBUG_MODE = args.debug or CONFIG.get('debug', False)
    
    try:
        chat_loop(debug_mode=DEBUG_MODE)
    except KeyboardInterrupt:
        print("\nHejdå!")
