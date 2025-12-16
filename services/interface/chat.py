import os
import sys

# Lägg till projektroten i sys.path för att hitta services-paketet
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Tysta tqdm/SentenceTransformer progress bars
os.environ["TOKENIZERS_PARALLELISM"] = "false"

# Patcha tqdm att vara tyst (innan SentenceTransformer importeras)
import tqdm
import tqdm.auto
_orig_tqdm_init = tqdm.tqdm.__init__
def _silent_tqdm_init(self, *args, **kwargs):
    kwargs['disable'] = True
    return _orig_tqdm_init(self, *args, **kwargs)
tqdm.tqdm.__init__ = _silent_tqdm_init
tqdm.auto.tqdm.__init__ = _silent_tqdm_init
import time
import yaml
import json
import logging
import datetime
import argparse
from google import genai
from google.genai import types
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.table import Table
from rich import box
from rich.live import Live

# Pipeline v8.2 imports - SessionEngine
try:
    from services.engine.session_engine import SessionEngine, get_engine, reset_engine
except ImportError as _import_err:
    try:
        from session_engine import SessionEngine, get_engine, reset_engine
    except ImportError as e:
        raise ImportError(f"HARDFAIL: Kan inte importera session_engine: {e}") from e

# Entity Register (OBJEKT-44) - Använder graph_builder direkt
try:
    from services.indexers.graph_builder import (
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
    from services.engine.session_logger import (
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
        os.path.join(script_dir, '..', '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, 'config', 'my_mem_config.yaml'),
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
        os.path.join(script_dir, '..', '..', 'config', 'chat_prompts.yaml'),
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



# --- PROCESS QUERY (API) ---
def process_query(query: str, chat_history: list = None, collect_debug: bool = False) -> dict:
    """
    Bearbetar en fråga och returnerar ett strukturerat svar.
    
    Pipeline v8.2: SessionEngine hanterar orchestration.
    OBS: Denna funktion skapar en ny engine per anrop (stateless).
    För stateful API, använd SessionEngine direkt.
    
    Args:
        query: Användarens fråga
        chat_history: Lista med tidigare meddelanden (ignoreras - engine hanterar)
        collect_debug: Om True, samla debug-information i svaret
    
    Returns:
        dict: {
            "answer": "Svaret...",
            "sources": ["fil1.md", "fil2.md"],
            "debug_trace": {...}
        }
    """
    # Skapa en stateless engine för detta anrop
    engine = SessionEngine()
    
    debug_trace = {} if collect_debug else None
    
    try:
        result = engine.run_query(query, debug_mode=False, debug_trace=debug_trace)
        
        return {
            "answer": result.get('answer', ''),
            "sources": result.get('sources', []),
            "debug_trace": result.get('debug_trace', {}) if collect_debug else {}
        }
    except Exception as e:
        LOGGER.error(f"process_query error: {e}")
        return {
            "answer": f"Ett fel uppstod: {e}",
            "sources": [],
            "debug_trace": debug_trace if collect_debug else {}
        }


# --- CHAT LOOP (CLI) ---
def chat_loop(debug_mode=False):
    """
    Interaktiv chattloop för CLI-användning.
    
    Pipeline v8.2: SessionEngine hanterar all orchestration.
    Pivot or Persevere: Tornet bevaras mellan turer.
    
    Args:
        debug_mode: Om True, visa debug-paneler under körning
    """
    console.clear()
    product_name = CONFIG.get('system', {}).get('product_name', 'MyMemory')
    console.print(Panel(f"[bold white]{product_name} v8.2[/bold white]", style="on blue", box=box.DOUBLE, expand=False))
    if debug_mode:
        console.print("[dim]Debug Mode: ON (Pivot or Persevere)[/dim]")
    
    # v8.2: SessionEngine hanterar allt
    engine = get_engine()
    last_query = ""  # Spara senaste frågan för /context

    try:
        while True:
            query = Prompt.ask("\n[bold green]Du[/bold green]")
            if query.lower() in ['exit', 'quit', 'sluta']:
                reset_engine()
                console.print("[dim]Session avslutad.[/dim]")
                break
            if not query.strip(): continue

            # === /show KOMMANDO - Visa senaste sökresultat ===
            if query.strip() == '/show':
                candidates = engine.get_last_candidates()
                if not candidates:
                    console.print("[yellow]Ingen tidigare sökning att visa.[/yellow]")
                    continue
                console.print(f"\n[bold cyan]Filer ({len(candidates)} st):[/bold cyan]")
                for c in candidates:
                    # Ta bort UUID från filnamn för läsbarhet
                    fname = c.get('filename', c.get('id', 'Okänd'))
                    clean_name = re.sub(r'_[a-f0-9-]{36}\.md$', '.md', fname)
                    console.print(f"  • {clean_name}")
                continue
            
            # === /export KOMMANDO - Exportera till hotfiles ===
            if query.strip() == '/export':
                candidates = engine.get_last_candidates()
                if not candidates:
                    console.print("[yellow]Ingen tidigare sökning att exportera.[/yellow]")
                    continue
                
                from services.utils.export_search import export_candidates
                hot_folder = CONFIG['paths'].get('hot_folder', '~/Downloads/MyMem Hotfiles')
                lake_path = CONFIG['paths'].get('lake_store', '~/MyMemory/Lake')
                
                result = export_candidates(candidates, hot_folder, lake_path)
                
                if result['status'] == 'OK':
                    if result.get('total', 0) > result['count']:
                        console.print(f"[green]✓ Exporterade top {result['count']} av {result['total']} filer till {result['folder']}[/green]")
                    else:
                        console.print(f"[green]✓ Exporterade {result['count']} filer till {result['folder']}[/green]")
                else:
                    console.print("[yellow]Inga filer att exportera.[/yellow]")
                continue
            
            # === /context KOMMANDO - Exportera K som markdown ===
            if query.strip() == '/context':
                synthesis = engine.get_synthesis()
                facts = engine.get_facts()
                candidates = engine.get_last_candidates()
                
                if not synthesis and not facts and not candidates:
                    console.print("[yellow]Ingen kontext att exportera. Kör en sökning först.[/yellow]")
                    continue
                
                from services.utils.export_context import export_context
                import subprocess
                hot_folder = CONFIG['paths'].get('hot_folder', '~/Downloads/MyMem Hotfiles')
                
                filepath = export_context(last_query, synthesis, facts, candidates, hot_folder)
                console.print(f"[green]✓ Exporterade kontext till {filepath}[/green]")
                subprocess.run(['open', '-R', filepath])
                continue
            
            # === KOLLA OM DET ÄR NATURLIGT SPRÅK FEEDBACK ===
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

            # === PIPELINE v8.2 via SessionEngine ===
            debug_trace = {} if debug_mode else None
            
            # Live callback för Tornbygget
            def print_iteration_live(data):
                gain = data.get('context_gain', 0)
                status = data.get('status', '?')
                patience = data.get('patience', 0)
                preview = data.get('synthesis_preview', '')[:120]
                
                # Färgkoda gain
                if gain >= 0.5:
                    gain_color = "green"
                elif gain >= 0.1:
                    gain_color = "yellow"
                else:
                    gain_color = "red"
                
                console.print(f"[dim]─── Iteration {data['iteration']} ───[/dim]")
                
                # v8.4: Thinking Out Loud - visa interface_reasoning (ENDAST UX, inte logik)
                if data.get('interface_reasoning'):
                    console.print(f"  [dim italic]💭 {data['interface_reasoning'][:200]}...[/dim italic]")
                
                console.print(f"  [{gain_color}]Gain: {gain:.2f}[/{gain_color}] | Status: {status} | Patience: {patience}")
                console.print(f"  [italic]Torn: \"{preview}...\"[/italic]")
                
                # Visa vilka agenter som skickades ut
                if data.get('agents_dispatched'):
                    agents = ", ".join(data['agents_dispatched'])
                    console.print(f"  [magenta]🐿️ Skickar ut: {agents}[/magenta]")
                
                if data.get('next_search'):
                    console.print(f"  [cyan]→ Söker: '{data['next_search']}'[/cyan]")
            
            # Live callback för Librarian Loop - "Thinking Out Loud"
            def print_scan_live(data):
                console.print(f"\n[bold cyan]FOKUS:[/bold cyan] \"{data['current_query']}\"")
                console.print(f"[dim]Scannar {data['scanned']} dok -> Behåller {data['kept']}, Kastar {data['discarded']}[/dim]")
                if data.get('kept_titles'):
                    titles = ', '.join(data['kept_titles'][:3])
                    console.print(f"[green]Läser:[/green] {titles}")
            
            if debug_mode:
                console.print("\n[bold cyan]🏗️ TORNBYGGET (live)[/bold cyan]")
            
            # Spara query för /context export
            last_query = query
            
            try:
                result = engine.run_query(
                    query, 
                    debug_mode=debug_mode, 
                    debug_trace=debug_trace,
                    on_iteration=print_iteration_live if debug_mode else None,
                    on_scan=print_scan_live if debug_mode else None
                )
            except Exception as e:
                LOGGER.error(f"Pipeline error: {e}")
                console.print(f"[red]Fel: {e}[/red]")
                continue
            
            if result.get('status') == 'NO_RESULTS':
                console.print(f"[yellow]{result.get('answer', 'Ingen information hittades.')}[/yellow]")
                continue
            
            # Slutstatus
            if debug_mode:
                synthesis_len = len(engine.get_synthesis())
                facts_count = len(engine.get_facts())
                console.print(f"\n[bold]✓ Klart:[/bold] Torn: {synthesis_len} chars | Facts: {facts_count}")
            
            # Visa svar
            console.print("\n[bold purple]MyMem:[/bold purple]")
            console.print(Markdown(result.get('answer', '')))
    
    except KeyboardInterrupt:
        reset_engine()
        console.print("\n[dim]Session avbruten.[/dim]")
        console.print("Hejdå!")


if __name__ == "__main__":
    args = parse_args()
    DEBUG_MODE = args.debug or CONFIG.get('debug', False)
    
    chat_loop(debug_mode=DEBUG_MODE)
