import os
import time
import yaml
import shutil
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
    # Sök uppåt i hierarkin efter config
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
# OBS: Använder nu korrekta nycklar från din config
LAKE_DIR = CONFIG['paths']['lake_store']     # Var Lake-filerna ligger
CHROMA_PATH = CONFIG['paths']['vector_db'] # Var databasen ligger
LOG_FILE = CONFIG['logging']['log_file_path']

# Mapp för trasiga filer (Karantän)
FAILED_DIR = os.path.join(LAKE_DIR, "_failed")
os.makedirs(FAILED_DIR, exist_ok=True)

# Regex för att hitta UUID i filnamn (t.ex. "Filnamn_UUID.md")
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.md$')

# Tidszon
TZ_NAME = CONFIG.get('system', {}).get('timezone', 'UTC')
try:
    SYSTEM_TZ = zoneinfo.ZoneInfo(TZ_NAME)
except Exception as e:
    print(f"[CRITICAL] HARDFAIL: Ogiltig timezone '{TZ_NAME}': {e}")
    exit(1)

# Logging Setup
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE, 
    level=logging.INFO, 
    format='%(asctime)s - VECTOR - %(levelname)s - %(message)s'
)
LOGGER = logging.getLogger('VectorIndexer')

# --- HELPERS ---
def _ts():
    return datetime.datetime.now(SYSTEM_TZ).strftime("[%H:%M:%S]")

def _kort(filnamn, max_len=30):
    if len(filnamn) <= max_len: return filnamn
    return filnamn[:15] + "..." + filnamn[-10:]

# --- CHROMA INIT ---
print(f"{_ts()} ⚙️  Connecting to ChromaDB at {CHROMA_PATH}...")
try:
    CHROMA_CLIENT = chromadb.PersistentClient(path=CHROMA_PATH)
    EMBEDDING_FUNC = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    # VIKTIGT: Vi använder standardnamnet "knowledge_base" för att matcha MCP-servern
    VECTOR_COLLECTION = CHROMA_CLIENT.get_or_create_collection(name="knowledge_base", embedding_function=EMBEDDING_FUNC)
    print(f"{_ts()} ✅ Connected. Collection size: {VECTOR_COLLECTION.count()}")
except Exception as e:
    print(f"{_ts()} ❌ CRITICAL: Could not connect to ChromaDB: {e}")
    exit(1)

# --- CORE LOGIC ---

def quarantine_file(filväg, orsak):
    """Flyttar en trasig fil till _failed-mappen."""
    filnamn = os.path.basename(filväg)
    dest = os.path.join(FAILED_DIR, filnamn)
    try:
        shutil.move(filväg, dest)
        LOGGER.warning(f"Quarantined {filnamn}: {orsak}")
        print(f"{_ts()} 🗑️  QUARANTINE: {_kort(filnamn)} -> Moved to _failed")
    except Exception as e:
        LOGGER.error(f"Failed to quarantine {filnamn}: {e}")

def indexera_fil(filväg):
    filnamn = os.path.basename(filväg)
    
    # 1. Validation: Filnamn & UUID
    match = UUID_SUFFIX_PATTERN.search(filnamn)
    unit_id = match.group(1) if match else None
    
    if not unit_id:
        # Om filen inte har ett UUID, ignorerar vi den bara (kanske en readme eller tmp-fil)
        return False

    try:
        # 2. Läs fil
        with open(filväg, 'r', encoding='utf-8') as f: 
            content = f.read()
        
        # 3. Validation: Frontmatter
        if not content.startswith("---"):
            raise ValueError("Saknar YAML frontmatter (---)")
            
        parts = content.split("---", 2)
        if len(parts) < 3:
            raise ValueError("Ogiltigt format (Saknar body efter frontmatter)")
        
        try:
            metadata = yaml.safe_load(parts[1])
        except yaml.YAMLError as e:
            raise ValueError(f"Trasig YAML: {e}")

        text = parts[2].strip()
        if not text:
            raise ValueError("Tom body text")
        
        # 4. Förbered Data
        ai_summary = metadata.get('ai_summary') or ""
        timestamp = metadata.get('timestamp_created') or ""
        
        # Inkludera graf-kontext om den finns
        graph_ctx = metadata.get("graph_context_summary")
        context_block = f"KONTEXT (Grafrelationer):\n{graph_ctx}\n\n" if graph_ctx else ""
        
        full_doc = f"{context_block}FILENAME: {filnamn}\nSUMMARY: {ai_summary}\n\nCONTENT:\n{text[:8000]}"

        # 5. Upsert till Chroma
        VECTOR_COLLECTION.upsert(
            ids=[unit_id],
            documents=[full_doc],
            metadatas=[{
                "timestamp": str(timestamp),
                "filename": filnamn,
                "type": metadata.get('type', 'Unknown'),
                "name": metadata.get('name', filnamn) # För sökbarhet i MCP
            }]
        )
        
        print(f"{_ts()} ✅ INDEX: {_kort(filnamn)}")
        LOGGER.info(f"Indexerad: {filnamn} ({unit_id})")
        return True

    except Exception as e:
        print(f"{_ts()} ❌ FAIL: {_kort(filnamn)} - {str(e)}")
        # HÄR ÄR DIN LOGIK: Om det misslyckas -> Karantän
        quarantine_file(filväg, str(e))
        return False

def perform_delta_sync():
    """Jämför Lake-filer med ChromaDB och indexerar det som saknas."""
    print(f"{_ts()} 🔄 Starting Delta Sync...")
    
    # 1. Hämta alla filer i Lake
    lake_files = {} # {uuid: full_path}
    try:
        for f in os.listdir(LAKE_DIR):
            if f.endswith(".md"):
                match = UUID_SUFFIX_PATTERN.search(f)
                if match:
                    lake_files[match.group(1)] = os.path.join(LAKE_DIR, f)
    except FileNotFoundError:
        print(f"{_ts()} ❌ Lake directory not found: {LAKE_DIR}")
        return

    # 2. Hämta alla IDn i Chroma
    try:
        db_data = VECTOR_COLLECTION.get(include=[]) # Hämta bara IDs
        existing_ids = set(db_data['ids'])
    except Exception:
        existing_ids = set()

    # 3. Räkna ut diff
    missing_ids = set(lake_files.keys()) - existing_ids
    
    if not missing_ids:
        print(f"{_ts()} ✨ System is in sync. No backlog.")
        return

    print(f"{_ts()} ⚠️  Found {len(missing_ids)} unindexed files. Processing backlog...")
    
    # 4. Bearbeta backlog
    success_count = 0
    fail_count = 0
    
    for uid in missing_ids:
        path = lake_files[uid]
        if indexera_fil(path):
            success_count += 1
        else:
            fail_count += 1
            
    print(f"{_ts()} 🏁 Delta Sync Complete. Added: {success_count}, Failed/Moved: {fail_count}")

# --- WATCHER ---

class VectorHandler(FileSystemEventHandler):
    def __init__(self):
        self.processed_cache = set() # Enkel debounce

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            self._try_process(event.src_path)

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            self._try_process(event.src_path)

    def _try_process(self, path):
        # Debounce: Om vi nyss körde den, vänta lite
        if path in self.processed_cache: return
        
        time.sleep(0.5) # Låt filen skrivas klart
        
        if indexera_fil(path):
            self.processed_cache.add(path)
            # Rensa cachen lite då och då så vi inte äter RAM
            if len(self.processed_cache) > 1000:
                self.processed_cache.clear()

if __name__ == "__main__":
    # 1. Kör Delta Sync först (Självläkning)
    perform_delta_sync()
    
    # 2. Starta Watcher
    print(f"{_ts()} 👀 Vector Indexer watching: {LAKE_DIR}")
    observer = Observer()
    observer.schedule(VectorHandler(), LAKE_DIR, recursive=False)
    observer.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: observer.stop()
    observer.join()