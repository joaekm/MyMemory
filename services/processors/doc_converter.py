#!/usr/bin/env python3
"""
MyMem DocConverter (v11.0 - Unified Pipeline)

Unified ingestion: Assets → Lake → Vector → Graf i en pipeline.
Entity resolution via GraphService.find_node_by_name().
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
from services.utils.graph_service import GraphService
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
LAKE_STORE = os.path.expanduser(CONFIG['paths']['lake_store'])
FAILED_FOLDER = os.path.expanduser(CONFIG['paths']['asset_failed'])
GRAPH_DB_PATH = os.path.expanduser(CONFIG['paths']['graph_db'])

API_KEY = CONFIG['ai_engine']['api_key']
MODEL_NAME = CONFIG.get('ai_engine', {}).get('models', {}).get('model_lite', 'models/gemini-2.0-flash-lite-preview')

# Processing limits från config
PROCESSING_CONFIG = CONFIG.get('processing', {})
SUMMARY_MAX_CHARS = PROCESSING_CONFIG.get('summary_max_chars', 30000)
HEADER_SCAN_CHARS = PROCESSING_CONFIG.get('header_scan_chars', 3000)

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - DOCCONV - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('DocConverter')

CLIENT = genai.Client(api_key=API_KEY)
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(txt|md|pdf|docx|csv|xlsx)$', re.IGNORECASE)
STANDARD_TIMESTAMP_PATTERN = re.compile(r'^DATUM_TID:\s+(.+)$', re.MULTILINE)
# Transcriber-format: DATUM: 2025-12-15 och START: 14:30
TRANSCRIBER_DATE_PATTERN = re.compile(r'^DATUM:\s+(\d{4}-\d{2}-\d{2})$', re.MULTILINE)
TRANSCRIBER_START_PATTERN = re.compile(r'^START:\s+(\d{2}:\d{2})$', re.MULTILINE)


def extract_content_date(text: str) -> str:
    """
    Extraherar timestamp_content - när innehållet faktiskt hände.

    Strikt extraktion utan fallbacks:
    1. DATUM_TID header (från collectors: Slack, Calendar, Gmail) → ISO-sträng
    2. Transcriber-format DATUM + START → kombineras till ISO-sträng
    3. Annars → "UNKNOWN"

    Returns:
        ISO-format sträng eller "UNKNOWN"
    """
    header_section = text[:HEADER_SCAN_CHARS]  # Headers är alltid i början

    # 1. Försök DATUM_TID (collectors: Slack, Calendar, Gmail)
    match = STANDARD_TIMESTAMP_PATTERN.search(header_section)
    if match:
        ts_str = match.group(1).strip()
        try:
            dt = datetime.datetime.fromisoformat(ts_str)
            LOGGER.debug(f"extract_content_date: DATUM_TID → {dt.isoformat()}")
            return dt.isoformat()
        except ValueError:
            LOGGER.warning(f"extract_content_date: Ogiltig DATUM_TID '{ts_str}'")

    # 2. Försök Transcriber-format (DATUM + START)
    date_match = TRANSCRIBER_DATE_PATTERN.search(header_section)
    start_match = TRANSCRIBER_START_PATTERN.search(header_section)

    if date_match and start_match:
        date_str = date_match.group(1)
        time_str = start_match.group(1)
        try:
            dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            LOGGER.debug(f"extract_content_date: Transcriber → {dt.isoformat()}")
            return dt.isoformat()
        except ValueError:
            LOGGER.warning(f"extract_content_date: Ogiltigt Transcriber-format '{date_str} {time_str}'")
    elif date_match:
        # Har datum men inte tid - använd mitt på dagen
        date_str = date_match.group(1)
        try:
            dt = datetime.datetime.strptime(f"{date_str} 12:00", "%Y-%m-%d %H:%M")
            LOGGER.debug(f"extract_content_date: Transcriber (endast datum) → {dt.isoformat()}")
            return dt.isoformat()
        except ValueError as e:
            LOGGER.debug(f"extract_content_date: Kunde inte parsa datum '{date_str}': {e}")

    # 3. Ingen källa hittades
    LOGGER.info("extract_content_date: Ingen datumkälla → UNKNOWN")
    return "UNKNOWN"

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
        LOGGER.error(f"HARDFAIL: Text extraction failed for {filepath}: {e}")
        raise RuntimeError(f"Text extraction failed for {filepath}: {e}") from e

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
        return {"context_summary": "", "relations_summary": "", "document_keywords": []}
    
    # Klipp texten om den är för lång för summary-modellen
    safe_text = text[:SUMMARY_MAX_CHARS]
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
            "context_summary": data.get("context_summary", ""),
            "relations_summary": data.get("relations_summary", ""),
            "document_keywords": data.get("document_keywords", []) or data.get("keywords", []), # Fallback om LLM slarvar
            "ai_model": lite_model
        }
    except Exception as e:
        LOGGER.warning(f"Semantic Metadata generation failed (non-critical): {e}")
        # Returnera tomma värden - dokumentet processas ändå men utan metadata
        return {"context_summary": "", "relations_summary": "", "document_keywords": [], "ai_model": "FAILED"}
        # OBS: raise inte här - semantic metadata är "nice to have", inte kritiskt

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
        LOGGER.error(f"HARDFAIL: MCP Extraction failed: {e}")
        raise RuntimeError(f"MCP Entity Extraction failed: {e}") from e

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

    # 3. Resolve entities via GraphService.find_node_by_name
    graph = GraphService(GRAPH_DB_PATH, read_only=True)
    seen_candidates = set()

    for node in nodes:
        name = node.get('name')
        type_str = node.get('type')
        confidence = node.get('confidence', 0.5)
        node_context_text = node.get('node_context', '')

        if not name or not type_str: continue

        # Brusfilter baserat på confidence (Krav från Område B)
        if confidence < 0.3: continue

        key = f"{name}|{type_str}"
        if key in seen_candidates: continue
        seen_candidates.add(key)

        # Om källan är "Trusted" (t.ex. Slack/Mail), höj confidence
        if source_type in ["Slack Log", "Email Thread"]:
            confidence = max(confidence, 0.8)

        # Entity resolution: LINK om finns, CREATE om ny
        existing_uuid = graph.find_node_by_name(type_str, name, fuzzy=True)

        if existing_uuid:
            action = "LINK"
            target_uuid = existing_uuid
        else:
            action = "CREATE"
            target_uuid = str(uuid.uuid4())

        name_to_uuid[name] = target_uuid

        mentions.append({
            "action": action,
            "target_uuid": target_uuid,
            "type": type_str,
            "label": name,
            "node_context_text": node_context_text,
            "confidence": confidence
        })

    graph.close()

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
        "ingestion_payload": mentions, 
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

        # --- UNIFIED PIPELINE SWITCH ---
        source_type = "Document"
        if "slack" in filväg.lower(): source_type = "Slack Log"
        elif "mail" in filväg.lower(): source_type = "Email Thread"
        elif "calendar" in filväg.lower(): source_type = "Calendar Event"

        # Ett enda anrop!
        full_result = unified_processing_strategy(raw_text, filnamn, source_type)

        ingestion_payload = full_result.get("ingestion_payload", [])
        semantic_metadata = full_result.get("semantic_metadata", {})

        # Spara (Area C compliant structure)
        # Tre tidsstämplar:
        # - timestamp_ingestion: När filen skapades i Lake (nu)
        # - timestamp_content: När innehållet hände (extraherat eller UNKNOWN)
        # - timestamp_updated: Sätts av Dreamer vid semantisk uppdatering (null initialt)
        timestamp_content = extract_content_date(raw_text)

        # Hämta default access_level från config (Privacy-First: default 5 = PRIVATE)
        default_access_level = CONFIG.get('security', {}).get('default_access_level', 5)

        frontmatter = {
            "unit_id": unit_id,
            "source_ref": lake_file,
            "original_filename": filnamn,
            "timestamp_ingestion": datetime.datetime.now().isoformat(),
            "timestamp_content": timestamp_content,
            "timestamp_updated": None,
            "source_type": source_type,
            "access_level": default_access_level,
            "context_summary": semantic_metadata.get("context_summary", ""),
            "relations_summary": semantic_metadata.get("relations_summary", ""),
            "document_keywords": semantic_metadata.get("document_keywords", []),
            "ai_model": semantic_metadata.get("ai_model", "unknown"),
        }

        fm_str = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True)
        with open(lake_file, 'w', encoding='utf-8') as f:
            f.write(f"---\n{fm_str}---\n\n# {filnamn}\n\n{raw_text}")
            
        LOGGER.info(f"✅ Lake: {filnamn} ({source_type}) -> {len(ingestion_payload)} mentions")

        # --- GRAF-SKRIVNING ---
        # Skriv extraherade entiteter till grafen
        from services.utils.graph_service import GraphService
        graph = GraphService(GRAPH_DB_PATH)

        nodes_written = 0
        edges_written = 0

        for entity in ingestion_payload:
            action = entity.get("action")

            if action in ["CREATE", "LINK"]:
                target_uuid = entity.get("target_uuid")
                node_type = entity.get("type")
                label = entity.get("label", "")
                confidence = entity.get("confidence", 0.5)
                node_context_text = entity.get("node_context_text", "")

                if not target_uuid or not node_type:
                    continue

                # Bygg node_context enligt schema: {text, origin}
                node_context_entry = {
                    "text": node_context_text or f"Omnämnd i {filnamn}",
                    "origin": unit_id
                }

                # Upsert noden (aggregerar node_context om noden finns)
                props = {
                    "name": label,
                    "status": "PROVISIONAL",
                    "confidence": confidence,
                    "node_context": [node_context_entry],
                    "source_system": "DocConverter"
                }

                graph.upsert_node(
                    id=target_uuid,
                    type=node_type,
                    properties=props
                )
                nodes_written += 1

                # Skapa MENTIONS-kant (Document -> Entity)
                graph.upsert_edge(
                    source=unit_id,
                    target=target_uuid,
                    edge_type="MENTIONS",
                    properties={"confidence": confidence}
                )
                edges_written += 1

            elif action == "CREATE_EDGE":
                source_uuid = entity.get("source_uuid")
                target_uuid = entity.get("target_uuid")
                edge_type = entity.get("edge_type")
                edge_conf = entity.get("confidence", 0.5)

                if source_uuid and target_uuid and edge_type:
                    graph.upsert_edge(
                        source=source_uuid,
                        target=target_uuid,
                        edge_type=edge_type,
                        properties={"confidence": edge_conf}
                    )
                    edges_written += 1

        LOGGER.info(f"✅ Graf: {filnamn} -> {nodes_written} noder, {edges_written} kanter")

        # --- VEKTOR-SKRIVNING ---
        # Indexera dokumentet i ChromaDB
        from services.utils.vector_service import get_vector_service
        vector_service = get_vector_service("knowledge_base")

        # Bygg dokumenttext för embedding
        ctx_summary = semantic_metadata.get("context_summary", "")
        rel_summary = semantic_metadata.get("relations_summary", "")

        vector_text = f"FILENAME: {filnamn}\nSUMMARY: {ctx_summary}\nRELATIONS: {rel_summary}\n\nCONTENT:\n{raw_text[:8000]}"

        vector_service.upsert(
            id=unit_id,
            text=vector_text,
            metadata={
                "timestamp": frontmatter.get("timestamp_ingestion", ""),
                "filename": filnamn,
                "source_type": source_type
            }
        )
        LOGGER.info(f"✅ Vektor: {filnamn} -> ChromaDB")

    except Exception as e:
        LOGGER.error(f"❌ HARDFAIL {filnamn}: {e}")
        # Släpp låset så filen kan försökas igen
        with PROCESS_LOCK:
            PROCESSED_FILES.discard(filnamn)
        # HARDFAIL: Låt högt - datakedjan är bruten
        raise RuntimeError(f"HARDFAIL: Dokumentbearbetning misslyckades för {filnamn}: {e}") from e

# --- INIT & WATCHDOG ---
if __name__ == "__main__":
    os.makedirs(LAKE_STORE, exist_ok=True)
    print(f"DocConverter (v11.0 - Unified Pipeline) online.")
    
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