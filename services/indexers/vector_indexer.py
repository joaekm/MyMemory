import os
import time
import yaml
import logging
import datetime
import chromadb
import re
import zoneinfo
from chromadb.utils import embedding_functions
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- CONFIG LOADER ---
def hitta_och_ladda_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Sök uppåt i hierarkin
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
        print("[VectorIndexer] CRITICAL: Config not found.")
        exit(1)
    
    with open(config_path, 'r') as f: config = yaml.safe_load(f)
    
    # Expand paths
    for k, v in config['paths'].items():
        config['paths'][k] = os.path.expanduser(v)
    config['logging']['log_file_path'] = os.path.expanduser(config['logging']['log_file_path'])
    return config

CONFIG = hitta_och_ladda_config()

# --- SETUP ---
LAKE_STORE = CONFIG['paths']['lake_store']
CHROMA_PATH = CONFIG['paths']['chroma_db']
LOG_FILE = CONFIG['logging']['log_file_path']

UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.md$')

# Tidszon
TZ_NAME = CONFIG.get('system', {}).get('timezone', 'UTC')
try:
    SYSTEM_TZ = zoneinfo.ZoneInfo(TZ_NAME)
except Exception as e:
    print(f"[CRITICAL] HARDFAIL: Ogiltig timezone '{TZ_NAME}': {e}")
    exit(1)

# Logging (endast till fil, tight logging till konsol)
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - VECTOR - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('VectorIndexer')

# --- TIGHT LOGGING ---
def _ts():
    return datetime.datetime.now(SYSTEM_TZ).strftime("[%H:%M:%S]")

def _kort(filnamn, max_len=25):
    if len(filnamn) <= max_len:
        return filnamn
    return "..." + filnamn[-(max_len-3):]

# Chroma Init (tyst)
os.makedirs(CHROMA_PATH, exist_ok=True)
CHROMA_CLIENT = chromadb.PersistentClient(path=CHROMA_PATH)
EMBEDDING_FUNC = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
VECTOR_COLLECTION = CHROMA_CLIENT.get_or_create_collection(name="dfm_knowledge_base", embedding_function=EMBEDDING_FUNC)

def indexera_vektor(filväg, filnamn):
    try:
        # 1. Validation
        match = UUID_SUFFIX_PATTERN.search(filnamn)
        unit_id = match.group(1) if match else None
        if not unit_id:
            return False  # Ignorera icke-standard filer tyst

        # 2. Läs fil
        with open(filväg, 'r', encoding='utf-8') as f: content = f.read()
        if not content.startswith("---"): return False
        parts = content.split("---", 2)
        if len(parts) < 3: return False
        
        metadata = yaml.safe_load(parts[1])
        text = parts[2].strip()
        
        # 3. Upsert Chroma
        ai_summary = metadata.get('ai_summary') or ""
        timestamp = metadata.get('timestamp_created') or ""
        
        full_doc = f"FILENAME: {filnamn}\nSUMMARY: {ai_summary}\n\nCONTENT:\n{text[:8000]}"

        VECTOR_COLLECTION.upsert(
            ids=[unit_id],
            documents=[full_doc],
            metadatas=[{
                "timestamp": timestamp,
                "filename": filnamn
            }]
        )
        
        print(f"{_ts()} ✅ INDEX: {_kort(filnamn)} → ChromaDB")
        LOGGER.info(f"Indexerad: {filnamn} ({unit_id})")
        return True

    except Exception as e:
        print(f"{_ts()} ❌ INDEX: {_kort(filnamn)} → FAILED (se logg)")
        LOGGER.error(f"Fel vid vektor-indexering av {filnamn}: {e}")
        return False

class VectorHandler(FileSystemEventHandler):
    def __init__(self):
        self.indexed_files = set()  # Spåra indexerade filer denna session

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            self._process(event.src_path)

    def on_modified(self, event):
        # Ignorera modified om filen redan indexerats denna session
        if not event.is_directory and event.src_path.endswith(".md"):
            if event.src_path not in self.indexed_files:
                self._process(event.src_path)

    def _process(self, path):
        if path in self.indexed_files:
            return
        time.sleep(0.5)  # Vänta på att filen skrivs klart
        if indexera_vektor(path, os.path.basename(path)):
            self.indexed_files.add(path)

if __name__ == "__main__":
    print(f"{_ts()} ✓ Vector Indexer online")
    observer = Observer()
    observer.schedule(VectorHandler(), LAKE_STORE, recursive=False)
    observer.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: observer.stop()
    observer.join()