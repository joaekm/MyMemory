"""
Process Manager for Rebuild System.

OBJEKT-73 Refactoring:
- ServiceManager REMOVED - orchestrator now calls ingestion_engine directly
- CompletionWatcher verifies Lake output after processing
"""

import os
import time
import logging
import re

LOGGER = logging.getLogger('ProcessManager')

# UUID-pattern för att matcha filnamn - ignorerar case
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.(md|txt|m4a|mp3|wav)$', re.IGNORECASE)

# Timeout-inställningar
INACTIVITY_TIMEOUT_SECONDS = 1800
POLL_INTERVAL_SECONDS = 5


def _log(msg):
    from datetime import datetime
    print(f"{datetime.now().strftime('[%H:%M:%S]')} {msg}")


class CompletionWatcher:
    """Verifies that processed files appear in Lake."""

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
        LOGGER.debug(f"Targets: {[uuid[:8] for uuid in target_uuids]}")

        last_activity_time = time.time()
        iteration = 0

        while target_uuids:
            iteration += 1
            time.sleep(POLL_INTERVAL_SECONDS)

            # 1. Kolla Lake
            if os.path.exists(self.lake_store):
                lake_files = os.listdir(self.lake_store)

                for f in lake_files:
                    if not f.endswith('.md'):
                        continue

                    match = UUID_SUFFIX_PATTERN.search(f)
                    if match:
                        found_uuid = match.group(1).lower()
                        if found_uuid in target_uuids:
                            target_uuids.remove(found_uuid)
                            self.manifest.mark_complete(found_uuid, "success")
                            _log(f"     ✓ Klar i Lake: {f}")
                            last_activity_time = time.time()

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
                            last_activity_time = time.time()

            # Timeout
            inactive_seconds = time.time() - last_activity_time
            if inactive_seconds >= INACTIVITY_TIMEOUT_SECONDS:
                remaining_names = [uuid_map[u] for u in target_uuids]

                print("\n🔍 DIAGNOS VID TIMEOUT:")
                print(f"   Väntade på UUIDs: {list(target_uuids)}")
                print(f"   Lake sökväg: {self.lake_store}")
                if os.path.exists(self.lake_store):
                    print(f"   Filer i Lake ({len(os.listdir(self.lake_store))} st):")
                    for f in os.listdir(self.lake_store)[-10:]:
                        print(f"     - {f}")

                raise RuntimeError(f"HARDFAIL: Timeout. Väntar fortfarande på: {remaining_names[:3]}...")
