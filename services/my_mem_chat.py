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
    from services.synthesizer import synthesize
except ImportError:
    # Direkt körning - försök importera utan services-prefix
    try:
        from intent_router import route_intent
        from context_builder import build_context
        from planner import create_report
        from synthesizer import synthesize
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
def process_query(query: str, chat_history: list = None, collect_debug: bool = False) -> dict:
    """
    Bearbetar en fråga och returnerar ett strukturerat svar.
    
    Pipeline v6.0: IntentRouter → ContextBuilder → Planner → Synthesizer
    
    Args:
        query: Användarens fråga
        chat_history: Lista med tidigare meddelanden [{"role": "user/assistant", "content": "..."}]
        collect_debug: Om True, samla debug-information i svaret
    
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
    
    # Syntes
    report = pipeline_result.get('report', '')
    sources = pipeline_result.get('sources', [])
    gaps = pipeline_result.get('gaps', [])
    
    synth_result = synthesize(
        query=query,
        report=report,
        gaps=gaps,
        chat_history=chat_history,
        debug_trace=debug_trace
    )
    
    answer = synth_result.get('answer', 'Fel vid syntes')
    
    # Spara timing
    if debug_trace is not None:
        debug_trace['total_duration'] = round(time.time() - start_time, 2)
        debug_trace['pipeline_version'] = 'v6.0'
    
    return {
        "answer": answer,
        "sources": sources,
        "debug_trace": debug_trace if collect_debug else {}
    }


# --- SESSION TILL ASSETS (Fas 1: DocConverter hanterar Lake) ---
def _save_session_to_assets(chat_history: list, learnings: list = None, reason: str = "normal") -> str:
    """
    Spara session som Markdown i Assets för DocConverter att processa.
    
    Struktur:
    - YAML frontmatter (metadata, inkl. summary med mjuka learnings)
    - ## Learnings (YAML-block med hårda learnings)
    - ## Konversation (chatthistorik)
    
    Args:
        chat_history: Lista med meddelanden
        learnings: Lista med extraherade lärdomar [{canonical, alias, type, confidence, evidence}]
        reason: "normal", "interrupted", etc.
    
    Returns:
        Sökväg till sparad fil
    """
    import uuid as uuid_module
    
    if not chat_history:
        return None
    
    # Läs sessions-mapp från config
    sessions_path = os.path.expanduser(CONFIG['paths']['asset_sessions'])
    os.makedirs(sessions_path, exist_ok=True)
    
    # Generera UUID och timestamp
    unit_id = str(uuid_module.uuid4())
    now = datetime.datetime.now()
    timestamp_iso = now.strftime("%Y-%m-%dT%H%M%S")  # Utan kolon för filnamn
    timestamp_full = now.isoformat()
    
    # Filnamn: Session_2025-12-11T153000_UUID.md
    filename = f"Session_{timestamp_iso}_{unit_id}.md"
    filepath = os.path.join(sessions_path, filename)
    
    # Bygg summary (mjuka learnings)
    summary_parts = [f"Chatt-session avslutad ({reason})."]
    if learnings:
        summary_parts.append(f"{len(learnings)} alias-kopplingar identifierade.")
    summary = " ".join(summary_parts)
    
    # Bygg graph_nodes från learnings
    graph_nodes = {"Händelser": 0.9}  # Sessions är alltid Händelser
    
    if learnings:
        # Gruppera entiteter per typ
        persons = {}
        for l in learnings:
            canonical = l.get('canonical')
            entity_type = l.get('type', 'Person')
            if canonical:
                # Lägg till med hög relevans (0.7) - dessa är bekräftade learnings
                if entity_type == 'Person':
                    if 'Person' not in graph_nodes:
                        graph_nodes['Person'] = {}
                    graph_nodes['Person'][canonical] = 0.7
                elif entity_type == 'Aktör':
                    if 'Aktör' not in graph_nodes:
                        graph_nodes['Aktör'] = {}
                    graph_nodes['Aktör'][canonical] = 0.7
                elif entity_type == 'Projekt':
                    if 'Projekt' not in graph_nodes:
                        graph_nodes['Projekt'] = {}
                    graph_nodes['Projekt'][canonical] = 0.7
    
    # Bygg YAML frontmatter
    frontmatter = {
        "unit_id": unit_id,
        "owner_id": CONFIG.get("owner", {}).get("id", "default"),
        "source_type": "Session",
        "timestamp_created": timestamp_full,
        "summary": summary,
        "graph_nodes": graph_nodes
    }
    
    # Bygg ## Learnings sektion (hårda learnings som YAML)
    learnings_section = ""
    if learnings:
        learnings_yaml = yaml.dump({"aliases": learnings}, allow_unicode=True, sort_keys=False)
        learnings_section = f"## Learnings\n\n```yaml\n{learnings_yaml}```\n\n"
    
    # Bygg ## Konversation sektion
    messages_text = ""
    for msg in chat_history:
        role = "**User:**" if msg['role'] == 'user' else "**MyMem:**"
        messages_text += f"{role} {msg['content']}\n\n"
    
    conversation_section = f"## Konversation\n\n{messages_text}"
    
    # Bygg komplett fil
    frontmatter_yaml = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)
    content = f"---\n{frontmatter_yaml}---\n\n{learnings_section}{conversation_section}"
    
    # Spara filen
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    
    LOGGER.info(f"Session sparad: {filename} ({len(learnings or [])} learnings)")
    return filepath


# --- CHAT LOOP (CLI) ---
def chat_loop(debug_mode=False):
    """
    Interaktiv chattloop för CLI-användning.
    
    Pipeline v6.0: IntentRouter → ContextBuilder → Planner → Synthesizer
    
    Args:
        debug_mode: Om True, visa debug-paneler under körning
    """
    console.clear()
    product_name = CONFIG.get('system', {}).get('product_name', 'MyMemory')
    console.print(Panel(f"[bold white]{product_name} v6.0[/bold white]", style="on blue", box=box.DOUBLE, expand=False))
    if debug_mode:
        console.print("[dim]Debug Mode: ON[/dim]")
        
    chat_history = [] 

    try:
        while True:
            query = Prompt.ask("\n[bold green]Du[/bold green]")
            if query.lower() in ['exit', 'quit', 'sluta']:
                # Extrahera learnings och spara session till Assets
                session_result = end_session(chat_history)
                learnings = session_result.get('learnings', [])
                filepath = _save_session_to_assets(chat_history, learnings, "normal")
                if filepath:
                    console.print(f"[dim]Session sparad: {os.path.basename(filepath)}[/dim]")
                    if learnings:
                        console.print(f"[dim]{len(learnings)} lärdomar extraherade[/dim]")
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
            
            # === PIPELINE v6.0 ===
            pipeline_result = execute_pipeline_v6(query, chat_history, debug_mode=debug_mode, debug_trace=debug_trace)
            
            if pipeline_result.get('status') in ['NO_RESULTS', 'ERROR']:
                console.print(f"[red]{pipeline_result.get('reason', 'Ingen information hittades.')}[/red]")
                continue
            
            report = pipeline_result.get('report', '')
            gaps = pipeline_result.get('gaps', [])
            
            console.print("\n[bold purple]MyMem:[/bold purple]")
            
            # Syntes med streaming
            synth_template = PROMPTS.get('synthesizer_v6', {}).get('instruction', '')
            if not synth_template:
                LOGGER.error("HARDFAIL: synthesizer_v6 prompt saknas")
                console.print("[red]Konfigurationsfel: synthesizer_v6 prompt saknas[/red]")
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
            
            collected_text = ""
            try:
                ai_client = get_ai_client()
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
        session_result = end_session(chat_history)
        learnings = session_result.get('learnings', [])
        filepath = _save_session_to_assets(chat_history, learnings, "interrupted")
        if filepath:
            console.print(f"\n[dim]Session sparad: {os.path.basename(filepath)}[/dim]")
            if learnings:
                console.print(f"[dim]{len(learnings)} lärdomar extraherade[/dim]")
        console.print("Hejdå!")


if __name__ == "__main__":
    args = parse_args()
    DEBUG_MODE = args.debug or CONFIG.get('debug', False)
    
    chat_loop(debug_mode=DEBUG_MODE)
