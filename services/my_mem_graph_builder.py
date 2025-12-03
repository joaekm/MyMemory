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
        except: pass

def _init_schema(conn):
    try: conn.execute("CREATE NODE TABLE Unit(id STRING, timestamp STRING, type STRING, summary STRING, PRIMARY KEY (id))")
    except: pass
    try: conn.execute("CREATE NODE TABLE Concept(id STRING, PRIMARY KEY (id))")
    except: pass
    try: conn.execute("CREATE NODE TABLE Person(id STRING, PRIMARY KEY (id))")
    except: pass
    
    # Relationer
    try: conn.execute("CREATE REL TABLE DEALS_WITH(FROM Unit TO Concept)")
    except: pass
    try: conn.execute("CREATE REL TABLE PART_OF(FROM Unit TO Concept)") # Ny relation för Context!
    except: pass
    try: conn.execute("CREATE REL TABLE CREATED_BY(FROM Unit TO Person)")
    except: pass

if __name__ == "__main__":
    print("--- MyMem Graph Builder (v4.1 - Explicit Tagging) ---")
    process_lake_batch()