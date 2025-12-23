"""
Process Manager for Rebuild System (Hyper-Verbose Debug Mode).
"""

import os
import sys
import signal
import subprocess
import time
import yaml
import logging
import re

# UUID-pattern för att matcha filnamn - ignorerar case
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.(md|txt|m4a|mp3|wav)$', re.IGNORECASE)

# Timeout-inställningar
INACTIVITY_TIMEOUT_SECONDS = 1800
POLL_INTERVAL_SECONDS = 5

# Services definition
SERVICES = [
    {"path": "services/collectors/file_retriever.py", "name": "File Retriever"},
    {"path": "services/processors/transcriber.py", "name": "Transcriber"},
    {"path": "services/processors/doc_converter.py", "name": "Doc Converter"},
    {"path": "services/indexers/vector_indexer.py", "name": "Vector Indexer"},
]

def _log(msg):
    from datetime import datetime
    print(f"{datetime.now().strftime('[%H:%M:%S]')} {msg}")

class ServiceManager:
    def __init__(self, config):
        self.config = config
        self.running_processes = []
        self.project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    
    def start(self, phase="foundation"):
        self.running_processes = []
        python_exec = sys.executable
        env = os.environ.copy()
        env['PYTHONPATH'] = self.project_root
        
        active_services = []
        if phase == "foundation":
            active_services = [s for s in SERVICES if s["name"] != "Transcriber"]
        else:
            active_services = SERVICES

        for service in active_services:
            script_path = os.path.join(self.project_root, service["path"])
            if not os.path.exists(script_path):
                print(f"⚠️  Script saknas: {script_path}")
                continue
            
            try:
                p = subprocess.Popen(
                    [python_exec, script_path],
                    cwd=self.project_root,
                    env=env
                )
                self.running_processes.append(p)
                time.sleep(0.5)
                if p.poll() is not None:
                    _log(f"❌ {service['name']} dog direkt! (Exit: {p.returncode})")
                    self.stop()
                    raise RuntimeError(f"Tjänst kraschade: {service['name']}")
                    
            except Exception as e:
                _log(f"❌ Kunde inte starta {service['name']}: {e}")
                raise
                
        _log(f"  ▶️ {len(self.running_processes)} tjänster startade")
    
    def stop(self):
        for p in self.running_processes:
            if p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.running_processes = []
        self._kill_graphdb_processes()
    
    def _kill_graphdb_processes(self):
        try:
            subprocess.run(["pkill", "-f", "services/processors"], capture_output=True)
            subprocess.run(["pkill", "-f", "services/indexers"], capture_output=True)
        except Exception:
            pass

class CompletionWatcher:
    def __init__(self, config, manifest):
        self.config = config
        self.manifest = manifest
        self.lake_store = os.path.expanduser(config['paths']['lake_store'])
        self.asset_failed = os.path.expanduser(config['paths']['asset_failed'])
    
    def wait_for_completion(self, day_files: list, date: str):
        pending_files = [f for f in day_files if not self.manifest.is_complete(f['uuid'])]
        
        if not pending_files:
            return

        # Normalisera UUIDs till lowercase
        uuid_map = {f['uuid'].lower(): f['filename'] for f in pending_files}
        target_uuids = set(uuid_map.keys())
        
        _log(f"  ⏳ Väntar på {len(target_uuids)} filer...")
        print(f"     [DEBUG] Targets: {[uuid[:8] for uuid in target_uuids]}")
        
        last_activity_time = time.time()
        iteration = 0
        
        while target_uuids:
            iteration += 1
            time.sleep(POLL_INTERVAL_SECONDS)
            
            # 1. Kolla Lake
            if os.path.exists(self.lake_store):
                lake_files = os.listdir(self.lake_store)
                
                # Debug-utskrift var 5:e sekund
                if iteration % 2 == 0:
                    # print(f"     [DEBUG] Scannar {len(lake_files)} filer i Lake...")
                    pass

                for f in lake_files:
                    if not f.endswith('.md'): continue
                    
                    match = UUID_SUFFIX_PATTERN.search(f)
                    if match:
                        found_uuid = match.group(1).lower()
                        if found_uuid in target_uuids:
                            target_uuids.remove(found_uuid)
                            self.manifest.mark_complete(found_uuid, "success")
                            _log(f"     ✓ Klar i Lake: {f}")
                        
                        # Extra debug om vi hittar filen men den inte matchar targets (kan vara en dubblett eller felaktig state)
                        # elif iteration % 10 == 0 and iteration < 20:
                        #    print(f"     [DEBUG] Ignorerar {found_uuid[:8]} (ej i targets)")

            # 2. Kolla Failed
            if os.path.exists(self.asset_failed):
                for f in os.listdir(self.asset_failed):
                    match = UUID_SUFFIX_PATTERN.search(f)
                    if match:
                        found_uuid = match.group(1).lower()
                        if found_uuid in target_uuids:
                            _log(f"     ❌ Misslyckades (i Failed): {f}")
                            target_uuids.remove(found_uuid)
                            self.manifest.mark_complete(found_uuid, "failed")

            # Timeout och Progress
            inactive_seconds = time.time() - last_activity_time
            if inactive_seconds >= INACTIVITY_TIMEOUT_SECONDS:
                remaining_names = [uuid_map[u] for u in target_uuids]
                
                print("\n🔍 DIAGNOS VID TIMEOUT:")
                print(f"   Väntade på UUIDs: {list(target_uuids)}")
                print(f"   Lake sökväg: {self.lake_store}")
                if os.path.exists(self.lake_store):
                    print(f"   Filer i Lake ({len(os.listdir(self.lake_store))} st):")
                    for f in os.listdir(self.lake_store)[-10:]: # Visa sista 10
                        print(f"     - {f}")
                
                raise RuntimeError(f"HARDFAIL: Timeout. Väntar fortfarande på: {remaining_names[:3]}...")