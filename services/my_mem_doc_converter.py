import os
import time
import yaml
import logging
import datetime
import json
import threading
import re
import zoneinfo
from concurrent.futures import ThreadPoolExecutor
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

try:
    import pypdf
    import docx
    import pandas as pd  # NYTT: För tabellhantering
except ImportError:
    print("[CRITICAL] Saknar nödvändiga bibliotek. Kör: pip install pypdf python-docx pandas openpyxl tabulate")
    exit(1)

try:
    from google import genai
    from google.genai import types
except ImportError:
    pass

# Entity Consolidator för context injection
try:
    from services.entity_register import get_known_entities, get_canonical
except ImportError:
    try:
        from entity_register import get_known_entities, get_canonical
    except ImportError:
        # Fallback om entity_consolidator inte finns
        def get_known_entities():
            return {"persons": [], "projects": [], "concepts": [], "aliases": {}}
        def get_canonical(name):
            return None

# --- CONFIG LOADER ---
def ladda_yaml(filnamn, strict=True):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [os.path.join(script_dir, 'config', filnamn), os.path.join(script_dir, '..', 'config', filnamn), os.path.join(script_dir, '..', filnamn)]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f: return yaml.safe_load(f)
    return {}

CONFIG = ladda_yaml('my_mem_config.yaml', strict=True)
PROMPTS = ladda_yaml('services_prompts.yaml', strict=True) 

# --- SYSTEM SETTINGS ---
try:
    TZ_NAME = CONFIG.get('system', {}).get('timezone', 'UTC')
    SYSTEM_TZ = zoneinfo.ZoneInfo(TZ_NAME)
except:
    SYSTEM_TZ = datetime.timezone.utc

# --- PATHS & ID ---
LAKE_STORE = os.path.expanduser(CONFIG['paths']['lake_store'])
ASSET_STORE = os.path.expanduser(CONFIG['paths']['asset_store'])
TAXONOMY_FILE = os.path.expanduser(CONFIG['paths'].get('taxonomy_file', '~/MyMemory/Index/my_mem_taxonomy.json'))
LOG_FILE = os.path.expanduser(CONFIG['logging']['log_file_path'])

OWNER_ID = CONFIG.get("owner", {}).get("id", "default")
DEFAULT_ACCESS_LEVEL = CONFIG.get("security", {}).get("default_access_level", 5)

DOC_EXTENSIONS = CONFIG.get('processing', {}).get('document_extensions', [])
API_KEY = CONFIG.get('ai_engine', {}).get('api_key', '')

# --- AI SETUP ---
AI_CLIENT = genai.Client(api_key=API_KEY) if API_KEY else None
MODEL_FAST = CONFIG.get('ai_engine', {}).get('models', {}).get('model_fast', 'models/gemini-flash-latest')

# --- WORKER SETUP ---
MAX_WORKERS = 5
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS)
PROCESSED_FILES = set()
PROCESS_LOCK = threading.Lock()

UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.[a-zA-Z0-9]+$')
STANDARD_TIMESTAMP_PATTERN = re.compile(r'^DATUM_TID:\s+(.+)$', re.MULTILINE)

# --- LOGGING ---
log_dir = os.path.dirname(LOG_FILE)
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - DOCS - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('MyMem_DocConverter')
os.makedirs(LAKE_STORE, exist_ok=True)

# --- TIGHT LOGGING ---
def _ts():
    return datetime.datetime.now(SYSTEM_TZ).strftime("[%H:%M:%S]")

def _kort(filnamn, max_len=25):
    if len(filnamn) <= max_len:
        return filnamn
    return "..." + filnamn[-(max_len-3):]

# --- TAXONOMY LOADER ---
def load_taxonomy_keys():
    """Läser in giltiga Masternoder från JSON-filen."""
    if not os.path.exists(TAXONOMY_FILE):
        return ["Okategoriserat", "Händelser", "Projekt", "Administration"] 
    try:
        with open(TAXONOMY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return list(data.keys())
    except Exception as e:
        LOGGER.error(f"Kunde inte läsa taxonomi: {e}")
        return ["Okategoriserat"]

# --- TEXT EXTRACTION ---
def extract_text(filväg, ext):
    """Extraherar text från filer. Använder Pandas för CSV/Excel (Code First)."""
    try:
        text = ""
        ext = ext.lower()
        
        if ext == '.pdf':
            reader = pypdf.PdfReader(filväg)
            for page in reader.pages: text += page.extract_text() + "\n"
            
        elif ext == '.docx':
            doc = docx.Document(filväg)
            for para in doc.paragraphs: text += para.text + "\n"
            
        elif ext == '.csv':
            try:
                # Läs CSV och konvertera till Markdown-tabell
                df = pd.read_csv(filväg)
                text = df.to_markdown(index=False)
            except Exception as e:
                LOGGER.error(f"Fel vid CSV-parsing {filväg}: {e}")
                return None
                
        elif ext in ['.xlsx', '.xls']:
            try:
                # Läs alla flikar i Excel-filen
                sheets = pd.read_excel(filväg, sheet_name=None)
                text_parts = []
                for sheet_name, df in sheets.items():
                    text_parts.append(f"### Blad: {sheet_name}")
                    text_parts.append(df.to_markdown(index=False))
                text = "\n\n".join(text_parts)
            except Exception as e:
                LOGGER.error(f"Fel vid Excel-parsing {filväg}: {e}")
                return None
                
        elif ext in ['.txt', '.md', '.json']:
            with open(filväg, 'r', encoding='utf-8', errors='ignore') as f: text = f.read()
            
        return text.strip()
    except Exception as e:
        LOGGER.error(f"Generellt extraheringsfel {filväg}: {e}")
        return None

# --- AI ANALYSIS (DYNAMIC PROMPT) ---
def _build_entity_context():
    """Bygg context-sträng med kända entiteter för prompt injection."""
    try:
        known = get_known_entities()
        context_parts = []
        
        if known.get('persons'):
            context_parts.append(f"KÄNDA PERSONER: {', '.join(known['persons'][:30])}")
        if known.get('projects'):
            context_parts.append(f"KÄNDA PROJEKT: {', '.join(known['projects'][:20])}")
        if known.get('aliases'):
            # Visa alias-mappningar som hjälp
            alias_examples = [f"'{a}' = '{c}'" for a, c in list(known['aliases'].items())[:10]]
            if alias_examples:
                context_parts.append(f"KÄNDA ALIAS: {', '.join(alias_examples)}")
        
        return "\n".join(context_parts) if context_parts else ""
    except Exception as e:
        LOGGER.debug(f"Kunde inte hämta entity context: {e}")
        return ""


def _normalize_entities(entities):
    """Normalisera entiteter via entity_consolidator."""
    normalized = []
    for entity in entities:
        canonical = get_canonical(entity)
        if canonical:
            if canonical not in normalized:
                normalized.append(canonical)
        else:
            if entity not in normalized:
                normalized.append(entity)
    return normalized


def generera_metadata(text, filnamn):
    if not AI_CLIENT or not text: 
        return {
            "summary": "No AI", 
            "keywords": [], 
            "entities": [],
            "graph_master_node": "Okategoriserat",
            "context_id": "INKORG"
        }
    
    valid_nodes = load_taxonomy_keys()
    raw_prompt = PROMPTS.get('doc_converter', {}).get('ots_injection_prompt', '')
    
    if not raw_prompt:
        LOGGER.error("CRITICAL: Prompt saknas i services_prompts.yaml!")
        return {"summary": "Prompt Error", "graph_master_node": "Okategoriserat"}

    # Bygg context med kända entiteter
    entity_context = _build_entity_context()
    
    system_instruction = raw_prompt.replace("{valid_nodes}", str(valid_nodes))
    
    # Injicera entity context om tillgänglig
    if entity_context:
        system_instruction = f"{entity_context}\n\n{system_instruction}"

    try:
        # Skicka max 30k tecken för analys för att spara tokens
        response = AI_CLIENT.models.generate_content(
            model=MODEL_FAST,
            contents=[
                types.Content(role="user", parts=[types.Part.from_text(text=f"{system_instruction}\n\nTEXT ATT ANALYSERA:\n{text[:30000]}")])
            ],
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        data = json.loads(response.text.replace('```json', '').replace('```', ''))
        
        # Normalisera entities via entity_consolidator
        if data.get("entities"):
            data["entities"] = _normalize_entities(data["entities"])
        
        if data.get("graph_master_node") not in valid_nodes:
            LOGGER.warning(f"AI gissade ogiltig nod '{data.get('graph_master_node')}'. Fallback till 'Okategoriserat'.")
            data["graph_master_node"] = "Okategoriserat"
            
        return data

    except Exception as e: 
        LOGGER.error(f"AI Error: {e}")
        return {
            "summary": "AI Error", 
            "keywords": [], 
            "graph_master_node": "Okategoriserat", 
            "context_id": "ERROR"
        }

# --- TIMESTAMP LOGIC ---
def get_best_timestamp(filepath, text_content):
    match = STANDARD_TIMESTAMP_PATTERN.search(text_content)
    if match:
        ts_str = match.group(1).strip()
        return ts_str

    try:
        stat = os.stat(filepath)
        timestamp = stat.st_birthtime if hasattr(stat, 'st_birthtime') else stat.st_mtime
        dt = datetime.datetime.fromtimestamp(timestamp, SYSTEM_TZ)
        return dt.isoformat()
    except:
        return datetime.datetime.now(SYSTEM_TZ).isoformat()

# --- MAIN PROCESSING ---
def processa_dokument(filväg, filnamn):
    with PROCESS_LOCK:
        if filnamn in PROCESSED_FILES: return
        base, ext = os.path.splitext(filnamn)
        if ext.lower() not in DOC_EXTENSIONS: return
        PROCESSED_FILES.add(filnamn)

    match = UUID_SUFFIX_PATTERN.search(filnamn)
    if not match:
        LOGGER.warning(f"Skippar fil utan UUID: {filnamn}")
        return

    unit_id = match.group(1)
    base_name = os.path.splitext(filnamn)[0]
    sjö_fil = os.path.join(LAKE_STORE, f"{base_name}.md") 
    
    # Skippa redan konverterade filer tyst
    if os.path.exists(sjö_fil): return

    raw_text = extract_text(filväg, ext)
    if not raw_text or len(raw_text) < 5: return

    ts = get_best_timestamp(filväg, raw_text)
    meta_data = generera_metadata(raw_text, filnamn)

    final_metadata = {
        "unit_id": unit_id,
        "owner_id": OWNER_ID,
        "access_level": DEFAULT_ACCESS_LEVEL,
        "source_type": "Doc_Converter_Local",
        "source_ref": sjö_fil,
        "original_binary_ref": filnamn,
        "data_format": "text/markdown",
        "timestamp_created": ts,
        "summary": meta_data.get("summary"),
        "keywords": meta_data.get("keywords"),
        "entities": meta_data.get("entities"),
        "graph_master_node": meta_data.get("graph_master_node"),
        "graph_sub_node": meta_data.get("graph_sub_node"),
        "context_id": meta_data.get("context_id"),
        "ai_model_used": MODEL_FAST
    }
    
    with open(sjö_fil, 'w', encoding='utf-8') as f:
        f.write(f"---\n{yaml.dump(final_metadata, allow_unicode=True, sort_keys=False)}---\n\n# Dokument: {filnamn}\n\n{raw_text}")
    
    master_node = meta_data.get('graph_master_node', 'Okategoriserat')
    print(f"{_ts()} ✅ CONV: {_kort(filnamn)} → Lake ({master_node})")
    LOGGER.info(f"Konverterad: {base_name}.md -> {master_node}")

class DocHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory: return
        filnamn = os.path.basename(event.src_path)
        if not filnamn.startswith('.') and os.path.splitext(filnamn)[1].lower() in DOC_EXTENSIONS:
             EXECUTOR.submit(processa_dokument, event.src_path, filnamn)

if __name__ == "__main__":
    # Räkna filer vid start
    already_done = 0
    pending = 0
    
    if os.path.exists(ASSET_STORE):
        for f in os.listdir(ASSET_STORE):
            ext = os.path.splitext(f)[1].lower()
            if ext in DOC_EXTENSIONS and not f.startswith('.') and UUID_SUFFIX_PATTERN.search(f):
                base_name = os.path.splitext(f)[0]
                if os.path.exists(os.path.join(LAKE_STORE, f"{base_name}.md")):
                    already_done += 1
                else:
                    pending += 1
                    EXECUTOR.submit(processa_dokument, os.path.join(ASSET_STORE, f), f)
    
    status = f"({already_done} i Lake" + (f", {pending} väntande)" if pending > 0 else ")")
    print(f"{_ts()} ✓ Doc Converter online {status}")
    
    observer = Observer()
    observer.schedule(DocHandler(), ASSET_STORE, recursive=False)
    observer.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: 
        EXECUTOR.shutdown(wait=False)
        observer.stop()
    observer.join()