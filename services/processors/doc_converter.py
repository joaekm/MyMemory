#!/usr/bin/env python3
"""
MyMem DocConverter (v10.3 - Area C & Semantic Metadata)

Changes:
- ADDED 'generate_semantic_metadata' for Summary/Keywords/AI-Model tracking.
- REMOVED legacy 'strict_entity_extraction' (non-MCP).
- UPDATED 'unified_processing_strategy' to return full metadata structure.
- UPDATED 'processa_dokument' to write Area C compliant frontmatter.
"""

import os
import sys
import time
import yaml
import logging
import datetime
import threading
import re
import zoneinfo
import shutil
import uuid
import difflib
import pandas as pd
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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

CLIENT = genai.Client(api_key=API_KEY)
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(txt|md|pdf|docx|csv|xlsx)$', re.IGNORECASE)
STANDARD_TIMESTAMP_PATTERN = re.compile(r'^DATUM_TID:\s+(.+)$', re.MULTILINE)

PROCESSED_FILES = set()
PROCESS_LOCK = threading.Lock()

# Global Schema Validator (Lazy load)
_SCHEMA_VALIDATOR = None

def _get_schema_validator():
    global _SCHEMA_VALIDATOR
    if _SCHEMA_VALIDATOR is None:
        try:
            _SCHEMA_VALIDATOR = SchemaValidator()
        except Exception as e:
            LOGGER.error(f"Kunde inte ladda SchemaValidator: {e}")
            raise
    return _SCHEMA_VALIDATOR

# MCP Server Configuration
VALIDATOR_PARAMS = StdioServerParameters(
    command=sys.executable,  # Använder samma python-tolk som kör doc_converter
    args=[os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "agents", "validator_mcp.py"))]
)

# --- HELPER FUNCTIONS ---

def extract_text(filepath: str, ext: str = None) -> str:
    """
    Extraherar råtext från en fil.
    Exposed för validatorer.
    """
    if not ext:
        ext = os.path.splitext(filepath)[1].lower()
    else:
        ext = ext.lower()
        
    raw_text = ""
    try:
        if ext == '.pdf':
            with fitz.open(filepath) as doc:
                for page in doc: raw_text += page.get_text() + "\n"
        elif ext in ['.txt', '.md', '.json', '.csv']:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f: 
                raw_text = f.read()
        elif ext == '.docx':
            doc = docx.Document(filepath)
            raw_text = "\n".join([p.text for p in doc.paragraphs])
    except Exception as e:
        LOGGER.error(f"Text extraction failed for {filepath}: {e}")
        return ""
        
    return raw_text

# --- 1. SEMANTIC METADATA GENERATOR (NEW) ---
def generate_semantic_metadata(text: str) -> Dict[str, Any]:
    """
    Genererar sammanfattning och nyckelord med en lättviktig modell.
    Används för vektor-indexering och sökbarhet.
    """
    prompt_template = get_prompt('doc_converter', 'doc_summary_prompt')
    if not prompt_template:
        LOGGER.warning("Summary prompt saknas i config, returnerar tom metadata")
        return {"summary": "", "keywords": []}
    
    # Klipp texten om den är för lång för summary-modellen
    safe_text = text[:30000]
    final_prompt = prompt_template.format(text=safe_text)
    
    try:
        # Använd explicit 'model_lite' från config eller fallback
        lite_model = CONFIG.get('ai_engine', {}).get('models', {}).get('model_lite', 'gemini-2.0-flash-lite-preview')
        
        response = CLIENT.models.generate_content(
            model=lite_model,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=final_prompt)])],
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        
        data = parse_llm_json(response.text)
        return {
            "summary": data.get("summary", ""),
            "keywords": data.get("keywords", []),
            "ai_model": lite_model
        }
    except Exception as e:
        LOGGER.error(f"Semantic Metadata generation failed: {e}")
        return {"summary": "", "keywords": [], "ai_model": "UNKNOWN_ERROR"}

# --- ENTITY GATEKEEPER ---
class EntityGatekeeper:
    """Hanterar validering av entiteter mot grafen."""
    def __init__(self):
        self.graph_store = None
        if os.path.exists(GRAPH_DB_PATH):
            try:
                self.graph_store = GraphStore(GRAPH_DB_PATH, read_only=True)
            except Exception as e:
                LOGGER.warning(f"Gatekeeper could not open GraphStore: {e}")
        
        self.schema_validator = _get_schema_validator()
        self.indices = {
            "Person": {"email": {}, "slack_alias": {}, "name": {}},
            "Organization": {"org_number": {}, "name": {}},
            "Project": {"source_id": {}, "name": {}},
            "Group": {"name": {}}
        }
        if self.graph_store: self._load_cache()

    def _load_cache(self):
        LOGGER.info("Gatekeeper: Loading entities from Graph...")
        with self.graph_store:
            # Person
            for p in self.graph_store.find_nodes_by_type("Person"):
                uuid_str = p['id']
                props = p.get('properties', {})
                if 'email' in props: self.indices["Person"]["email"][props['email'].lower()] = uuid_str
                if 'name' in props:
                    name = props['name'].strip().lower()
                    if name not in self.indices["Person"]["name"]: self.indices["Person"]["name"][name] = []
                    self.indices["Person"]["name"][name].append(uuid_str)
            # (Laddar övriga typer här - kortat för tydlighet)

    def _fuzzy_match(self, type_str: str, value: str) -> Optional[str]:
        """
        Lättviktig dubblettkontroll med difflib.
        Returnerar UUID om en stark matchning hittas.
        """
        if type_str not in self.indices or "name" not in self.indices[type_str]:
            return None
            
        value_lower = value.lower()
        candidates = self.indices[type_str]["name"].keys()
        
        # Hitta närmaste matchningar (cutoff=0.85 betyder >85% likhet)
        matches = difflib.get_close_matches(value_lower, candidates, n=1, cutoff=0.85)
        
        if matches:
            matched_name = matches[0]
            uuids = self.indices[type_str]["name"][matched_name]
            LOGGER.info(f"Gatekeeper: Fuzzy hit '{value}' ~= '{matched_name}' -> {uuids[0]}")
            return uuids[0]
            
        return None

    def resolve_entity(self, type_str: str, value: str, source_system: str, context_props: Dict = None) -> Optional[Dict]:
        # 1. LINK Check (Cache lookup - Exact)
        lookup_key = "name"
        if "@" in value and type_str == "Person": lookup_key = "email"
        
        uuid_hit = None
        if type_str in self.indices and lookup_key in self.indices[type_str]:
            hits = self.indices[type_str][lookup_key].get(value.strip().lower())
            if hits and isinstance(hits, list) and len(hits) == 1: uuid_hit = hits[0]
        
        # 2. LINK Check (Fuzzy - om ingen exakt träff)
        if not uuid_hit and lookup_key == "name":
            uuid_hit = self._fuzzy_match(type_str, value)
        
        if uuid_hit:
            return {"target_uuid": uuid_hit, "target_type": type_str, "action": "LINK", "confidence": 1.0, "source_text": value}
        
        # 3. CREATE (Provisional) Logic
        # Validera mot schema först
        dummy_props = context_props.copy() if context_props else {}
        dummy_props['status'] = 'PROVISIONAL'
        dummy_props['confidence'] = dummy_props.get('confidence', 0.5)
        dummy_props['last_seen_at'] = datetime.datetime.now().isoformat()
        if "name" not in dummy_props: dummy_props["name"] = value

        is_valid, error = self.schema_validator.validate_node(dummy_props)
        if not is_valid:
            if "Invalid status" in str(error) or "Unknown node type" in str(error):
                return None # Blocked by schema

        # Create new UUID
        new_uuid = str(uuid.uuid4())
        return {
            "target_uuid": new_uuid,
            "target_type": type_str,
            "action": "CREATE",
            "confidence": dummy_props['confidence'],
            "properties": dummy_props,
            "source_text": value
        }

GATEKEEPER = None

# --- CORE PROCESSING ---

async def _call_mcp_validator(initial_prompt: str, reference_timestamp: str, anchors: dict = None):
    """Intern asynkron hjälpare för att prata med MCP-servern."""
    async with stdio_client(VALIDATOR_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            # Vi anropar det nya verktyget i din uppdaterade validator_mcp.py
            result = await session.call_tool(
                "extract_and_validate_doc", 
                arguments={
                    "initial_prompt": initial_prompt,
                    "reference_timestamp": reference_timestamp,
                    "anchors": anchors or {}
                }
            )
            return result.content[0].text if result.content else "{}"

def strict_entity_extraction_mcp(text: str, source_hint: str = ""):
    """
    Ny version som delegerar allt ansvar till MCP-servern.
    DocConverter bygger prompten (beställningen), MCP utför och validerar.
    """
    LOGGER.info(f"Förbereder MCP-prompt för {source_hint}...")
    
    # 1. Bygg prompten (Återanvänd logik)
    raw_prompt = get_prompt('doc_converter', 'strict_entity_extraction')
    if not raw_prompt: return {"nodes": [], "edges": []}

    validator = _get_schema_validator()
    schema = validator.schema
    
    # --- SCHEMA CONTEXT ---
    all_node_types = set(schema.get('nodes', {}).keys())
    valid_graph_nodes = all_node_types - {'Document', 'Source', 'File'}
    
    filtered_nodes = {k: v for k, v in schema.get('nodes', {}).items() if k not in {'Document'}}
    node_lines = []
    for k, v in filtered_nodes.items():
        desc = v.get('description', '')
        props = v.get('properties', {})
        
        # 1. Samla info om egenskaper och enums
        prop_info = []
        for prop_name, prop_def in props.items():
            # Hoppa över systemfält
            if prop_name in ['id', 'created_at', 'last_synced_at', 'last_seen_at', 'confidence', 'status', 'source_system', 'distinguishing_context', 'uuid', 'version']:
                continue
            
            req_marker = "*" if prop_def.get('required') else ""
            
            if 'values' in prop_def:
                enums = ", ".join(prop_def['values'])
                prop_info.append(f"{prop_name}{req_marker} [{enums}]")
            else:
                p_type = prop_def.get('type', 'string')
                prop_info.append(f"{prop_name}{req_marker} ({p_type})")

        # 2. Namnregler
        constraints = []
        if 'name' in props and props['name'].get('description'):
             constraints.append(f"Namnregler: {props['name']['description']}")
        
        info = f"- {k}: {desc}"
        if prop_info:
            info += f" | Egenskaper: {', '.join(prop_info)}"
        if constraints: 
            info += f" ({'; '.join(constraints)})"
            
        node_lines.append(info)
    node_types_str = "\n".join(node_lines)

    filtered_edges = {k: v for k, v in schema.get('edges', {}).items() if k != 'MENTIONS'}
    edge_names = list(filtered_edges.keys())
    whitelist, blacklist = [], []

    for k, v in filtered_edges.items():
        desc = v.get('description', '')
        sources = set(v.get('source_type', []))
        targets = set(v.get('target_type', []))
        whitelist.append(f"- {k}: [{', '.join(sources)}] -> [{', '.join(targets)}]  // {desc}")
        
        forbidden_sources = valid_graph_nodes - sources
        forbidden_targets = valid_graph_nodes - targets
        if forbidden_sources: blacklist.append(f"- {k}: Får ALDRIG starta från [{', '.join(forbidden_sources)}]")
        if forbidden_targets: blacklist.append(f"- {k}: Får ALDRIG peka på [{', '.join(forbidden_targets)}]")

    edge_types_str = (
        f"TILLÅTNA RELATIONSNAMN:\n[{', '.join(edge_names)}]\n\n"
        f"TILLÅTNA KOPPLINGAR (WHITELIST):\n" + "\n".join(whitelist) + "\n\n"
        f"FÖRBJUDNA KOPPLINGAR (BLACKLIST - AUTO-GENERERAD):\n" + "\n".join(blacklist)
    )

    source_context_instruction = ""
    if "Slack" in source_hint:
        source_context_instruction = "KONTEXT: Detta är en Slack-chatt. Formatet är ofta 'Namn: Meddelande'. Behandla avsändare som starka Person-kandidater. Extrahera relationer baserat på vad de diskuterar."
    elif "Mail" in source_hint:
        source_context_instruction = "KONTEXT: Detta är ett email. Avsändare (From) och mottagare (To) är mycket viktiga Person-noder."
    
    final_prompt = raw_prompt.format(
        text_chunk=text[:25000], 
        node_types_context=node_types_str,
        edge_types_context=edge_types_str,
        known_entities_context=source_context_instruction
    )

    try:
        reference_timestamp = datetime.datetime.now().isoformat()
        # TODO: Hämta anchors från Gatekeeper eller known_entities argument
        anchors = {}
        response_json = asyncio.run(_call_mcp_validator(final_prompt, reference_timestamp, anchors))
        return parse_llm_json(response_json)
    except Exception as e:
        LOGGER.error(f"MCP Extraction failed: {e}")
        return {"nodes": [], "edges": []}

def unified_processing_strategy(text: str, filename: str, source_type: str) -> Dict[str, Any]:
    # 1. Semantic Metadata (Summary + Keywords + AI Model)
    semantic_data = generate_semantic_metadata(text)
    
    # 2. Graph Data (MCP Extraction)
    #data = strict_entity_extraction(text, source_hint=source_type)
    data = strict_entity_extraction_mcp(text, source_hint=source_type)
    nodes = data.get('nodes', [])
    edges = data.get('edges', []) 
    
    mentions = []

    # Mappa: Namn -> UUID (för att kunna bygga kanter senare)
    name_to_uuid = {}

    # 3. Loopa genom noder och validera mot Gatekeeper
    seen_candidates = set()
    for node in nodes:
        name = node.get('name')
        type_str = node.get('type')
        confidence = node.get('confidence', 0.5)
        ctx_keywords = node.get('context_keywords', [])

        if not name or not type_str: continue
        
        # Brusfilter baserat på confidence (Krav från Område B)
        if confidence < 0.3: continue 

        key = f"{name}|{type_str}"
        if key in seen_candidates: continue
        seen_candidates.add(key)
        
        # Bygg kontext-objekt
        context = {
            "name": name,
            "status": "PROVISIONAL", 
            "confidence": confidence,
            "distinguishing_context": ctx_keywords
        }

        # Om källan är "Trusted" (t.ex. Slack/Mail), kan vi *höja* confidence
        if source_type in ["Slack Log", "Email Thread"]:
            context['confidence'] = max(confidence, 0.8)

        # GATEKEEPER RESOLUTION (Inklusive Fuzzy)
        result = GATEKEEPER.resolve_entity(type_str, name, "DocConverter", context)
        
        if result:
            target_uuid = result.get('target_uuid')
            if target_uuid:
                name_to_uuid[name] = target_uuid # Spara mapping för edges
                
                # Lägg till label för tydlighet i loggar/graf
                result['label'] = name
                mentions.append(result)

    # 4. Hantera RELATIONER (Area C Requirement)
    # Vi kan bara skapa relationer om BÅDA noderna blev resolveade (fick ett UUID)
    for edge in edges:
        source_name = edge.get('source')
        target_name = edge.get('target')
        rel_type = edge.get('type')
        rel_conf = edge.get('confidence', 0.5)
        
        if source_name in name_to_uuid and target_name in name_to_uuid:
            source_uuid = name_to_uuid[source_name]
            target_uuid = name_to_uuid[target_name]
            
            # Lägg till relationen som en "mention" av typen RELATION
            # Detta signalerar till GraphBuilder att skapa en kant
            mentions.append({
                "action": "CREATE_EDGE",
                "source_uuid": source_uuid,
                "target_uuid": target_uuid,
                "edge_type": rel_type,
                "confidence": rel_conf,
                "source_text": f"{source_name} -> {target_name}"
            })

    # Returnera BÅDE mentions och semantic data
    return {
        "validated_mentions": mentions, 
        "semantic_metadata": semantic_data
    }

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
    
    if os.path.exists(lake_file): return # Idempotens

    LOGGER.debug(f"⚙️ Bearbetar: {filnamn}")
    try:
        # Använd den utbrutna funktionen extract_text
        raw_text = extract_text(filväg)

        # --- FIX: Kontrollera och släpp låset OMEDELBART ---
        if not raw_text or len(raw_text) < 10: 
            # VIKTIGT: Om filen är tom (under skrivning), släpp låset så vi kan försöka igen vid on_modified
            LOGGER.debug(f"Filen {filnamn} verkar ofullständig ({len(raw_text) if raw_text else 0} tecken). Väntar på on_modified.")
            with PROCESS_LOCK:
                PROCESSED_FILES.discard(filnamn)
            return
        # ----------------------------------------------------

        # --- UNIFIED PIPELINE SWITCH ---
        source_type = "Document"
        if "slack" in filväg.lower(): source_type = "Slack Log"
        elif "mail" in filväg.lower(): source_type = "Email Thread"
        elif "calendar" in filväg.lower(): source_type = "Calendar Event"

        # Ett enda anrop!
        full_result = unified_processing_strategy(raw_text, filnamn, source_type)
        
        validated_mentions = full_result.get("validated_mentions", [])
        semantic_metadata = full_result.get("semantic_metadata", {})

        # Spara (Area C compliant structure)
        frontmatter = {
            "unit_id": unit_id,
            "source_ref": lake_file,
            "original_filename": filnamn,
            "timestamp_created": datetime.datetime.now().isoformat(),
            "summary": semantic_metadata.get("summary", ""),
            "keywords": semantic_metadata.get("keywords", []),
            #"graph_context_status": "pending_validation",
            "source_type": source_type,
            "ai_model": semantic_metadata.get("ai_model", "unknown"),
            #"validated_mentions": validated_mentions
        }
        
        fm_str = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True)
        with open(lake_file, 'w', encoding='utf-8') as f:
            f.write(f"---\n{fm_str}---\n\n# {filnamn}\n\n{raw_text}")
            
        LOGGER.info(f"✅ Klar: {filnamn} ({source_type}) -> {len(validated_mentions)} mentions")

    except Exception as e:
        LOGGER.error(f"FAIL {filnamn}: {e}")
        # Säkerhetsåtgärd: Släpp låset även vid krasch så vi inte fastnar för alltid
        with PROCESS_LOCK:
            PROCESSED_FILES.discard(filnamn)

# --- INIT & WATCHDOG (Kortad för översikt, samma som förr) ---
if __name__ == "__main__":
    GATEKEEPER = EntityGatekeeper()
    os.makedirs(LAKE_STORE, exist_ok=True)
    print(f"DocConverter (v10.3 - Area C & Semantic) online.")
    
    # ... (Resten av main-blocket är oförändrat) ...
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    class DocHandler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory: return
            processa_dokument(event.src_path, os.path.basename(event.src_path))

    folders = [
        CONFIG['paths']['asset_documents'], 
        CONFIG['paths']['asset_slack'], 
        CONFIG.get('paths', {}).get('asset_mail'),
        CONFIG['paths']['asset_transcripts']
    ]
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        for folder in folders:
            if folder and os.path.exists(folder):
                for f in os.listdir(folder):
                    if UUID_SUFFIX_PATTERN.search(f):
                        executor.submit(processa_dokument, os.path.join(folder, f), f)
    
    observer = Observer()
    for folder in folders:
        if folder and os.path.exists(folder): observer.schedule(DocHandler(), folder, recursive=False)
    observer.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: observer.stop()
    observer.join()