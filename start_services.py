import subprocess
import sys
import time
import os
import signal
import datetime
import yaml
import logging

# Loggning
logging.basicConfig(level=logging.WARNING, format='%(levelname)s - %(message)s')
LOGGER = logging.getLogger('StartServices')

# Import validering från tool_validate_system
from tools.tool_validate_system import run_startup_checks

# Tjänsterna som ska startas (som moduler för korrekt PYTHONPATH)
SERVICES = [
    {"module": "services.collectors.file_retriever", "name": "File Retriever"},
    {"module": "services.collectors.slack_collector", "name": "Slack Collector"},
    {"module": "services.processors.doc_converter", "name": "Doc Converter"},
    {"module": "services.processors.transcriber", "name": "Transcriber"},
    {"module": "services.indexers.vector_indexer", "name": "Vector Indexer"},
]

processes = []

def _ts():
    return datetime.datetime.now().strftime("[%H:%M:%S]")

def _load_config():
    """Ladda config för sökvägar"""
    config_path = os.path.join(os.path.dirname(__file__), 'config', 'my_mem_config.yaml')
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    return None

def auto_repair(health_info):
    """Reparerar saknade filer i Vector"""
    if not health_info:
        return

    lake_count = health_info['lake_count']
    vector_count = health_info['vector_count']
    lake_store = health_info['lake_store']
    chroma_path = health_info['chroma_path']
    lake_ids_dict = health_info.get('lake_ids', {})

    repaired = False

    # --- VECTOR REPAIR ---
    if vector_count < lake_count:
        try:
            # Använd VectorService (SSOT för embedding-modell)
            from services.utils.vector_service import get_vector_service

            lake_id_set = set(lake_ids_dict.keys())

            vector_service = get_vector_service("knowledge_base")
            coll = vector_service.collection
            vector_ids = set(coll.get()['ids'])

            missing = lake_id_set - vector_ids
            if missing:
                print(f"{_ts()} 🔧 REPAIR: Indexerar {len(missing)} saknade filer i Vector...")

                for uid in missing:
                    filename = lake_ids_dict.get(uid, f"{uid}.md")
                    filepath = os.path.join(lake_store, filename)
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            content = f.read()

                        if not content.startswith("---"):
                            continue
                        parts = content.split("---", 2)
                        if len(parts) < 3:
                            continue

                        metadata = yaml.safe_load(parts[1])
                        text = parts[2].strip()

                        ai_summary = metadata.get('ai_summary') or ""
                        timestamp = metadata.get('timestamp_ingestion') or ""

                        full_doc = f"FILENAME: {filename}\nSUMMARY: {ai_summary}\n\nCONTENT:\n{text[:8000]}"

                        coll.upsert(
                            ids=[uid],
                            documents=[full_doc],
                            metadatas=[{"timestamp": timestamp, "filename": filename}]
                        )
                    except Exception as e:
                        LOGGER.warning(f"Kunde inte indexera {filename}: {e}")
                        print(f"{_ts()} ⚠️ Kunde inte indexera {filename}: {e}")

                print(f"{_ts()} ✅ REPAIR: Vector klar")
                repaired = True

        except Exception as e:
            LOGGER.error(f"Vector repair misslyckades: {e}")
            print(f"{_ts()} ❌ Vector repair misslyckades: {e}")

    if repaired:
        print()


def start_all():
    print(f"\n--- MyMem Services (v6.0) ---\n")
    
    # Kör validering (inkl. loggrensning) och auto-repair
    health_info = run_startup_checks()
    auto_repair(health_info)
    
    python_exec = sys.executable

    for service in SERVICES:
        module_name = service["module"]
        try:
            p = subprocess.Popen([python_exec, "-m", module_name])
            processes.append(p)
            time.sleep(0.8)  # Låt tjänsten starta och skriva sin egen output
        except Exception as e:
            LOGGER.error(f"Kunde inte starta {service['name']}: {e}")
            print(f"{_ts()} ❌ {service['name']}: {e}")

    print(f"\n--- Ready ---\n")

def stop_all(signum, frame):
    print(f"\n{_ts()} Stänger ner...")
    for p in processes:
        try:
            p.terminate()
        except Exception as e:
            LOGGER.debug(f"Process redan avslutad: {e}")
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, stop_all)
    start_all()
    while True:
        time.sleep(1)