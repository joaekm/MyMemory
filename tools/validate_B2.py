#!/usr/bin/env python3
"""
VALIDATE B2 (EVIL EDITION v2) - The Qualitative Inquisitor
Kör brutalt dömande extrahering på filer från Assets.

Nyheter i v2:
- SCHEMA-POLIS: Läser graph_schema_template.json och validerar relationer.
- GRAMMATIK-POLIS: Jagar bestämd form (Chefen -> Chef).
- LOGIK-POLIS: Jagar tautologier.

Användning:
    python tools/validate_B2.py <söksträng>
"""

import sys
import os
import yaml
import json
import time
import logging
import unicodedata
import re
from rich.console import Console
from rich.panel import Panel
from rich.tree import Tree

# Setup path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Tysta loggar
logging.getLogger("google").setLevel(logging.WARNING)
logging.getLogger("google_genai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)
logging.basicConfig(level=logging.INFO, format='%(message)s')

console = Console()

try:
    from services.processors.doc_converter import strict_entity_extraction_mcp, extract_text
except ImportError as e:
    console.print(f"[bold red]CRITICAL: Importfel. {e}[/bold red]")
    sys.exit(1)

# --- CONFIG & SCHEMA ---
def load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml')
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def load_schema():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    schema_path = os.path.join(script_dir, '..', 'config', 'graph_schema_template.json')
    try:
        with open(schema_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        console.print(f"[bold red]Kunde inte ladda schema: {e}[/bold red]")
        sys.exit(1)

CONFIG = load_config()
SCHEMA = load_schema()
ASSET_STORE = os.path.expanduser(CONFIG['paths']['asset_store'])

# --- THE BLACKLIST ---
FORBIDDEN_CONCEPTS = [
    "möte", "mötet", "fika", "rast", "lunch", "rum", "tid", "klockan", 
    "projektet", "projekt", "gruppen", "teamet", "avdelningen", "företaget",
    "kunden", "leverantören", "systemet", "lösningen", "idén", "frågan",
    "svar", "dokument", "fil", "pdf", "excel", "listan", "status", "info",
    "hej", "tack", "mvh", "diskussion", "beslut", "agenda", "punkter"
]

# Regex för bestämd form (en, et, na, erna) i slutet av ord
DEFINITE_FORM_REGEX = re.compile(r'(en|et|na|erna)$', re.IGNORECASE)

def find_files(prefix):
    matches = []
    prefix_norm = unicodedata.normalize('NFC', prefix).lower()
    for root, _, files in os.walk(ASSET_STORE):
        for f in files:
            if f.startswith(".") or f.startswith("temp_"): continue
            f_norm = unicodedata.normalize('NFC', f).lower()
            if prefix_norm in f_norm:
                matches.append(os.path.join(root, f))
    return matches

def visualize_result(filename, nodes, edges):
    """Ritar ut trädet med resultatet."""
    tree = Tree(f"🔍 Resultat för {filename}")
    
    # Noder
    node_branch = tree.add(f"[bold green]Noder ({len(nodes)})[/bold green]")
    nodes_by_type = {}
    for n in nodes:
        ntype = n.get('type', 'Unknown')
        if ntype not in nodes_by_type: nodes_by_type[ntype] = []
        nodes_by_type[ntype].append(n)
        
    for ntype, nlist in sorted(nodes_by_type.items()):
        type_branch = node_branch.add(f"[yellow]{ntype}[/yellow]")
        for n in nlist:
            name = n.get('name', 'Unknown')
            conf = n.get('confidence', 0.0)
            status = n.get('status', '?')
            
            if conf >= 0.8: conf_str = f"[green]{conf}[/green]"
            elif conf >= 0.5: conf_str = f"[yellow]{conf}[/yellow]"
            else: conf_str = f"[red]{conf}[/red]"
            
            type_branch.add(f"{name} ({conf_str}) [dim]{status}[/dim]")

    # Kanter
    edge_branch = tree.add(f"[bold magenta]Relationer ({len(edges)})[/bold magenta]")
    for e in edges:
        s = e.get('source', '?')
        t = e.get('target', '?')
        r = e.get('type', '?')
        c = e.get('confidence', 0.0)
        edge_branch.add(f"{s} --[[italic]{r}[/italic]]--> {t} ({c})")

    console.print(tree)

def judge_extraction(filename, text, result, duration):
    """
    Den ÄNNU elakare domaren.
    """
    nodes = result.get('nodes', [])
    edges = result.get('edges', [])
    
    # 1. Visa resultatet
    visualize_result(filename, nodes, edges)
    
    score = 100
    crimes = []

    # Mappa namn till typ för schema-koll
    node_map = {n['name']: n['type'] for n in nodes}

    # 2. Performance Shaming
    if duration > 10.0:
        score -= 5
        crimes.append(f"🐢 SLÖT: Tog {duration:.1f}s. (-5p)")

    if not nodes:
        console.print("[bold red]FAIL: Tomt resultat![/bold red]")
        return -100

    # 3. Node Inquisition
    for n in nodes:
        name = n.get('name', 'Unknown')
        type_str = n.get('type', 'Unknown')
        conf = n.get('confidence', 0.0)
        ctx = n.get('context_keywords', [])

        # Brus
        if name.lower() in FORBIDDEN_CONCEPTS:
            score -= 25
            crimes.append(f"🗑️  BRUS: '{name}' är generiskt skräp. (-25p)")

        # Konfidens
        if conf < 0.3:
            score -= 20
            crimes.append(f"💩 USEL KONFIDENS: '{name}' ({conf}). (-20p)")
        elif conf < 0.7:
            score -= 5
            crimes.append(f"⚠️  TVEKSAM: '{name}' ({conf}). (-5p)")

        # Hallucinationer
        if name.lower() not in text.lower():
            score -= 15
            crimes.append(f"👻 HALLUCINATION?: '{name}' finns inte exakt i texten. (-15p)")

        # Kontext
        if not ctx:
            score -= 10
            crimes.append(f"🕳️  KONTEXTLÖS: '{name}' saknar keywords. (-10p)")

        # Olagliga typer
        if type_str == "Document":
            score -= 50
            crimes.append(f"🚨 OLAGLIG TYP: 'Document' får inte skapas! (-50p)")

        # GRAMMATIK-POLISEN (Nyhet!)
        # Slår ner på bestämd form för Roller och Grupper
        if type_str in ["Roles", "Group"]:
            # Enkel heuristik: slutar på "en", "et", "na" och är inte ett undantag
            if len(name.split()) == 1 and DEFINITE_FORM_REGEX.search(name):
                # Undantag för ord som "Möten", "Vatten" etc, men vi chansar hårt här
                score -= 10
                crimes.append(f"📖 GRAMMATIKFEL: '{name}' verkar vara bestämd form. Ska vara grundform (t.ex. 'Chef', inte 'Chefen'). (-10p)")

    # 4. Edge Inquisition (Schema-Polisen)
    schema_edges = SCHEMA.get('edges', {})
    
    for e in edges:
        source = e.get('source')
        target = e.get('target')
        rel_type = e.get('type')
        
        # LOGIK-POLISEN (Tautologier)
        if source == target:
            score -= 20
            crimes.append(f"♾️  TAUTOLOGI: '{source}' pekar på sig själv. Värdelöst. (-20p)")
            continue

        s_type = node_map.get(source)
        t_type = node_map.get(target)

        # Om noderna saknas (spöknoder) har vi redan straffat det indirekt, men kolla här med
        if not s_type or not t_type:
            score -= 10
            crimes.append(f"👻 SPÖK-RELATION: En av noderna i '{source} -> {target}' finns inte. (-10p)")
            continue

        # SCHEMA-POLISEN (Krysstabulering)
        if rel_type not in schema_edges:
            score -= 20
            crimes.append(f"🏴‍☠️ PÅHITTAD RELATION: '{rel_type}' finns inte i schemat. (-20p)")
        else:
            definition = schema_edges[rel_type]
            allowed_sources = definition.get('source_type', [])
            allowed_targets = definition.get('target_type', [])
            
            # Validera riktning
            s_ok = s_type in allowed_sources
            t_ok = t_type in allowed_targets
            
            if not s_ok or not t_ok:
                score -= 25
                expected = f"{allowed_sources} -> {allowed_targets}"
                actual = f"{s_type} -> {t_type}"
                crimes.append(f"⚖️ SCHEMA-BROTT: '{rel_type}' mellan {source} och {target} ({actual}) är olagligt. Kräver {expected}. (-25p)")

    # 5. Domen
    color = "green"
    verdict = "GODKÄNT"
    
    if score < 0: color, verdict = "bold red", "KATASTROF"
    elif score < 50: color, verdict = "red", "UNDERKÄNT"
    elif score < 80: color, verdict = "yellow", "MEDELMÅTTIGT"

    if crimes:
        console.print("\n[bold]Anklagelsepunkter:[/bold]")
        for crime in crimes:
            console.print(f"  {crime}")
    else:
        console.print("\n[italic]Inga fel hittades. Otroligt.[/italic]")

    console.print(f"\nPOÄNG: [{color}]{score}/100 ({verdict})[/{color}]")
    console.print("-" * 60)
    
    return score

def main():
    if len(sys.argv) < 2:
        console.print("[bold red]Ange sökord![/bold red] Ex: python tools/validate_B2.py 'Breif'")
        sys.exit(1)
        
    prefix = sys.argv[1]
    files = find_files(prefix)
    
    if not files:
        console.print("[yellow]Inga filer hittades.[/yellow]")
        sys.exit(0)
        
    console.print(f"[bold]Granskar {len(files)} filer...[/bold]\n")
    
    total_score = 0
    for filepath in files:
        try:
            filename = os.path.basename(filepath)
            ext = os.path.splitext(filename)[1]
            text = extract_text(filepath, ext)
            if not text: continue
            
            start_time = time.time()
            # Kör en rejäl chunk för att ge AI:n en chans att göra fel
            result = strict_entity_extraction_mcp(text[:25000], source_hint="Validator_Test")
            duration = time.time() - start_time
            
            total_score += judge_extraction(filename, text, result, duration)
        except Exception as e:
            console.print(f"[red]Fel vid {filename}: {e}[/red]")
            import traceback
            traceback.print_exc()

    avg = total_score / len(files)
    console.print(f"\n[bold on white] SNITT: {avg:.1f}/100 [/bold on white]")

if __name__ == "__main__":
    main()