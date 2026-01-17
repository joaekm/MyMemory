"""
Rebuild Orchestrator.

Coordinates all rebuild modules to execute staged rebuild process.
"""

import os
import sys
import json
import time
import logging

# Lägg till project root i path för imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.rebuild.file_manager import FileManager
from tools.rebuild.process_manager import ServiceManager, CompletionWatcher
from services.utils.graph_service import GraphService

LOGGER = logging.getLogger('RebuildOrchestrator')


def _log(msg):
    """Helper för att logga med timestamp."""
    from datetime import datetime
    print(f"{datetime.now().strftime('[%H:%M:%S]')} {msg}")


class RebuildOrchestrator:
    """Orkestrerar rebuild-processen."""
    
    def __init__(self, phase, config):
        self.phase = phase
        self.config = config
        self.file_manager = FileManager(config)
        self.service_manager = ServiceManager(config)
        self.completion_watcher = CompletionWatcher(config, self.file_manager.manifest)
        self.project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.staging_info = {}
    
    def _run_dreamer(self):
        """Kör Dreamer (Entity Resolver) för att städa grafen."""
        _log("  😴 Kör Dreamer (Städning & Länkning)...")
        try:
            from services.utils.graph_service import GraphService
            from services.utils.vector_service import VectorService
            from services.engines.dreamer import Dreamer

            # Ladda paths från config
            graph_path = os.path.expanduser(self.config['paths']['graph_db'])

            # Initiera tjänster
            graph_service = GraphService(graph_path)
            vector_service = VectorService()
            dreamer = Dreamer(graph_service, vector_service)

            # Kör cykel
            stats = dreamer.run_resolution_cycle(dry_run=False)
            
            _log(f"  ✅ Dreamer klar: Merged={stats['merged']}, Reviewed={stats['reviewed']}")
            graph_service.close()
            
        except Exception as e:
            _log(f"  ⚠️  Dreamer fel (Icke-kritiskt): {e}")
            LOGGER.error(f"Dreamer Error: {e}", exc_info=True)
            # Vi låter inte Dreamer-fel stoppa hela rebuilden, men vi loggar det.
    
    def run(self, days_limit=None, use_multipass=False):
        """Kör rebuild-processen."""
        _log("═══════════════════════════════════════════════")
        _log(f"  STAGED REBUILD - Fas: {self.phase.upper()}")
        _log("═══════════════════════════════════════════════")
        
        # 1. Initiera Manifest
        self.file_manager.manifest.set_phase(self.phase)
        
        # 2. Samla filer för fasen
        _log("\n📁 Samlar filer...")
        all_files = self.file_manager.get_all_source_files(self.phase)
        if not all_files:
            _log("❌ Inga filer att processa för denna fas.")
            return

        # Registrera targets i manifest
        all_uuids = [f['uuid'] for f in all_files]
        self.file_manager.manifest.add_targets(all_uuids)
        
        pending_files = [f for f in all_files if not self.file_manager.manifest.is_complete(f['uuid'])]
        _log(f"   Totalt {len(all_files)} filer, {len(pending_files)} återstår att processa.")
        
        if not pending_files:
            _log("✅ Alla filer i denna fas är redan klara.")
            return

        # 3. Gruppera PENDING files per datum (vi behöver inte processa klara dagar)
        files_by_date = self.file_manager.group_files_by_date(all_files)  # Gruppera ALLA för att kunna återställa rätt
        sorted_dates = sorted(files_by_date.keys())
        
        # Filtrera bort helt klara dagar INNAN vi applicerar days_limit
        # Annars fastnar vi på samma dag om den redan är klar
        pending_dates = []
        completed_dates = []
        for date in sorted_dates:
            day_files = files_by_date[date]
            day_pending = [f for f in day_files if not self.file_manager.manifest.is_complete(f['uuid'])]
            if day_pending:
                pending_dates.append(date)
            else:
                completed_dates.append(date)
        
        if days_limit:
            # Begränsa till X PENDING dagar, men inkludera alla completed för återställning
            dates_to_process = pending_dates[:days_limit]
            sorted_dates = completed_dates + dates_to_process  # Först completed (för cleanup), sen pending
            _log(f"   Begränsat till {days_limit} pending dagar ({len(pending_dates)} totalt).")


        # 4. Flytta ALLA filer till staging (för att tömma assets)
        _log("\n📦 Flyttar filer till staging...")
        self.staging_info = self.file_manager.move_to_staging(all_files)
        
        if use_multipass:
            os.environ['DOC_CONVERTER_MULTIPASS'] = '1'
            self.config.setdefault("processing", {})["multipass_enabled"] = True
            _log("   🔬 Multipass-extraktion aktiverad")

        try:
            for i, date in enumerate(sorted_dates, 1):
                day_files = files_by_date[date]
                
                # Kolla om dagens filer redan är klara
                day_pending = [f for f in day_files if not self.file_manager.manifest.is_complete(f['uuid'])]
                if not day_pending:
                    # Dagen är helt klar, men vi måste ändå återställa filerna från staging 
                    # så de ligger rätt i Assets (annars försvinner de vid cleanup).
                    # Men vi behöver inte starta tjänster.
                    _log(f"📅 DAG {i}/{len(sorted_dates)}: {date} (Redan klar)")
                    self.file_manager.restore_files_for_date(date, files_by_date, self.staging_info)
                    continue

                _log(f"\n{'─' * 50}")
                _log(f"📅 DAG {i}/{len(sorted_dates)}: {date}")
                _log(f"   {len(day_pending)} filer att indexera (av {len(day_files)})")
                
                # Starta tjänster FÖRST så att watchdogs är redo när filer återställs
                _log("   🚀 Startar tjänster...")
                service_start_time = time.time()
                self.service_manager.start(self.phase)
                service_start_duration = time.time() - service_start_time
                
                # Kort paus för att tjänsterna ska starta och watchdogs ska vara redo
                time.sleep(2)
                LOGGER.info(f"DEBUG: Tjänster startade, återställer nu filer...")
                
                # Återställ filer EFTER att tjänsterna startat (så watchdogs ser dem som nya)
                _log("   📂 Återställer dagens filer...")
                self.file_manager.restore_files_for_date(date, files_by_date, self.staging_info)
                
                # Verifiera att filerna faktiskt finns i Assets efter återställning
                for f in day_pending:
                    if os.path.exists(f['path']):
                        LOGGER.info(f"DEBUG: Fil verifierad i Assets efter återställning: {f['path']}")
                    else:
                        LOGGER.error(f"DEBUG: Fil saknas i Assets efter återställning: {f['path']}")
                
                # DIREKT PROCESSING: Bypaassa watchdog helt!
                # macOS FSEvents är opålitligt, så vi anropar DocConverter direkt
                _log(f"   🔧 Triggar DocConverter direkt för {len(day_pending)} filer...")
                try:
                    # VIKTIGT: Importera modulen FÖRST och initiera GATEKEEPER INNAN vi importerar funktioner
                    # Annars får funktionerna en None-referens till GATEKEEPER
                    import services.processors.doc_converter as dc_module
                    
                    # Initiera Gatekeeper om den inte redan är initierad
                    if dc_module.GATEKEEPER is None:
                        _log("      📦 Initierar Gatekeeper...")
                        dc_module.GATEKEEPER = dc_module.EntityGatekeeper()
                        _log(f"      ✓ Gatekeeper redo")
                    
                    for f in day_pending:
                        if os.path.exists(f['path']):
                            _log(f"      → {f['filename']}")
                            dc_module.processa_dokument(f['path'], f['filename'])
                except ImportError as e:
                    LOGGER.warning(f"Kunde inte importera DocConverter direkt: {e}")
                    # Fallback: vänta på watchdog
                    _log("   ⏳ Fallback: Väntar på att watchdogs ska upptäcka filer...")
                    time.sleep(5)

                
                LOGGER.info(f"DEBUG: Filer processade, väntar nu på att de ska dyka upp i Lake...")

                
                # Vänta på completion
                try:
                    self.completion_watcher.wait_for_completion(day_files, date)
                except RuntimeError as e:
                    _log(f"\n❌ {e}")
                    self.service_manager.stop()
                    raise
                
                self.service_manager.stop()

                # Städning (Dreamer)
                self._run_dreamer()
                
                _log(f"   ✅ Dag {date} klar!")
                
            _log(f"\n{'═' * 50}")
            _log("🎉 FAS KLAR!")
            
        finally:
            self.service_manager.stop()
            if self.staging_info:
                _log("\n📂 Återställer kvarvarande filer...")
                self.file_manager.restore_all_from_staging(self.staging_info)
            self.file_manager.cleanup_staging()

