"""
MyMem Graph Builder - Bygger graf från Lake-dokument.

Använder GraphStore (DuckDB) för all graflagring.
"""

import os
import sys
import yaml
import json
import logging
import re
import time

# Lägg till projektroten i sys.path för att hitta services-paketet
# graph_builder.py ligger i services/indexers/, så vi behöver gå upp 3 nivåer för att nå projektroten
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
sys.path.insert(0, project_root)

from services.utils.graph_service import GraphStore

# --- CONFIG LOADER ---
def hitta_och_ladda_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths_to_check = [
        os.path.join(script_dir, '..', '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, 'config', 'my_mem_config.yaml'),
    ]
    config_path = None
    for p in paths_to_check:
        if os.path.exists(p):
            config_path = p
            break
    if not config_path:
        print("[GraphBuilder] CRITICAL: Config not found.")
        exit(1)
    
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    for k, v in config['paths'].items():
        config['paths'][k] = os.path.expanduser(v)
    config['logging']['log_file_path'] = os.path.expanduser(config['logging']['log_file_path'])
    return config

CONFIG = hitta_och_ladda_config()

# --- SETUP ---
LAKE_STORE = CONFIG['paths']['lake_store']
GRAPH_PATH = CONFIG['paths']['graph_db']
TAXONOMY_FILE = CONFIG['paths'].get('taxonomy_file', os.path.expanduser("~/MyMemory/Index/my_mem_taxonomy.json"))
LOG_FILE = CONFIG['logging']['log_file_path']

# Logging
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - GRAPH - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('GraphBuilder')
LOGGER.addHandler(logging.StreamHandler())

UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.md$')

# --- SINGLETON GRAPH STORE (Thread-Safe) ---
import threading
_GRAPH_INSTANCE: GraphStore | None = None
_GRAPH_LOCK = threading.Lock()


def _get_graph() -> GraphStore:
    """Thread-safe singleton-anslutning till GraphStore."""
    global _GRAPH_INSTANCE
    
    if _GRAPH_INSTANCE is None:
        with _GRAPH_LOCK:
            # Double-checked locking
            if _GRAPH_INSTANCE is None:
                # Använd read_write (default) för att undvika konflikter med read_only-anslutningar
                # read_write kan användas för både läsning och skrivning
                _GRAPH_INSTANCE = GraphStore(GRAPH_PATH, read_only=False)
    
    return _GRAPH_INSTANCE


def close_db_connection():
    """Stäng databasanslutningen explicit. Anropas vid shutdown."""
    global _GRAPH_INSTANCE
    with _GRAPH_LOCK:
        if _GRAPH_INSTANCE:
            _GRAPH_INSTANCE.close()
            _GRAPH_INSTANCE = None


def _entity_exists_in_taxonomy(entity_name: str, taxonomy: dict) -> bool:
    """
    Kolla om en entitet redan finns i någon masternod i taxonomin.
    
    Args:
        entity_name: Entitetens namn att kolla
        taxonomy: Taxonomi-dict
        
    Returns:
        True om entiteten finns i någon masternod
    """
    if not taxonomy:
        return False
    
    for master_node, data in taxonomy.items():
        if entity_name in data.get("sub_nodes", []):
            return True
    return False


# --- GRAPH ENGINE ---

def process_lake_batch():
    """Huvudloop för grafbyggande - Nu med GraphStore (DuckDB)."""
    
    # Retry-logik för att hantera DuckDB-lock-konflikter efter att tjänster stoppats
    # DuckDB kan ta tid att frigöra låset efter att processer dödats
    max_retries = 10
    retry_delay = 1.0
    graph = None
    
    for attempt in range(max_retries):
        try:
            graph = GraphStore(GRAPH_PATH, read_only=False)
            break
        except Exception as e:
            error_str = str(e).lower()
            if ("lock" in error_str or "conflicting" in error_str or "different configuration" in error_str) and attempt < max_retries - 1:
                LOGGER.warning(f"Kunde inte öppna GraphStore (försök {attempt + 1}/{max_retries}), väntar {retry_delay:.1f}s: {e}")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 5.0)  # Exponential backoff, max 5s
            else:
                LOGGER.error(f"HARDFAIL: Kunde inte öppna GraphStore efter {attempt + 1} försök: {e}", exc_info=True)
                raise
    
    if graph is None:
        raise RuntimeError("HARDFAIL: Kunde inte öppna GraphStore efter alla försök")
    
    # Använd context manager för att säkerställa korrekt stängning
    try:
        with graph:
            # Starta en enda transaktion för hela batch-processningen
            # Detta förhindrar write-write conflicts när flera filer skriver samma noder
            with graph._lock:
                graph.conn.execute("BEGIN TRANSACTION")
            
            try:
                files_processed = 0
                relations_created = 0
                
                # Ladda taxonomi en gång för hela batch-processningen
                taxonomy = {}
                try:
                    if os.path.exists(TAXONOMY_FILE):
                        with open(TAXONOMY_FILE, 'r', encoding='utf-8') as f:
                            taxonomy = json.load(f)
                        LOGGER.debug(f"Taxonomi laddad: {len(taxonomy)} masternoder")
                except Exception as e:
                    LOGGER.warning(f"Kunde inte ladda taxonomi för entity-check: {e}")
                    taxonomy = {}
                
                print(f"🔍 Scannar {LAKE_STORE}...")

                for filename in os.listdir(LAKE_STORE):
                    if not filename.endswith(".md"):
                        continue
                    
                    match = UUID_SUFFIX_PATTERN.search(filename)
                    unit_id = match.group(1) if match else None
                    if not unit_id:
                        continue
                    
                    filepath = os.path.join(LAKE_STORE, filename)
                    
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            content = f.read()
                        if "---" not in content:
                            continue
                        parts = content.split("---", 2)
                        metadata = yaml.safe_load(parts[1])
                        
                        # Metadata Extraction
                        timestamp = metadata.get('timestamp_created') or ""
                        source_type = metadata.get('source_type') or "unknown"
                        summary = (metadata.get('summary') or "").replace('"', "'")
                        
                        # 1. Skapa Dokument-noden (Unit)
                        graph.upsert_node(
                            id=unit_id,
                            type="Unit",
                            properties={
                                "timestamp": timestamp,
                                "source_type": source_type,
                                "summary": summary[:500] if summary else ""  # Begränsa längd
                            }
                        )

                        # 2. Skapa Relationer baserat på EXPLICIT DATA
                        
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
                                graph.upsert_edge(
                                    source=unit_id, 
                                    target=node_key, 
                                    edge_type="DEALS_WITH",
                                    properties={"weight": node_value}
                                )
                                relations_created += 1
                            
                            # Typad entitet - värde är dict med namn -> relevans
                            elif isinstance(node_value, dict):
                                entity_type = node_key  # Typ från taxonomin
                                for entity_name, relevance in node_value.items():
                                    # Kolla om entiteten redan finns i taxonomin
                                    if _entity_exists_in_taxonomy(entity_name, taxonomy):
                                        LOGGER.debug(f"Skippar '{entity_name}' - finns redan i taxonomin")
                                        continue  # Hoppa över - entiteten är redan klar
                                    
                                    # Skapa Entity-nod
                                    graph.upsert_node(
                                        id=entity_name,
                                        type="Entity",
                                        properties={"entity_type": entity_type}
                                    )
                                    # Skapa relation Unit -> Entity
                                    graph.upsert_edge(
                                        source=unit_id,
                                        target=entity_name,
                                        edge_type="UNIT_MENTIONS",
                                        properties={"relevance": relevance}
                                    )
                                    relations_created += 1

                        # C. PERSON (Ägare)
                        owner = metadata.get('owner_id')
                        if owner:
                            graph.upsert_node(id=owner, type="Person")
                            graph.upsert_edge(
                                source=unit_id,
                                target=owner,
                                edge_type="CREATED_BY"
                            )
                        
                        files_processed += 1

                    except Exception as e:
                        LOGGER.error(f"Fel vid graf-processning av {filename}: {e}")
                        # Fortsätt med nästa fil även om denna misslyckades
                        continue
                
                # Commit transaktionen när alla filer är processade
                with graph._lock:
                    graph.conn.execute("COMMIT")
                print(f"✅ Klar. {files_processed} filer bearbetade. {relations_created} relationer skapade.")
            
            except Exception as e:
                # Rollback vid fel
                with graph._lock:
                    graph.conn.execute("ROLLBACK")
                LOGGER.error(f"Fel i batch-transaktion: {e}", exc_info=True)
                raise
    
    except Exception as main_e:
        LOGGER.error(f"Kritiskt fel i Graf-loopen: {main_e}")
        raise


# === ENTITY FUNCTIONS ===

def get_entity(canonical: str) -> dict | None:
    """
    Hämta en Entity från grafen.
    
    Returns:
        dict med id, type, aliases eller None
    """
    try:
        graph = _get_graph()
        node = graph.get_node(canonical)
        
        if not node or node.get('type') != 'Entity':
            return None
        
        return {
            "id": node['id'],
            "type": node.get('properties', {}).get('entity_type', 'Unknown'),
            "aliases": node.get('aliases', [])
        }
    except Exception as e:
        LOGGER.error(f"get_entity error: {e}")
        return None


def get_canonical_from_graph(variant: str) -> str | None:
    """
    Slå upp canonical name för ett alias i grafen.
    
    Args:
        variant: Alias eller canonical name
    
    Returns:
        Canonical name eller None
    """
    try:
        graph = _get_graph()
        
        # Kolla om det är ett alias
        matches = graph.find_nodes_by_alias(variant)
        if matches:
            return matches[0]['id']
        
        # Kolla om det är ett canonical name (direkt ID)
        node = graph.get_node(variant)
        if node and node.get('type') == 'Entity':
            return node['id']
        
        return None
    except Exception as e:
        LOGGER.error(f"get_canonical_from_graph error: {e}")
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
    try:
        graph = _get_graph()
        
        # Hämta befintlig nod
        node = graph.get_node(canonical)
        
        if node:
            # Lägg till alias om det inte redan finns
            existing_aliases = node.get('aliases', [])
            if alias not in existing_aliases:
                new_aliases = existing_aliases + [alias]
                graph.upsert_node(
                    id=canonical,
                    type="Entity",
                    aliases=new_aliases,
                    properties=node.get('properties', {"entity_type": entity_type})
                )
                LOGGER.info(f"Lade till alias '{alias}' för '{canonical}'")
        else:
            # Skapa ny Entity
            graph.upsert_node(
                id=canonical,
                type="Entity",
                aliases=[alias],
                properties={"entity_type": entity_type}
            )
            LOGGER.info(f"Skapade Entity '{canonical}' med alias '{alias}'")
        
        return True
    except Exception as e:
        LOGGER.error(f"add_entity_alias error: {e}")
        return False


def get_all_entities() -> list:
    """
    Hämta alla Entity-noder från grafen.
    
    Returns:
        Lista med {id, type, aliases}
    """
    try:
        graph = _get_graph()
        entities = graph.find_nodes_by_type("Entity")
        
        result = []
        for entity in entities:
            result.append({
                "id": entity['id'],
                "type": entity.get('properties', {}).get('entity_type', 'Unknown'),
                "aliases": entity.get('aliases', [])
            })
        
        return result
    except Exception as e:
        LOGGER.error(f"get_all_entities error: {e}")
        return []


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
    try:
        graph = _get_graph()
        
        lines = []
        search_terms = keywords + [e.split(':')[-1] if ':' in e else e for e in entities]
        
        for term in search_terms[:5]:  # Max 5 termer
            # Fuzzy match mot Entity-namn och aliases
            matches = graph.find_nodes_fuzzy(term, limit=3)
            
            match_lines = []
            for entity in matches:
                if entity.get('type') != 'Entity':
                    continue
                
                entity_id = entity['id']
                entity_type = entity.get('properties', {}).get('entity_type', 'Unknown')
                aliases = entity.get('aliases', [])
                
                # Hitta relaterade dokument (Units)
                related_units = graph.get_related_units(entity_id, limit=3)
                
                match_info = f"  - {entity_id} ({entity_type})"
                if aliases:
                    match_info += f" [alias: {', '.join(aliases[:3])}]"
                if related_units:
                    match_info += f"\n    → Nämns i: {len(related_units)} dokument"
                match_lines.append(match_info)
            
            if match_lines:
                lines.append(f'"{term}":')
                lines.extend(match_lines)
        
        if not lines:
            return "(Inga grafkopplingar hittades för söktermerna)"
        
        return "\n".join(lines)
    
    except Exception as e:
        LOGGER.error(f"get_graph_context_for_search error: {e}")
        return f"(Kunde inte hämta grafkontext: {e})"


def upgrade_canonical(old_canonical: str, new_canonical: str) -> bool:
    """
    Uppgradera canonical name för en Entity.
    
    Det gamla canonical-namnet flyttas till aliases[].
    Alla befintliga aliases behålls.
    
    Args:
        old_canonical: Nuvarande canonical name (id)
        new_canonical: Nytt, bättre canonical name
    
    Returns:
        True om lyckad
    
    Exempel:
        Före:  id="Cenk", aliases=["Sänk"]
        Efter: id="Cenk Bisgen", aliases=["Cenk", "Sänk"]
    """
    try:
        graph = _get_graph()
        
        # Hämta befintlig entity
        node = graph.get_node(old_canonical)
        
        if not node:
            LOGGER.warning(f"Entity '{old_canonical}' finns inte - kan inte uppgradera")
            return False
        
        entity_type = node.get('properties', {}).get('entity_type', 'Unknown')
        existing_aliases = node.get('aliases', [])
        
        # Bygg ny alias-lista: gamla canonical + befintliga aliases
        new_aliases = [old_canonical] + [a for a in existing_aliases if a != new_canonical]
        
        # Ta bort gamla entity
        graph.delete_node(old_canonical)
        
        # Skapa ny entity med uppgraderat id
        graph.upsert_node(
            id=new_canonical,
            type="Entity",
            aliases=new_aliases,
            properties={"entity_type": entity_type}
        )
        
        LOGGER.info(f"Uppgraderade canonical: '{old_canonical}' -> '{new_canonical}'")
        return True
        
    except Exception as e:
        LOGGER.error(f"upgrade_canonical error: {e}")
        return False


if __name__ == "__main__":
    print("--- MyMem Graph Builder (v5.0 - DuckDB) ---")
    try:
        process_lake_batch()
        sys.exit(0)
    except KeyboardInterrupt:
        print("\n⚠️ Avbruten av användaren")
        LOGGER.warning("Graph builder avbruten av användaren")
        sys.exit(130)  # Standard exit code för Ctrl+C
    except Exception as e:
        error_msg = f"KRITISKT FEL i Graph Builder: {e}"
        print(f"❌ {error_msg}")
        LOGGER.error(error_msg, exc_info=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
