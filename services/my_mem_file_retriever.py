import os
import time
import yaml
import logging
import shutil
import uuid
import re
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- CONFIG ---
def ladda_yaml(filnamn):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, 'config', filnamn),
        os.path.join(script_dir, '..', 'config', filnamn)
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f: return yaml.safe_load(f)
    exit(1)

CONFIG = ladda_yaml('my_mem_config.yaml')
DROP_FOLDER = os.path.expanduser(CONFIG['paths']['drop_folder'])
ASSET_STORE = os.path.expanduser(CONFIG['paths']['asset_store'])
LOG_FILE = os.path.expanduser(CONFIG['logging']['log_file_path'])

# --- LOGGING ---
log_dir = os.path.dirname(LOG_FILE)
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - RETRIEVER - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('MyMem_Retriever')

import datetime
def _ts():
    return datetime.datetime.now().strftime("[%H:%M:%S]")

def _kort(filnamn, max_len=25):
    if len(filnamn) <= max_len:
        return filnamn
    return "..." + filnamn[-(max_len-3):]

# Regex för standard UUID (8-4-4-4-12) var som helst i texten
UUID_PATTERN = re.compile(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})')

class MoveHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory: return
        self.process_file(event.src_path)

    def on_modified(self, event):
        if event.is_directory: return
        if not os.path.exists(event.src_path): return
        time.sleep(1) 
        self.process_file(event.src_path)

    def process_file(self, src_path):
        filnamn = os.path.basename(src_path)
        if filnamn.startswith('.'): return
        
        base, ext = os.path.splitext(filnamn)
        
        # 1. SÖK UUID: Finns det ett giltigt UUID någonstans i namnet?
        match = UUID_PATTERN.search(base)
        
        if match:
            found_uuid = match.group(0)
            
            # 2. STÄDA: Ta bort UUID + ev. skräptecken från originalnamnet
            # Vi ersätter UUID med tom sträng
            clean_base = base.replace(found_uuid, "")
            
            # Städa bort dubbla understreck, mellanslag eller bindestreck som blev kvar
            # Ersätt alla separatorer med ett snyggt understreck
            clean_base = re.sub(r'[ _-]+', '_', clean_base)
            
            # Ta bort ledande/avslutande understreck
            clean_base = clean_base.strip('_')
            
            if not clean_base:
                clean_base = "Namnlos_Fil"

            # 3. KONSTRUERA: [Namn]_[UUID].[ext]
            final_name = f"{clean_base}_{found_uuid}{ext}"
            
            LOGGER.info(f"Normaliserar: {filnamn} -> {final_name}")
            
        else:
            new_uuid = str(uuid.uuid4())
            clean_base = re.sub(r'[ _-]+', '_', base).strip('_')
            final_name = f"{clean_base}_{new_uuid}{ext}"
            LOGGER.info(f"Ny UUID: {filnamn} -> {final_name}")

        dest_path = os.path.join(ASSET_STORE, final_name)
        
        if os.path.exists(dest_path):
            LOGGER.warning(f"Dubblett: {final_name}")
            return

        try:
            shutil.move(src_path, dest_path)
            print(f"{_ts()} 📦 DROP: {_kort(filnamn)} → Assets")
            LOGGER.info(f"Flyttad till Assets: {final_name}")
        except Exception as e:
            print(f"{_ts()} ❌ DROP: {_kort(filnamn)} → FAILED")
            LOGGER.error(f"Flyttfel {filnamn}: {e}")

if __name__ == "__main__":
    os.makedirs(DROP_FOLDER, exist_ok=True)
    os.makedirs(ASSET_STORE, exist_ok=True)

    # Kör igenom befintliga filer i Drop vid start
    pending = 0
    for f in os.listdir(DROP_FOLDER):
        full_path = os.path.join(DROP_FOLDER, f)
        if os.path.isfile(full_path) and not f.startswith('.'):
            pending += 1
            handler = MoveHandler()
            handler.process_file(full_path)

    if pending > 0:
        print(f"{_ts()} ✓ File Retriever online ({pending} flyttade)")
    else:
        print(f"{_ts()} ✓ File Retriever online")

    observer = Observer()
    observer.schedule(MoveHandler(), DROP_FOLDER, recursive=False)
    observer.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: observer.stop()
    observer.join()