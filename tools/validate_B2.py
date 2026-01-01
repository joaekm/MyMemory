#!/usr/bin/env python3
"""
VALIDATE B2 - Real File Inspector (Area B)
Kör schema-driven extrahering på FAKTISKA filer från Assets.

Detta verktyg är kritiskt för att verifiera:
1. Att systemet hanterar långa, komplexa texter (inte bara lab-meningar).
2. Att "Jocke-dilemmat" löses (kontext extraheras).
3. Att relationsriktningen blir rätt i verkliga scenarion.

Användning:
    python tools/validate_B2.py <söksträng>
    
Exempel:
    python tools/validate_B2.py "Mötesanteckning"
"""

import sys
import os
import yaml
import json
import logging
import unicodedata
from rich.console import Console
from rich.panel import Panel
from rich.tree import Tree
from rich.table import Table

# Tysta loggar
logging.getLogger("google").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.basicConfig(level=logging.CRITICAL)
console = Console()

# Setup path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from services.processors.doc_converter import strict_entity_extraction, extract_text
except ImportError as e:
    console.print(f"[bold red]CRITICAL: {e}[/bold red]")
    sys.exit(1)

# --- CONFIG ---
def load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml')
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

CONFIG = load_config()
ASSET_STORE = os.path.expanduser(CONFIG['paths']['asset_store'])

# --- HELPER FUNCTIONS ---

def find_files(prefix):
    """
    Hitta filer i Assets (rekursivt) som matchar prefixet.
    Hanterar macOS Unicode (NFC/NFD) normalisering.
    """
    matches = []
    # Normalisera söksträngen till NFC (standard)
    prefix_norm = unicodedata.normalize('NFC', prefix).lower()
    
    #console.print(f"[dim]Söker i: {ASSET_STORE}[/dim]")
    
    for root, _, files in os.walk(ASSET_STORE):
        for f in files:
            if f.startswith(".") or f.startswith("temp_"): continue
            
            # Normalisera filnamnet från disken till NFC för jämförelse
            f_norm = unicodedata.normalize('NFC', f).lower()
            
            if prefix_norm in f_norm:
                matches.append(os.path.join(root, f))
                
    return matches

def analyze_file(filepath):
    """Kör extrahering på en fil och visa djupgående analys."""
    filename = os.path.basename(filepath)
    ext = os.path.splitext(filename)[1]
    
    console.print(f"\n[bold white on blue] 📄 ANALYSERAR: {filename} [/bold white on blue]")
    
    # 1. Extrahera text
    try:
        text = extract_text(filepath, ext)
        if not text or len(text) < 10:
            console.print("[yellow]⚠️  Ingen text kunde extraheras eller filen är tom.[/yellow]")
            return
        
        console.print(f"[dim]   Läst {len(text)} tecken text.[/dim]")
        
    except Exception as e:
        console.print(f"[bold red]❌ Fel vid textläsning: {e}[/bold red]")
        return

    # 2. Kör AI-extrahering
    try:
        # Använd samma chunk size som i produktion (om möjligt) eller en rejäl bit
        chunk_size = 25000 
        text_chunk = text[:chunk_size]
        
        start_marker = "[bold yellow]...Kör Schema-Driven Extraction...[/bold yellow]"
        with console.status(start_marker):
            result = strict_entity_extraction(text_chunk)
            
        nodes = result.get('nodes', [])
        edges = result.get('edges', [])
        
    except Exception as e:
        console.print(f"[bold red]❌ Fel vid AI-extrahering: {e}[/bold red]")
        import traceback
        console.print(traceback.format_exc())
        return

    # 3. Visualisera Resultat (Tree View)
    tree = Tree(f"🔍 Resultat för {filename}")
    
    # Noder
    node_branch = tree.add(f"[bold green]Noder ({len(nodes)})[/bold green]")
    
    # Sortera noder per typ för överskådlighet
    nodes_by_type = {}
    for n in nodes:
        ntype = n.get('type', 'Unknown')
        if ntype not in nodes_by_type: nodes_by_type[ntype] = []
        nodes_by_type[ntype].append(n)
        
    for ntype, nlist in sorted(nodes_by_type.items()):
        type_branch = node_branch.add(f"[yellow]{ntype}[/yellow]")
        for n in nlist:
            name = n.get('name', 'Unknown')
            ctx = n.get('context_keywords', [])
            status = n.get('status', 'PROVISIONAL')
            
            # Formatera output
            node_text = f"[cyan]{name}[/cyan]"
            if ctx:
                node_text += f" [dim]ctx: {', '.join(ctx[:4])}[/dim]"
            
            if status != 'PROVISIONAL':
                node_text += f" [red]({status})[/red]" # Varna om status är fel
                
            type_branch.add(node_text)

    # Kanter
    edge_branch = tree.add(f"[bold magenta]Relationer ({len(edges)})[/bold magenta]")
    for e in edges:
        s = e.get('source', '?')
        t = e.get('target', '?')
        r = e.get('type', '?')
        edge_branch.add(f"{s} --[[bold]{r}[/bold]]--> {t}")

    console.print(tree)
    
    # 4. Kvalitetskontroll (Automatiska varningar)
    warnings = []
    
    # Varning 1: Dokument-noder (Ska inte finnas enligt Negativa Regler)
    doc_nodes = [n for n in nodes if n.get('type') == 'Document']
    if doc_nodes:
        warnings.append(f"Hittade {len(doc_nodes)} noder av typen 'Document' (ska vara förbjudet).")
        
    # Varning 2: Generiska namn (Enkelt heuristiskt test)
    generic_terms = ["kaffe", "möte", "rum", "tid", "idé", "projektet"]
    for n in nodes:
        if n.get('name', '').lower() in generic_terms:
            warnings.append(f"Misstänkt generisk nod: '{n.get('name')}' ({n.get('type')})")
            
    # Varning 3: Saknad kontext
    no_context = [n['name'] for n in nodes if not n.get('context_keywords')]
    if len(no_context) > 0:
        warnings.append(f"{len(no_context)} noder saknar kontext (t.ex. {no_context[:3]}).")

    if warnings:
        console.print("\n[bold red]⚠️  KVALITETSVARNINGAR:[/bold red]")
        for w in warnings:
            console.print(f"  - {w}")
    else:
        console.print("\n[bold green]✅ Inga uppenbara kvalitetsbrister detekterade.[/bold green]")

    console.print("-" * 60)

def main():
    if len(sys.argv) < 2:
        console.print("[bold]validate_B2 - Real File Inspector[/bold]")
        console.print("Användning: python tools/validate_B2.py <söksträng>")
        sys.exit(1)
        
    prefix = sys.argv[1]
    
    console.print(f"Söker efter filer som matchar: '[bold cyan]{prefix}[/bold cyan]'...")
    files = find_files(prefix)
    
    if not files:
        console.print(f"[red]Inga filer hittades i Assets som matchar '{prefix}'[/red]")
        # Lista några filer som finns för hjälp
        console.print("[dim]Exempel på filer som finns:[/dim]")
        for root, _, fs in os.walk(ASSET_STORE):
            for f in fs[:3]:
                if not f.startswith("."): console.print(f" - {f}")
            break
        sys.exit(1)
        
    console.print(f"Hittade {len(files)} filer. Analyserar första matchningen...\n")
    
    # Vi analyserar bara den första (eller loopa om du vill testa batch)
    analyze_file(files[0])

if __name__ == "__main__":
    main()