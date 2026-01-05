import os
import sys
import yaml
import json
import logging
import chromadb
from pathlib import Path
from typing import Dict, List, Any, Optional

# 1. Setup Logging (Stderr för MCP)
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format='%(levelname)s:%(name)s:%(message)s'
)
logging.getLogger("chromadb").setLevel(logging.WARNING)

# Path setup
project_root = str(Path(__file__).parent.parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from mcp.server.fastmcp import FastMCP
from services.utils.graph_service import GraphStore

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
VECTOR_PATH = os.path.expanduser(PATHS.get('vector_db', '~/MyMemory/Index/VectorDB'))
LAKE_PATH = os.path.expanduser(PATHS.get('lake_dir', '~/MyMemory/Lake'))

mcp = FastMCP("MyMemoryTrinityConsole")

# --- HELPERS ---

def _get_vector_collection():
    """Hämtar ChromaDB collection eller kastar fel."""
    # OBS: Tar bort try/except här för att låta verktyget hantera felet och visa det för dig
    if not os.path.exists(VECTOR_PATH):
        raise FileNotFoundError(f"Path does not exist: {VECTOR_PATH}")
        
    client = chromadb.PersistentClient(path=VECTOR_PATH)
    return client.get_collection("knowledge_base")

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

# Uppdaterat verktyg som visar det faktiska felet
@mcp.tool()
def query_vector_memory(query_text: str, n_results: int = 5) -> str:
    try:
        # Nu kommer vi se exakt varför den kraschar om den gör det
        coll = _get_vector_collection()
        
        results = coll.query(query_texts=[query_text], n_results=n_results)
        
        if not results['ids'][0]:
            return f"VEKTOR: Inga semantiska matchningar för '{query_text}'."

        # ... (resten av koden är samma som förut) ...
        
        output = [f"=== VEKTOR RESULTAT ('{query_text}') ==="]
        ids = results['ids'][0]
        distances = results['distances'][0]
        metadatas = results['metadatas'][0]
        documents = results['documents'][0]
        
        for i, uid in enumerate(ids):
            dist = distances[i]
            meta = metadatas[i]
            content_preview = documents[i].replace('\n', ' ')[:100] + "..."
            
            quality = "🔥 Stark" if dist < 0.8 else "❄️ Svag" if dist > 1.2 else "☁️ Medel"
            
            output.append(f"{i+1}. [{quality} Match] (Dist: {dist:.3f})")
            output.append(f"   Source: {meta.get('name', 'Unknown')}")
            output.append(f"   Type: {meta.get('type', 'Unknown')}")
            output.append(f"   Content: \"{content_preview}\"")
            output.append(f"   ID: {uid}")
            output.append("---")
            
        return "\n".join(output)

    except Exception as e:
        # HÄR ÄR NYCKELN: Returnera felet till chatten!
        return f"⚠️ VEKTOR-FEL: {str(e)} (Path: {VECTOR_PATH})"

# --- TOOL 3: LAKE (Metadata) ---

@mcp.tool()
def search_lake_metadata(keyword: str, field: str = None) -> str:
    """
    Söker i KÄLLFILERNAS metadata (Lake Header).
    Skannar markdown-filer för att se hur de är taggade.
    
    Args:
        keyword: Ordet du letar efter (t.ex. "Sälj", ett UUID, eller ett namn).
        field: (Optional) Sök bara i specifikt fält t.ex. 'mentions', 'keywords', 'summary'.
    
    Används för att svara på: "Är källfilerna korrekt taggade med ID/nyckelord?"
    """
    matches = []
    scanned_count = 0
    
    try:
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