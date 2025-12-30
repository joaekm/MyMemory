"""
File Manager for Rebuild System.

Handles manifest (state tracking), file collection, and staging operations.
"""

import os
import sys
import re
import json
import shutil
import logging
from collections import defaultdict

# Lägg till project root i path för imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from services.utils.date_service import get_date

LOGGER = logging.getLogger('FileManager')

# UUID-pattern för att matcha filnamn
UUID_PATTERN = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.(md|txt|m4a|mp3|wav)$')


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


class FileManager:
    """Hanterar filhantering, manifest och staging för rebuild-processen."""
    
    def __init__(self, config):
        self.config = config
        self.asset_documents = os.path.expanduser(config['paths']['asset_documents'])
        self.asset_slack = os.path.expanduser(config['paths']['asset_slack'])
        self.asset_recordings = os.path.expanduser(config['paths']['asset_recordings'])
        self.asset_calendar = config['paths'].get('asset_calendar', os.path.join(config['paths']['asset_store'], 'Calendar'))
        self.asset_calendar = os.path.expanduser(self.asset_calendar)
        self.asset_mail = config['paths'].get('asset_mail', os.path.join(config['paths']['asset_store'], 'Mail'))
        self.asset_mail = os.path.expanduser(self.asset_mail)
        self.staging_root = os.path.join(os.path.expanduser(config['paths']['asset_store']), '.staging')
        manifest_file = os.path.join(os.path.expanduser(config['paths']['asset_store']), '.rebuild_manifest.json')
        self.manifest = RebuildManifest(manifest_file)
    
    def extract_uuid(self, filename):
        """Extrahera UUID från filnamn."""
        match = UUID_SUFFIX_PATTERN.search(filename)
        if match:
            return match.group(1)
        # Fallback: sök var som helst i strängen
        match_loose = UUID_PATTERN.search(filename)
        return match_loose.group(0) if match_loose else None
    
    def get_all_source_files(self, phase="foundation") -> list:
        """
        Samla filer beroende på fas.
        Foundation: Slack, Docs, Mail, Calendar (textbaserat).
        Enrichment: Recordings (ljud).
        """
        files = []
        
        if phase == "foundation":
            folders = [self.asset_slack, self.asset_calendar, self.asset_mail]
        elif phase == "enrichment":
            folders = [self.asset_recordings, self.asset_documents]
        else:
            # Default to foundation folders (Strict: No documents)
            folders = [self.asset_slack, self.asset_calendar, self.asset_mail]

        for folder in folders:
            if not os.path.exists(folder):
                continue
            for f in os.listdir(folder):
                if f.startswith('.'):
                    continue
                filepath = os.path.join(folder, f)
                if os.path.isfile(filepath):
                    uuid = self.extract_uuid(f)
                    if uuid:
                        files.append({
                            'path': filepath,
                            'folder': folder,
                            'filename': f,
                            'uuid': uuid
                        })
        return files
    
    def group_files_by_date(self, files: list) -> dict:
        """Gruppera filer per datum."""
        by_date = defaultdict(list)
        for f in files:
            try:
                date = get_date(f['path'])
                by_date[date].append(f)
            except RuntimeError as e:
                LOGGER.error(f"Kunde inte extrahera datum för {f['filename']}: {e}")
        return dict(by_date)
    
    def move_to_staging(self, files: list) -> dict:
        """Flytta filer till staging-mappen."""
        staging_info = {}
        os.makedirs(self.staging_root, exist_ok=True)
        
        for f in files:
            original_path = f['path']
            folder_name = os.path.basename(f['folder'])
            staging_folder = os.path.join(self.staging_root, folder_name)
            os.makedirs(staging_folder, exist_ok=True)
            
            staging_path = os.path.join(staging_folder, f['filename'])
            
            shutil.move(original_path, staging_path)
            # Logga inte varje filflytt för att minska brus
            staging_info[f['filename']] = {
                'staging_path': staging_path,
                'original_folder': f['folder']
            }
        
        return staging_info
    
    def restore_files_for_date(self, date: str, files_by_date: dict, staging_info: dict):
        """Flytta tillbaka filer för datumet, MEN bara de som inte är klara."""
        files = files_by_date.get(date, [])
        restored_count = 0
        
        LOGGER.info(f"DEBUG: restore_files_for_date: {len(files)} filer för {date}")
        
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
            
            if self.manifest.is_complete(f['uuid']):
                # Redan klar i tidigare körning (t.ex. avbruten dag).
                # Återställ den så den inte raderas vid cleanup, men förvänta ingen action.
                LOGGER.debug(f"DEBUG: Fil redan klar enligt manifest: {f['filename']} (UUID: {f['uuid']})")
                pass
            
            info = staging_info.get(f['filename'])
            if not info:
                LOGGER.warning(f"DEBUG: Ingen staging-info för {f['filename']}")
                continue
            
            staging_path = info['staging_path']
            original_folder = info['original_folder']
            original_path = os.path.join(original_folder, f['filename'])
            
            if os.path.exists(staging_path):
                # VIKTIGT: Använd copy + remove istället för move
                # Detta triggar on_created event som watchdog ser som en ny fil
                # move() från staging triggar inte alltid on_moved eftersom källan är utanför bevakad mapp
                shutil.copy2(staging_path, original_path)
                os.remove(staging_path)
                
                # FORCE watchdog detection på macOS:
                # 1. Uppdatera filens timestamp för att garantera FSEvents ser ändringen
                os.utime(original_path, None)  # Touch filen
                
                # 2. Kort paus mellan filer så watchdog hinner reagera
                import time
                time.sleep(0.1)  # 100ms mellan varje fil
                
                restored_count += 1
                LOGGER.info(f"DEBUG: Återställde fil: {f['filename']} -> {original_path}")
            else:
                LOGGER.warning(f"DEBUG: Staging-fil saknas: {staging_path}")
                
        if restored_count > 0:
            LOGGER.info(f"RESTORE: {restored_count} filer återställda för {date}")

    
    def restore_all_from_staging(self, staging_info: dict):
        """Återställ alla filer från staging."""
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
    
    def cleanup_staging(self):
        """Rensa staging-katalogen."""
        if os.path.exists(self.staging_root):
            shutil.rmtree(self.staging_root)
            LOGGER.info("🧹 Staging-katalog borttagen")







