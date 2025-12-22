import os
import sys
import time
import yaml
import logging
import datetime
import json
import threading
import re
import zoneinfo
import atexit
import uuid

# Lägg till projektroten i sys.path för att hitta services-paketet
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from concurrent.futures import ThreadPoolExecutor
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from services.utils.date_service import get_timestamp as date_service_timestamp
from services.utils.graph_service import GraphStore
from services.utils.json_parser import parse_llm_json

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
    from services.indexers.graph_builder import (
        get_all_entities as get_known_entities,
        get_canonical_from_graph as get_canonical,
        add_entity_alias,
        get_entity,
        upgrade_canonical,
        close_db_connection
    )
except ImportError:
    try:
        from my_mem_graph_builder import (
            get_all_entities as get_known_entities,
            get_canonical_from_graph as get_canonical,
            add_entity_alias,
            get_entity,
            upgrade_canonical,
            close_db_connection
        )
    except ImportError as e:
        raise ImportError(
            "HARDFAIL: my_mem_graph_builder.py saknas eller har fel."
        ) from e

# Registrera cleanup vid exit för att stänga databasanslutningar
atexit.register(close_db_connection)

# --- CONFIG LOADER ---
def ladda_yaml(filnamn, strict=True):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', '..', 'config', filnamn),
        os.path.join(script_dir, '..', 'config', filnamn),
        os.path.join(script_dir, 'config', filnamn),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f: return yaml.safe_load(f)
    if strict:
        print(f"[CRITICAL] Kunde inte hitta: {filnamn}")
    return {}

CONFIG = ladda_yaml('my_mem_config.yaml', strict=True)
PROMPTS = ladda_yaml('services_prompts.yaml', strict=True) 

# --- LOGGING (måste initieras tidigt) ---
LOG_FILE = os.path.expanduser(CONFIG['logging']['log_file_path'])
log_dir = os.path.dirname(LOG_FILE)
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - DOCS - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('MyMem_DocConverter')

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
# Undermappar att övervaka för dokument
TRANSCRIPTS_FOLDER = os.path.expanduser(CONFIG['paths']['asset_transcripts'])
DOCUMENTS_FOLDER = os.path.expanduser(CONFIG['paths']['asset_documents'])
SLACK_FOLDER = os.path.expanduser(CONFIG['paths']['asset_slack'])
SESSIONS_FOLDER = os.path.expanduser(CONFIG['paths']['asset_sessions'])
CALENDAR_FOLDER = os.path.expanduser(CONFIG['paths'].get('asset_calendar', os.path.join(ASSET_STORE, 'Calendar')))
MAIL_FOLDER = os.path.expanduser(CONFIG['paths'].get('asset_mail', os.path.join(ASSET_STORE, 'Mail')))
FAILED_FOLDER = os.path.expanduser(CONFIG['paths']['asset_failed'])
WATCH_FOLDERS = [TRANSCRIPTS_FOLDER, DOCUMENTS_FOLDER, SLACK_FOLDER, SESSIONS_FOLDER, CALENDAR_FOLDER, MAIL_FOLDER]
TAXONOMY_FILE = os.path.expanduser(CONFIG['paths'].get('taxonomy_file', '~/MyMemory/Index/my_mem_taxonomy.json'))

OWNER_ID = CONFIG.get("owner", {}).get("id", "default")
DEFAULT_ACCESS_LEVEL = CONFIG.get("security", {}).get("default_access_level", 5)

DOC_EXTENSIONS = CONFIG.get('processing', {}).get('document_extensions', [])
API_KEY = CONFIG.get('ai_engine', {}).get('api_key', '')

# --- AI SETUP ---
AI_CLIENT = genai.Client(api_key=API_KEY) if API_KEY else None
MODEL_FAST = CONFIG.get('ai_engine', {}).get('models', {}).get('model_fast', 'models/gemini-flash-latest')
MODEL_LITE = CONFIG.get('ai_engine', {}).get('models', {}).get('model_lite', 'models/gemini-1.5-flash')
GRAPH_PATH = os.path.expanduser(CONFIG['paths']['graph_db'])
# Lazy initialization av EVIDENCE_STORE för att undvika konflikter vid modulimport
_EVIDENCE_STORE = None
_EVIDENCE_STORE_LOCK = threading.Lock()

def get_evidence_store():
    """Lazy initialization av EVIDENCE_STORE för att undvika DuckDB-anslutningskonflikter."""
    global _EVIDENCE_STORE
    if _EVIDENCE_STORE is None:
        with _EVIDENCE_STORE_LOCK:
            if _EVIDENCE_STORE is None:
                LOGGER.info(f"DEBUG: Skapar EVIDENCE_STORE för {GRAPH_PATH}")
                max_retries = 5
                retry_delay = 0.5
                for attempt in range(max_retries):
                    try:
                        _EVIDENCE_STORE = GraphStore(GRAPH_PATH, read_only=False)
                        LOGGER.info(f"DEBUG: EVIDENCE_STORE skapad framgångsrikt")
                        break
                    except Exception as e:
                        error_str = str(e).lower()
                        if ("lock" in error_str or "conflicting" in error_str or "different configuration" in error_str) and attempt < max_retries - 1:
                            LOGGER.warning(f"DEBUG: Kunde inte skapa EVIDENCE_STORE (försök {attempt + 1}/{max_retries}), väntar {retry_delay}s: {e}")
                            time.sleep(retry_delay)
                            retry_delay *= 2  # Exponential backoff
                        else:
                            # Om alla försök misslyckas, logga felet men kasta inte exception
                            # Istället kommer get_extraction_context() att returnera tom context
                            LOGGER.error(f"HARDFAIL: Kunde inte skapa EVIDENCE_STORE efter {attempt + 1} försök: {e}. Processeringen fortsätter utan evidence context.", exc_info=True)
                            # Sätt _EVIDENCE_STORE till en special marker för att undvika att försöka igen
                            _EVIDENCE_STORE = "FAILED"
                            break
    # Om EVIDENCE_STORE är "FAILED", returnera None istället för att kasta exception
    if _EVIDENCE_STORE == "FAILED":
        return None
    return _EVIDENCE_STORE

# --- WORKER SETUP ---
MAX_WORKERS = 5
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS)
PROCESSED_FILES = set()
PROCESS_LOCK = threading.Lock()

UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.[a-zA-Z0-9]+$')
STANDARD_TIMESTAMP_PATTERN = re.compile(r'^DATUM_TID:\s+(.+)$', re.MULTILINE)

os.makedirs(LAKE_STORE, exist_ok=True)

# --- FAILED FILE HANDLING ---
def _move_to_failed(filepath: str, reason: str) -> bool:
    """Flytta fil till Failed-mappen vid HARDFAIL."""
    try:
        os.makedirs(FAILED_FOLDER, exist_ok=True)
        filename = os.path.basename(filepath)
        dest = os.path.join(FAILED_FOLDER, filename)
        
        # Om filen redan finns i Failed, lägg till timestamp
        if os.path.exists(dest):
            base, ext = os.path.splitext(filename)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = os.path.join(FAILED_FOLDER, f"{base}_{timestamp}{ext}")
        
        import shutil
        shutil.move(filepath, dest)
        LOGGER.warning(f"Fil flyttad till Failed: {filename} - Anledning: {reason}")
        print(f"{_ts()} ❌ FAIL: {filename[:30]}... → Failed/ ({reason})")
        return True
    except Exception as e:
        LOGGER.error(f"Kunde inte flytta till Failed: {filepath} - {e}")
        return False


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


def load_taxonomy_full():
    """Läs hela taxonomin med multipass_definition. HARDFAIL om saknas."""
    if not os.path.exists(TAXONOMY_FILE):
        raise FileNotFoundError(
            f"HARDFAIL: Taxonomifil saknas: {TAXONOMY_FILE}. "
            "Skapa filen enligt Princip 8 i projektreglerna."
        )
    try:
        with open(TAXONOMY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not data:
            raise ValueError("HARDFAIL: Taxonomin är tom")
        return data
    except json.JSONDecodeError as e:
        raise ValueError(f"HARDFAIL: Kunde inte parsa taxonomi-JSON: {e}") from e
    except Exception as e:
        raise RuntimeError(f"HARDFAIL: Kunde inte läsa taxonomi: {e}") from e


def _is_multipass_enabled() -> bool:
    """Multipass är alltid aktiverat."""
    return True

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


# --- MULTIPASS PROMPT BUILDER ---
GLOBAL_CONTEXT_CONSTRAINT = """SYSTEM CONTEXT:
Du agerar som det intelligenta minnet för organisationen "Digitalist".
Din uppgift är att klassificera information strikt ur Digitalists perspektiv.

UNIVERSUM-DEFINITIONER:
1. "VI", "VÅR", "OSS": Syftar ALLTID på Digitalist (företaget, medarbetarna, den interna kulturen).
2. "DE", "KUNDEN", "PARTNERN": Syftar på externa parter. Dessa är objekt, inte subjekt.
3. TOLKNING AV TAXONOMI:
   - Om taxonomin säger "Vår Strategi", betyder det Digitalists strategi.
   - En kunds strategi ska INTE sorteras under "Strategi", utan under "Aktör" eller kontexten för ett "Projekt".
"""


def build_multipass_prompt(master_node: str, definition: str, text: str) -> str:
    """Bygg strikt multipass-prompt med global kontext."""
    return f"""{GLOBAL_CONTEXT_CONSTRAINT}

UPPGIFT:
Leta efter entiteter som passar definitionen för Masternoden: "{master_node}"

DEFINITION:
{definition}

VAD ÄR EN ENTITET?
En entitet är ett identifierbart objekt med ett namn som kan refereras till som en enhet.

BRA EXEMPEL (är entiteter):
- Personer: "Tim Ekman", "Ammi Bohlin", "Pär"
- Organisationer: "Digitalist", "Clarendo", "DNV"
- Verktyg/Produkter: "Slack", "Figma", "Flask", "Kubernetes"
- Projekt: "AI-PoC", "Jurivo", "Carbon Copy Machine"
- Koncept/Metodik: "Change Management", "Agile", "Scrum"
- Platser: "Köpenhamn", "OLG"
- Standarder: "ISO 14001", "ISO 9001"

DÅLIGA EXEMPEL (är INTE entiteter):
- Meningar: "Vår vision är att demokratisera AI" → ska vara "Vision" eller "AI-demokratisering"
- Beskrivningar: "Säkerställa att minst 50% av intäkterna..." → ska vara "Återkommande intäkter" eller liknande
- Processer: "Säljprocessen" → kan vara OK om det är ett namngivet koncept, annars skippa
- Filnamn: "agent_v0.5.py", "ccm_core.py" → INTE entiteter
- Versioner: "v1.8", "v2.4" → INTE entiteter
- Nummer: "9001", "45001" → INTE entiteter (men "ISO 9001" är OK)
- Generiska termer: "nyheter", "poddar", "embeddingar" → INTE entiteter
- Långa beskrivningar: "Utvecklingen av Clarendos nya lagbevakningstjänst" → ska vara "Clarendo Lagbevakningstjänst"

REGEL:
entity_name ska vara NAMNET på entiteten (1-4 ord), INTE en mening, beskrivning, filnamn, version eller processbeskrivning.

INSTRUKTION:
- Hitta ALLA entiteter i texten som passar denna kategori
- För varje entitet, extrahera NAMNET på entiteten (entity_name) - kort och identifierbart
- Skriv en kort kontextbeskrivning (1-2 meningar) i context_description som förklarar VARFÖR entiteten passar denna kategori
- Ge confidence score (0.0-1.0) baserat på hur säker du är
- SKIPPA filnamn, versioner, nummer, generiska termer och långa beskrivningar

TEXT ATT ANALYSERA:
{text}

RETURNERA ENDAST GILTIG JSON:
{{
  "entities": [
    {{"entity_name": "Slack", "context_description": "Användes för att chatta med Tim om EKN-projektet. Nämns som kommunikationsverktyg.", "confidence": 0.95}}
  ]
}}
"""
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


GRAPH_NODES_CUTOFF = 0.2  # Noder med lägre relevans sparas inte


def _filter_graph_nodes(graph_nodes: dict, valid_master_nodes: list) -> dict:
    """Filtrera graph_nodes: ta bort vikter under cutoff och validera masternoder.
    
    Returns:
        Filtrerad dict, eller None om inga giltiga noder finns (HARDFAIL).
    """
    if not graph_nodes:
        LOGGER.error("HARDFAIL: LLM returnerade tom graph_nodes")
        return None
    
    filtered = {}
    for key, value in graph_nodes.items():
        # Abstrakt koncept (masternode) - värde är float
        if isinstance(value, (int, float)):
            if value >= GRAPH_NODES_CUTOFF:
                if key in valid_master_nodes:
                    filtered[key] = value
                else:
                    LOGGER.warning(f"Ogiltig masternode '{key}' ignorerad")
        # Typad entitet (Person, Aktör, Projekt) - värde är dict
        elif isinstance(value, dict):
            if key in valid_master_nodes:  # Typen måste vara giltig
                filtered_entities = {name: weight for name, weight in value.items() 
                                   if weight >= GRAPH_NODES_CUTOFF}
                if filtered_entities:
                    filtered[key] = filtered_entities
    
    # HARDFAIL om allt filtrerades bort - LLM måste kunna kategorisera
    if not filtered:
        LOGGER.error("HARDFAIL: Alla graph_nodes filtrerades bort (under cutoff eller ogiltiga)")
        return None
    
    return filtered


# --- MULTIPASS EXTRACTION ---
def extract_with_multipass(text: str, filnamn: str, unit_id: str, timestamp: str = None) -> dict | None:
    """
    Kör en LLM-pass per masternod (parallellt) och returnerar evidence.
    
    Returns:
        dict med:
            - summary_per_node: {masternode: count}
            - entities: [{entity_name, context_description, confidence, master_node}]
    """
    if not AI_CLIENT:
        LOGGER.error(f"HARDFAIL: AI_CLIENT saknas - kan inte processa {filnamn}")
        return None

    # Ladda taxonomi med definitioner
    taxonomy = load_taxonomy_full()

    # Säkerställ MODEL_FAST finns
    if not MODEL_FAST:
        LOGGER.error("HARDFAIL: model_fast saknas i config")
        return None

    def _extract_for_node(master_node: str, definition: str):
        prompt = build_multipass_prompt(master_node, definition, text)
        try:
            response = AI_CLIENT.models.generate_content(
                model=MODEL_FAST,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            
            # DIAGNOSTIK: Logga raw response
            LOGGER.info(f"Multipass {master_node} raw response (första 500 tecken): {response.text[:500]}")
            
            parsed = parse_llm_json(response.text)
            
            # DIAGNOSTIK: Logga vad som parsades
            LOGGER.info(f"Multipass {master_node} parsed type: {type(parsed)}, keys: {list(parsed.keys()) if isinstance(parsed, dict) else 'N/A'}")
            
            if not isinstance(parsed, dict):
                LOGGER.warning(f"Multipass {master_node}: Parsed resultat är inte en dict, typ: {type(parsed)}, värde: {parsed}")
                return master_node, []
            
            entities = parsed.get("entities", [])
            
            # DIAGNOSTIK: Logga entities
            if not entities:
                LOGGER.warning(f"Multipass {master_node}: Ingen entities-nyckel eller tom lista. Parsed keys: {list(parsed.keys())}")
            else:
                LOGGER.info(f"Multipass {master_node}: {len(entities)} entities extraherade från {filnamn}")
                # Logga första entity för att se strukturen
                if len(entities) > 0:
                    LOGGER.debug(f"Multipass {master_node} första entity: {entities[0]}")
            
            return master_node, entities
        except Exception as e:
            LOGGER.error(f"HARDFAIL: Multipass fel för {master_node} i {filnamn}: {e}", exc_info=True)
            return master_node, []

    # Logga start av multipass
    LOGGER.info(f"Multipass startar för {filnamn} (textlängd: {len(text)} tecken)")
    
    results = {}
    entities_all = []
    nodes_processed = 0
    nodes_skipped = 0

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        for node, meta in taxonomy.items():
            definition = meta.get("multipass_definition")
            if not definition:
                LOGGER.warning(f"Skippar {node}: saknar multipass_definition")
                nodes_skipped += 1
                continue
            futures.append(executor.submit(_extract_for_node, node, definition))
            nodes_processed += 1

        LOGGER.info(f"Multipass: Processar {nodes_processed} masternoder parallellt för {filnamn}")

        for future in futures:
            master_node, ents = future.result()
            results[master_node] = len(ents)
            LOGGER.info(f"Multipass {master_node}: {len(ents)} entities extraherade från {filnamn}")
            for ent in ents:
                # Normalisera confidence
                conf = ent.get("confidence")
                try:
                    conf = float(conf) if conf is not None else None
                except Exception as e:
                    LOGGER.warning(f"Confidence parse misslyckades för {ent.get('entity_name')}: {e}")
                    conf = None
                entities_all.append({
                    "entity_name": ent.get("entity_name"),
                    "context_description": ent.get("context_description"),
                    "confidence": conf,
                    "master_node_candidate": master_node
                })

    # Spara evidence till GraphStore
    evidence_saved = 0
    evidence_failed = 0
    for ent in entities_all:
        if not ent.get("entity_name") or not ent.get("context_description"):
            LOGGER.warning(f"Skippar tom entity i {filnamn}: entity_name={ent.get('entity_name')}, context={bool(ent.get('context_description'))}")
            continue
        evidence_id = str(uuid.uuid4())
        try:
            LOGGER.info(f"DEBUG: Sparar evidence för {ent['entity_name']} ({ent['master_node_candidate']}) från {filnamn}")
            evidence_store = get_evidence_store()
            if evidence_store is None:
                LOGGER.warning(f"DEBUG: EVIDENCE_STORE är inte tillgänglig (lock-konflikt), hoppar över evidence för {ent['entity_name']}")
                evidence_failed += 1
                continue
            evidence_store.add_evidence(
                id=evidence_id,
                entity_name=ent["entity_name"],
                master_node_candidate=ent["master_node_candidate"],
                context_description=ent["context_description"],
                source_file=filnamn,
                source_timestamp=timestamp,
                extraction_pass=ent["master_node_candidate"],
                confidence=ent.get("confidence"),
            )
            evidence_saved += 1
            LOGGER.info(f"DEBUG: Evidence sparad framgångsrikt för {ent['entity_name']} ({ent['master_node_candidate']}) från {filnamn}")
        except Exception as e:
            evidence_failed += 1
            LOGGER.error(f"Kunde inte spara evidence ({ent.get('entity_name')}) för {filnamn}: {e}")

    # Sammanfattande loggning
    total_entities = len(entities_all)
    LOGGER.info(f"Multipass slutförd för {filnamn}: {total_entities} entities totalt, {evidence_saved} sparade, {evidence_failed} misslyckade, {nodes_processed} noder processade, {nodes_skipped} noder hoppades över")

    summary = {
        "summary_per_node": results,
        "entities": entities_all
    }
    return summary

# --- TIMESTAMP LOGIC ---
def get_best_timestamp(filepath, text_content):
    """
    Hämta bästa timestamp för en fil.
    
    Prioritet:
    1. DATUM_TID i textinnehåll (från transkribering)
    2. Central DateService (frontmatter → filnamn → PDF → filesystem)
    """
    # Först: kolla om transkribering har satt DATUM_TID
    match = STANDARD_TIMESTAMP_PATTERN.search(text_content)
    if match:
        ts_str = match.group(1).strip()
        return ts_str

    # Fallback: använd central DateService
    try:
        ts = date_service_timestamp(filepath)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=SYSTEM_TZ)
        return ts.isoformat()
    except RuntimeError as e:
        LOGGER.error(f"HARDFAIL: {e}")
        raise

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
    LOGGER.info(f"DEBUG: processa_dokument anropad för: {filnamn} (path: {filväg})")
    
    with PROCESS_LOCK:
        if filnamn in PROCESSED_FILES:
            LOGGER.debug(f"DEBUG: Fil redan processad: {filnamn}")
            return
        base, ext = os.path.splitext(filnamn)
        if ext.lower() not in DOC_EXTENSIONS:
            LOGGER.warning(f"DEBUG: Fil har ogiltig extension: {ext.lower()} (tillåtna: {DOC_EXTENSIONS})")
            return
        PROCESSED_FILES.add(filnamn)
        LOGGER.debug(f"DEBUG: Fil markerad som processad: {filnamn}")

    match = UUID_SUFFIX_PATTERN.search(filnamn)
    if not match:
        LOGGER.warning(f"Skippar fil utan UUID: {filnamn}")
        return

    unit_id = match.group(1)
    LOGGER.debug(f"DEBUG: UUID extraherad: {unit_id}")
    base_name = os.path.splitext(filnamn)[0]
    sjö_fil = os.path.join(LAKE_STORE, f"{base_name}.md")
    
    # Skippa redan konverterade filer tyst
    if os.path.exists(sjö_fil):
        LOGGER.debug(f"DEBUG: Fil finns redan i Lake: {sjö_fil}")
        return

    # Kolla om det är en sessions-fil (har redan frontmatter och learnings)
    if filnamn.startswith("Session_"):
        LOGGER.info(f"DEBUG: Sessions-fil upptäckt: {filnamn}")
        _process_session_file(filväg, filnamn, unit_id)
        return

    LOGGER.info(f"DEBUG: Börjar extrahera text från {filnamn} (extension: {ext})")
    raw_text = extract_text(filväg, ext)
    if not raw_text or len(raw_text) < 5:
        LOGGER.warning(f"DEBUG: Text extraktion misslyckades eller för kort för {filnamn} (längd: {len(raw_text) if raw_text else 0})")
        _move_to_failed(filväg, "Tom eller för kort text")
        return
    
    LOGGER.info(f"DEBUG: Text extraherad ({len(raw_text)} tecken), hämtar timestamp")

    ts = get_best_timestamp(filväg, raw_text)
    LOGGER.debug(f"DEBUG: Timestamp: {ts}")

    # Multipass är alltid aktiverat
    LOGGER.info(f"Multipass aktiverat för {filnamn} (unit_id: {unit_id})")
    multipass_result = extract_with_multipass(raw_text, filnamn, unit_id, timestamp=ts)
    if multipass_result is None:
        LOGGER.error(f"HARDFAIL: Multipass misslyckades för {filnamn}")
        _move_to_failed(filväg, "Multipass misslyckades")
        return
    LOGGER.info(f"DEBUG: Multipass slutförd för {filnamn}")

    LOGGER.info(f"DEBUG: Bygger final_metadata för {filnamn}")
    final_metadata = {
        "unit_id": unit_id,
        "owner_id": OWNER_ID,
        "access_level": DEFAULT_ACCESS_LEVEL,
        "source_type": "Doc_Converter_Local",
        "source_ref": sjö_fil,
        "original_binary_ref": filnamn,
        "data_format": "text/markdown",
        "timestamp_created": ts,
        "ai_model_used": MODEL_FAST,
    }

    # Multipass är alltid aktiverat
    LOGGER.debug(f"DEBUG: Lägger till multipass-metadata för {filnamn}")
    final_metadata["graph_nodes"] = {}  # multipass sparas som evidence, inte graph_nodes
    final_metadata["multipass_summary_per_node"] = multipass_result.get("summary_per_node", {})
    final_metadata["multipass_run"] = True
    final_metadata["summary"] = None
    final_metadata["dates_mentioned"] = []
    final_metadata["actions"] = []
    final_metadata["deadlines"] = []
    
    LOGGER.info(f"DEBUG: Skriver fil till Lake: {sjö_fil}")
    try:
        with open(sjö_fil, 'w', encoding='utf-8') as f:
            f.write(f"---\n{yaml.dump(final_metadata, allow_unicode=True, sort_keys=False)}---\n\n# Dokument: {filnamn}\n\n{raw_text}")
        LOGGER.info(f"DEBUG: Fil skriven till Lake: {sjö_fil}")
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Kunde inte skriva fil till Lake för {filnamn}: {e}", exc_info=True)
        _move_to_failed(filväg, f"Kunde inte skriva till Lake: {e}")
        return
    
    # Extrahera huvudkategori för loggning
    # Multipass är alltid aktiverat - välj den masternod som fick flest träffar
    if multipass_result and multipass_result.get("summary_per_node"):
        master_node = max(multipass_result["summary_per_node"].items(), key=lambda x: x[1])[0]
    else:
        master_node = "Multipass"
    print(f"{_ts()} ✅ CONV: {_kort(filnamn)} → Lake ({master_node})")
    LOGGER.info(f"Konverterad: {base_name}.md -> {master_node}")

class DocHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory: 
            LOGGER.debug(f"DEBUG: on_created: Ignorerar mapp: {event.src_path}")
            return
        filnamn = os.path.basename(event.src_path)
        ext = os.path.splitext(filnamn)[1].lower()
        LOGGER.info(f"DEBUG: on_created: Fil detekterad: {filnamn} (ext: {ext}, i DOC_EXTENSIONS: {ext in DOC_EXTENSIONS})")
        if not filnamn.startswith('.') and ext in DOC_EXTENSIONS:
            LOGGER.info(f"DEBUG: on_created: Skickar fil till processering: {filnamn}")
            EXECUTOR.submit(processa_dokument, event.src_path, filnamn)
        else:
            LOGGER.warning(f"DEBUG: on_created: Fil ignoreras: {filnamn} (starts_with_dot: {filnamn.startswith('.')}, ext_in_DOC_EXTENSIONS: {ext in DOC_EXTENSIONS}, DOC_EXTENSIONS: {DOC_EXTENSIONS})")

if __name__ == "__main__":
    # Säkerställ att undermapparna finns
    for folder in WATCH_FOLDERS:
        os.makedirs(folder, exist_ok=True)
    
    # Räkna filer vid start - iterera över alla undermappar
    already_done = 0
    pending = 0
    
    for folder in WATCH_FOLDERS:
        if os.path.exists(folder):
            for f in os.listdir(folder):
                ext = os.path.splitext(f)[1].lower()
                if ext in DOC_EXTENSIONS and not f.startswith('.') and UUID_SUFFIX_PATTERN.search(f):
                    base_name = os.path.splitext(f)[0]
                    if os.path.exists(os.path.join(LAKE_STORE, f"{base_name}.md")):
                        already_done += 1
                    else:
                        pending += 1
                        EXECUTOR.submit(processa_dokument, os.path.join(folder, f), f)
    
    status = f"({already_done} i Lake" + (f", {pending} väntande)" if pending > 0 else ")")
    print(f"{_ts()} ✓ Doc Converter online {status}")
    
    # Övervaka alla undermappar
    observer = Observer()
    for folder in WATCH_FOLDERS:
        observer.schedule(DocHandler(), folder, recursive=False)
    observer.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: 
        EXECUTOR.shutdown(wait=False)
        observer.stop()
    observer.join()