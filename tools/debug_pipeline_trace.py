#!/usr/bin/env python3
"""
debug_pipeline_trace.py - Deterministisk trace av Pipeline v8.2 (FIXED)

Kör hela pipelinen steg för steg med verbose debug output.
Säkerställer att search_fn injiceras korrekt till Planner.
"""
import os
import sys
import json
import logging
from pathlib import Path

# Setup paths - måste göras INNAN imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.panel import Panel
from rich.json import JSON

console = Console()
LOGGER = logging.getLogger('DebugPipelineTrace')

# --- CRITICAL IMPORTS ---
# Alla imports måste lyckas, annars HARDFAIL
try:
    from services.intent_router import route_intent
    console.print("[green]✓ Imported: intent_router.route_intent[/green]")
except ImportError as e:
    console.print(f"[bold red]CRITICAL IMPORT ERROR (intent_router): {e}[/bold red]")
    sys.exit(1)

try:
    from services.context_builder import build_context, search
    console.print("[green]✓ Imported: context_builder.build_context[/green]")
    console.print("[green]✓ Imported: context_builder.search[/green]")
except ImportError as e:
    console.print(f"[bold red]CRITICAL IMPORT ERROR (context_builder): {e}[/bold red]")
    sys.exit(1)

try:
    from services.planner import run_planner_loop
    console.print("[green]✓ Imported: planner.run_planner_loop[/green]")
except ImportError as e:
    console.print(f"[bold red]CRITICAL IMPORT ERROR (planner): {e}[/bold red]")
    sys.exit(1)

try:
    from services.synthesizer import synthesize
    console.print("[green]✓ Imported: synthesizer.synthesize[/green]")
except ImportError as e:
    console.print(f"[bold red]CRITICAL IMPORT ERROR (synthesizer): {e}[/bold red]")
    sys.exit(1)


def print_step(title: str, data: dict):
    """Skriv ut ett steg som JSON-panel."""
    console.print(Panel(
        JSON(json.dumps(data, default=str, ensure_ascii=False)), 
        title=f"[bold green]{title}[/bold green]", 
        expand=False
    ))


def run_trace(query: str):
    """Kör hela pipelinen med verbose debug output."""
    console.print(f"\n[bold yellow]🔍 TRACING QUERY: '{query}'[/bold yellow]\n")
    debug_trace = {}

    # ============================================================
    # STEG 1: INTENT ROUTER
    # ============================================================
    try:
        console.print("[dim]1. Running IntentRouter...[/dim]")
        intent = route_intent(query, chat_history=[], debug_trace=debug_trace)
        print_step("STEP 1 OUTPUT: IntentRouter", intent)
    except Exception as e:
        LOGGER.error(f"IntentRouter failed: {e}")
        console.print(f"[bold red]FAIL IntentRouter:[/bold red] {e}")
        return

    # ============================================================
    # STEG 2: CONTEXT BUILDER
    # ============================================================
    try:
        console.print("[dim]2. Running ContextBuilder...[/dim]")
        context = build_context(
            keywords=intent.get('keywords', []),
            entities=intent.get('entities', []),
            time_filter=intent.get('time_filter'),
            debug_trace=debug_trace
        )
        
        # Visa bara statistik och första kandidaten för att inte dränka loggen
        stats = context.get('stats', {})
        first_candidate = context.get('candidates', [])[0] if context.get('candidates') else "Inga kandidater"
        
        print_step("STEP 2 OUTPUT: ContextBuilder (Stats)", stats)
        print_step("STEP 2 SAMPLE: Första kandidaten", first_candidate)
        
        if context['status'] == 'NO_RESULTS':
            console.print("[bold red]STOPP: Inga dokument hittades.[/bold red]")
            return

    except Exception as e:
        LOGGER.error(f"ContextBuilder failed: {e}")
        console.print(f"[bold red]FAIL ContextBuilder:[/bold red] {e}")
        return

    # ============================================================
    # STEG 3: PLANNER LOOP (CRITICAL: search_fn VERIFICATION)
    # ============================================================
    console.print("[dim]3. Running Planner Loop...[/dim]")
    
    # --- DEBUG: VERIFIERA SEARCH_FN ---
    console.print("\n[bold cyan]DEBUG: Verifierar search_fn innan Planner...[/bold cyan]")
    console.print(f"  search is None: {search is None}")
    console.print(f"  search type: {type(search)}")
    console.print(f"  search callable: {callable(search) if search is not None else 'N/A'}")
    
    if search is None:
        console.print("[bold red]CRITICAL ERROR: 'search' är None![/bold red]")
        console.print("[bold red]Import från context_builder misslyckades tyst.[/bold red]")
        sys.exit(1)
    
    if not callable(search):
        console.print(f"[bold red]CRITICAL ERROR: 'search' är inte callable![/bold red]")
        console.print(f"[bold red]search = {search}[/bold red]")
        sys.exit(1)
    
    console.print("[green]✓ search_fn verifierad: är callable[/green]")
    console.print(f"[green]✓ search_fn = {search.__module__}.{search.__name__}[/green]")
    
    try:
        session_id = "debug_trace_session"
        
        # Callback för att visa varje iteration
        def on_iteration(data: dict):
            gain = data.get('context_gain', 0)
            status = data.get('status', 'unknown')
            next_search = data.get('next_search', '')
            console.print(
                f"  [dim]Iter {data.get('iteration', '?')}:[/dim] "
                f"Gain={gain} | Status={status} | Search='{next_search}'"
            )
        
        # Callback för Librarian Loop - "Thinking Out Loud"
        def on_scan(data: dict):
            console.print(f"\n[bold cyan]FOKUS:[/bold cyan] \"{data['current_query']}\"")
            console.print(f"[dim]Scannade {data['scanned']} kandidater -> Behåller {data['kept']}, Kastar {data['discarded']}[/dim]")
            if data.get('kept_titles'):
                titles = ', '.join(data['kept_titles'][:3])
                console.print(f"[green]Deep Reading:[/green] {titles}")
        
        console.print("\n[bold]Startar Planner Loop med search_fn...[/bold]")
        
        planner_result = run_planner_loop(
            mission_goal=intent.get('mission_goal'),
            query=query,
            initial_candidates=context.get('candidates_full', []),
            candidates_formatted=context.get('candidates_formatted', ''),
            session_id=session_id,
            search_fn=search,  # <-- CRITICAL: Skicka search-funktionen
            on_iteration=on_iteration,
            on_scan=on_scan,  # <-- Librarian Loop reasoning
            debug_trace=debug_trace
        )
        
        print_step("STEP 3 OUTPUT: Planner Report", {
            "status": planner_result.get('status'),
            "report_length": len(planner_result.get('report', '')),
            "report_preview": planner_result.get('report', '')[:500] + "...",
            "gaps": planner_result.get('gaps')
        })

    except Exception as e:
        LOGGER.error(f"Planner failed: {e}", exc_info=True)
        console.print(f"[bold red]FAIL Planner:[/bold red] {e}")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
        return

    # ============================================================
    # STEG 4: SYNTHESIZER
    # ============================================================
    try:
        console.print("[dim]4. Running Synthesizer...[/dim]")
        synth_result = synthesize(
            query=query,
            report=planner_result.get('report', ''),
            gaps=planner_result.get('gaps', []),
            status=planner_result.get('status'),
            chat_history=[],
            debug_trace=debug_trace,
            debug_mode=True  # Visa raw LLM-svar
        )
        
        print_step("STEP 4 OUTPUT: Final Answer", synth_result)

    except Exception as e:
        LOGGER.error(f"Synthesizer failed: {e}")
        console.print(f"[bold red]FAIL Synthesizer:[/bold red] {e}")
        return
    
    # ============================================================
    # SUMMARY
    # ============================================================
    console.print("\n[bold green]✓ Pipeline trace completed successfully[/bold green]")


if __name__ == "__main__":
    # Standardfråga för testning
    test_query = "Vad sa vi om Adda-projektet på mötet igår?"
    if len(sys.argv) > 1:
        test_query = sys.argv[1]
    
    console.print("[bold]=" * 60 + "[/bold]")
    console.print("[bold]DEBUG PIPELINE TRACE v8.2[/bold]")
    console.print("[bold]=" * 60 + "[/bold]")
        
    run_trace(test_query)
