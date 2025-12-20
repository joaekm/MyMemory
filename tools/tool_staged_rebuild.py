#!/usr/bin/env python3
"""
tool_staged_rebuild.py - Kronologisk återuppbyggnad av MyMemory

Kör efter hard reset för att indexera data dag-för-dag,
med pauser för konsolidering ("drömma") mellan varje dag.

Användning:
    python tools/tool_staged_rebuild.py --confirm

Logik:
    1. Flytta alla filer till staging-katalog
    2. För varje dag (äldst först):
       - Flytta dagens filer tillbaka till Assets
       - Starta indexeringstjänster
       - Vänta på completion (nya filer i Lake + inactivity timeout)
       - Kör Graph Builder (konsolidering)
       - Stäng ner tjänster
    3. Upprepa tills alla dagar processerade
"""

import os
import sys
import re
import json
import shutil
import signal
import subprocess
import time
import argparse
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
import logging

# Lägg till project root i path för imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.utils.date_service import get_date, get_timestamp
from services.utils.graph_service import GraphStore

# === LOGGING ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - REBUILD - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('StagedRebuild')

# === CONFIG ===

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'my_mem_config.yaml')
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    for k, v in config['paths'].items():
        config['paths'][k] = os.path.expanduser(v)
    return config

CONFIG = load_config()

LAKE_STORE = CONFIG['paths']['lake_store']
ASSET_RECORDINGS = CONFIG['paths']['asset_recordings']
ASSET_DOCUMENTS = CONFIG['paths']['asset_documents']
ASSET_FAILED = CONFIG['paths']['asset_failed']
ASSET_SLACK = CONFIG['paths']['asset_slack']
ASSET_TRANSCRIPTS = CONFIG['paths']['asset_transcripts']
STAGING_ROOT = os.path.join(CONFIG['paths']['asset_store'], '.staging')
PROGRESS_FILE = os.path.join(CONFIG['paths']['asset_store'], '.rebuild_progress.json')

# Vilka mappar ska processeras (källfiler)
SOURCE_FOLDERS = [ASSET_RECORDINGS, ASSET_DOCUMENTS, ASSET_SLACK]

# Timeout-inställningar
INACTIVITY_TIMEOUT_SECONDS = 1800  # 30 minuter utan nya filer i Lake → HARDFAIL
POLL_INTERVAL_SECONDS = 10        # Hur ofta vi kollar Lake

# UUID-pattern för att matcha filnamn
UUID_PATTERN = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.(md|txt)$')

# === HELPERS ===

def _ts():
    return datetime.now().strftime("[%H:%M:%S]")

def _log(msg):
    print(f"{_ts()} {msg}")

# extract_date_from_file ersatt med get_date() från DateService

def get_all_source_files() -> list:
    """Samla alla filer från källmappar."""
    files = []
    for folder in SOURCE_FOLDERS:
        if not os.path.exists(folder):
            continue
        for f in os.listdir(folder):
            if f.startswith('.'):
                continue
            filepath = os.path.join(folder, f)
            if os.path.isfile(filepath):
                files.append({
                    'path': filepath,
                    'folder': folder,
                    'filename': f
                })
    return files

def group_files_by_date(files: list) -> dict:
    """Gruppera filer per datum."""
    by_date = defaultdict(list)
    for f in files:
        try:
            date = get_date(f['path'])
            by_date[date].append(f)
        except RuntimeError as e:
            LOGGER.error(f"Kunde inte extrahera datum för {f['filename']}: {e}")
            # Filer utan datum skippas
    return dict(by_date)

def count_lake_files() -> int:
    """Räkna antal filer i Lake."""
    if not os.path.exists(LAKE_STORE):
        return 0
    return len([f for f in os.listdir(LAKE_STORE) if f.endswith('.md')])


def get_latest_lake_date() -> str:
    """
    Hitta senaste dokumentdatum i Lake.
    
    Läser timestamp_created från Lake-filer för att avgöra
    vilket datum som redan är indexerat.
    
    Returns:
        Senaste datum som "YYYY-MM-DD" eller None om Lake är tom.
    """
    if not os.path.exists(LAKE_STORE):
        return None
    
    lake_files = [f for f in os.listdir(LAKE_STORE) if f.endswith('.md')]
    if not lake_files:
        return None
    
    latest_date = None
    for f in lake_files:
        filepath = os.path.join(LAKE_STORE, f)
        try:
            date = get_date(filepath)
            if not latest_date or date > latest_date:
                latest_date = date
        except RuntimeError:
            # Skippa filer utan giltigt datum
            LOGGER.debug(f"Kunde inte läsa datum från Lake-fil: {f}")
    
    return latest_date

# === STAGING ===

def move_to_staging(files: list) -> dict:
    """
    Flytta alla filer till staging-katalog.
    Bevarar mappstruktur.
    
    Returns:
        Dict med staging-info för återställning
    """
    staging_info = {}
    
    os.makedirs(STAGING_ROOT, exist_ok=True)
    
    for f in files:
        original_path = f['path']
        folder_name = os.path.basename(f['folder'])
        staging_folder = os.path.join(STAGING_ROOT, folder_name)
        os.makedirs(staging_folder, exist_ok=True)
        
        staging_path = os.path.join(staging_folder, f['filename'])
        
        shutil.move(original_path, staging_path)
        LOGGER.info(f"STAGING: {f['filename']} flyttad från {folder_name}/ till staging/{folder_name}/")
        staging_info[f['filename']] = {
            'staging_path': staging_path,
            'original_folder': f['folder']
        }
    
    return staging_info

def restore_files_for_date(date: str, files_by_date: dict, staging_info: dict):
    """Flytta tillbaka filer för ett specifikt datum."""
    files = files_by_date.get(date, [])
    
    for f in files:
        info = staging_info.get(f['filename'])
        if not info:
            _log(f"  ⚠️ Hittade inte staging-info för {f['filename']}")
            continue
        
        staging_path = info['staging_path']
        original_folder = info['original_folder']
        original_path = os.path.join(original_folder, f['filename'])
        
        if os.path.exists(staging_path):
            shutil.move(staging_path, original_path)
            LOGGER.info(f"RESTORE: {f['filename']} återställd till {os.path.basename(original_folder)}/")

def restore_all_from_staging(staging_info: dict):
    """Återställ ALLA kvarvarande filer från staging till original-mappar."""
    if not staging_info:
        return 0
    
    restored = 0
    for filename, info in staging_info.items():
        staging_path = info['staging_path']
        original_folder = info['original_folder']
        original_path = os.path.join(original_folder, filename)
        
        if os.path.exists(staging_path):
            os.makedirs(original_folder, exist_ok=True)
            shutil.move(staging_path, original_path)
            LOGGER.info(f"RESTORE_ALL: {filename} återställd till {os.path.basename(original_folder)}/")
            restored += 1
    
    return restored


def cleanup_staging():
    """Ta bort staging-katalogen."""
    if os.path.exists(STAGING_ROOT):
        shutil.rmtree(STAGING_ROOT)
        _log("🧹 Staging-katalog borttagen")


def save_progress(staging_info: dict, completed_dates: list, all_dates: list):
    """Spara progress till disk för att kunna fortsätta efter avbrott."""
    progress = {
        'staging_info': staging_info,
        'completed_dates': completed_dates,
        'all_dates': all_dates,
        'timestamp': datetime.now().isoformat()
    }
    with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def load_progress() -> dict:
    """Ladda sparad progress om den finns."""
    if not os.path.exists(PROGRESS_FILE):
        return None
    try:
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        LOGGER.warning(f"Kunde inte ladda progress-fil: {e}")
        return None


def clear_progress():
    """Ta bort progress-fil efter lyckad körning."""
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)

# === SERVICE MANAGEMENT ===

# Slack Collector exkluderad - vi vill inte hämta nya Slack-meddelanden under rebuild
SERVICES = [
    {"path": "services/my_mem_file_retriever.py", "name": "File Retriever"},
    {"path": "services/my_mem_doc_converter.py", "name": "Doc Converter"},
    {"path": "services/my_mem_transcriber.py", "name": "Transcriber"},
    {"path": "services/my_mem_vector_indexer.py", "name": "Vector Indexer"},
]

_running_processes = []
_current_staging_info = {}  # Global för interrupt-hantering
_completed_dates = []       # Global för interrupt-hantering

def start_services():
    """Starta alla indexeringstjänster."""
    global _running_processes
    _running_processes = []
    
    python_exec = sys.executable
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    for service in SERVICES:
        script_path = os.path.join(project_root, service["path"])
        if not os.path.exists(script_path):
            _log(f"  ⚠️ {service['name']}: fil saknas")
            continue
        
        try:
            # Visa output för Transcriber (lång process, bra att se progress)
            if service["name"] == "Transcriber":
                p = subprocess.Popen(
                    [python_exec, script_path],
                    cwd=project_root
                    # stdout/stderr visas i terminalen
                )
            else:
                p = subprocess.Popen(
                    [python_exec, script_path],
                    cwd=project_root,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            _running_processes.append(p)
            time.sleep(0.5)
        except Exception as e:
            _log(f"  ❌ {service['name']}: {e}")
            raise RuntimeError(f"HARDFAIL: Kunde inte starta {service['name']}: {e}") from e
    
    _log(f"  ▶️ {len(_running_processes)} tjänster startade")

def stop_services():
    """Stoppa alla körande tjänster."""
    global _running_processes
    
    for p in _running_processes:
        try:
            p.terminate()
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _log(f"  ⚠️ Process {p.pid} svarar inte, tvingar kill")
            p.kill()
        except Exception as e:
            # Cleanup-fel loggas men re-raisas inte - viktigare att fortsätta städa
            LOGGER.warning(f"Fel vid stopp av process {p.pid}: {e}")
            _log(f"  ⚠️ Fel vid stopp av process {p.pid}: {e}")
    
    _running_processes = []
    _log("  ⏹️ Tjänster stoppade")

def run_graph_builder():
    """Kör Graf-byggning."""
    _log("  🧠 Kör Graf-byggning...")
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script_path = os.path.join(project_root, "services", "indexers", "graph_builder.py")
    
    if not os.path.exists(script_path):
        error_msg = f"HARDFAIL: Graph builder script saknas: {script_path}"
        LOGGER.error(error_msg)
        raise RuntimeError(error_msg)
    
    try:
        # Sätt PYTHONPATH explicit så att subprocess kan hitta services-modulen
        env = os.environ.copy()
        env['PYTHONPATH'] = project_root
        
        result = subprocess.run(
            [sys.executable, script_path],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=300
        )
        if result.returncode == 0:
            _log("  ✅ Graf-byggning klar")
            # Visa output även vid lyckad körning (för debugging)
            if result.stdout:
                LOGGER.debug(f"Graph builder stdout: {result.stdout}")
        else:
            # Visa både stdout och stderr vid fel
            error_details = []
            if result.stdout:
                error_details.append(f"STDOUT:\n{result.stdout}")
            if result.stderr:
                error_details.append(f"STDERR:\n{result.stderr}")
            
            error_msg = f"Graf-byggning avslutade med kod {result.returncode}"
            if error_details:
                error_msg += f"\n{'='*60}\n" + "\n".join(error_details) + f"\n{'='*60}"
            
            _log(f"  ⚠️ {error_msg}")
            LOGGER.error(error_msg)
            
            # Re-raise med detaljerad information
            raise RuntimeError(f"HARDFAIL: Graf-byggning misslyckades (exit code {result.returncode})\n{error_msg}")
    except subprocess.TimeoutExpired as e:
        _log("  ⚠️ Graf-byggning timeout (5 min)")
        raise RuntimeError("HARDFAIL: Graf-byggning timeout efter 5 minuter") from e
    except RuntimeError:
        # Re-raise RuntimeError (våra egna fel) utan att ändra dem
        raise
    except Exception as e:
        error_msg = f"HARDFAIL: Graf-byggning misslyckades: {e}"
        _log(f"  ❌ {error_msg}")
        LOGGER.error(error_msg, exc_info=True)
        raise RuntimeError(error_msg) from e


def run_dreamer():
    """Kör Dreamer för taxonomi-konsolidering."""
    _log("  💭 Kör Dreaming...")
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    try:
        # Importera och kör consolidate direkt
        sys.path.insert(0, project_root)
        from services.processors.dreamer import consolidate
        
        result = consolidate()
        if result.get("status") == "OK":
            added = result.get("concepts_added", 0)
            if added > 0:
                _log(f"  ✅ Dreaming klar: {added} noder tillagda i taxonomi")
            else:
                _log("  ✅ Dreaming klar: taxonomi synkad")
        elif result.get("status") == "SKIPPED":
            _log("  ⏭️ Dreaming: inga nya noder att kategorisera")
        else:
            _log(f"  ⚠️ Dreaming: {result.get('error', 'okänt fel')}")
    except ImportError as e:
        LOGGER.warning(f"Dreamer kunde inte laddas: {e}")
        _log(f"  ⚠️ Dreamer kunde inte laddas: {e}")
    except Exception as e:
        LOGGER.warning(f"Dreaming misslyckades: {e}")
        _log(f"  ⚠️ Dreaming misslyckades: {e}")

# === DAY COMPLETION DETECTION ===

def get_expected_lake_files(day_files: list) -> list:
    """
    Bygg lista med förväntade Lake-filnamn från källfiler.

    Args:
        day_files: Lista med fil-dicts från staging

    Returns:
        Lista med förväntade .md-filnamn i Lake
    """
    expected = []
    for f in day_files:
        base = os.path.splitext(f['filename'])[0]
        expected.append(f"{base}.md")
    return expected


def get_expected_failed_filename(source_filename: str) -> str:
    """
    Generera förväntat filnamn i Failed/ (UUID strippad från originalfilnamn).
    
    Ex: "Inspelning_20251202_1532_a678a2c9-0fcc-4764-890d-5672300b6eb7.m4a"
        → "Inspelning_20251202_1532.m4a"
    """
    base, ext = os.path.splitext(source_filename)
    # Ta bort UUID-suffix om det finns
    clean_base = UUID_PATTERN.sub('', base).rstrip('_')
    return f"{clean_base}{ext}"


def wait_for_day_completion(day_files: list, date: str):
    """
    Vänta på att alla filer för dagens datum har processats.
    
    En fil räknas som "klar" om:
    - Den finns som .md i Lake (lyckad processning), ELLER
    - Originalfilen finns i Failed/ (misslyckad processning)
    
    Args:
        day_files: Lista med fil-dicts som ska processas
        date: Dagens datum (för loggning)
    
    Raises:
        RuntimeError: Vid 30 minuters inaktivitet (HARDFAIL)
    """
    expected_lake_files = get_expected_lake_files(day_files)
    # Bygg mapping: lake_filename -> failed_filename för varje fil
    lake_to_failed = {}
    for f in day_files:
        lake_name = f"{os.path.splitext(f['filename'])[0]}.md"
        failed_name = get_expected_failed_filename(f['filename'])
        lake_to_failed[lake_name] = failed_name
    
    initial_lake_count = count_lake_files()
    
    last_missing_count = len(expected_lake_files)
    last_activity_time = time.time()
    
    _log(f"  ⏳ Väntar på {len(expected_lake_files)} filer...")
    _log(f"     Start: {initial_lake_count} filer i Lake")
    
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        
        # Kolla vilka filer som finns i Lake och Failed
        existing_lake = set(os.listdir(LAKE_STORE)) if os.path.exists(LAKE_STORE) else set()
        existing_failed = set(os.listdir(ASSET_FAILED)) if os.path.exists(ASSET_FAILED) else set()
        
        # En fil är "klar" om den finns i Lake ELLER i Failed
        missing = []
        failed_count = 0
        for lake_file in expected_lake_files:
            in_lake = lake_file in existing_lake
            in_failed = lake_to_failed[lake_file] in existing_failed
            if not in_lake and not in_failed:
                missing.append(lake_file)
            elif in_failed:
                failed_count += 1
        
        # Kolla om alla filer är klara
        if not missing:
            final_count = count_lake_files()
            success_count = len(expected_lake_files) - failed_count
            if failed_count > 0:
                _log(f"  ✅ Dagen klar! {success_count} i Lake, {failed_count} i Failed")
            else:
                _log(f"  ✅ Dagen klar! Alla {len(expected_lake_files)} filer i Lake")
            return
        
        # Kolla aktivitet (färre saknade filer = aktivitet)
        if len(missing) < last_missing_count:
            completed = last_missing_count - len(missing)
            _log(f"     Aktivitet: +{completed} klar, {len(missing)} kvar")
            last_missing_count = len(missing)
            last_activity_time = time.time()
        
        # Kolla inaktivitets-timeout
        inactive_seconds = time.time() - last_activity_time
        if inactive_seconds >= INACTIVITY_TIMEOUT_SECONDS:
            raise RuntimeError(
                f"HARDFAIL: Ingen aktivitet på {int(inactive_seconds)} sekunder. "
                f"Saknar {len(missing)} filer: {missing[:3]}{'...' if len(missing) > 3 else ''}. "
                f"Datum: {date}"
            )
        
        # Progress-uppdatering var 60:e sekund
        if int(inactive_seconds) % 60 == 0 and inactive_seconds > 0:
            remaining = INACTIVITY_TIMEOUT_SECONDS - inactive_seconds
            _log(f"     Väntar på {len(missing)} filer... ({int(remaining)}s kvar till timeout)")

# === TAXONOMY-ONLY REBUILD ===

def get_all_lake_files() -> list:
    """Samla alla .md filer från Lake."""
    files = []
    if not os.path.exists(LAKE_STORE):
        return files
    
    for f in os.listdir(LAKE_STORE):
        if not f.endswith('.md'):
            continue
        if f.startswith('.'):
            continue
        filepath = os.path.join(LAKE_STORE, f)
        if os.path.isfile(filepath):
            files.append({
                'path': filepath,
                'filename': f
            })
    return files


def build_transcript_exclude_list() -> set:
    """Bygg exkluderingslista med UUIDs från transcript-filer."""
    transcript_uuids = set()
    
    if not os.path.exists(ASSET_TRANSCRIPTS):
        LOGGER.warning(f"Transcript-mapp saknas: {ASSET_TRANSCRIPTS}")
        return transcript_uuids
    
    for f in os.listdir(ASSET_TRANSCRIPTS):
        if not f.endswith('.txt'):
            continue
        if f.startswith('.'):
            continue
        
        # Extrahera UUID från filnamn
        match = UUID_SUFFIX_PATTERN.search(f)
        if match:
            uuid = match.group(1)
            transcript_uuids.add(uuid)
    
    LOGGER.info(f"Byggde exkluderingslista: {len(transcript_uuids)} transcript-UUIDs")
    return transcript_uuids


def load_master_taxonomy() -> dict:
    """Ladda master taxonomy och rensa sub_nodes."""
    taxonomy_file = CONFIG['paths']['taxonomy_file']
    
    if not os.path.exists(taxonomy_file):
        LOGGER.warning(f"Taxonomi-fil saknas: {taxonomy_file}, skapar tom struktur")
        return {}
    
    try:
        with open(taxonomy_file, 'r', encoding='utf-8') as f:
            taxonomy = json.load(f)
        
        # Behåll master nodes och descriptions, rensa sub_nodes
        cleaned = {}
        for key, value in taxonomy.items():
            cleaned[key] = {
                'description': value.get('description', ''),
                'sub_nodes': []
            }
        
        LOGGER.info(f"Laddade taxonomi: {len(cleaned)} masternoder, sub_nodes rensade")
        return cleaned
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Kunde inte ladda taxonomi: {e}")
        raise RuntimeError(f"HARDFAIL: Kunde inte ladda taxonomi: {e}") from e


def save_taxonomy(taxonomy: dict):
    """Spara taxonomy till disk."""
    taxonomy_file = CONFIG['paths']['taxonomy_file']
    
    # Deduplicera och sortera sub_nodes
    for key, value in taxonomy.items():
        if 'sub_nodes' in value:
            value['sub_nodes'] = sorted(list(set(value['sub_nodes'])))
    
    os.makedirs(os.path.dirname(taxonomy_file), exist_ok=True)
    with open(taxonomy_file, 'w', encoding='utf-8') as f:
        json.dump(taxonomy, f, ensure_ascii=False, indent=2)
    
    total_sub_nodes = sum(len(v.get('sub_nodes', [])) for v in taxonomy.values())
    LOGGER.info(f"Taxonomi sparad: {len(taxonomy)} masternoder, {total_sub_nodes} sub_nodes totalt")


def update_file_graph_nodes(filepath: str, new_graph_nodes: dict) -> bool:
    """Uppdatera graph_nodes i filens frontmatter."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        if '---' not in content:
            LOGGER.warning(f"Fil saknar frontmatter: {filepath}")
            return False
        
        parts = content.split('---', 2)
        if len(parts) < 3:
            LOGGER.warning(f"Fil har ogiltigt frontmatter-format: {filepath}")
            return False
        
        # Parse YAML
        try:
            metadata = yaml.safe_load(parts[1])
        except yaml.YAMLError as e:
            LOGGER.warning(f"Kunde inte parsa YAML för {filepath}: {e}")
            return False
        
        # Uppdatera graph_nodes
        metadata['graph_nodes'] = new_graph_nodes
        
        # Rekonstruera fil
        new_yaml = yaml.dump(metadata, allow_unicode=True, sort_keys=False, default_flow_style=False)
        new_content = f"---\n{new_yaml}---\n{parts[2]}"
        
        # Skriv tillbaka
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
        
        return True
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Kunde inte uppdatera fil {filepath}: {e}")
        return False


def aggregate_to_taxonomy(taxonomy: dict, graph_nodes: dict):
    """Aggregera graph_nodes till taxonomy struktur."""
    for key, value in graph_nodes.items():
        if key not in taxonomy:
            # Skippa om masternode inte finns i taxonomi
            continue
        
        if isinstance(value, (int, float)):
            # Abstrakt koncept (masternode) - lägg till key som sub_node
            if key not in taxonomy[key]['sub_nodes']:
                taxonomy[key]['sub_nodes'].append(key)
        elif isinstance(value, dict):
            # Typad entitet (Person, Aktör, Projekt) - lägg till alla entity names
            for entity_name in value.keys():
                if entity_name not in taxonomy[key]['sub_nodes']:
                    taxonomy[key]['sub_nodes'].append(entity_name)


def run_taxonomy_only_rebuild(days_limit: int = None):
    """Huvudloop för taxonomy-only rebuild.
    
    Args:
        days_limit: Antal dagar att processa (äldst först). None = alla.
    """
    _log("═══════════════════════════════════════════════")
    _log("  TAXONOMY-ONLY REBUILD - Trusted Sources Only")
    _log("═══════════════════════════════════════════════")
    
    # 1. Bygg transcript exkluderingslista
    _log("\n📋 Bygger transcript exkluderingslista...")
    transcript_uuids = build_transcript_exclude_list()
    _log(f"   {len(transcript_uuids)} transcript-UUIDs att exkludera")
    
    # 2. Ladda master taxonomy
    _log("\n📚 Laddar master taxonomy...")
    taxonomy = load_master_taxonomy()
    if not taxonomy:
        _log("❌ HARDFAIL: Kunde inte ladda taxonomi")
        return
    
    # 3. Samla Lake-filer
    _log("\n📁 Samlar Lake-filer...")
    all_lake_files = get_all_lake_files()
    if not all_lake_files:
        _log("❌ Inga filer i Lake!")
        return
    
    _log(f"   Hittade {len(all_lake_files)} filer i Lake")
    
    # 4. Gruppera per datum
    files_by_date = group_files_by_date(all_lake_files)
    sorted_dates = sorted(files_by_date.keys())
    
    # 5. I taxonomy-only-läge: Processa ALLA datum (ignorera historik)
    # Anledning: Vi nollställer taxonomin i minnet, så vi måste läsa alla
    # historiska filer för att bygga upp taxonomin från grunden.
    # (I vanligt läge skulle vi hoppa över gamla datum för effektivitet)
    
    # 6. Begränsa till days_limit om angivet
    if days_limit and days_limit < len(sorted_dates):
        sorted_dates = sorted_dates[:days_limit]
        _log(f"   Begränsat till {days_limit} dagar")
    
    if not sorted_dates:
        _log("❌ Inga datum att processa!")
        return
    
    _log(f"   Processar {len(sorted_dates)} dagar: {sorted_dates[0]} → {sorted_dates[-1]}")
    
    # 7. Importera generera_metadata
    try:
        from services.processors.doc_converter import generera_metadata
    except ImportError as e:
        LOGGER.error(f"HARDFAIL: Kunde inte importera generera_metadata: {e}")
        raise RuntimeError(f"HARDFAIL: Kunde inte importera generera_metadata: {e}") from e
    
    # 8. Processa dag-för-dag
    total_scanned = 0
    total_trusted = 0
    total_updated = 0
    
    try:
        for i, date in enumerate(sorted_dates, 1):
            day_files = files_by_date[date]
            
            _log(f"\n{'─' * 50}")
            _log(f"📅 DAG {i}/{len(sorted_dates)}: {date}")
            _log(f"   {len(day_files)} filer att processa")
            
            # 8.1 Filtrera trusted sources för denna dag
            trusted_files = []
            for f in day_files:
                # Extrahera UUID från filnamn
                match = UUID_SUFFIX_PATTERN.search(f['filename'])
                if match:
                    uuid = match.group(1)
                    if uuid not in transcript_uuids:
                        trusted_files.append(f)
            
            _log(f"   {len(trusted_files)} trusted filer (exkluderade {len(day_files) - len(trusted_files)} transcripts)")
            total_scanned += len(day_files)
            total_trusted += len(trusted_files)
            
            # 8.2 Extrahera och uppdatera graph_nodes för varje trusted fil
            day_updated = 0
            for f in trusted_files:
                filepath = f['path']
                filename = f['filename']
                
                try:
                    # Läs innehåll
                    with open(filepath, 'r', encoding='utf-8') as file:
                        content = file.read()
                    
                    if '---' not in content:
                        LOGGER.warning(f"Skippar fil utan frontmatter: {filename}")
                        continue
                    
                    parts = content.split('---', 2)
                    if len(parts) < 3:
                        LOGGER.warning(f"Skippar fil med ogiltigt frontmatter: {filename}")
                        continue
                    
                    # Extrahera markdown body
                    body = parts[2] if len(parts) > 2 else ""
                    
                    # LLM extraction
                    meta_data = generera_metadata(body, filename)
                    if meta_data and meta_data.get('graph_nodes'):
                        new_graph_nodes = meta_data['graph_nodes']
                        
                        # Uppdatera frontmatter
                        if update_file_graph_nodes(filepath, new_graph_nodes):
                            day_updated += 1
                            total_updated += 1
                            
                            # Aggregera till taxonomy
                            aggregate_to_taxonomy(taxonomy, new_graph_nodes)
                    else:
                        LOGGER.warning(f"LLM returnerade inga graph_nodes för {filename}")
                
                except Exception as e:
                    LOGGER.error(f"Fel vid processning av {filename}: {e}")
                    continue
            
            _log(f"   ✅ {day_updated} filer uppdaterade med LLM")
            
            # Säkerställ att GraphStore singleton-instansen är stängd
            # Detta förhindrar DuckDB fil-låsningar när vi kör graph_builder/dreamer
            try:
                from services.indexers.graph_builder import close_db_connection
                close_db_connection()
            except Exception as e:
                LOGGER.warning(f"Kunde inte stänga GraphStore-anslutning: {e}")
            
            # 8.3 Konsolidering (efter varje dag)
            run_graph_builder()
            run_dreamer()
            
            # Spara taxonomy inkrementellt
            save_taxonomy(taxonomy)
            _log(f"   💾 Taxonomy sparad (inkrementellt)")
        
        # 9. Final taxonomy save
        _log(f"\n{'═' * 50}")
        _log("🎉 TAXONOMY-ONLY REBUILD KLAR!")
        _log(f"   Files Scanned: {total_scanned}")
        _log(f"   Trusted Files Processed: {total_trusted}")
        _log(f"   Files Updated with LLM: {total_updated}")
        
        total_nodes = sum(len(v.get('sub_nodes', [])) for v in taxonomy.values())
        _log(f"   Taxonomy Nodes Mapped: {total_nodes}")
        _log(f"{'═' * 50}")
        
        # Final save
        save_taxonomy(taxonomy)
        
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Taxonomy-only rebuild misslyckades: {e}")
        _log(f"\n❌ HARDFAIL: {e}")
        raise


# === MAIN REBUILD LOOP ===

def run_staged_rebuild(days_limit: int = None):
    """Huvudloop för kronologisk återuppbyggnad.
    
    Args:
        days_limit: Antal dagar att processa (äldst först). None = alla.
    """
    global _current_staging_info, _completed_dates
    
    _log("═══════════════════════════════════════════════")
    _log("  STAGED REBUILD - Kronologisk Indexering")
    _log("═══════════════════════════════════════════════")
    
    # 1. Samla alla filer
    _log("\n📁 Samlar filer...")
    all_files = get_all_source_files()
    
    if not all_files:
        _log("❌ Inga filer att processa!")
        return
    
    _log(f"   Hittade {len(all_files)} filer totalt")
    
    # 2. Gruppera per datum
    files_by_date = group_files_by_date(all_files)
    sorted_dates = sorted(files_by_date.keys())
    
    # 3. Kolla vad som redan finns i Lake och skippa de datumen
    latest_in_lake = get_latest_lake_date()
    if latest_in_lake:
        original_count = len(sorted_dates)
        sorted_dates = [d for d in sorted_dates if d > latest_in_lake]
        skipped = original_count - len(sorted_dates)
        if skipped > 0:
            _log(f"   📅 Lake innehåller data t.o.m. {latest_in_lake}")
            _log(f"   ⏭️ Skippar {skipped} redan indexerade dagar")
        
        if not sorted_dates:
            _log("✅ Alla datum redan processade i Lake!")
            return
    
    # 4. Begränsa till days_limit om angivet
    if days_limit and days_limit < len(sorted_dates):
        sorted_dates = sorted_dates[:days_limit]
        _log(f"   Begränsat till {days_limit} dagar")
    
    # OBS: Vi filtrerar INTE all_files här - alla filer flyttas till staging
    # så att Assets är tom när services startar. Endast valda datum processas.
    
    if not sorted_dates:
        _log("❌ Inga datum att processa!")
        return
    
    _log(f"   Processar {len(sorted_dates)} dagar: {sorted_dates[0]} → {sorted_dates[-1]}")
    
    # Räkna hur många filer som tillhör de valda datumen (INNAN flytt)
    files_to_process = sum(len(files_by_date.get(d, [])) for d in sorted_dates)
    
    # 4. Flytta ALLA filer till staging (så Assets är tom när services startar)
    # Detta förhindrar att DocConverter processar filer från framtida datum
    _log("\n📦 Flyttar ALLA filer till staging...")
    staging_info = move_to_staging(all_files)  # all_files = ALLA källfiler
    _current_staging_info = staging_info  # Sätt global för interrupt-hantering
    _completed_dates = []                  # Återställ
    
    _log(f"   {len(staging_info)} filer i staging ({files_to_process} att processa, {len(staging_info) - files_to_process} väntar)")
    
    # 5. Processa dag för dag
    try:
        for i, date in enumerate(sorted_dates, 1):
            day_files = files_by_date[date]
            
            _log(f"\n{'─' * 50}")
            _log(f"📅 DAG {i}/{len(sorted_dates)}: {date}")
            _log(f"   {len(day_files)} filer att indexera")
            
            # Återställ dagens filer
            _log("   📂 Återställer filer...")
            restore_files_for_date(date, files_by_date, staging_info)
            
            # Ta bort återställda filer från staging_info (de är nu processade)
            for f in day_files:
                if f['filename'] in _current_staging_info:
                    del _current_staging_info[f['filename']]
            
            # Starta tjänster
            _log("   🚀 Startar tjänster...")
            start_services()
            
            # Vänta på completion - väntar på SPECIFIKA filer, inte bara antal
            try:
                wait_for_day_completion(day_files, date)
            except RuntimeError as e:
                _log(f"\n❌ {e}")
                stop_services()
                raise
            
            # Stoppa tjänster
            stop_services()
            
            # Kör graf-byggning och dreaming (konsolidering)
            run_graph_builder()
            run_dreamer()
            
            # Markera dag som klar
            _completed_dates.append(date)
            _log(f"   ✅ Dag {date} klar!")
        
        _log(f"\n{'═' * 50}")
        _log("🎉 REBUILD KLAR!")
        _log(f"   Processade {len(sorted_dates)} dagar")
        _log(f"   Totalt {files_to_process} filer indexerade")
        _log(f"   Lake innehåller nu {count_lake_files()} dokument")
        _log(f"{'═' * 50}")
        
        # Rensa progress-fil vid lyckad körning
        clear_progress()
        
    finally:
        # Säkerställ att tjänster stoppas
        stop_services()
        
        # Om det finns kvarvarande filer i staging, återställ dem
        if _current_staging_info:
            _log("\n📂 Återställer kvarvarande filer från staging...")
            restored = restore_all_from_staging(_current_staging_info)
            if restored > 0:
                _log(f"   ✅ {restored} filer återställda")
        
        # Rensa staging-katalog (nu tom)
        cleanup_staging()

# === SIGNAL HANDLER ===

def handle_interrupt(signum, frame):
    """Hantera Ctrl+C gracefully - återställ filer istället för att radera."""
    global _current_staging_info, _completed_dates
    
    _log("\n⚠️ Avbruten av användare")
    stop_services()
    
    # Återställ kvarvarande filer från staging
    if _current_staging_info:
        _log("📂 Återställer kvarvarande filer från staging...")
        restored = restore_all_from_staging(_current_staging_info)
        _log(f"   ✅ {restored} filer återställda till ursprungliga mappar")
    
    # Rensa staging-katalog (nu tom)
    cleanup_staging()
    clear_progress()
    
    if _completed_dates:
        _log(f"\n💡 {len(_completed_dates)} dagar processades innan avbrott:")
        for d in _completed_dates:
            _log(f"   ✓ {d}")
    
    sys.exit(1)

# === ENTRY POINT ===

def main():
    parser = argparse.ArgumentParser(
        description="Kronologisk återuppbyggnad av MyMemory efter hard reset"
    )
    parser.add_argument(
        '--confirm',
        action='store_true',
        help='Bekräfta att du vill köra rebuild (krävs)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Visa vad som skulle hända utan att köra'
    )
    parser.add_argument(
        '--days',
        type=int,
        default=None,
        help='Antal dagar att processa (äldst först). Standard: alla dagar'
    )
    parser.add_argument(
        '--taxonomy-only',
        action='store_true',
        help='Kör taxonomy-only rebuild (trusted sources only, exkluderar transcripts)'
    )
    
    args = parser.parse_args()
    
    if args.dry_run:
        _log("DRY RUN - Visar plan utan att köra")
        _log("")
        
        all_files = get_all_source_files()
        files_by_date = group_files_by_date(all_files)
        sorted_dates = sorted(files_by_date.keys())
        
        # Kolla vad som redan finns i Lake
        latest_in_lake = get_latest_lake_date()
        if latest_in_lake:
            original_count = len(sorted_dates)
            sorted_dates = [d for d in sorted_dates if d > latest_in_lake]
            skipped = original_count - len(sorted_dates)
            if skipped > 0:
                _log(f"📅 Lake innehåller data t.o.m. {latest_in_lake}")
                _log(f"⏭️ Skippar {skipped} redan indexerade dagar")
                _log("")
        
        if not sorted_dates:
            _log("✅ Alla datum redan processade i Lake!")
            return
        
        # Begränsa till --days om angivet
        if args.days and args.days < len(sorted_dates):
            sorted_dates = sorted_dates[:args.days]
            _log(f"Begränsat till {args.days} dagar")
        
        total_files = sum(len(files_by_date[d]) for d in sorted_dates)
        _log(f"Filer att processa: {total_files}")
        _log(f"Dagar att processa: {len(sorted_dates)}")
        _log("")
        
        for date in sorted_dates:
            day_files = files_by_date[date]
            _log(f"  {date}: {len(day_files)} filer")
            for f in day_files[:3]:
                _log(f"    - {f['filename']}")
            if len(day_files) > 3:
                _log(f"    ... och {len(day_files) - 3} till")
        
        return
    
    # Route to taxonomy-only mode if flag is set
    if args.taxonomy_only:
        # Registrera signal handler
        signal.signal(signal.SIGINT, handle_interrupt)
        signal.signal(signal.SIGTERM, handle_interrupt)
        
        # Kör taxonomy-only rebuild
        run_taxonomy_only_rebuild(days_limit=args.days)
        return
    
    if not args.confirm:
        print("⚠️  STAGED REBUILD")
        print("")
        print("Detta verktyg kommer att:")
        print("  1. Flytta alla Assets-filer till staging")
        print("  2. Processa dag-för-dag (äldst först)")
        print("  3. Vänta på indexering + köra konsolidering")
        print("")
        print("Kör med --dry-run för att se planen först.")
        print("Kör med --confirm för att starta.")
        return
    
    # Registrera signal handler
    signal.signal(signal.SIGINT, handle_interrupt)
    signal.signal(signal.SIGTERM, handle_interrupt)
    
    # Kör rebuild
    run_staged_rebuild(days_limit=args.days)

if __name__ == "__main__":
    main()
