#!/usr/bin/env python3
"""
test_full_flow_interactive.py - Human-in-the-Loop (v5 Batch Edition)

Detta skript:
1. Kör DocConverter (valfritt).
2. Kör Dreamer ASYNKRONT i BATCHAR (för att spara tokens/tid).
3. SMART REVIEW (Auto-approve teman/säkra noder).
4. Genererar Rapport.
"""

import os
import sys
import yaml
import asyncio
import logging
import datetime
from dataclasses import dataclass
from typing import List, Dict, Any
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.progress import Progress, SpinnerColumn, TextColumn

# Setup paths
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# Imports
from services.processors.doc_converter import processa_dokument
from services.processors.dreamer import EvidenceConsolidator, backpropagate_to_lake
from services.indexers.vector_indexer import indexera_vektor

# Tysta loggar
logging.getLogger("DOCCONV").setLevel(logging.WARNING)
logging.getLogger("DREAMER").setLevel(logging.WARNING)
logging.basicConfig(level=logging.CRITICAL)

console = Console()

# --- CONFIG & PATHS ---
def load_config():
    with open(os.path.join(project_root, 'config', 'my_mem_config.yaml'), 'r') as f:
        config = yaml.safe_load(f)
    for k, v in config['paths'].items():
        config['paths'][k] = os.path.expanduser(v)
    return config

CONFIG = load_config()
LAKE_STORE = CONFIG['paths']['lake_store']
LOG_DIR = os.path.join(os.path.dirname(CONFIG['logging']['log_file_path']), "Reports")

@dataclass
class Proposal:
    entity_name: str
    analysis: Dict
    evidence_list: List[Dict]

@dataclass
class GraphAction:
    action_type: str
    node_id: str
    master_node: str
    edge_type: str
    description: str
    confidence: float
    auto_approved: bool

async def run_analysis_phase(dreamer):
    """Kör analysen i BATCHAR parallellt."""
    evidence_groups = dreamer.fetch_pending_evidence()
    if not evidence_groups: return []

    # 1. Samla kandidater
    candidates = []
    for name, ev_list in evidence_groups.items():
        if len(ev_list) >= 2 or any(e['confidence'] > 0.8 for e in ev_list):
            candidates.append((name, ev_list))

    if not candidates: return []

    # 2. Skapa batchar (15 st per batch)
    BATCH_SIZE = 15
    batches = [candidates[i:i + BATCH_SIZE] for i in range(0, len(candidates), BATCH_SIZE)]
    
    semaphore = asyncio.Semaphore(5) # Max 5 samtidiga batch-anrop
    
    # 3. Kör analys
    results = []
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
        task_id = progress.add_task(f"Analyserar {len(candidates)} entiteter ({len(batches)} batchar)...", total=len(batches))
        
        # Anropa analyze_batch_async istället för entity
        tasks = [dreamer.analyze_batch_async(batch, semaphore) for batch in batches]
        batch_results = await asyncio.gather(*tasks)
        progress.update(task_id, completed=len(batches))

    # 4. Platta ut resultaten (List of Lists -> List)
    all_analyses = [item for sublist in batch_results for item in sublist]
    
    # 5. Mappa tillbaka till Evidence för att skapa Proposals
    # (LLM returnerar bara analysen, vi måste para ihop den med bevisen igen)
    evidence_map = {name: ev_list for name, ev_list in candidates}
    
    proposals = []
    for analysis in all_analyses:
        name = analysis.get('entity')
        ev_list = evidence_map.get(name)
        
        if ev_list:
            # Injicera aggregated confidence för Smart Review
            agg_conf = dreamer._calculate_aggregated_confidence(ev_list)
            for ev in ev_list:
                ev['aggregated_confidence'] = agg_conf
                
            proposals.append(Proposal(name, analysis, ev_list))
        else:
            # Om LLM hallucinerade ett namn som inte fanns i input
            pass
            
    return proposals

def interactive_loop(proposals, dreamer):
    """Smart Review Loop."""
    stats = {"nodes_manual": 0, "nodes_auto": 0, "themes": 0, "rejected": 0}
    actions = []
    CONFIDENCE_THRESHOLD = 0.9
    
    to_review = []
    for p in proposals:
        # Använd aggregated confidence om det finns, annars max
        agg_conf = p.evidence_list[0].get('aggregated_confidence', 0) if p.evidence_list else 0
        if p.analysis.get('is_atomic_node') and agg_conf < CONFIDENCE_THRESHOLD:
            to_review.append(p)
            
    console.print(f"\n[bold cyan]-- SMART INTERACTIVE REVIEW --[/bold cyan]")
    console.print(f"Totalt: {len(proposals)} förslag. Du granskar {len(to_review)} osäkra noder.\n")
    
    for i, p in enumerate(proposals, 1):
        analysis = p.analysis
        is_node = analysis.get('is_atomic_node')
        master = analysis.get('master_node')
        suggested_id = analysis.get('suggested_node_id')
        summary = analysis.get('canonical_summary', '')
        source_files = [e['source_file'] for e in p.evidence_list]
        
        agg_conf = p.evidence_list[0].get('aggregated_confidence', 0) if p.evidence_list else 0

        # --- LOGIK ---
        auto_approve = False
        decision = "SKIP"
        
        if not is_node:
            auto_approve = True
        elif agg_conf >= CONFIDENCE_THRESHOLD:
            auto_approve = True

        # --- EXECUTION ---
        if auto_approve:
            decision = "YES"
            if is_node:
                stats["nodes_auto"] += 1
                console.print(f"[dim]🤖 Auto-Nod: {suggested_id} ({agg_conf:.2f})[/dim]")
            else:
                stats["themes"] += 1
        else:
            # Manuell Review
            console.rule(f"Granska Osäker Nod")
            if p.evidence_list:
                console.print(f"[dim]Context: {p.evidence_list[0]['context'][:150]}...[/dim]")
            console.print(f"[bold yellow]⚠️  Konfidens: {agg_conf:.2f}[/bold yellow]")
            console.print(f"[bold green]FÖRSLAG:[/bold green] Skapa nod [bold white]'{suggested_id}'[/bold white] ({master})")
            console.print("[dim][y]es, [n]o (radera), [s]kip, [q]uit[/dim]")
            
            choice = Prompt.ask("Godkänn?", choices=["y", "n", "s", "q"], default="y")
            
            if choice == "q": break
            if choice == "y": 
                decision = "YES"
                stats["nodes_manual"] += 1
                console.print(f"[green]✓ Nod Sparad[/green]")
            elif choice == "n":
                decision = "NO"
                stats["rejected"] += 1
                console.print("[red]✗ Raderad[/red]")

        # --- COMMIT ---
        if decision == "YES":
            success, msg = dreamer.commit_to_graph(analysis, source_files)
            if success:
                topic = suggested_id if is_node else master
                backpropagate_to_lake(source_files, topic, summary)
                dreamer.clear_processed_evidence(p.entity_name)
                
                actions.append(GraphAction(
                    action_type="NODE_CREATED" if is_node else "THEME_LINKED",
                    node_id=suggested_id if is_node else master,
                    master_node=master,
                    edge_type="UNIT_MENTIONS" if is_node else "DEALS_WITH",
                    description=summary,
                    confidence=agg_conf,
                    auto_approved=auto_approve
                ))
        
        elif decision == "NO":
            dreamer.clear_processed_evidence(p.entity_name)

    return stats, actions

def save_report(filename, stats, actions):
    """Sparar rapport."""
    os.makedirs(LOG_DIR, exist_ok=True)
    report_file = os.path.join(LOG_DIR, f"Report_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(f"# MyMem Graph Import Report\n**Fil:** `{filename}`\n\n")
        f.write("## 📊 Statistik\n")
        f.write(f"- **Nya Noder (Auto):** {stats['nodes_auto']}\n")
        f.write(f"- **Nya Noder (Manuell):** {stats['nodes_manual']}\n")
        f.write(f"- **Teman Kopplade:** {stats['themes']}\n\n")
        
        f.write("## 🟢 Skapade Noder\n| Nod ID | Typ | Konfidens | Auto? | Beskrivning |\n|---|---|---|---|---|\n")
        for n in [a for a in actions if a.action_type == "NODE_CREATED"]:
            auto = "🤖" if n.auto_approved else "👤"
            f.write(f"| **{n.node_id}** | {n.master_node} | {n.confidence:.2f} | {auto} | {n.description[:100]} |\n")
            
        f.write("\n## 🔵 Teman\n| Master Nod | Beskrivning |\n|---|---|\n")
        for t in [a for a in actions if a.action_type == "THEME_LINKED"]:
            f.write(f"| **{t.master_node}** | {t.description} |\n")
            
    return report_file

async def main(filepath):
    console.clear()
    console.print(Panel.fit(f"[bold blue]MyMem Interactive (Batch)[/bold blue]\nFil: {os.path.basename(filepath)}"))

    if Prompt.ask("\nVill du köra DocConverter?", choices=["y", "n"], default="n") == "y":
        try: processa_dokument(filepath, os.path.basename(filepath))
        except Exception as e: 
            console.print(f"[red]Fel i DocConverter: {e}[/red]")
            return

    dreamer = EvidenceConsolidator()
    
    # Kör den nya BATCH-analysen
    proposals = await run_analysis_phase(dreamer)
    
    if not proposals:
        console.print("[yellow]Inga förslag att granska.[/yellow]")
        dreamer.close()
        return

    stats, actions = interactive_loop(proposals, dreamer)
    dreamer.close()

    console.print("\n[yellow]Uppdaterar Sökindex...[/yellow]")
    filename = os.path.basename(filepath)
    for f in os.listdir(LAKE_STORE):
        if f.startswith(os.path.splitext(filename)[0]):
            indexera_vektor(os.path.join(LAKE_STORE, f), f)
            break

    report_path = save_report(filename, stats, actions)

    console.print(Panel(f"""
    **Slutrapport:**
    - Auto Noder: {stats['nodes_auto']}
    - Manuella Noder: {stats['nodes_manual']}
    - Teman: {stats['themes']}
    
    📄 **Rapport:** [link=file://{report_path}]{report_path}[/link]
    """, title="Klar"))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Användning: python tools/test_full_flow_interactive.py <fil>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))