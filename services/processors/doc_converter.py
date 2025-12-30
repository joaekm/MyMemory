#!/usr/bin/env python3
"""
MyMem DocConverter (v9.4) - Strict Validation Pipeline (Trusted vs Untrusted)

Changes:
- Implements EntityGatekeeper for strict entity validation.
- Differentiates between Trusted Sources (Slack, Mail) and Untrusted (Documents).
- Trusted sources can CREATE entities (action: CREATE).
- Untrusted sources can only LINK to existing entities (action: LINK).
- Uses SchemaValidator to enforce allowed_sources.
"""

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
import shutil
import uuid
import atexit
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Any, List, Optional

# Imports Dependencies
try:
    import fitz  # pymupdf
    import docx
except ImportError:
    print("[CRITICAL] Saknar nödvändiga bibliotek (pymupdf, python-docx).")
    sys.exit(1)

# Lägg till projektroten
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google import genai
from google.genai import types
from services.utils.json_parser import parse_llm_json
from services.utils.graph_service import GraphStore
from services.utils.schema_validator import SchemaValidator

try:
    from services.utils.date_service import get_timestamp as date_service_timestamp
except ImportError:
    sys.stderr.write("[INFO] date_service inte tillgänglig, använder datetime.now()\n")
    date_service_timestamp = lambda x: datetime.datetime.now()

# --- CONFIG LOADER ---
def _load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, 'config', 'my_mem_config.yaml'),
    ]
    main_conf = {}
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f: main_conf = yaml.safe_load(f)
            for k, v in main_conf.get('paths', {}).items():
                main_conf['paths'][k] = os.path.expanduser(v)
            
            config_dir = os.path.dirname(p)
            prompts_conf = {}
            for name in ['services_prompts.yaml', 'service_prompts.yaml']:
                pp = os.path.join(config_dir, name)
                if os.path.exists(pp):
                    with open(pp, 'r') as f: prompts_conf = yaml.safe_load(f)
                    break 
            
            return main_conf, prompts_conf

    raise FileNotFoundError("HARDFAIL: Config saknas")

CONFIG, PROMPTS_RAW = _load_config()

# Helpers
def get_prompt(agent, key):
    if 'prompts' in PROMPTS_RAW:
        return PROMPTS_RAW['prompts'].get(agent, {}).get(key)
    return PROMPTS_RAW.get(agent, {}).get(key)

def get_setting(agent, key, default):
    val = PROMPTS_RAW.get('settings', {}).get(agent, {}).get(key)
    if val is not None: return val
    return default

# Settings
LAKE_STORE = CONFIG['paths']['lake_store']
FAILED_FOLDER = CONFIG['paths']['asset_failed']
GRAPH_DB_PATH = CONFIG['paths']['graph_db']

API_KEY = CONFIG['ai_engine']['api_key']
MODEL_NAME = CONFIG.get('ai_engine', {}).get('models', {}).get('model_lite', 'models/gemini-2.0-flash-lite-preview')

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - DOCCONV - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('DocConverter')

# Silence external loggers
logging.getLogger("google").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

CLIENT = genai.Client(api_key=API_KEY)

# Patterns
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(txt|md|pdf|docx|csv|xlsx)$', re.IGNORECASE)
STANDARD_TIMESTAMP_PATTERN = re.compile(r'^DATUM_TID:\s+(.+)$', re.MULTILINE)
SLACK_MSG_PATTERN = re.compile(r'^\s*(?:↳\s*)?\[.*?\] (.*?):', re.MULTILINE) # [12:00] Name: Message (handles threads)

PROCESSED_FILES = set()
PROCESS_LOCK = threading.Lock()

# --- ENTITY GATEKEEPER ---
class EntityGatekeeper:
    """
    Hanterar validering av entiteter mot grafen.
    Säkerställer att vi aldrig hittar på entiteter från otillåtna källor.
    """
    def __init__(self):
        # FIX: Check if DB exists before trying to open in read-only mode
        self.graph_store = None
        # DuckDB creates .wal file, so we check specifically for the main db file
        if os.path.exists(GRAPH_DB_PATH):
            try:
                self.graph_store = GraphStore(GRAPH_DB_PATH, read_only=True)
            except Exception as e:
                LOGGER.warning(f"Gatekeeper could not open GraphStore: {e}")
        
        self.schema_validator = SchemaValidator()
        self.indices = {
            "Person": {
                "email": {},
                "slack_alias": {}, 
                "name": {}
            },
            "Organization": {
                "org_number": {}, 
                "name": {}
            },
            "Project": {
                "source_id": {},
                "name": {}
            },
            "Group": {
                "name": {}
            }
        }
        
        if self.graph_store:
            self._load_cache()
        else:
            LOGGER.info("Gatekeeper: No existing graph found. Starting with empty cache.")

    def _load_cache(self):
        LOGGER.info("Gatekeeper: Loading entities from Graph...")
        with self.graph_store:
            # Person
            persons = self.graph_store.find_nodes_by_type("Person")
            for p in persons:
                uuid_str = p['id']
                props = p.get('properties', {})
                
                if 'email' in props: 
                    self.indices["Person"]["email"][props['email'].lower()] = uuid_str
                
                if 'slack_alias' in props: 
                    self.indices["Person"]["slack_alias"][props['slack_alias']] = uuid_str
                
                if 'name' in props:
                    name = props['name'].strip().lower()
                    if name not in self.indices["Person"]["name"]: 
                        self.indices["Person"]["name"][name] = []
                    self.indices["Person"]["name"][name].append(uuid_str)
            
            # Organization
            orgs = self.graph_store.find_nodes_by_type("Organization")
            for o in orgs:
                uuid_str = o['id']
                props = o.get('properties', {})
                if 'org_number' in props: 
                    self.indices["Organization"]["org_number"][props['org_number']] = uuid_str
                if 'name' in props:
                    name = props['name'].strip().lower()
                    # Organizations usually unique by name in loose lookups
                    if name not in self.indices["Organization"]["name"]:
                         self.indices["Organization"]["name"][name] = []
                    self.indices["Organization"]["name"][name].append(uuid_str)
            
            # Project
            projects = self.graph_store.find_nodes_by_type("Project")
            for p in projects:
                uuid_str = p['id']
                props = p.get('properties', {})
                if 'source_id' in props:
                     self.indices["Project"]["source_id"][str(props['source_id'])] = uuid_str
                if 'name' in props:
                    name = props['name'].strip().lower()
                    if name not in self.indices["Project"]["name"]:
                        self.indices["Project"]["name"][name] = []
                    self.indices["Project"]["name"][name].append(uuid_str)

            # Group
            groups = self.graph_store.find_nodes_by_type("Group")
            for g in groups:
                uuid_str = g['id']
                props = g.get('properties', {})
                if 'name' in props:
                    name = props['name'].strip().lower()
                    if name not in self.indices["Group"]["name"]:
                        self.indices["Group"]["name"][name] = []
                    self.indices["Group"]["name"][name].append(uuid_str)

        LOGGER.info(f"Gatekeeper loaded: {len(persons)} Persons, {len(orgs)} Orgs, {len(projects)} Projects, {len(groups)} Groups.")

    def lookup(self, type_str: str, value: str, key_type: str = "name") -> Optional[str]:
        """
        Slå upp UUID för ett värde.
        Returnerar UUID om unikt.
        Returnerar None om 0 träffar eller dubbletter (ambiguous).
        """
        if not value: return None
        value = value.strip().lower()
        if type_str not in self.indices: return None
        if key_type not in self.indices[type_str]: return None
        
        hits = self.indices[type_str][key_type].get(value)
        
        if not hits: return None
        
        # Om det är en lista (Name lookups)
        if isinstance(hits, list):
            if len(hits) == 1:
                return hits[0]
            else:
                LOGGER.warning(f"Gatekeeper: Ambiguous lookup for {type_str} '{value}': {len(hits)} matches. Ignoring.")
                return None
        
        # Om direkt value (Email/OrgNr)
        return hits

    def resolve_entity(self, type_str: str, value: str, source_system: str, context_props: Dict = None) -> Optional[Dict]:
        """
        Huvudmetod för att lösa upp eller skapa entiteter.
        
        Args:
            type_str: "Person", "Organization", etc.
            value: Namn, Email, etc. (beroende på kontext)
            source_system: "Slack", "Mail", "DocConverter"
            context_props: Properties att använda vid skapande (t.ex. {name: "..."})
            
        Returns:
            Dict med {target_uuid, target_type, action, properties?} eller None
        """
        # 1. Försök att slå upp (LINK)
        # Försök olika nycklar beroende på input
        uuid_hit = None
        
        # Enkel heuristik för lookup-nyckel
        lookup_key = "name"
        if "@" in value and type_str == "Person": lookup_key = "email"
        # TODO: Bättre hantering av OrgNr osv? För nu antar vi namn eller email.
        
        uuid_hit = self.lookup(type_str, value, lookup_key)
        
        if uuid_hit:
            return {
                "target_uuid": uuid_hit,
                "target_type": type_str,
                "action": "LINK",
                "confidence": 1.0,
                "source_text": value
            }
        
        # 2. MISS -> Check Schema (CREATE?)
        # Kontrollera om denna source får skapa denna typ
        # Vi använder validate_node för att kolla allowed_sources
        # Vi skickar in dummy props för att validera källan
        dummy_props = context_props or {}
        if "name" not in dummy_props: dummy_props["name"] = value # Minsta krav oftast
        
        is_valid, _, error = self.schema_validator.validate_node(type_str, dummy_props, source_system)
        
        # validate_node returnerar False om source inte är allowed
        # (Den returnerar också False om props är fel, men vi kollar error meddelandet om vi vill vara petiga, 
        # eller så litar vi på att om det är en Trusted Source så har vi skickat rätt props)
        
        # Om vi får "Source ... not allowed" i error, då är det kört.
        if error and "not allowed for" in error:
            LOGGER.debug(f"Gatekeeper: Denied creation of {type_str} from {source_system} ({value}). Error: {error}")
            return None
            
        # Om det var annat valideringsfel (t.ex. missing required prop) så ska vi logga det men inte skapa
        if not is_valid:
            LOGGER.debug(f"Gatekeeper: Validation failed for potential new {type_str} from {source_system}: {error}")
            return None
            
        # 3. CREATE ALLOWED
        new_uuid = str(uuid.uuid4())
        
        # Förbered properties för skapande
        creation_props = context_props.copy() if context_props else {}
        if type_str == "Person":
            if "@" in value and "email" not in creation_props: creation_props["email"] = value
            if "name" not in creation_props: creation_props["name"] = value
        elif "name" not in creation_props:
            creation_props["name"] = value
            
        return {
            "target_uuid": new_uuid,
            "target_type": type_str,
            "action": "CREATE",
            "confidence": 1.0,
            "properties": creation_props,
            "source_text": value
        }

# Global Instance
GATEKEEPER = None

# --- TEXT EXTRACTION ---
def extract_text(filväg: str, ext: str) -> str | None:
    try:
        text = ""
        ext = ext.lower()
        if ext == '.pdf':
            with fitz.open(filväg) as doc:
                for page in doc:
                    blocks = page.get_text("blocks")
                    for b in blocks:
                        if b[6] == 0:  # Text block
                            text += b[4] + "\n"
        elif ext == '.docx':
            doc = docx.Document(filväg)
            for para in doc.paragraphs: text += para.text + "\n"
        elif ext == '.csv':
            try:
                df = pd.read_csv(filväg)
                text = df.to_markdown(index=False)
            except: text = "CSV Error"
        elif ext in ['.xlsx', '.xls']:
            try:
                sheets = pd.read_excel(filväg, sheet_name=None)
                parts = []
                for name, df in sheets.items():
                    parts.append(f"### Sheet: {name}")
                    parts.append(df.to_markdown(index=False))
                text = "\n\n".join(parts)
            except: text = "Excel Error"
        elif ext in ['.txt', '.md', '.json']:
            with open(filväg, 'r', encoding='utf-8', errors='ignore') as f: text = f.read()
        return text.strip()
    except Exception as e:
        LOGGER.error(f"Text extraction failed {filväg}: {e}")
        return None

def get_best_timestamp(filepath: str, text_content: str) -> str:
    match = STANDARD_TIMESTAMP_PATTERN.search(text_content)
    if match: return match.group(1).strip()
    try:
        ts = date_service_timestamp(filepath)
        tz = zoneinfo.ZoneInfo(CONFIG.get('system', {}).get('timezone', 'UTC'))
        if ts.tzinfo is None: ts = ts.replace(tzinfo=tz)
        return ts.isoformat()
    except: return datetime.datetime.now().isoformat()

def generate_metadata(text):
    raw_prompt = get_prompt('doc_converter', 'generate_metadata')
    if not raw_prompt: return {"summary": "", "keywords": []}

    chunk_size = get_setting('doc_converter', 'chunk_size_metadata', 10000)
    prompt = raw_prompt.format(text_chunk=text[:chunk_size])
    try:
        response = CLIENT.models.generate_content(
            model=MODEL_NAME,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        return parse_llm_json(response.text)
    except Exception as e:
        LOGGER.error(f"Generate Metadata Error: {e}")
        return {"summary": "", "keywords": []}

def strict_entity_extraction(text):
    raw_prompt = get_prompt('doc_converter', 'strict_entity_extraction')
    if not raw_prompt: 
        LOGGER.error("Saknar prompt: strict_entity_extraction")
        return {"candidates": [], "dates": []}

    chunk_size = get_setting('doc_converter', 'chunk_size_extraction', 15000)
    prompt = raw_prompt.format(text_chunk=text[:chunk_size])
    try:
        response = CLIENT.models.generate_content(
            model=MODEL_NAME,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        return parse_llm_json(response.text)
    except Exception as e:
        LOGGER.error(f"Strict Extraction Error: {e}")
        return {"candidates": [], "dates": []}

# --- STRATEGY HANDLERS ---

def handle_slack_file(text: str, filename: str) -> List[Dict]:
    """
    Strategi för Slack-loggar (Trusted Source).
    Regex: [Time] Name: Msg
    """
    mentions = []
    seen_names = set()
    
    # Hitta alla talare
    matches = SLACK_MSG_PATTERN.findall(text)
    
    for name in matches:
        name = name.strip()
        if name in seen_names: continue
        seen_names.add(name)
        
        # Anropa Gatekeeper (Trusted Source = Slack)
        # Om CREATION: Sätt type="INTERNAL" för Slack-användare (oftast kollegor)
        context = {"name": name, "type": "INTERNAL"}
        
        if GATEKEEPER is None:
            continue
            
        result = GATEKEEPER.resolve_entity("Person", name, "Slack", context)
        
        if result:
            mentions.append(result)
            
    return mentions



# --- Regex patterns för Mail ---
MAIL_FROM_PATTERN = re.compile(r'^From:\s*(?:"?([^"<]+)"?\s*)?<?([^>\s]+@[^>\s]+)>?', re.MULTILINE | re.IGNORECASE)
MAIL_TO_PATTERN = re.compile(r'^To:\s*(.+?)$', re.MULTILINE | re.IGNORECASE)
MAIL_CC_PATTERN = re.compile(r'^Cc:\s*(.+?)$', re.MULTILINE | re.IGNORECASE)
MAIL_EMAIL_PATTERN = re.compile(r'<?([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)>?')


def handle_mail_file(text: str, filename: str) -> List[Dict]:
    """
    Strategi för Mail-filer (Trusted Source).
    Parsear headers (From, To, Cc) för att hitta personer.
    """
    mentions = []
    seen_emails = set()
    
    # 1. Parsa From-header
    from_match = MAIL_FROM_PATTERN.search(text)
    if from_match:
        name = from_match.group(1)
        email = from_match.group(2)
        if email and email.lower() not in seen_emails:
            seen_emails.add(email.lower())
            context = {"name": name.strip() if name else email, "email": email.lower()}
            result = GATEKEEPER.resolve_entity("Person", email, "Mail", context)
            if result:
                mentions.append(result)
    
    # 2. Parsa To-header
    to_match = MAIL_TO_PATTERN.search(text)
    if to_match:
        for email_match in MAIL_EMAIL_PATTERN.finditer(to_match.group(1)):
            email = email_match.group(1).lower()
            if email not in seen_emails:
                seen_emails.add(email)
                context = {"email": email}
                result = GATEKEEPER.resolve_entity("Person", email, "Mail", context)
                if result:
                    mentions.append(result)
    
    # 3. Parsa Cc-header
    cc_match = MAIL_CC_PATTERN.search(text)
    if cc_match:
        for email_match in MAIL_EMAIL_PATTERN.finditer(cc_match.group(1)):
            email = email_match.group(1).lower()
            if email not in seen_emails:
                seen_emails.add(email)
                context = {"email": email}
                result = GATEKEEPER.resolve_entity("Person", email, "Mail", context)
                if result:
                    mentions.append(result)
    
    # 4. Komplettera med LLM-extraktion för innehållet (men med Mail som källa)
    data = strict_entity_extraction(text)
    candidates = data.get('candidates', [])
    
    seen_candidates = set()
    for cand in candidates:
        name = cand.get('text')
        type_str = cand.get('type')
        
        if not name or not type_str: continue
        
        key = f"{name}|{type_str}"
        if key in seen_candidates: continue
        seen_candidates.add(key)
        
        # Mail är trusted source - kan skapa entiteter
        context = {"name": name} if type_str == "Person" else {}
        result = GATEKEEPER.resolve_entity(type_str, name, "Mail", context)
        
        if result:
            mentions.append(result)
    
    return mentions


# --- Regex patterns för Calendar ---
CALENDAR_ATTENDEE_PATTERN = re.compile(r'(?:Deltagare|Attendee|Participant)s?:\s*(.+?)(?:\n\n|\n[A-Z]|$)', re.IGNORECASE | re.DOTALL)
CALENDAR_ORGANIZER_PATTERN = re.compile(r'(?:Organisatör|Organizer|Arrangör):\s*(.+?)$', re.MULTILINE | re.IGNORECASE)


def handle_calendar_file(text: str, filename: str) -> List[Dict]:
    """
    Strategi för Calendar-filer (Trusted Source).
    Parsear deltagare och organisatör.
    """
    mentions = []
    seen_names = set()
    
    # 1. Parsa organisatör
    org_match = CALENDAR_ORGANIZER_PATTERN.search(text)
    if org_match:
        organizer = org_match.group(1).strip()
        # Kan vara email eller namn
        if organizer and organizer.lower() not in seen_names:
            seen_names.add(organizer.lower())
            context = {"name": organizer}
            result = GATEKEEPER.resolve_entity("Person", organizer, "Calendar", context)
            if result:
                mentions.append(result)
    
    # 2. Parsa deltagare
    attendee_match = CALENDAR_ATTENDEE_PATTERN.search(text)
    if attendee_match:
        attendees_str = attendee_match.group(1)
        # Splitta på komma, semikolon, eller newline
        for attendee in re.split(r'[;,\n]+', attendees_str):
            attendee = attendee.strip()
            # Rensa bort email-format om det finns
            email_match = MAIL_EMAIL_PATTERN.search(attendee)
            if email_match:
                attendee = email_match.group(1)
            
            if attendee and len(attendee) > 2 and attendee.lower() not in seen_names:
                seen_names.add(attendee.lower())
                context = {"name": attendee}
                result = GATEKEEPER.resolve_entity("Person", attendee, "Calendar", context)
                if result:
                    mentions.append(result)
    
    # 3. Komplettera med LLM-extraktion (med Calendar som källa)
    data = strict_entity_extraction(text)
    candidates = data.get('candidates', [])
    
    seen_candidates = set()
    for cand in candidates:
        name = cand.get('text')
        type_str = cand.get('type')
        
        if not name or not type_str: continue
        
        key = f"{name}|{type_str}"
        if key in seen_candidates: continue
        seen_candidates.add(key)
        
        # Calendar är trusted source - kan skapa entiteter
        context = {"name": name} if type_str == "Person" else {}
        result = GATEKEEPER.resolve_entity(type_str, name, "Calendar", context)
        
        if result:
            mentions.append(result)
    
    return mentions


def handle_unstructured_file(text: str, filename: str) -> List[Dict]:
    """
    Strategi för Dokument (Untrusted Source).
    Använder LLM + Gatekeeper (LINK only).
    """
    mentions = []
    
    # LLM Extraction
    data = strict_entity_extraction(text)
    candidates = data.get('candidates', [])
    
    seen_candidates = set()
    
    for cand in candidates:
        name = cand.get('text')
        type_str = cand.get('type')
        
        if not name or not type_str: continue
        
        key = f"{name}|{type_str}"
        if key in seen_candidates: continue
        seen_candidates.add(key)
        
        # Anropa Gatekeeper (Untrusted Source = DocConverter)
        # Kontext props spelar ingen roll då DocConverter inte får skapa
        result = GATEKEEPER.resolve_entity(type_str, name, "DocConverter")
        
        if result:
            mentions.append(result)
            
    return mentions

# --- MAIN PROCESS ---
def processa_dokument(filväg: str, filnamn: str):
    with PROCESS_LOCK:
        if filnamn in PROCESSED_FILES: return
        PROCESSED_FILES.add(filnamn)

    match = UUID_SUFFIX_PATTERN.search(filnamn)
    if not match: return
    unit_id = match.group(1)
    
    base_name = os.path.splitext(filnamn)[0]
    lake_file = os.path.join(LAKE_STORE, f"{base_name}.md")
    
    # IDEMPOTENS
    if os.path.exists(lake_file):
        LOGGER.debug(f"⏭️  Skippar (redan klar): {filnamn}")
        return

    LOGGER.debug(f"⚙️ Bearbetar: {filnamn}")
    try:
        ext = os.path.splitext(filnamn)[1]
        raw_text = extract_text(filväg, ext)
        if not raw_text or len(raw_text) < 10:
            _move_to_failed(filväg)
            return

        ts = get_best_timestamp(filväg, raw_text)
        
        # Metadata (Summary/Keywords)
        meta = generate_metadata(raw_text)

        # --- VALIDATED MENTIONS ---
        validated_mentions = []
        
        # Identifiera strategi baserat på path (Trusted vs Untrusted Sources)
        is_slack = "asset_slack" in filväg.lower() or "slack" in filväg.lower()
        is_mail = "asset_mail" in filväg.lower() or "mail" in filväg.lower()
        is_calendar = "asset_calendar" in filväg.lower() or "calendar" in filväg.lower()
        
        if is_slack:
            # TRUSTED SOURCE: Slack - kan skapa Person
            validated_mentions = handle_slack_file(raw_text, filnamn)
        elif is_mail:
            # TRUSTED SOURCE: Mail - kan skapa Person
            validated_mentions = handle_mail_file(raw_text, filnamn)
        elif is_calendar:
            # TRUSTED SOURCE: Calendar - kan skapa Person
            validated_mentions = handle_calendar_file(raw_text, filnamn)
        else:
            # UNTRUSTED SOURCE: Documents - kan bara länka till existerande
            validated_mentions = handle_unstructured_file(raw_text, filnamn)
            
        # Spara till Lake
        frontmatter = {
            "unit_id": unit_id,
            "source_ref": lake_file,
            "original_filename": filnamn,
            "timestamp_created": ts,
            "summary": meta.get('summary', ''),
            "keywords": meta.get('keywords', []),
            "graph_context_status": "pending_validation",
            "ai_model": MODEL_NAME,
            "validated_mentions": validated_mentions
        }
        
        fm_str = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)
        content = f"---\n{fm_str}---\n\n# Dokument: {filnamn}\n\n{raw_text}"
        
        with open(lake_file, 'w', encoding='utf-8') as f:
            f.write(content)
            
        LOGGER.info(f"✅ Klar: {filnamn} (Hittade {len(validated_mentions)} mentions)")

    except Exception as e:
        LOGGER.error(f"HARDFAIL {filnamn}: {e}", exc_info=True)
        _move_to_failed(filväg)

def _move_to_failed(filepath):
    os.makedirs(FAILED_FOLDER, exist_ok=True)
    try:
        shutil.move(filepath, os.path.join(FAILED_FOLDER, os.path.basename(filepath)))
    except Exception as e:
        LOGGER.error(f"Kunde inte flytta till failed: {filepath} -> {e}")

# --- WATCHDOG ---
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class DocHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory: return
        filename = os.path.basename(event.src_path)
        if not filename.startswith('.'):
            threading.Thread(target=processa_dokument, args=(event.src_path, filename)).start()

    def on_moved(self, event):
        if event.is_directory: return
        filename = os.path.basename(event.dest_path)
        if not filename.startswith('.'):
            threading.Thread(target=processa_dokument, args=(event.dest_path, filename)).start()

if __name__ == "__main__":
    # Initiera Gatekeeper
    try:
        GATEKEEPER = EntityGatekeeper()
    except Exception as e:
        print(f"[CRITICAL] Kunde inte initiera Gatekeeper: {e}")
        sys.exit(1)

    folders = [
        CONFIG['paths']['asset_documents'],
        CONFIG['paths']['asset_transcripts'],
        CONFIG['paths']['asset_slack'],
        CONFIG.get('paths', {}).get('asset_calendar'),
        CONFIG.get('paths', {}).get('asset_mail')
    ]
    os.makedirs(LAKE_STORE, exist_ok=True)
    
    print(f"DocConverter (Strict Mode - Trusted vs Untrusted) online.")
    
    # Initial Scan
    with ThreadPoolExecutor(max_workers=5) as executor:
        for folder in folders:
            if folder and os.path.exists(folder):
                for f in os.listdir(folder):
                    if UUID_SUFFIX_PATTERN.search(f):
                        executor.submit(processa_dokument, os.path.join(folder, f), f)

    observer = Observer()
    for folder in folders:
        if folder and os.path.exists(folder):
            observer.schedule(DocHandler(), folder, recursive=False)
    
    observer.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
