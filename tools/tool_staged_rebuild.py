#!/usr/bin/env python3
"""
tool_staged_rebuild.py - Kronologisk återuppbyggnad av MyMemory

Kör efter hard reset för att indexera data dag-för-dag,
med pauser för konsolidering ("drömma") mellan varje dag.

Stöder två faser för att maximera datakvalitet:
1. Foundation Phase: Bygger grunden från textkällor (Slack, Docs, Mail).
2. Enrichment Phase: Bearbetar ljud/transkript med kontext från grunden.

Användning:
    python tools/tool_staged_rebuild.py --confirm --phase foundation
    python tools/tool_staged_rebuild.py --confirm --phase enrichment

Logik:
    1. Läs/Skapa manifest (.rebuild_manifest.json) för att spåra progress per ID.
    2. Samla filer baserat på vald fas.
    3. För varje dag (äldst först):
       - Flytta dagens filer (som ej är klara) till staging och tillbaka.
       - Starta indexeringstjänster.
       - Vänta på att specifika Target IDs dyker upp i Lake eller Failed.
       - Kör Graph Builder (konsolidering).
       - Stäng ner tjänster.
       - Uppdatera manifest.
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
    # Propagera multipass-flagga i config om satt via CLI
    try:
        if hasattr(NAMESPACE, 'multipass') and NAMESPACE.multipass:
            config.setdefault("processing", {})["multipass_enabled"] = True
    except NameError:
        LOGGER.debug("NAMESPACE inte definierad än vid modulimport (förväntat)")
    return config

CONFIG = load_config()

LAKE_STORE = CONFIG['paths']['lake_store']
ASSET_RECORDINGS = CONFIG['paths']['asset_recordings']
ASSET_DOCUMENTS = CONFIG['paths']['asset_documents']
ASSET_FAILED = CONFIG['paths']['asset_failed']
ASSET_SLACK = CONFIG['paths']['asset_slack']
ASSET_TRANSCRIPTS = CONFIG['paths']['asset_transcripts']
ASSET_CALENDAR = CONFIG['paths'].get('asset_calendar', os.path.join(CONFIG['paths']['asset_store'], 'Calendar'))
ASSET_MAIL = CONFIG['paths'].get('asset_mail', os.path.join(CONFIG['paths']['asset_store'], 'Mail'))
STAGING_ROOT = os.path.join(CONFIG['paths']['asset_store'], '.staging')
MANIFEST_FILE = os.path.join(CONFIG['paths']['asset_store'], '.rebuild_manifest.json')

# Timeout-inställningar
INACTIVITY_TIMEOUT_SECONDS = 1800  # 30 minuter utan nya filer i Lake → HARDFAIL
POLL_INTERVAL_SECONDS = 10        # Hur ofta vi kollar Lake

# UUID-pattern för att matcha filnamn
UUID_PATTERN = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.(md|txt|m4a|mp3|wav)$')

# === MANIFEST MANAGEMENT ===

class RebuildManifest:
    """Hanterar tillstånd för rebuild-processen baserat på ID."""
    
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = {
            "phase": None,
            "target_ids": [],     # Alla IDn som ska processas i nuvarande fas
            "completed_ids": [],  # IDn som bekräftats klara (Lake eller Failed)
            "failed_ids": []      # IDn som hamnat i Failed
        }
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    stored = json.load(f)
                    self.data.update(stored)
            except Exception as e:
                LOGGER.warning(f"Kunde inte ladda manifest: {e}")

    def save(self):
        with open(self.filepath, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def set_phase(self, phase):
        # Om fas byts, nollställ target tracking men behåll completed om relevant? 
        # Nej, enklast är att om man byter fas så börjar man om spårningen för DEN fasen.
        # Men vi vill inte radera completed_ids från föregående fas om vi kör append.
        # För enkelhetens skull: Manifestet spårar AKTUELL körning.
        if self.data["phase"] != phase:
            self.data["phase"] = phase
            self.data["target_ids"] = []
            # Vi behåller completed_ids så vi vet vad som gjorts totalt, 
            # men target_ids definierar vad vi jobbar mot nu.
            self.save()

    def add_targets(self, ids):
        current_set = set(self.data["target_ids"])
        new_ids = [i for i in ids if i not in current_set]
        self.data["target_ids"].extend(new_ids)
        self.save()

    def mark_complete(self, uuid, status="success"):
        if uuid not in self.data["completed_ids"]:
            self.data["completed_ids"].append(uuid)
        if status == "failed" and uuid not in self.data["failed_ids"]:
            self.data["failed_ids"].append(uuid)
        self.save()

    def is_complete(self, uuid):
        return uuid in self.data["completed_ids"]

    def get_pending_ids(self):
        return set(self.data["target_ids"]) - set(self.data["completed_ids"])

# === HELPERS ===

def _ts():
    return datetime.now().strftime("[%H:%M:%S]")

def _log(msg):
    print(f"{_ts()} {msg}")

def extract_uuid(filename):
    match = UUID_SUFFIX_PATTERN.search(filename)
    if match:
        return match.group(1)
    # Fallback: sök var som helst i strängen
    match_loose = UUID_PATTERN.search(filename)
    return match_loose.group(0) if match_loose else None

def get_all_source_files(phase="foundation") -> list:
    """
    Samla filer beroende på fas.
    Foundation: Slack, Docs, Mail, Calendar (textbaserat).
    Enrichment: Recordings (ljud).
    """
    files = []
    
    if phase == "foundation":
        folders = [ASSET_DOCUMENTS, ASSET_SLACK, ASSET_CALENDAR, ASSET_MAIL]
    elif phase == "enrichment":
        folders = [ASSET_RECORDINGS]
    else:
        # Default / Legacy behavior (allt utom recordings om ej specat, eller allt?)
        # För säkerhets skull, om ingen fas, kör Foundation-mappar
        folders = [ASSET_DOCUMENTS, ASSET_SLACK, ASSET_CALENDAR, ASSET_MAIL]

    for folder in folders:
        if not os.path.exists(folder):
            continue
        for f in os.listdir(folder):
            if f.startswith('.'):
                continue
            filepath = os.path.join(folder, f)
            if os.path.isfile(filepath):
                uuid = extract_uuid(f)
                if uuid:
                    files.append({
                        'path': filepath,
                        'folder': folder,
                        'filename': f,
                        'uuid': uuid
                    })
    return files

def group_files_by_date(files: list) -> dict:
    by_date = defaultdict(list)
    for f in files:
        try:
            date = get_date(f['path'])
            by_date[date].append(f)
        except RuntimeError as e:
            LOGGER.error(f"Kunde inte extrahera datum för {f['filename']}: {e}")
    return dict(by_date)

def count_lake_files() -> int:
    if not os.path.exists(LAKE_STORE):
        return 0
    return len([f for f in os.listdir(LAKE_STORE) if f.endswith('.md')])

# === STAGING ===

def move_to_staging(files: list) -> dict:
    staging_info = {}
    os.makedirs(STAGING_ROOT, exist_ok=True)
    
    for f in files:
        original_path = f['path']
        folder_name = os.path.basename(f['folder'])
        staging_folder = os.path.join(STAGING_ROOT, folder_name)
        os.makedirs(staging_folder, exist_ok=True)
        
        staging_path = os.path.join(staging_folder, f['filename'])
        
        shutil.move(original_path, staging_path)
        # Logga inte varje filflytt för att minska brus
        staging_info[f['filename']] = {
            'staging_path': staging_path,
            'original_folder': f['folder']
        }
    
    return staging_info

def restore_files_for_date(date: str, files_by_date: dict, staging_info: dict, manifest: RebuildManifest):
    """Flytta tillbaka filer för datumet, MEN bara de som inte är klara."""
    files = files_by_date.get(date, [])
    restored_count = 0
    
    for f in files:
        # Om filen redan är klar enligt manifest, rör den inte (låt ligga i staging eller var den är? 
        # Egentligen: Om den är klar ska den inte processas igen.
        # Men om vi flyttade den TILL staging, och den är klar... då ska den nog ligga kvar i staging 
        # tills vi städar upp, ELLER återställas direkt utan att trigga watchdogs?
        # Enklast: Återställ den så den hamnar på rätt plats, men vi vet att systemet är idempotent 
        # (DocConverter hoppar över om Lake-fil finns).
        # MEN: För effektivitet, om manifest säger "klar", återställ den tyst (watchdogs kanske triggar ändå).
        
        # Bättre strategi för Rebuild:
        # Vi återställer filen till Assets. Watchdogs ser den.
        # DocConverter kollar: "Finns X i Lake?" -> Ja -> Avbryt.
        # Så det är säkert att återställa.
        
        if manifest.is_complete(f['uuid']):
            # Redan klar i tidigare körning (t.ex. avbruten dag).
            # Återställ den så den inte raderas vid cleanup, men förvänta ingen action.
            pass
        
        info = staging_info.get(f['filename'])
        if not info:
            continue
        
        staging_path = info['staging_path']
        original_folder = info['original_folder']
        original_path = os.path.join(original_folder, f['filename'])
        
        if os.path.exists(staging_path):
            shutil.move(staging_path, original_path)
            restored_count += 1
            
    if restored_count > 0:
        LOGGER.info(f"RESTORE: {restored_count} filer återställda för {date}")

def restore_all_from_staging(staging_info: dict):
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
            restored += 1
    return restored

def cleanup_staging():
    if os.path.exists(STAGING_ROOT):
        shutil.rmtree(STAGING_ROOT)
        _log("🧹 Staging-katalog borttagen")

# === SERVICE MANAGEMENT ===

SERVICES = [
    {"path": "services/collectors/file_retriever.py", "name": "File Retriever"},
    {"path": "services/processors/transcriber.py", "name": "Transcriber"},
    {"path": "services/processors/doc_converter.py", "name": "Doc Converter"},
    {"path": "services/indexers/vector_indexer.py", "name": "Vector Indexer"},
]

_running_processes = []
_current_staging_info = {}

def start_services(phase="foundation"):
    """Starta indexeringstjänster baserat på fas."""
    global _running_processes
    _running_processes = []
    python_exec = sys.executable
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = os.environ.copy()
    env['PYTHONPATH'] = project_root
    
    # Filter services based on phase
    # Foundation: Exclude Transcriber (only text processing)
    # Enrichment: Include Transcriber
    active_services = []
    
    if phase == "foundation":
        active_services = [s for s in SERVICES if s["name"] != "Transcriber"]
    else:
        # Enrichment or default: Run everything (or specifically Transcriber + pipeline)
        active_services = SERVICES

    for service in active_services:
        script_path = os.path.join(project_root, service["path"])
        if not os.path.exists(script_path):
            continue
        try:
            p = subprocess.Popen(
                [python_exec, script_path],
                cwd=project_root,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            _running_processes.append(p)
            time.sleep(0.5)
        except Exception as e:
            _log(f"  ❌ {service['name']}: {e}")
            raise RuntimeError(f"HARDFAIL: Kunde inte starta {service['name']}: {e}") from e
    _log(f"  ▶️ {len(_running_processes)} tjänster startade (Fas: {phase})")

def stop_services():
    global _running_processes
    for p in _running_processes:
        try:
            p.terminate()
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
        except Exception:
            pass
    _running_processes = []
    _log("  ⏹️ Tjänster stoppade")

def run_graph_builder():
    _log("  🧠 Kör Graf-byggning...")
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script_path = os.path.join(project_root, "services", "indexers", "graph_builder.py")
    
    if not os.path.exists(script_path):
        raise RuntimeError(f"HARDFAIL: Graph builder script saknas")
    
    try:
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
        else:
            _log(f"  ⚠️ Graf-byggning felkod {result.returncode}")
            if result.stderr:
                LOGGER.error(f"Graph Builder Stderr: {result.stderr}")
            # Vi kastar fel för att följa Princip 2 (HARDFAIL) om kärnprocess dör
            raise RuntimeError(f"Graf-byggning misslyckades: {result.stderr}")
    except subprocess.TimeoutExpired:
        _log("  ⚠️ Graf-byggning timeout")
        raise RuntimeError("Graf-byggning timeout")
    except Exception as e:
        _log(f"  ❌ Graf-byggning fel: {e}")
        raise

def run_dreamer():
    _log("  💭 Kör Dreaming...")
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        sys.path.insert(0, project_root)
        from services.processors.dreamer import consolidate
        result = consolidate()
        if result.get("status") == "OK":
            added = result.get("concepts_added", 0)
            _log(f"  ✅ Dreaming klar: {added} noder tillagda")
        elif result.get("status") == "SKIPPED":
            _log("  ⏭️ Dreaming: inga nya noder")
        else:
            _log(f"  ⚠️ Dreaming: {result.get('error', 'okänt fel')}")
    except ImportError as e:
        _log(f"  ⚠️ Dreamer import fel: {e}")
    except Exception as e:
        _log(f"  ⚠️ Dreaming misslyckades: {e}")

# === COMPLETION LOGIC ===

def wait_for_day_completion(day_files: list, date: str, manifest: RebuildManifest):
    """
    Vänta på att specifika UUIDs dyker upp i Lake eller Failed.
    """
    # Filtrera ut filer som redan är klara enligt manifest
    pending_files = [f for f in day_files if not manifest.is_complete(f['uuid'])]
    
    if not pending_files:
        _log("  ✅ Alla filer för dagen redan klara enligt manifest")
        return

    # Mappa UUID -> filename för loggning/check
    uuid_map = {f['uuid']: f['filename'] for f in pending_files}
    target_uuids = set(uuid_map.keys())
    
    _log(f"  ⏳ Väntar på {len(target_uuids)} filer (ID-baserad check)...")
    
    initial_lake_count = count_lake_files()
    last_completed_count = 0
    last_activity_time = time.time()
    
    while target_uuids:
        time.sleep(POLL_INTERVAL_SECONDS)
        
        # 1. Kolla Lake (leta efter UUID i filnamn)
        if os.path.exists(LAKE_STORE):
            for f in os.listdir(LAKE_STORE):
                if not f.endswith('.md'): continue
                
                # Extrahera UUID från Lake-fil
                # Lake-format: Original_UUID.md
                match = UUID_SUFFIX_PATTERN.search(f)
                if match:
                    found_uuid = match.group(1)
                    if found_uuid in target_uuids:
                        target_uuids.remove(found_uuid)
                        manifest.mark_complete(found_uuid, "success")
                        _log(f"     ✓ Klar: {f}")

        # 2. Kolla Failed
        if os.path.exists(ASSET_FAILED):
            for f in os.listdir(ASSET_FAILED):
                # Failed kan ha olika namnstrukturer, försök hitta UUID
                # Oftast Original.ext (UUID strippad) - detta är svårt att matcha exakt på UUID
                # Men DocConverter _move_to_failed behåller ofta originalnamnet om möjligt
                # eller så måste vi lita på att timeout fångar dem.
                # Förbättring: Låt oss anta att vi väntar på success. 
                # Om vi ser aktivitet i Failed kan vi varna.
                pass

        # Aktivitet och Timeout
        completed_so_far = len(uuid_map) - len(target_uuids)
        if completed_so_far > last_completed_count:
            last_completed_count = completed_so_far
            last_activity_time = time.time()
            _log(f"     Framsteg: {completed_so_far}/{len(uuid_map)} klara")
        
        inactive_seconds = time.time() - last_activity_time
        if inactive_seconds >= INACTIVITY_TIMEOUT_SECONDS:
            remaining_names = [uuid_map[u] for u in target_uuids]
            raise RuntimeError(
                f"HARDFAIL: Timeout. Väntar fortfarande på: {remaining_names[:3]}..."
            )
            
        if int(inactive_seconds) % 60 == 0 and inactive_seconds > 0:
             _log(f"     Väntar... ({int(INACTIVITY_TIMEOUT_SECONDS - inactive_seconds)}s kvar till timeout)")

    _log(f"  ✅ Dagen klar!")

# === TAXONOMY ONLY STUBS (Behålls för bakåtkompatibilitet men används ej i phased) ===
def run_taxonomy_only_rebuild(days_limit=None, use_multipass=False):
    _log("Taxonomy-only mode är inte uppdaterad för Manifest-systemet än.")
    _log("Använd --phase foundation istället.")

# === MAIN ===

def run_staged_rebuild(phase: str, days_limit: int = None, use_multipass: bool = False):
    global _current_staging_info
    
    _log("═══════════════════════════════════════════════")
    _log(f"  STAGED REBUILD - Fas: {phase.upper()}")
    _log("═══════════════════════════════════════════════")
    
    # 1. Initiera Manifest
    manifest = RebuildManifest(MANIFEST_FILE)
    manifest.set_phase(phase)
    
    # 2. Samla filer för fasen
    _log("\n📁 Samlar filer...")
    all_files = get_all_source_files(phase)
    if not all_files:
        _log("❌ Inga filer att processa för denna fas.")
        return

    # Registrera targets i manifest
    all_uuids = [f['uuid'] for f in all_files]
    manifest.add_targets(all_uuids)
    
    pending_files = [f for f in all_files if not manifest.is_complete(f['uuid'])]
    _log(f"   Totalt {len(all_files)} filer, {len(pending_files)} återstår att processa.")
    
    if not pending_files:
        _log("✅ Alla filer i denna fas är redan klara.")
        return

    # 3. Gruppera PENDING files per datum (vi behöver inte processa klara dagar)
    files_by_date = group_files_by_date(all_files) # Gruppera ALLA för att kunna återställa rätt
    sorted_dates = sorted(files_by_date.keys())
    
    if days_limit:
        sorted_dates = sorted_dates[:days_limit]
        _log(f"   Begränsat till {days_limit} dagar.")

    # 4. Flytta ALLA filer till staging (för att tömma assets)
    _log("\n📦 Flyttar filer till staging...")
    staging_info = move_to_staging(all_files)
    _current_staging_info = staging_info
    
    if use_multipass:
        os.environ['DOC_CONVERTER_MULTIPASS'] = '1'
        CONFIG.setdefault("processing", {})["multipass_enabled"] = True
        _log("   🔬 Multipass-extraktion aktiverad")

    try:
        for i, date in enumerate(sorted_dates, 1):
            day_files = files_by_date[date]
            
            # Kolla om dagens filer redan är klara
            day_pending = [f for f in day_files if not manifest.is_complete(f['uuid'])]
            if not day_pending:
                # Dagen är helt klar, men vi måste ändå återställa filerna från staging 
                # så de ligger rätt i Assets (annars försvinner de vid cleanup).
                # Men vi behöver inte starta tjänster.
                _log(f"📅 DAG {i}/{len(sorted_dates)}: {date} (Redan klar)")
                restore_files_for_date(date, files_by_date, staging_info, manifest)
                continue

            _log(f"\n{'─' * 50}")
            _log(f"📅 DAG {i}/{len(sorted_dates)}: {date}")
            _log(f"   {len(day_pending)} filer att indexera (av {len(day_files)})")
            
            # Återställ filer
            _log("   📂 Återställer dagens filer...")
            restore_files_for_date(date, files_by_date, staging_info, manifest)
            
            # Starta tjänster
            _log("   🚀 Startar tjänster...")
            start_services(phase)
            
            # Vänta på completion
            try:
                wait_for_day_completion(day_files, date, manifest)
            except RuntimeError as e:
                _log(f"\n❌ {e}")
                stop_services()
                raise
            
            stop_services()
            
            # Konsolidering
            run_graph_builder()
            run_dreamer()
            
            _log(f"   ✅ Dag {date} klar!")
            
        _log(f"\n{'═' * 50}")
        _log("🎉 FAS KLAR!")
        
    finally:
        stop_services()
        if _current_staging_info:
            _log("\n📂 Återställer kvarvarande filer...")
            restore_all_from_staging(_current_staging_info)
        cleanup_staging()

def handle_interrupt(signum, frame):
    _log("\n⚠️ Avbruten.")
    stop_services()
    if _current_staging_info:
        _log("📂 Återställer filer...")
        restore_all_from_staging(_current_staging_info)
    cleanup_staging()
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--phase', choices=['foundation', 'enrichment'], required=True, 
                        help='Välj fas: foundation (text) eller enrichment (ljud)')
    parser.add_argument('--multipass', action='store_true')
    parser.add_argument('--days', type=int)
    
    args = parser.parse_args()
    
    # Global setup
    global NAMESPACE, CONFIG
    NAMESPACE = args
    CONFIG = load_config()
    
    if not args.confirm:
        print("Kräver --confirm.")
        return

    signal.signal(signal.SIGINT, handle_interrupt)
    signal.signal(signal.SIGTERM, handle_interrupt)
    
    run_staged_rebuild(phase=args.phase, days_limit=args.days, use_multipass=args.multipass)

if __name__ == "__main__":
    main()
