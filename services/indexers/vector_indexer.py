import os
import time
import yaml
import logging
import datetime
import re
import zoneinfo
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import sys

# Path setup
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from services.utils.vector_service import get_vector_service

# --- CONFIG LOADER ---
def hitta_och_ladda_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths_to_check = [
        os.path.join(script_dir, '..', '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, 'config', 'my_mem_config.yaml'),
    ]
    for p in paths_to_check:
        if os.path.exists(p):
            with open(p, 'r') as f:
                conf = yaml.safe_load(f)
                for k, v in conf['paths'].items():
                    conf['paths'][k] = os.path.expanduser(v)
                return conf
    print("[VectorIndexer] CRITICAL: Config not found.")
    exit(1)

CONFIG = hitta_och_ladda_config()
LAKE_STORE = CONFIG['paths']['lake_store']
LOG_FILE = os.path.expanduser(CONFIG['logging']['log_file_path'])
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.md$')

# Tidszon
TZ_NAME = CONFIG.get('system', {}).get('timezone', 'UTC')
try:
    SYSTEM_TZ = zoneinfo.ZoneInfo(TZ_NAME)
except Exception:
    SYSTEM_TZ = zoneinfo.ZoneInfo("UTC")

# Logging
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - VECTOR - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('VectorIndexer')

def _ts(): return datetime.datetime.now(SYSTEM_TZ).strftime("[%H:%M:%S]")
def _kort(f, l=25): return f if len(f)<=l else "..." + f[-(l-3):]

# Initiera Service
try:
    VECTOR_SERVICE = get_vector_service("knowledge_base")
except Exception as e:
    print(f"[CRITICAL] Kunde inte initiera VectorService: {e}")
    exit(1)

def indexera_vektor(filväg, filnamn):
    try:
        # 1. Validation
        match = UUID_SUFFIX_PATTERN.search(filnamn)
        unit_id = match.group(1) if match else None
        if not unit_id: return False

        # 2. Läs fil
        with open(filväg, 'r', encoding='utf-8') as f: content = f.read()
        if not content.startswith("---"): return False
        parts = content.split("---", 2)
        if len(parts) < 3: return False
        
        metadata = yaml.safe_load(parts[1])
        text = parts[2].strip()
        
        # 3. Upsert via Service
        ai_summary = metadata.get('ai_summary') or ""
        timestamp = metadata.get('timestamp_created') or ""
        
        graph_ctx = metadata.get("graph_context_summary")
        ctx_text = f"KONTEXT (Grafrelationer):\n{graph_ctx}\n\n" if graph_ctx else ""
        
        full_doc = f"{ctx_text}FILENAME: {filnamn}\nSUMMARY: {ai_summary}\n\nCONTENT:\n{text[:8000]}"

        VECTOR_SERVICE.upsert(
            id=unit_id,
            text=full_doc,
            metadata={"timestamp": timestamp, "filename": filnamn}
        )
        
        print(f"{_ts()} ✅ INDEX: {_kort(filnamn)} → ChromaDB")
        LOGGER.info(f"Indexerad: {filnamn} ({unit_id})")
        return True

    except Exception as e:
        print(f"{_ts()} ❌ INDEX: {_kort(filnamn)} → FAILED")
        LOGGER.error(f"Fel vid indexering {filnamn}: {e}")
        return False

# --- SMART DELTA SCANNING ---
def run_initial_scan():
    """Jämför Lake mot Vektorindex och indexera BARA det som saknas."""
    print(f"{_ts()} 🔍 Startar Smart Delta-Scan av {LAKE_STORE}...")
    if not os.path.exists(LAKE_STORE):
        print(f"{_ts()} ⚠️ Lake-mappen saknas!")
        return

    # 1. Hämta alla filer i Lake
    lake_files = {f: None for f in os.listdir(LAKE_STORE) if f.endswith(".md") and UUID_SUFFIX_PATTERN.search(f)}
    
    # 2. Extrahera UUIDs från filnamn
    lake_uuids = {}
    for fname in lake_files:
        match = UUID_SUFFIX_PATTERN.search(fname)
        if match:
            lake_uuids[match.group(1)] = fname

    # 3. Hämta befintliga IDs från Vektordatabasen
    print(f"{_ts()} 📡 Hämtar status från Vektordatabasen...")
    existing_ids = set(VECTOR_SERVICE.collection.get()['ids'])
    
    # 4. Beräkna Diff (Vad saknas i indexet?)
    missing_ids = set(lake_uuids.keys()) - existing_ids
    
    if not missing_ids:
        print(f"{_ts()} ✅ Indexet är synkat. Inga åtgärder behövs.")
        return

    print(f"{_ts()} 📦 Hittade {len(missing_ids)} filer som saknas i indexet. Börjar indexera...")
    
    count = 0
    for uid in missing_ids:
        filename = lake_uuids[uid]
        filepath = os.path.join(LAKE_STORE, filename)
        if indexera_vektor(filepath, filename):
            count += 1
            
    print(f"{_ts()} ✅ Delta-Scan klar. {count} filer indexerade.")

class VectorHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            self._process(event.src_path)
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            self._process(event.src_path)
    def _process(self, path):
        time.sleep(0.5)
        indexera_vektor(path, os.path.basename(path))

if __name__ == "__main__":
    # KÖR DELTA-SCAN FÖRST
    run_initial_scan()
    
    print(f"{_ts()} ✓ Vector Indexer online (Watchdog active)")
    observer = Observer()
    observer.schedule(VectorHandler(), LAKE_STORE, recursive=False)
    observer.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: observer.stop()
    observer.join()