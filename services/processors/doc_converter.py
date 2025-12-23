#!/usr/bin/env python3
"""
MyMem DocConverter (v9.3) - Idempotent Batch

Fixar:
- Aktiverad idempotens (hoppar över filer som redan finns i Lake).
- Robust Config Handling.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Imports Dependencies
try:
    import fitz  # pymupdf
    import docx
except ImportError:
    print("[CRITICAL] Saknar nödvändiga bibliotek (pymupdf, python-docx).")
    sys.exit(1)

# Lägg till projektroten
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google import genai
from google.genai import types
from services.utils.graph_service import GraphStore
from services.utils.json_parser import parse_llm_json
try:
    from services.utils.date_service import get_timestamp as date_service_timestamp
except ImportError:
    # date_service is optional, fallback to datetime.now()
    sys.stderr.write("[INFO] date_service inte tillgänglig, använder datetime.now()\n")
    date_service_timestamp = lambda x: datetime.datetime.now()

# Entity Register imports (samma som förut)
try:
    from services.indexers.graph_builder import (
        get_all_entities as get_known_entities,
        get_canonical_from_graph as get_canonical,
        add_entity_alias,
        close_db_connection
    )
except ImportError:
    # Graph builder imports are optional in some contexts
    sys.stderr.write("[INFO] graph_builder inte tillgänglig, kör utan entity-funktioner\n")
    get_known_entities = lambda: []
    get_canonical = lambda x: None
    add_entity_alias = lambda x, y, z: False
    close_db_connection = lambda: None

atexit.register(close_db_connection)

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
TAXONOMY_FILE = CONFIG['paths']['taxonomy_file']

API_KEY = CONFIG['ai_engine']['api_key']
MODEL_NAME = CONFIG.get('ai_engine', {}).get('models', {}).get('model_lite', 'models/gemini-2.0-flash-lite-preview')

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - DOCCONV - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('DocConverter')
CLIENT = genai.Client(api_key=API_KEY)

# Patterns
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(txt|md|pdf|docx|csv|xlsx)$', re.IGNORECASE)
STANDARD_TIMESTAMP_PATTERN = re.compile(r'^DATUM_TID:\s+(.+)$', re.MULTILINE)
PROCESSED_FILES = set()
PROCESS_LOCK = threading.Lock()

# --- TEXT EXTRACTION ---
def extract_text(filväg: str, ext: str) -> str | None:
    try:
        text = ""
        ext = ext.lower()
        if ext == '.pdf':
            with fitz.open(filväg) as doc:
                for page in doc:
                    # Använd block-baserad extraktion för bättre layout-hantering (stycken)
                    blocks = page.get_text("blocks")
                    for b in blocks:
                        if b[6] == 0:  # Text block (0=text, 1=image)
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

# --- CLUSTERED MULTIPASS ---

def load_taxonomy_data():
    """Laddar hela taxonomi-strukturen från JSON-filen."""
    if not os.path.exists(TAXONOMY_FILE): 
        LOGGER.warning(f"Taxonomy file not found at {TAXONOMY_FILE}")
        return {}
    try:
        with open(TAXONOMY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Could not load taxonomy: {e}")
        return {}

def get_dynamic_clusters():
    """
    Bygger kluster dynamiskt genom att läsa 'cluster'-fältet från taxonomin.
    Gör Taxonomin till SSOT för både noder och deras gruppering.
    """
    taxonomy = load_taxonomy_data()
    clusters = {}
    
    for node, data in taxonomy.items():
        # Hämta kluster-tagg, fallback till 'Other' om den saknas i datan
        cluster_name = data.get('cluster', 'Other')
        
        if cluster_name not in clusters:
            clusters[cluster_name] = []
        
        clusters[cluster_name].append(node)
    
    # Logga om taxonomin var tom (kan hända innan first run)
    if not clusters:
        LOGGER.warning("No clusters found in taxonomy. Is it initialized?")
        
    return clusters

# Initiera globala variabler dynamiskt vid start
CATEGORY_CLUSTERS = get_dynamic_clusters()
# VALID_NODES blir en platt lista av alla noder som hittades i klustren
VALID_NODES = [node for sublist in CATEGORY_CLUSTERS.values() for node in sublist]

def analyze_cluster(text_chunk, cluster_name, categories, doc_name):
    active = [c for c in categories if c in VALID_NODES]
    if not active: return {"found_items": []}

    categories_list = "\n".join([f"- {c}" for c in active])
    
    raw_prompt = get_prompt('doc_converter', 'multipass_analysis')
    if not raw_prompt:
        LOGGER.error("HARDFAIL: Prompt 'multipass_analysis' saknas i yaml.")
        return {"found_items": []}

    chunk_size = get_setting('doc_converter', 'chunk_size_analysis', 12000)
    
    prompt = raw_prompt.format(
        categories_list=categories_list,
        doc_name=doc_name,
        text_chunk=text_chunk[:chunk_size]
    )

    try:
        response = CLIENT.models.generate_content(
            model=MODEL_NAME,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        return parse_llm_json(response.text)
    except Exception as e:
        LOGGER.error(f"Cluster {cluster_name} error: {e}")
        return {"found_items": []}

def save_to_evidence_db(items, source_uuid, graph_store):
    count = 0
    thresh = get_setting('doc_converter', 'evidence_threshold', 0.4)
    
    for item in items:
        cat = item.get('category')
        if cat not in VALID_NODES: continue
        if item.get('confidence', 0) > thresh:
            graph_store.add_evidence(
                id=str(uuid.uuid4()),
                source_file=source_uuid,
                entity_name=item['entity'],
                master_node_candidate=cat,
                context_description=item.get('context', ''),
                confidence=item.get('confidence')
            )
            count += 1
    return count

def generate_summary(text):
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
    
    # --- IDEMPOTENS-CHECK: HOPPA ÖVER OM KLAR ---
    if os.path.exists(lake_file):
        LOGGER.info(f"⏭️  Skippar (redan klar): {filnamn}")
        return

    LOGGER.info(f"⚙️ Bearbetar: {filnamn}")
    try:
        ext = os.path.splitext(filnamn)[1]
        raw_text = extract_text(filväg, ext)
        if not raw_text or len(raw_text) < 10:
            _move_to_failed(filväg)
            return

        ts = get_best_timestamp(filväg, raw_text)
        meta = generate_summary(raw_text)

        graph = GraphStore(GRAPH_DB_PATH)
        total_evidence = 0
        node_stats = {}
        
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(analyze_cluster, raw_text, name, cats, filnamn): name
                for name, cats in CATEGORY_CLUSTERS.items()
            }
            for future in as_completed(futures):
                items = future.result().get('found_items', [])
                if items:
                    count = save_to_evidence_db(items, unit_id, graph)
                    total_evidence += count
                    for i in items:
                        c = i.get('category')
                        node_stats[c] = node_stats.get(c, 0) + 1
        
        graph.close()

        frontmatter = {
            "unit_id": unit_id,
            "source_ref": lake_file,
            "original_filename": filnamn,
            "timestamp_created": ts,
            "summary": meta.get('summary', ''),
            "keywords": meta.get('keywords', []),
            "graph_context_status": "pending_consolidation",
            "ai_model": MODEL_NAME
        }
        
        fm_str = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)
        content = f"---\n{fm_str}---\n\n# Dokument: {filnamn}\n\n{raw_text}"
        
        with open(lake_file, 'w', encoding='utf-8') as f:
            f.write(content)
            
        LOGGER.info(f"✅ Klar: {filnamn} ({total_evidence} bevis)")

    except Exception as e:
        LOGGER.error(f"HARDFAIL {filnamn}: {e}")
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

if __name__ == "__main__":
    folders = [
        CONFIG['paths']['asset_documents'],
        CONFIG['paths']['asset_transcripts'],
        CONFIG['paths']['asset_slack'],
        CONFIG.get('paths', {}).get('asset_calendar'),
        CONFIG.get('paths', {}).get('asset_mail')
    ]
    os.makedirs(LAKE_STORE, exist_ok=True)
    
    print(f"DocConverter (Robust v9.3 Idempotent) online.")
    
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