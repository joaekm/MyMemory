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
                
                # A. MASTER NODE (Taxonomi) - The Critical Fix!
                master_node = metadata.get('graph_master_node')
                if master_node and master_node != "Okategoriserat":
                    # Skapa Koncept-noden om den inte finns
                    conn.execute(f'MERGE (c:Concept {{id: "{master_node}"}})')
                    # Skapa relationen
                    conn.execute(f'MATCH (u:Unit {{id: "{unit_id}"}}), (c:Concept {{id: "{master_node}"}}) MERGE (u)-[:DEALS_WITH]->(c)')
                    relations_created += 1

                # B. SUB NODE (Finlir)
                sub_node = metadata.get('graph_sub_node')
                if sub_node:
                    conn.execute(f'MERGE (c:Concept {{id: "{sub_node}"}})')
                    conn.execute(f'MATCH (u:Unit {{id: "{unit_id}"}}), (c:Concept {{id: "{sub_node}"}}) MERGE (u)-[:DEALS_WITH]->(c)')

                # C. CONTEXT ID (Projekt/Samling)
                context_id = metadata.get('context_id')
                if context_id and context_id != "INKORG":
                    conn.execute(f'MERGE (c:Concept {{id: "{context_id}"}})')
                    conn.execute(f'MATCH (u:Unit {{id: "{unit_id}"}}), (c:Concept {{id: "{context_id}"}}) MERGE (u)-[:PART_OF]->(c)')

                # D. PERSON (Ägare)
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
        "CREATE REL TABLE PART_OF(FROM Unit TO Concept)",
        "PART_OF")
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


if __name__ == "__main__":
    print("--- MyMem Graph Builder (v4.1 - Explicit Tagging) ---")
    process_lake_batch()