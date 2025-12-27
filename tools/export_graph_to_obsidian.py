#!/usr/bin/env python3
"""
The Shadow Graph Exporter
Exportera DuckDB-grafen till en Obsidian-vänlig markdown-struktur.
Detta möjliggör visuell debuggning av grafen (dubbletter, orphans, kluster).

Princip:
1. Raderar hela målmappen (Total Rewrite).
2. Noder -> Filer (med frontmatter för Aliases/Props).
3. Kanter -> Wikilinks.
"""

import os
import sys
import yaml
import json
import duckdb
import re
import logging
import shutil
import time
from datetime import datetime

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger("ShadowGraph")

def load_config():
    """Ladda konfiguration från my_mem_config.yaml."""
    paths_to_check = [
        "config/my_mem_config.yaml",
        "../config/my_mem_config.yaml",
        "/Users/jekman/Projects/MyMemory/config/my_mem_config.yaml"
    ]
    
    config_path = None
    for p in paths_to_check:
        if os.path.exists(p):
            config_path = p
            break
            
    if not config_path:
        logger.error("HARDFAIL: Kunde inte hitta my_mem_config.yaml")
        sys.exit(1)
        
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    # Expandera sökvägar
    for k, v in config.get('paths', {}).items():
        config['paths'][k] = os.path.expanduser(v)
        
    return config

def clean_filename(name):
    """Gör om ett nodnamn till ett säkert filnamn."""
    # Ersätt otillåtna tecken med underscore.
    clean = re.sub(r'[\\/*?:"<>|]', '_', name)
    return clean.strip()

def export_graph():
    config = load_config()
    db_path = config['paths']['graph_db']
    lake_path = config['paths']['lake_store']
    
    # Placera ShadowGraph parallellt med Lake (i MyMemory-roten)
    root_dir = os.path.dirname(lake_path.rstrip(os.sep))
    output_dir = os.path.join(root_dir, "ShadowGraph")
    
    logger.info(f"Kopplar upp mot grafdatabas: {db_path}")
    logger.info(f"Export destination: {output_dir}")
    
    if not os.path.exists(db_path):
        logger.error(f"HARDFAIL: Databasfilen saknas: {db_path}")
        sys.exit(1)

    # --- STEG 1: STÄDNING (Total Rewrite) ---
    if os.path.exists(output_dir):
        logger.info("Raderar gammal graf-data (Fullständig rensning)...")
        try:
            shutil.rmtree(output_dir)
        except Exception as e:
            logger.error(f"Kunde inte radera mappen: {e}")
            sys.exit(1)
    
    # Skapa mappen på nytt
    os.makedirs(output_dir, exist_ok=True)

    # --- STEG 2: SNAPSHOT ---
    timestamp = int(time.time())
    snapshot_path = f"{db_path}.snapshot.{timestamp}.duckdb"
    
    try:
        shutil.copy2(db_path, snapshot_path)
        con = duckdb.connect(snapshot_path, read_only=True)
    except Exception as e:
        logger.error(f"HARDFAIL vid DB-anslutning: {e}")
        if os.path.exists(snapshot_path): os.remove(snapshot_path)
        sys.exit(1)

    try:
        # --- STEG 3: HÄMTA DATA ---
        
        # Hämta noder - Filtrera bort rena dokument för att minska brus
        # Justera WHERE-klausulen om du vill se dokumenten också.
        logger.info("Hämtar noder...")
        nodes_query = """
            SELECT id, type, aliases, properties 
            FROM nodes 
            WHERE type NOT IN ('Document', 'Source', 'File', 'Chunk')
        """
        nodes = con.sql(nodes_query).fetchall()
        
        # Hämta kanter
        logger.info("Hämtar kanter...")
        edges = con.sql("SELECT source, target, edge_type FROM edges").fetchall()
        
        # Indexera kanter för snabb uppslagning
        edges_by_source = {}
        for source, target, edge_type in edges:
            if source not in edges_by_source: edges_by_source[source] = []
            edges_by_source[source].append((target, edge_type))

        count = 0
        logger.info(f"Startar export av {len(nodes)} noder...")

        # --- STEG 4: SKRIV FILER ---
        for node_id, node_type, aliases_raw, props_raw in nodes:
            
            # Typ-säker hantering av JSON-fält
            aliases = []
            if isinstance(aliases_raw, str):
                try: aliases = json.loads(aliases_raw)
                except: aliases = [aliases_raw] if aliases_raw else []
            elif isinstance(aliases_raw, list):
                aliases = aliases_raw
            
            props = {}
            if isinstance(props_raw, str):
                try: props = json.loads(props_raw)
                except: props = {"raw_value": props_raw}
            elif isinstance(props_raw, dict):
                props = props_raw

            # Filnamn
            filename = clean_filename(node_id) + ".md"
            filepath = os.path.join(output_dir, filename)
            
            # Frontmatter
            frontmatter = {
                "type": node_type,
                "id": node_id,
                "created": datetime.now().isoformat()
            }
            if aliases:
                frontmatter["aliases"] = aliases
            
            if props:
                for k, v in props.items():
                    if v is None: continue
                    # Obsidian frontmatter gillar enkla typer
                    if isinstance(v, (str, int, float, bool, list)):
                        frontmatter[k] = v
                    else:
                        frontmatter[k] = str(v)

            with open(filepath, "w", encoding="utf-8") as f:
                # Frontmatter
                f.write("---\n")
                yaml.dump(frontmatter, f, default_flow_style=False, allow_unicode=True)
                f.write("---\n\n")
                
                # Titel
                f.write(f"# {node_id}\n\n")
                
                # Egenskaper i body (för tydlighet)
                if props:
                    f.write("### Egenskaper\n")
                    for k, v in props.items():
                        if v: f.write(f"- **{k}:** {v}\n")
                    f.write("\n")

                # Relationer
                if node_id in edges_by_source:
                    f.write("## Relationer\n")
                    for target, edge_type in edges_by_source[node_id]:
                        target_clean = clean_filename(target)
                        # Använd pipe | för att visa snyggt namn om filnamnet skiljer sig
                        link = f"[[{target_clean}|{target}]]" if target_clean != target else f"[[{target}]]"
                        f.write(f"- {link} ({edge_type})\n")
            
            count += 1
            if count % 100 == 0:
                print(f"Exporterat {count} noder...", end="\r")

        print(f"\n✅ Export klar! {count} filer skapade i {output_dir}")

    except Exception as e:
        logger.error(f"HARDFAIL: Fel vid export: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try: con.close() 
        except: pass
        if os.path.exists(snapshot_path):
            try: os.remove(snapshot_path)
            except: pass

if __name__ == "__main__":
    export_graph()