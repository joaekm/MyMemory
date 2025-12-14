import os
import time
import yaml
import json
import logging
import kuzu
import re

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
KUZU_PATH = CONFIG['paths']['kuzu_db']
TAXONOMY_FILE = CONFIG['paths'].get('taxonomy_file', os.path.expanduser("~/MyMemory/Index/my_mem_taxonomy.json"))
LOG_FILE = CONFIG['logging']['log_file_path']

# Logging
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - GRAPH - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('GraphBuilder')
LOGGER.addHandler(logging.StreamHandler())

UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.md$')

# --- GRAPH ENGINE ---

def process_lake_batch():
    """Huvudloop för grafbyggande - Nu med DIRECT LINKING (v4.1)"""
    
    db = None
    conn = None
    
    try:
        os.makedirs(os.path.dirname(KUZU_PATH), exist_ok=True)
        db = kuzu.Database(KUZU_PATH)
        conn = kuzu.Connection(db)
        
        _init_schema(conn)
        
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
                
                # 1. Skapa Dokument-noden (Unit)
                # MERGE garanterar att vi inte skapar dubbletter, men uppdaterar egenskaper
                conn.execute(f'MERGE (u:Unit {{id: "{unit_id}"}}) SET u.timestamp = "{timestamp}", u.type = "{source_type}", u.summary = "{summary}"')

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
                        conn.execute(f'MERGE (c:Concept {{id: "{node_key}"}})')
                        conn.execute(f'MATCH (u:Unit {{id: "{unit_id}"}}), (c:Concept {{id: "{node_key}"}}) MERGE (u)-[:DEALS_WITH]->(c)')
                        relations_created += 1
                    
                    # Typad entitet - värde är dict med namn -> relevans
                    elif isinstance(node_value, dict):
                        entity_type = node_key  # Typ från taxonomin
                        for entity_name, relevance in node_value.items():
                            # Escape quotes i entity_name
                            safe_name = entity_name.replace('"', '\\"')
                            # Skapa Entity-nod om den inte finns
                            conn.execute(f'MERGE (e:Entity {{id: "{safe_name}"}}) SET e.type = "{entity_type}"')
                            # Skapa relation Unit -> Entity
                            conn.execute(f'MATCH (u:Unit {{id: "{unit_id}"}}), (e:Entity {{id: "{safe_name}"}}) MERGE (u)-[:UNIT_MENTIONS]->(e)')

                # C. PERSON (Ägare)
                owner = metadata.get('owner_id')
                if owner:
                    conn.execute(f'MERGE (p:Person {{id: "{owner}"}})')
                    conn.execute(f'MATCH (u:Unit {{id: "{unit_id}"}}), (p:Person {{id: "{owner}"}}) MERGE (u)-[:CREATED_BY]->(p)')
                
                files_processed += 1

            except Exception as e:
                LOGGER.error(f"Fel vid graf-processning av {filename}: {e}")

        print(f"✅ Klar. {files_processed} filer bearbetade. {relations_created} master-kopplingar skapade.")

    except Exception as main_e:
        LOGGER.error(f"Kritiskt fel i Graf-loopen: {main_e}")

    finally:
        try:
            if conn: del conn
            if db: del db
            import gc
            gc.collect()
        except Exception as e:
            LOGGER.error(f"HARDFAIL: Kunde inte städa upp databasanslutning: {e}")
            raise RuntimeError(f"HARDFAIL: Kunde inte städa upp databasanslutning: {e}") from e

def _safe_create_table(conn, create_statement: str, table_name: str):
    """
    Skapa tabell om den inte finns. Hardfail vid andra fel.
    
    Kuzu kastar exception om tabellen redan finns - detta är OK.
    Alla ANDRA fel ska hardfaila.
    """
    try:
        conn.execute(create_statement)
        LOGGER.info(f"Skapade tabell: {table_name}")
    except Exception as e:
        error_str = str(e).lower()
        # Kända "OK"-fel: tabellen finns redan
        if "already exists" in error_str or "duplicate" in error_str or "catalog exception" in error_str:
            LOGGER.debug(f"Tabell finns redan: {table_name}")
        else:
            LOGGER.error(f"HARDFAIL: Kunde inte skapa tabell {table_name}: {e}")
            raise RuntimeError(f"HARDFAIL: Kunde inte skapa tabell {table_name}: {e}") from e


def _init_schema(conn):
    # Befintliga tabeller
    _safe_create_table(conn, 
        "CREATE NODE TABLE Unit(id STRING, timestamp STRING, type STRING, summary STRING, PRIMARY KEY (id))",
        "Unit")
    _safe_create_table(conn,
        "CREATE NODE TABLE Concept(id STRING, PRIMARY KEY (id))",
        "Concept")
    _safe_create_table(conn,
        "CREATE NODE TABLE Person(id STRING, PRIMARY KEY (id))",
        "Person")
    
    # NY: Entity-tabell med aliases (för alla entitetstyper)
    # type: Kategori från taxonomin (Person, Aktör, Projekt, etc.)
    # aliases: Lista med alternativa namn (smeknamn, förkortningar, felstavningar)
    _safe_create_table(conn,
        "CREATE NODE TABLE Entity(id STRING, type STRING, aliases STRING[], PRIMARY KEY (id))",
        "Entity")
    
    # Befintliga relationer
    _safe_create_table(conn,
        "CREATE REL TABLE DEALS_WITH(FROM Unit TO Concept)",
        "DEALS_WITH")
    _safe_create_table(conn,
        "CREATE REL TABLE CREATED_BY(FROM Unit TO Person)",
        "CREATED_BY")
    
    # NY: Relationer för Entity
    _safe_create_table(conn,
        "CREATE REL TABLE UNIT_MENTIONS(FROM Unit TO Entity)",
        "UNIT_MENTIONS")


# === ENTITY FUNCTIONS (Aliases i grafen) ===

# Singleton för databas-anslutning (KRITISKT: Undvik att öppna/stänga konstant)
_DB_INSTANCE = None
_CONN_INSTANCE = None
_SCHEMA_INITIALIZED = False

def _get_db_connection():
    """Singleton-anslutning till KuzuDB. Återanvänds för alla queries."""
    global _DB_INSTANCE, _CONN_INSTANCE, _SCHEMA_INITIALIZED
    
    if _CONN_INSTANCE is not None:
        return _DB_INSTANCE, _CONN_INSTANCE
    
    _DB_INSTANCE = kuzu.Database(KUZU_PATH)
    _CONN_INSTANCE = kuzu.Connection(_DB_INSTANCE)
    
    if not _SCHEMA_INITIALIZED:
        _init_schema(_CONN_INSTANCE)
        _SCHEMA_INITIALIZED = True
    
    return _DB_INSTANCE, _CONN_INSTANCE


def close_db_connection():
    """Stäng databas-anslutningen explicit. Anropas vid shutdown."""
    global _DB_INSTANCE, _CONN_INSTANCE, _SCHEMA_INITIALIZED
    if _CONN_INSTANCE:
        del _CONN_INSTANCE
        _CONN_INSTANCE = None
    if _DB_INSTANCE:
        del _DB_INSTANCE
        _DB_INSTANCE = None
    _SCHEMA_INITIALIZED = False
    import gc; gc.collect()


def get_entity(canonical: str) -> dict:
    """
    Hämta en Entity från grafen.
    
    Returns:
        dict med id, type, aliases eller None
    """
    try:
        _, conn = _get_db_connection()
        
        result = conn.execute(
            "MATCH (e:Entity {id: $id}) RETURN e.id, e.type, e.aliases",
            {"id": canonical}
        )
        
        while result.has_next():
            row = result.get_next()
            return {
                "id": row[0],
                "type": row[1],
                "aliases": row[2] or []
            }
        return None
    except Exception as e:
        LOGGER.error(f"get_entity error: {e}")
        return None


def get_canonical_from_graph(variant: str) -> str:
    """
    Slå upp canonical name för ett alias i grafen.
    
    Args:
        variant: Alias eller canonical name
    
    Returns:
        Canonical name eller None
    """
    try:
        _, conn = _get_db_connection()
        
        # Kolla om det är ett alias
        result = conn.execute(
            "MATCH (e:Entity) WHERE list_contains(e.aliases, $variant) RETURN e.id",
            {"variant": variant}
        )
        
        while result.has_next():
            return result.get_next()[0]
        
        # Kolla om det är ett canonical name
        result = conn.execute(
            "MATCH (e:Entity {id: $id}) RETURN e.id",
            {"id": variant}
        )
        
        while result.has_next():
            return result.get_next()[0]
        
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
        _, conn = _get_db_connection()
        
        # Kolla om Entity finns
        result = conn.execute(
            "MATCH (e:Entity {id: $id}) RETURN e.aliases",
            {"id": canonical}
        )
        
        existing_aliases = None
        while result.has_next():
            existing_aliases = result.get_next()[0] or []
        
        if existing_aliases is not None:
            # Lägg till alias om det inte redan finns
            if alias not in existing_aliases:
                new_aliases = existing_aliases + [alias]
                conn.execute(
                    "MATCH (e:Entity {id: $id}) SET e.aliases = $aliases",
                    {"id": canonical, "aliases": new_aliases}
                )
                LOGGER.info(f"Lade till alias '{alias}' för '{canonical}'")
        else:
            # Skapa ny Entity
            conn.execute(
                "CREATE (e:Entity {id: $id, type: $type, aliases: $aliases})",
                {"id": canonical, "type": entity_type, "aliases": [alias]}
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
        _, conn = _get_db_connection()
        
        result = conn.execute("MATCH (e:Entity) RETURN e.id, e.type, e.aliases")
        
        entities = []
        while result.has_next():
            row = result.get_next()
            entities.append({
                "id": row[0],
                "type": row[1],
                "aliases": row[2] or []
            })
        
        return entities
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
        _, conn = _get_db_connection()
        
        lines = []
        search_terms = keywords + [e.split(':')[-1] if ':' in e else e for e in entities]
        
        for term in search_terms[:5]:  # Max 5 termer
            # Fuzzy match mot Entity-namn och aliases
            result = conn.execute("""
                MATCH (e:Entity)
                WHERE e.id CONTAINS $term OR list_any(e.aliases, x -> x CONTAINS $term)
                RETURN e.id, e.type, e.aliases
                LIMIT 3
            """, {"term": term})
            
            matches = []
            while result.has_next():
                row = result.get_next()
                entity_id = row[0]
                entity_type = row[1]
                aliases = row[2] or []
                
                # Hitta relaterade dokument (Units)
                rel_result = conn.execute("""
                    MATCH (u:Unit)-[:UNIT_MENTIONS]->(e:Entity {id: $id})
                    RETURN u.title
                    LIMIT 3
                """, {"id": entity_id})
                
                related_docs = []
                while rel_result.has_next():
                    doc_row = rel_result.get_next()
                    if doc_row[0]:
                        related_docs.append(doc_row[0][:30])
                
                match_info = f"  - {entity_id} ({entity_type})"
                if aliases:
                    match_info += f" [alias: {', '.join(aliases[:3])}]"
                if related_docs:
                    match_info += f"\n    → Nämns i: {', '.join(related_docs)}"
                matches.append(match_info)
            
            if matches:
                lines.append(f'"{term}":')
                lines.extend(matches)
        
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
        _, conn = _get_db_connection()
        
        # Hämta befintlig entity
        result = conn.execute(
            "MATCH (e:Entity {id: $id}) RETURN e.type, e.aliases",
            {"id": old_canonical}
        )
        
        entity_type = None
        existing_aliases = []
        while result.has_next():
            row = result.get_next()
            entity_type = row[0]
            existing_aliases = row[1] or []
        
        if entity_type is None:
            LOGGER.warning(f"Entity '{old_canonical}' finns inte - kan inte uppgradera")
            return False
        
        # Bygg ny alias-lista: gamla canonical + befintliga aliases
        new_aliases = [old_canonical] + [a for a in existing_aliases if a != new_canonical]
        
        # Ta bort gamla entity
        conn.execute(
            "MATCH (e:Entity {id: $id}) DELETE e",
            {"id": old_canonical}
        )
        
        # Skapa ny entity med uppgraderat id
        conn.execute(
            "CREATE (e:Entity {id: $id, type: $type, aliases: $aliases})",
            {"id": new_canonical, "type": entity_type, "aliases": new_aliases}
        )
        
        LOGGER.info(f"Uppgraderade canonical: '{old_canonical}' -> '{new_canonical}'")
        return True
        
    except Exception as e:
        LOGGER.error(f"upgrade_canonical error: {e}")
        return False


if __name__ == "__main__":
    print("--- MyMem Graph Builder (v4.1 - Explicit Tagging) ---")
    process_lake_batch()