#!/usr/bin/env python3
"""
debug_report_flow.py - Testar "Jakt -> Leverans" flödet med minne.

Scenario:
1. Användaren frågar om status (Jakt) -> Planner bygger "Tornet".
2. Användaren ber om rapport (Leverans) -> Planner återanvänder Tornet, Synthesizer byter format.
"""

import sys
import os
import time
import logging
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

# Lägg till rot-mappen i sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from services.session_engine import SessionEngine
except ImportError as e:
    print(f"CRITICAL: Kunde inte importera SessionEngine: {e}")
    sys.exit(1)

console = Console()
LOGGER = logging.getLogger('DebugReportFlow')


def print_step_header(step, title):
    console.print(f"\n[bold white on blue] {step} [/bold white on blue] [bold cyan]{title}[/bold cyan]")


def main():
    console.print("[bold]🚀 Startar Session Flow Test: Jakt -> Leverans[/bold]\n")
    
    # Initiera motorn (Simulerar att starta appen)
    engine = SessionEngine()
    
    # --- STEG 1: JAKTEN ---
    query_1 = "Vad är den senaste statusen i Adda-projektet?"
    print_step_header("STEG 1", f"Jakt: '{query_1}'")
    
    start = time.time()
    try:
        result_1 = engine.run_query(query_1, debug_mode=True)
    except Exception as e:
        LOGGER.error(f"Jakt misslyckades: {e}")
        console.print(f"[bold red]FAIL Jakt:[/bold red] {e}")
        return
    duration = time.time() - start
    
    console.print(f"⏱️ Tid: {duration:.2f}s")
    console.print(f"Status: [green]{result_1['status']}[/green]")
    console.print(f"Svar: {result_1['answer'][:100]}...")
    
    # --- MELLANAKT: INSPEKTERA MINNET ---
    print_step_header("PAUS", "Inspekterar Session State (Minnet)")
    
    synthesis = engine.get_synthesis()
    facts = engine.get_facts()
    
    if synthesis:
        console.print(f"✅ [bold green]Tornet finns![/bold green] ({len(synthesis)} tecken)")
        console.print(Panel(synthesis[:300] + "...", title="Tornet (Preview)"))
    else:
        console.print("❌ [bold red]Tornet är tomt! Något är fel.[/bold red]")
        sys.exit(1)
        
    console.print(f"✅ [bold green]Bevis samlade:[/bold green] {len(facts)} st")

    # --- STEG 2: LEVERANSEN ---
    # Notera: Vi nämner inte "Adda" här, så IntentRouter måste fatta det från historiken!
    query_2 = "Skriv en utförlig veckorapport till Suzanne baserat på detta."
    print_step_header("STEG 2", f"Leverans: '{query_2}'")
    
    start = time.time()
    try:
        # Här skickas samma engine-instans in, så den har historiken och state
        result_2 = engine.run_query(query_2, debug_mode=True)
    except Exception as e:
        LOGGER.error(f"Leverans misslyckades: {e}")
        console.print(f"[bold red]FAIL Leverans:[/bold red] {e}")
        return
    duration = time.time() - start
    
    # --- ANALYS AV RESULTAT 2 ---
    op_mode = result_2.get('op_mode')
    fmt = result_2.get('delivery_format')
    
    console.print(f"⏱️ Tid: {duration:.2f}s")
    console.print(f"Mode: [bold magenta]{op_mode}[/bold magenta] (Ska vara 'deliver')")
    console.print(f"Format: [bold magenta]{fmt}[/bold magenta]")
    
    console.print("\n[bold]GENERERAD RAPPORT:[/bold]")
    console.print(Panel(Markdown(result_2['answer']), title="Veckorapport", border_style="green"))


if __name__ == "__main__":
    main()





