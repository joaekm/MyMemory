"""
Process Manager for Rebuild System.

Handles service lifecycle (start/stop) and completion detection.
"""

import os
import sys
import signal
import subprocess
import time
import yaml
import logging
import re

LOGGER = logging.getLogger('ProcessManager')

# UUID-pattern för att matcha filnamn
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.(md|txt|m4a|mp3|wav)$')

# Timeout-inställningar
INACTIVITY_TIMEOUT_SECONDS = 1800  # 30 minuter utan nya filer i Lake → HARDFAIL
POLL_INTERVAL_SECONDS = 10        # Hur ofta vi kollar Lake

# Services definition
SERVICES = [
    {"path": "services/collectors/file_retriever.py", "name": "File Retriever"},
    {"path": "services/processors/transcriber.py", "name": "Transcriber"},
    {"path": "services/processors/doc_converter.py", "name": "Doc Converter"},
    {"path": "services/indexers/vector_indexer.py", "name": "Vector Indexer"},
]


def _log(msg):
    """Helper för att logga med timestamp."""
    from datetime import datetime
    print(f"{datetime.now().strftime('[%H:%M:%S]')} {msg}")


class ServiceManager:
    """Hanterar lifecycle för indexeringstjänster."""
    
    def __init__(self, config):
        self.config = config
        self.running_processes = []
        self.project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    
    def start(self, phase="foundation"):
        """Starta indexeringstjänster baserat på fas."""
        self.running_processes = []
        python_exec = sys.executable
        env = os.environ.copy()
        env['PYTHONPATH'] = self.project_root
        
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
            script_path = os.path.join(self.project_root, service["path"])
            if not os.path.exists(script_path):
                LOGGER.warning(f"DEBUG: Service-script saknas: {script_path}")
                continue
            try:
                p = subprocess.Popen(
                    [python_exec, script_path],
                    cwd=self.project_root,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                self.running_processes.append(p)
                LOGGER.info(f"DEBUG: Startade {service['name']} (PID: {p.pid})")
                time.sleep(0.5)
            except Exception as e:
                LOGGER.error(f"DEBUG: Kunde inte starta {service['name']}: {e}")
                _log(f"  ❌ {service['name']}: {e}")
                raise RuntimeError(f"HARDFAIL: Kunde inte starta {service['name']}: {e}") from e
        _log(f"  ▶️ {len(self.running_processes)} tjänster startade (Fas: {phase})")
    
    def stop(self):
        """Stoppa alla körande tjänster."""
        for p in self.running_processes:
            try:
                p.terminate()
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=2)  # Vänta efter kill
            except Exception as e:
                LOGGER.warning(f"Kunde inte stoppa process {p.pid} korrekt: {e}")
        self.running_processes = []
        _log("  ⏹️ Tjänster stoppade")
        
        # Ytterligare cleanup: Hitta och döda alla Python-processer som kan hålla DuckDB-låset
        self._kill_graphdb_processes()
    
    def _kill_graphdb_processes(self):
        """Hitta och döda alla Python-processer som kan hålla DuckDB-låset för GraphDB."""
        try:
            services_dir = os.path.join(self.project_root, "services")
            
            # Läs GraphDB-sökväg från config
            config_path = os.path.join(self.project_root, "config", "my_mem_config.yaml")
            graphdb_path = None
            if os.path.exists(config_path):
                try:
                    with open(config_path, 'r') as f:
                        config = yaml.safe_load(f)
                        if config and 'paths' in config and 'graph_db' in config['paths']:
                            graphdb_path = os.path.expanduser(config['paths']['graph_db'])
                except Exception as e:
                    # HARDFAIL: Logga men fortsätt (config-läsning är optional för process-killing)
                    LOGGER.debug(f"Kunde inte läsa config för graph_db-sökväg: {e}")
            
            # Använd ps för att hitta Python-processer som kör services-script eller kan ha öppnat GraphDB
            result = subprocess.run(
                ["ps", "aux"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                killed_any = False
                for line in result.stdout.split('\n'):
                    # Sök efter Python-processer som antingen:
                    # 1. Kör services-script
                    # 2. Innehåller GraphDB-sökvägen i kommandoraden
                    # 3. Kör Python i MyMemory-projektet
                    is_my_memory_process = (
                        'python' in line.lower() and (
                            services_dir in line or
                            (graphdb_path and graphdb_path in line) or
                            self.project_root in line
                        )
                    )
                    
                    if is_my_memory_process:
                        # Extrahera PID
                        parts = line.split()
                        if len(parts) > 1:
                            try:
                                pid = int(parts[1])
                                # Hoppa över vår egen process
                                if pid == os.getpid():
                                    continue
                                # Kontrollera om processen fortfarande lever
                                try:
                                    os.kill(pid, 0)  # Test signal, kastar OSError om processen inte finns
                                    LOGGER.warning(f"DEBUG: Hittade kvarvarande process {pid}, dödar den: {line[:100]}")
                                    try:
                                        os.kill(pid, signal.SIGTERM)
                                        time.sleep(0.5)
                                        # Kontrollera om den fortfarande lever
                                        try:
                                            os.kill(pid, 0)
                                            # Fortfarande lever, force kill
                                            os.kill(pid, signal.SIGKILL)
                                            LOGGER.warning(f"DEBUG: Force-killade process {pid}")
                                        except OSError as e:
                                            # HARDFAIL: Processen är död (förväntat beteende)
                                            LOGGER.debug(f"Process {pid} är redan död: {e}")
                                            pass
                                        killed_any = True
                                    except OSError as e:
                                        # HARDFAIL: Processen är redan död (förväntat beteende)
                                        LOGGER.debug(f"Process {pid} är redan död vid SIGTERM: {e}")
                                        pass
                                except OSError as e:
                                    # HARDFAIL: Processen finns inte (förväntat beteende)
                                    LOGGER.debug(f"Process {pid} finns inte: {e}")
                                    pass
                            except (ValueError, IndexError):
                                continue
                
                if killed_any:
                    LOGGER.info(f"DEBUG: Dödade kvarvarande processer, väntar 2s för att lås ska frigöras...")
                    time.sleep(2)
        except Exception as e:
            LOGGER.warning(f"DEBUG: Kunde inte rensa GraphDB-processer: {e}")


class CompletionWatcher:
    """Övervakar när filer är klara genom att kolla Lake och Failed-mappar."""
    
    def __init__(self, config, manifest):
        self.config = config
        self.manifest = manifest
        self.lake_store = os.path.expanduser(config['paths']['lake_store'])
        self.asset_failed = os.path.expanduser(config['paths']['asset_failed'])
    
    def _count_lake_files(self) -> int:
        """Räkna antal .md filer i Lake."""
        if not os.path.exists(self.lake_store):
            return 0
        return len([f for f in os.listdir(self.lake_store) if f.endswith('.md')])
    
    def wait_for_completion(self, day_files: list, date: str):
        """
        Vänta på att specifika UUIDs dyker upp i Lake eller Failed.
        """
        # Filtrera ut filer som redan är klara enligt manifest
        pending_files = [f for f in day_files if not self.manifest.is_complete(f['uuid'])]
        
        if not pending_files:
            _log("  ✅ Alla filer för dagen redan klara enligt manifest")
            return

        # Mappa UUID -> filename för loggning/check
        uuid_map = {f['uuid']: f['filename'] for f in pending_files}
        target_uuids = set(uuid_map.keys())
        
        _log(f"  ⏳ Väntar på {len(target_uuids)} filer (ID-baserad check)...")
        LOGGER.info(f"DEBUG: Letar efter UUIDs: {list(target_uuids)}")
        LOGGER.info(f"DEBUG: Filnamn: {[uuid_map[u] for u in target_uuids]}")
        
        # Kolla om filerna faktiskt finns i Assets
        for f in pending_files:
            if not os.path.exists(f['path']):
                LOGGER.warning(f"DEBUG: Fil saknas i Assets: {f['path']}")
            else:
                LOGGER.info(f"DEBUG: Fil finns i Assets: {f['path']}")
        
        initial_lake_count = self._count_lake_files()
        last_completed_count = 0
        last_activity_time = time.time()
        iteration = 0
        
        while target_uuids:
            iteration += 1
            time.sleep(POLL_INTERVAL_SECONDS)
            
            # Logga varje 6:e iteration (varje minut)
            if iteration % 6 == 0:
                LOGGER.info(f"DEBUG: Iteration {iteration}, väntar fortfarande på {len(target_uuids)} UUIDs")
                LOGGER.info(f"DEBUG: Lake-filer totalt: {self._count_lake_files()}")
                if os.path.exists(self.lake_store):
                    lake_files = [f for f in os.listdir(self.lake_store) if f.endswith('.md')]
                    LOGGER.info(f"DEBUG: Lake-filer (första 5): {lake_files[:5]}")
            
            # 1. Kolla Lake (leta efter UUID i filnamn)
            if os.path.exists(self.lake_store):
                lake_files = os.listdir(self.lake_store)
                LOGGER.debug(f"DEBUG: Iteration {iteration}: Scanning {len(lake_files)} files in Lake, looking for {len(target_uuids)} UUIDs: {list(target_uuids)}")
                for f in lake_files:
                    if not f.endswith('.md'): continue
                    
                    # Extrahera UUID från Lake-fil
                    # Lake-format: Original_UUID.md
                    match = UUID_SUFFIX_PATTERN.search(f)
                    if match:
                        found_uuid = match.group(1)
                        LOGGER.debug(f"DEBUG: Found UUID {found_uuid} in Lake file {f}, checking if in target_uuids: {found_uuid in target_uuids}")
                        if found_uuid in target_uuids:
                            target_uuids.remove(found_uuid)
                            self.manifest.mark_complete(found_uuid, "success")
                            _log(f"     ✓ Klar: {f}")
                            LOGGER.info(f"DEBUG: Hittade match i Lake: {f} -> UUID {found_uuid}, återstående: {len(target_uuids)}")
                    elif iteration % 6 == 0:  # Logga bara varje minut
                        # Logga filer som inte matchar mönstret
                        LOGGER.debug(f"DEBUG: Lake-fil matchar inte UUID-mönster: {f}")

            # 2. Kolla Failed
            if os.path.exists(self.asset_failed):
                failed_files = os.listdir(self.asset_failed)
                if failed_files and iteration % 6 == 0:
                    LOGGER.warning(f"DEBUG: Filer i Failed-mappen: {failed_files[:3]}")
                for f in failed_files:
                    # Försök matcha UUID även i Failed
                    match = UUID_SUFFIX_PATTERN.search(f)
                    if match:
                        found_uuid = match.group(1)
                        if found_uuid in target_uuids:
                            LOGGER.warning(f"DEBUG: Fil hamnade i Failed: {f} (UUID: {found_uuid})")
                            target_uuids.remove(found_uuid)
                            self.manifest.mark_complete(found_uuid, "failed")

            # Aktivitet och Timeout
            completed_so_far = len(uuid_map) - len(target_uuids)
            if completed_so_far > last_completed_count:
                last_completed_count = completed_so_far
                last_activity_time = time.time()
                _log(f"     Framsteg: {completed_so_far}/{len(uuid_map)} klara")
            
            inactive_seconds = time.time() - last_activity_time
            if inactive_seconds >= INACTIVITY_TIMEOUT_SECONDS:
                remaining_names = [uuid_map[u] for u in target_uuids]
                LOGGER.error(f"HARDFAIL: Timeout efter {inactive_seconds}s inaktivitet")
                LOGGER.error(f"HARDFAIL: Återstående UUIDs: {list(target_uuids)}")
                LOGGER.error(f"HARDFAIL: Återstående filnamn: {remaining_names}")
                raise RuntimeError(
                    f"HARDFAIL: Timeout. Väntar fortfarande på: {remaining_names[:3]}..."
                )
                
            if int(inactive_seconds) % 60 == 0 and inactive_seconds > 0:
                 _log(f"     Väntar... ({int(INACTIVITY_TIMEOUT_SECONDS - inactive_seconds)}s kvar till timeout)")

        _log(f"  ✅ Dagen klar!")

