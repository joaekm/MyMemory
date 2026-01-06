import os
import sys
import yaml
import json
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional

# 1. Setup Logging (Stderr för MCP)
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format='%(levelname)s:%(name)s:%(message)s'
)

# Path setup
project_root = str(Path(__file__).parent.parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from mcp.server.fastmcp import FastMCP
from services.utils.graph_service import GraphStore
# NY IMPORT: Använd VectorService (Single Source of Truth)
from services.utils.vector_service import get_vector_service

# --- CONFIG LOADING ---
def _load_config():
    config_path = os.path.join(project_root, "config", "my_mem_config.yaml")
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logging.error(f"Config load failed: {e}")
        return {}

CONFIG = _load_config()
PATHS = CONFIG.get('paths', {})

GRAPH_PATH = os.path.expanduser(PATHS.get('graph_db', '~/MyMemory/Index/GraphDB'))
LAKE_PATH = os.path.expanduser(PATHS.get('lake_dir', '~/MyMemory/Lake'))

mcp = FastMCP("MyMemoryTrinityConsole")

# --- HELPERS ---

def _parse_frontmatter(file_path: str) -> Dict:
    """Läser YAML-frontmatter från en markdown-fil."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    return yaml.safe_load(parts[1])
        return {}
    except Exception:
        return {}

# --- TOOL 1: GRAPH (Structure) ---

@mcp.tool()
def search_graph_nodes(query: str, node_type: str = None) -> str:
    """
    Söker efter STRUKTUR i Grafdatabasen.
    Hittar entiteter baserat på namn, ID eller alias.
    
    Används för att svara på: "Finns noden X?" eller "Hur ser relationerna ut?"
    """
    try:
        # GraphStore använder DuckDB internt och är robust
        graph = GraphStore(GRAPH_PATH, read_only=True)
        limit = 15
        
        # Direkt SQL för prestanda och filtrering
        sql = "SELECT id, type, aliases, properties FROM nodes WHERE (id ILIKE ? OR aliases ILIKE ?)"
        params = [f"%{query}%", f"%{query}%"]
        
        if node_type:
            sql += " AND type = ?"
            params.append(node_type)
            
        sql += " LIMIT ?"
        params.append(limit)
        
        rows = graph.conn.execute(sql, params).fetchall()
        graph.close()
        
        if not rows:
            return f"GRAF: Inga träffar för '{query}'" + (f" (Typ: {node_type})" if node_type else "")

        output = [f"=== GRAF RESULTAT ({len(rows)}) ==="]
        for r in rows:
            node_id, n_type, aliases_raw, props_raw = r
            props = json.loads(props_raw) if props_raw else {}
            aliases = json.loads(aliases_raw) if aliases_raw else []
            
            # Formatera output för läsbarhet
            name = props.get('name', node_id)
            ctx = props.get('context_keywords', [])
            ctx_str = f"Context: {ctx}" if ctx else "No context"
            alias_str = f"Aliases: {len(aliases)}" if aliases else ""
            
            output.append(f"• [{n_type}] {name}")
            output.append(f"  ID: {node_id}")
            if alias_str: output.append(f"  {alias_str}")
            output.append(f"  {ctx_str}")
            
        return "\n".join(output)
    except Exception as e:
        return f"Grafsökning misslyckades: {e}"

# --- TOOL 2: VECTOR (Semantics) ---

@mcp.tool()
def query_vector_memory(query_text: str, n_results: int = 5) -> str:
    """
    Söker i VEKTOR-minnet (Semantisk sökning).
    Använder VectorService för att garantera rätt modell och collection.
    """
    try:
        # 1. Hämta Singleton för Knowledge Base (samma som indexeraren använder)
        # Vi ber explicit om "knowledge_base" enligt din instruktion
        vs = get_vector_service("knowledge_base")
        
        # 2. Sök (VectorService returnerar en ren lista med dicts)
        results = vs.search(query_text=query_text, limit=n_results)
        
        if not results:
            return f"VEKTOR: Inga semantiska matchningar för '{query_text}'."

        output = [f"=== VEKTOR RESULTAT ('{query_text}') ==="]
        output.append(f"Modell: {vs.model_name}") # Bekräfta modellen för transparens
        output.append("-" * 30)
        
        for i, item in enumerate(results):
            # VectorService har redan packat upp Chroma-strukturen åt oss
            dist = item['distance']
            meta = item['metadata']
            content = item['document']
            uid = item['id']
            
            content_preview = content.replace('\n', ' ')[:150] + "..."
            
            # Bedöm kvalitet (lägre distans = bättre)
            quality = "🔥 Stark" if dist < 0.8 else "❄️ Svag" if dist > 1.2 else "☁️ Medel"
            
            output.append(f"{i+1}. [{quality} Match] (Dist: {dist:.3f})")
            output.append(f"   Fil: {meta.get('filename', 'Unknown')}")
            output.append(f"   Content: \"{content_preview}\"")
            output.append(f"   ID: {uid}")
            output.append("---")
            
        return "\n".join(output)

    except Exception as e:
        # Returnera felet till chatten för transparens
        return f"⚠️ VEKTOR-FEL: {str(e)}"

# --- TOOL 3: LAKE (Metadata) ---

@mcp.tool()
def search_lake_metadata(keyword: str, field: str = None) -> str:
    """
    Söker i KÄLLFILERNAS metadata (Lake Header).
    Skannar markdown-filer för att se hur de är taggade.
    """
    matches = []
    scanned_count = 0
    
    try:
        if not os.path.exists(LAKE_PATH):
             return f"⚠️ LAKE-FEL: Mappen {LAKE_PATH} finns inte."

        # Hämta alla .md filer
        files = [f for f in os.listdir(LAKE_PATH) if f.endswith('.md')]
        
        for filename in files:
            scanned_count += 1
            full_path = os.path.join(LAKE_PATH, filename)
            frontmatter = _parse_frontmatter(full_path)
            
            found = False
            hit_details = []
            
            # Söklogik
            for k, v in frontmatter.items():
                # Om användaren specificerat fält, hoppa över andra
                if field and k != field:
                    continue
                
                # Sök i listor (t.ex. mentions, keywords)
                if isinstance(v, list):
                    for item in v:
                        if keyword.lower() in str(item).lower():
                            found = True
                            hit_details.append(f"{k}: ...{item}...")
                # Sök i strängar (t.ex. summary, title)
                elif isinstance(v, str):
                    if keyword.lower() in v.lower():
                        found = True
                        hit_details.append(f"{k}: {v[:50]}...")
            
            if found:
                matches.append(f"📄 {filename} -> [{', '.join(hit_details)}]")
                if len(matches) >= 10: # Cap results
                    break
        
        if not matches:
            return f"LAKE: Inga metadata-träffar för '{keyword}' (Skannade {scanned_count} filer)."
            
        output = [f"=== LAKE METADATA ({len(matches)} träffar) ==="]
        output.extend(matches)
        return "\n".join(output)

    except Exception as e:
        return f"Lake-sökning misslyckades: {e}"

if __name__ == "__main__":
    try:
        mcp.run()
    except Exception as e:
        logging.critical(f"Server Crash: {e}")
        sys.exit(1)