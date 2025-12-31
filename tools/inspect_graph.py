#!/usr/bin/env python3
"""
test_full_flow.py - End-to-End test av MyMemory v8.3 (REAL DREAMER)

Detta skript kör det faktiska systemflödet:
1. DocConverter (Multipass -> Skapar Evidence i DB)
2. Dreamer (Consolidate -> Läser Evidence -> Uppdaterar Graf & Taxonomi)
3. Vector Indexer (Indexerar den uppdaterade filen)
4. Rapport

Ingen simulering. Ingen manuell handpåläggning.
"""

import os
import sys
import yaml
import logging
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Setup paths
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# Imports
from services.processors.doc_converter import processa_dokument, UUID_SUFFIX_PATTERN
from services.processors.dreamer import consolidate
from services.indexers.vector_indexer import indexera_vektor
from services.utils.graph_service import GraphStore

console = Console()
# Sätt logging till INFO för att se Dreamers output, men filtrera bort brus
logging.basicConfig(level=logging.INFO, format='%(message)s')
logging.getLogger('googleapiclient').setLevel(logging.WARNING)

# --- CONFIG ---
def load_config():
    with open(os.path.join(project_root, 'config', 'my_mem_config.yaml'), 'r') as f:
        config = yaml.safe_load(f)
    for k, v in config['paths'].items():
        config['paths'][k] = os.path.expanduser(v)
    return config

CONFIG = load_config()
LAKE_STORE = CONFIG['paths']['lake_store']
GRAPH_PATH = CONFIG['paths']['graph_db']

def get_evidence_count(unit_id):
    """Kolla hur många bevis som skapades (innan de tas bort av Dreamer)."""
    graph = GraphStore(GRAPH_PATH, read_only=True)
    try:
        count = graph.conn.execute(
            "SELECT count(*) FROM evidence WHERE source_file = ?", [unit_id]
        ).fetchone()[0]
        return count
    except Exception:
        return 0
    finally:
        graph.close()

def main(filepath):
    console.clear()
    console.print(Panel.fit(f"[bold blue]MyMem Full Flow (Real AI)[/bold blue]\nFil: {os.path.basename(filepath)}"))

    # 0. SETUP
    filename = os.path.basename(filepath)
    match = UUID_SUFFIX_PATTERN.search(filename)
    if not match:
        console.print("[bold red]❌ Filen saknar giltigt UUID-suffix![/bold red]")
        return
    unit_id = match.group(1)
    
    lake_file = os.path.join(LAKE_STORE, f"{os.path.splitext(filename)[0]}.md")
    
    # Rensa gammalt (Lake) för rent test
    if os.path.exists(lake_file):
        os.remove(lake_file)
        console.print("[dim]🗑️  Rensade gammal Lake-fil[/dim]")

    # 1. DOC CONVERTER (MULTIPASS)
    console.print("\n[bold yellow]1. DocConverter (Genererar Evidence)...[/bold yellow]")
    try:
        processa_dokument(filepath, filename)
    except Exception as e:
        console.print(f"[bold red]DocConverter kraschade: {e}[/bold red]")
        return

    if not os.path.exists(lake_file):
        console.print("[bold red]❌ Ingen Lake-fil skapades![/bold red]")
        return
    
    ev_count_pre = get_evidence_count(unit_id)
    console.print(f"[green]✅ DocConverter klar. Skapade {ev_count_pre} bevis i grafen.[/green]")

    # 2. DREAMER (CONSOLIDATION)
    console.print("\n[bold yellow]2. Dreamer (Konsoliderar Sanning)...[/bold yellow]")
    console.print("[dim]Detta läser bevisen, kör LLM-bedömning och uppdaterar grafen...[/dim]")
    
    try:
        dream_stats = consolidate()
    except Exception as e:
        console.print(f"[bold red]Dreamer kraschade: {e}[/bold red]")
        import traceback
        traceback.print_exc()
        return

    # 3. VECTOR INDEXER
    console.print("\n[bold yellow]3. Vector Indexer (Smart Indexering)...[/bold yellow]")
    # Nu indexerar vi Lake-filen som (förhoppningsvis) har fått backpropagated context
    indexera_vektor(lake_file, os.path.basename(lake_file))
    
    # 4. SLUTRAPPORT
    console.print("\n[bold green]✅ FULL FLOW COMPLETE[/bold green]")
    
    # Analysera resultatet
    ev_count_post = get_evidence_count(unit_id) # Borde vara 0 om Dreamer städat
    
    processed = dream_stats.get('processed', 0)
    consolidated = dream_stats.get('consolidated', 0)
    rejected = dream_stats.get('rejected', 0)
    
    console.print(Panel(f"""
    **Resultat:**
    - **Evidence:** {ev_count_pre} skapade -> {ev_count_post} kvar (Dreamer städar: {'Ja' if ev_count_post==0 else 'Nej'})
    - **Dreamer Analys:**
        - Processade entiteter: {processed}
        - Godkända (nya noder): {consolidated}
        - Avvisade: {rejected}
    
    **Status:**
    Dokumentet är nu indexerat och grafen har lärt sig de nya entiteterna.
    """, title="Slutrapport"))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Användning: python tools/test_full_flow.py <path_to_file>")
        sys.exit(1)
    
    main(sys.argv[1])







