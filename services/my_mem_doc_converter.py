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
except ImportError as e:
    raise ImportError(
        "HARDFAIL: google-genai biblioteket saknas. "
        "Kör: pip install google-genai"
    ) from e

# Entity Register för context injection (OBJEKT-44) - Använder graph_builder direkt
try:
    from services.my_mem_graph_builder import (
        get_all_entities as get_known_entities,
        get_canonical_from_graph as get_canonical,
        add_entity_alias,
        get_entity,
        upgrade_canonical
    )
except ImportError:
    try:
        from my_mem_graph_builder import (
            get_all_entities as get_known_entities,
            get_canonical_from_graph as get_canonical,
            add_entity_alias,
            get_entity,
            upgrade_canonical
        )
    except ImportError as e:
        raise ImportError(
            "HARDFAIL: my_mem_graph_builder.py saknas eller har fel."
        ) from e

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
TZ_NAME = CONFIG.get('system', {}).get('timezone', 'UTC')
try:
    SYSTEM_TZ = zoneinfo.ZoneInfo(TZ_NAME)
except Exception as e:
    LOGGER.error(f"HARDFAIL: Ogiltig timezone '{TZ_NAME}': {e}")
    raise ValueError(f"HARDFAIL: Ogiltig timezone '{TZ_NAME}' i config") from e

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
    """Läser in giltiga Masternoder från JSON-filen. HARDFAIL om det misslyckas."""
    if not os.path.exists(TAXONOMY_FILE):
        raise FileNotFoundError(
            f"HARDFAIL: Taxonomifil saknas: {TAXONOMY_FILE}. "
            "Skapa filen enligt Princip 8 i projektreglerna."
        )
    try:
        with open(TAXONOMY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            keys = list(data.keys())
            if not keys:
                raise ValueError("HARDFAIL: Taxonomin är tom")
            return keys
    except json.JSONDecodeError as e:
        raise ValueError(f"HARDFAIL: Kunde inte parsa taxonomi-JSON: {e}") from e
    except Exception as e:
        raise RuntimeError(f"HARDFAIL: Kunde inte läsa taxonomi: {e}") from e

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
        entities = get_known_entities()  # Lista med {id, type, aliases}
        context_parts = []
        
        # Extrahera personer
        persons = [e['id'] for e in entities if e.get('type') == 'Person']
        if persons:
            context_parts.append(f"KÄNDA PERSONER: {', '.join(persons[:30])}")
        
        # Extrahera projekt
        projects = [e['id'] for e in entities if e.get('type') == 'Projekt']
        if projects:
            context_parts.append(f"KÄNDA PROJEKT: {', '.join(projects[:20])}")
        
        # Extrahera alias-mappningar
        alias_mappings = []
        for e in entities:
            for alias in (e.get('aliases') or []):
                alias_mappings.append(f"'{alias}' = '{e['id']}'")
        if alias_mappings:
            context_parts.append(f"KÄNDA ALIAS: {', '.join(alias_mappings[:10])}")
        
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


def _process_potential_aliases(potential_aliases: list) -> list:
    """
    Processa potentiella alias från AI-analys.
    
    Validerar mot grafen och lägger till/uppgraderar alias.
    
    Args:
        potential_aliases: Lista med {name_variant, likely_refers_to, entity_type, confidence}
    
    Returns:
        Lista med processade alias (för loggning)
    """
    processed = []
    
    for alias_data in (potential_aliases or []):
        name_variant = alias_data.get('name_variant')
        likely_refers_to = alias_data.get('likely_refers_to')
        entity_type = alias_data.get('entity_type', 'Person')
        confidence = alias_data.get('confidence', 'low')
        
        if not name_variant or not likely_refers_to:
            continue
        
        # Endast processa medium/high confidence
        if confidence == 'low':
            LOGGER.debug(f"Skippar låg-confidence alias: {name_variant} -> {likely_refers_to}")
            continue
        
        try:
            # Kolla om det redan finns en entity för likely_refers_to
            existing = get_entity(likely_refers_to)
            
            if existing:
                # Entity finns - lägg till alias om det inte redan finns
                existing_aliases = existing.get('aliases') or []
                if name_variant not in existing_aliases:
                    success = add_entity_alias(likely_refers_to, name_variant, entity_type)
                    if success:
                        processed.append({
                            'action': 'added_alias',
                            'canonical': likely_refers_to,
                            'alias': name_variant,
                            'type': entity_type
                        })
                        LOGGER.info(f"Alias tillagt: {name_variant} -> {likely_refers_to}")
            else:
                # Kolla om name_variant är ett känt alias för något annat
                canonical_for_variant = get_canonical(name_variant)
                
                if canonical_for_variant:
                    # name_variant är redan ett alias - kolla om likely_refers_to är bättre
                    # (t.ex. fullständigt namn istället för förnamn)
                    if len(likely_refers_to) > len(canonical_for_variant):
                        # likely_refers_to verkar vara ett mer komplett namn - uppgradera
                        success = upgrade_canonical(canonical_for_variant, likely_refers_to)
                        if success:
                            processed.append({
                                'action': 'upgraded_canonical',
                                'old_canonical': canonical_for_variant,
                                'new_canonical': likely_refers_to,
                                'type': entity_type
                            })
                            LOGGER.info(f"Uppgraderade canonical: {canonical_for_variant} -> {likely_refers_to}")
                else:
                    # Ny entity - skapa den med alias
                    success = add_entity_alias(likely_refers_to, name_variant, entity_type)
                    if success:
                        processed.append({
                            'action': 'created_entity',
                            'canonical': likely_refers_to,
                            'alias': name_variant,
                            'type': entity_type
                        })
                        LOGGER.info(f"Ny entity skapad: {likely_refers_to} (alias: {name_variant})")
                        
        except Exception as e:
            LOGGER.error(f"Fel vid alias-processning: {e}")
    
    return processed


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
        
        # Processa potentiella alias och skriv till graf
        if data.get("potential_aliases"):
            processed = _process_potential_aliases(data["potential_aliases"])
            if processed:
                LOGGER.info(f"Processade {len(processed)} alias från {filnamn}")
            
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
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Kunde inte läsa filens tidsstämpel: {e}")
        raise RuntimeError(f"HARDFAIL: Kunde inte läsa tidsstämpel för {filepath}") from e

# --- SESSION FILE PROCESSING ---
def _parse_session_learnings(content: str) -> list:
    """
    Parsa ## Learnings-sektionen från en sessions-fil.
    
    Args:
        content: Filinnehållet (inklusive frontmatter)
    
    Returns:
        Lista med learnings [{canonical, alias, type, confidence, evidence}]
    """
    import re as re_module
    
    # Hitta ## Learnings-sektionen
    learnings_match = re_module.search(r'## Learnings\s*```yaml\s*(.*?)```', content, re_module.DOTALL)
    if not learnings_match:
        return []
    
    learnings_yaml = learnings_match.group(1).strip()
    
    try:
        data = yaml.safe_load(learnings_yaml)
        return data.get('aliases', [])
    except Exception as e:
        LOGGER.error(f"Kunde inte parsa learnings YAML: {e}")
        return []


def _process_session_file(filväg: str, filnamn: str, unit_id: str) -> bool:
    """
    Processa en sessions-fil: parsa learnings och kopiera till Lake.
    
    Sessions-filer har redan frontmatter och behöver inte AI-analys.
    Learnings extraheras och skrivs till grafen.
    
    Args:
        filväg: Sökväg till filen
        filnamn: Filnamn
        unit_id: UUID från filnamnet
    
    Returns:
        True om framgångsrik
    """
    base_name = os.path.splitext(filnamn)[0]
    sjö_fil = os.path.join(LAKE_STORE, f"{base_name}.md")
    
    # Läs fil
    with open(filväg, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Parsa learnings och skriv till graf
    learnings = _parse_session_learnings(content)
    learnings_count = 0
    
    for learning in learnings:
        canonical = learning.get('canonical')
        alias = learning.get('alias')
        entity_type = learning.get('type', 'Person')
        
        if canonical and alias:
            try:
                success = add_entity_alias(canonical, alias, entity_type)
                if success:
                    learnings_count += 1
                    LOGGER.info(f"Session lärdom: {alias} -> {canonical}")
            except Exception as e:
                LOGGER.error(f"Kunde inte spara session-lärdom: {e}")
    
    # Kopiera filen till Lake (behåll befintligt innehåll)
    with open(sjö_fil, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print(f"{_ts()} ✅ SESSION: {_kort(filnamn)} → Lake ({learnings_count} learnings)")
    LOGGER.info(f"Session processad: {filnamn} ({learnings_count} learnings)")
    return True


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

    # Kolla om det är en sessions-fil (har redan frontmatter och learnings)
    if filnamn.startswith("Session_"):
        _process_session_file(filväg, filnamn, unit_id)
        return

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