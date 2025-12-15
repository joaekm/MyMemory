"""
MyMem Graph Builder - Batch-processning av Lake-dokument till graf.

Migrerad från KuzuDB till DuckDB (LÖST-54: The DuckDB Pivot)
"""

import os
import sys
import yaml
import json
import logging
import re

# Lägg till projekt-root i path för import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from services.graph_service import GraphStore

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
        print("[GraphBuilder] CRITICAL: Config not found.")
        exit(1)
    
    with open(config_path, 'r') as f: config = yaml.safe_load(f)
    
    for k, v in config['paths'].items():
        config['paths'][k] = os.path.expanduser(v)
    config['logging']['log_file_path'] = os.path.expanduser(config['logging']['log_file_path'])
    return config

CONFIG = hitta_och_ladda_config()

# --- SETUP ---
LAKE_STORE = CONFIG['paths']['lake_store']
GRAPH_PATH = CONFIG['paths']['kuzu_db']  # Återanvänder config-nyckel, pekar på .duckdb
TAXONOMY_FILE = CONFIG['paths'].get('taxonomy_file', os.path.expanduser("~/MyMemory/Index/my_mem_taxonomy.json"))
LOG_FILE = CONFIG['logging']['log_file_path']

# Logging
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - GRAPH - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('GraphBuilder')
LOGGER.addHandler(logging.StreamHandler())

UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.md$')


# === SINGLETON GRAPH STORE ===

_GRAPH_STORE = None

def _get_graph_store() -> GraphStore:
    """Singleton-anslutning till GraphStore. Återanvänds för alla queries."""
    global _GRAPH_STORE
    
    if _GRAPH_STORE is not None:
        return _GRAPH_STORE
    
    _GRAPH_STORE = GraphStore(GRAPH_PATH)
    return _GRAPH_STORE


def close_db_connection():
    """Stäng databas-anslutningen explicit. Anropas vid shutdown."""
    global _GRAPH_STORE
    if _GRAPH_STORE:
        _GRAPH_STORE.close()
        _GRAPH_STORE = None


# === GRAPH ENGINE ===

def process_lake_batch():
    """Huvudloop för grafbyggande - Nu med DuckDB (v5.0)"""
    
    graph = None
    
    try:
        graph = _get_graph_store()
        
        files_processed = 0
        relations_created = 0
        
        print(f"🔍 Scannar {LAKE_STORE}...")

        for filename in os.listdir(LAKE_STORE):
            if not filename.endswith(".md"): continue
            
            match = UUID_SUFFIX_PATTERN.search(filename)
            unit_id = match.group(1) if match else None
            if not unit_id: continue
            
            filepath = os.path.join(LAKE_STORE, filename)
            
            try:
                with open(filepath, 'r', encoding='utf-8') as f: content = f.read()
                if "---" not in content: continue
                parts = content.split("---", 2)
                metadata = yaml.safe_load(parts[1])
                
                # Metadata Extraction
                timestamp = metadata.get('timestamp_created') or ""
                source_type = metadata.get('source_type') or "unknown"
                summary = (metadata.get('summary') or "").replace('"', "'")
                title = filename.replace('.md', '')[:50]  # Filnamn som titel
                
                # 1. Skapa Dokument-noden (Unit)
                graph.upsert_node(
                    id=unit_id,
                    type="Unit",
                    properties={
                        "timestamp": timestamp,
                        "source_type": source_type,
                        "summary": summary,
                        "title": title
                    }
                )

                # 2. Skapa Relationer baserat på EXPLICIT DATA (Inte gissningar)
                
                # A. GRAPH_NODES (Ny struktur med viktade koncept och typade entiteter)
                graph_nodes = metadata.get('graph_nodes', {})
                
                # Hantera legacy-format (graph_master_node/graph_sub_node)
                if not graph_nodes:
                    master_node = metadata.get('graph_master_node')
                    if master_node:
                        graph_nodes[master_node] = 1.0
                    sub_node = metadata.get('graph_sub_node')
                    if sub_node:
                        graph_nodes[sub_node] = 0.5
                
                for node_key, node_value in graph_nodes.items():
                    # Abstrakt koncept (masternode) - värde är float
                    if isinstance(node_value, (int, float)):
                        graph.upsert_node(id=node_key, type="Concept")
                        graph.upsert_edge(source=unit_id, target=node_key, edge_type="DEALS_WITH")
                        relations_created += 1
                    
                    # Typad entitet - värde är dict med namn -> relevans
                    elif isinstance(node_value, dict):
                        entity_type = node_key  # Typ från taxonomin
                        for entity_name, relevance in node_value.items():
                            # Skapa Entity-nod med typ som property
                            graph.upsert_node(
                                id=entity_name,
                                type="Entity",
                                properties={"entity_type": entity_type}
                            )
                            # Skapa relation Unit -> Entity
                            graph.upsert_edge(source=unit_id, target=entity_name, edge_type="UNIT_MENTIONS")
                            relations_created += 1

                # C. PERSON (Ägare) - legacy support
                owner = metadata.get('owner_id')
                if owner:
                    graph.upsert_node(id=owner, type="Person")
                    graph.upsert_edge(source=unit_id, target=owner, edge_type="CREATED_BY")
                
                files_processed += 1

            except Exception as e:
                LOGGER.error(f"Fel vid graf-processning av {filename}: {e}")

        print(f"✅ Klar. {files_processed} filer bearbetade. {relations_created} relationer skapade.")
        
        # Visa statistik
        stats = graph.get_stats()
        print(f"📊 Graf-statistik: {stats['total_nodes']} noder, {stats['total_edges']} kanter")

    except Exception as main_e:
        LOGGER.error(f"HARDFAIL: Kritiskt fel i Graf-loopen: {main_e}")
        raise RuntimeError(f"HARDFAIL: Kritiskt fel i Graf-loopen: {main_e}") from main_e


# === ENTITY FUNCTIONS ===

def get_entity(canonical: str) -> dict:
    """
    Hämta en Entity från grafen.
    
    Returns:
        dict med id, type, aliases eller None
    """
    graph = _get_graph_store()
    node = graph.get_node(canonical)
    
    if node and node.get('type') == 'Entity':
        return {
            "id": node['id'],
            "type": node.get('properties', {}).get('entity_type', 'Unknown'),
            "aliases": node.get('aliases', [])
        }
    return None


def get_canonical_from_graph(variant: str) -> str:
    """
    Slå upp canonical name för ett alias i grafen.
    
    Args:
        variant: Alias eller canonical name
    
    Returns:
        Canonical name eller None
    """
    graph = _get_graph_store()
    
    # Kolla om det är ett canonical name (direkt match)
    node = graph.get_node(variant)
    if node:
        return node['id']
    
    # Kolla om det är ett alias
    matches = graph.find_nodes_by_alias(variant)
    if matches:
        return matches[0]['id']
    
    return None


def add_entity_alias(canonical: str, alias: str, entity_type: str) -> bool:
    """
    Lägg till ett alias för en Entity i grafen.
    Skapar Entity-noden om den inte finns.
    
    Args:
        canonical: Kanoniskt namn
        alias: Alias att lägga till
        entity_type: Typ från taxonomin (Person, Aktör, etc.)
    
    Returns:
        True om lyckad
    """
    graph = _get_graph_store()
    
    # Kolla om Entity finns
    node = graph.get_node(canonical)
    
    if node:
        # Lägg till alias om det inte redan finns
        return graph.add_alias(canonical, alias)
    else:
        # Skapa ny Entity med alias
        graph.upsert_node(
            id=canonical,
            type="Entity",
            aliases=[alias],
            properties={"entity_type": entity_type}
        )
        LOGGER.info(f"Skapade Entity '{canonical}' med alias '{alias}'")
        return True


def get_all_entities() -> list:
    """
    Hämta alla Entity-noder från grafen.
    
    Returns:
        Lista med {id, type, aliases}
    """
    graph = _get_graph_store()
    nodes = graph.find_nodes_by_type("Entity")
    
    return [
        {
            "id": n['id'],
            "type": n.get('properties', {}).get('entity_type', 'Unknown'),
            "aliases": n.get('aliases', [])
        }
        for n in nodes
    ]


def get_graph_context_for_search(keywords: list, entities: list) -> str:
    """
    Hämta graf-kontext för söktermerna.
    Hjälper Planner att hitta kreativa spår att utforska.
    
    Args:
        keywords: Sökord från IntentRouter
        entities: Entiteter från IntentRouter
    
    Returns:
        Formaterad sträng med grafkopplingar
    """
    graph = _get_graph_store()
    
    lines = []
    search_terms = keywords + [e.split(':')[-1] if ':' in e else e for e in entities]
    
    for term in search_terms[:5]:  # Max 5 termer
        # Fuzzy match mot Entity-namn och aliases
        matches = graph.find_nodes_fuzzy(term)
        
        entity_matches = []
        for node in matches:
            if node.get('type') != 'Entity':
                continue
                
            entity_id = node['id']
            entity_type = node.get('properties', {}).get('entity_type', 'Unknown')
            aliases = node.get('aliases', [])
            
            # Hitta relaterade dokument (Units) via edges
            edges = graph.get_edges_to(entity_id, edge_type="UNIT_MENTIONS")
            related_docs = []
            for edge in edges[:3]:
                unit = graph.get_node(edge['source'])
                if unit:
                    title = unit.get('properties', {}).get('title', unit['id'][:30])
                    related_docs.append(title)
            
            match_info = f"  - {entity_id} ({entity_type})"
            if aliases:
                match_info += f" [alias: {', '.join(aliases[:3])}]"
            if related_docs:
                match_info += f"\n    → Nämns i: {', '.join(related_docs)}"
            entity_matches.append(match_info)
        
        if entity_matches:
            lines.append(f'"{term}":')
            lines.extend(entity_matches)
    
    if not lines:
        return "(Inga grafkopplingar hittades för söktermerna)"
    
    return "\n".join(lines)


def upgrade_canonical(old_canonical: str, new_canonical: str) -> bool:
    """
    Uppgradera canonical name för en Entity.
    
    Det gamla canonical-namnet flyttas till aliases[].
    Alla befintliga aliases behålls.
    KRITISKT: Uppdaterar alla kanter FÖRE nod-radering!
    
    Args:
        old_canonical: Nuvarande canonical name (id)
        new_canonical: Nytt, bättre canonical name
    
    Returns:
        True om lyckad
    
    Exempel:
        Före:  id="Cenk", aliases=["Sänk"]
        Efter: id="Cenk Bisgen", aliases=["Cenk", "Sänk"]
    """
    graph = _get_graph_store()
    return graph.upgrade_canonical(old_canonical, new_canonical)


if __name__ == "__main__":
    print("--- MyMem Graph Builder (v5.0 - DuckDB) ---")
    process_lake_batch()
