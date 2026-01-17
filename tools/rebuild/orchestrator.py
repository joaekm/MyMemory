"""
Rebuild Orchestrator.

Coordinates all rebuild modules to execute staged rebuild process.
Uses ingestion_engine.process_document() for consistent pipeline.

Refactored as part of OBJEKT-73.
"""

import os
import sys
import json
import time
import logging

# Lägg till project root i path för imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.rebuild.file_manager import FileManager
from tools.rebuild.process_manager import CompletionWatcher
from services.utils.graph_service import GraphService
from services.utils.shared_lock import resource_lock, clear_stale_locks

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
        self.completion_watcher = CompletionWatcher(config, self.file_manager.manifest)
        self.project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.staging_info = {}

        # Clear stale locks from previous crashed runs
        clear_stale_locks()
    
    def _run_dreamer(self):
        """Kör Dreamer (Entity Resolver) för att städa grafen."""
        _log("  😴 Kör Dreamer (Städning & Länkning)...")
        try:
            from services.utils.vector_service import VectorService
            from services.engines.dreamer import Dreamer

            # Ladda paths från config
            graph_path = os.path.expanduser(self.config['paths']['graph_db'])

            # Ta lås för hela Dreamer-cykeln
            with resource_lock("graph", exclusive=True):
                with resource_lock("vector", exclusive=True):
                    # Initiera tjänster
                    graph_service = GraphService(graph_path)
                    vector_service = VectorService()
                    dreamer = Dreamer(graph_service, vector_service)

                    # Kör cykel
                    stats = dreamer.run_resolution_cycle(dry_run=False)

                    _log(f"  ✅ Dreamer klar: Merged={stats.get('merged', 0)}, Renamed={stats.get('renamed', 0)}")
                    graph_service.close()

            # Reset counter after Dreamer run
            try:
                from services.engines.ingestion_engine import reset_dreamer_counter
                reset_dreamer_counter()
            except ImportError:
                pass

        except Exception as e:
            _log(f"  ❌ KRITISKT Dreamer-fel: {e}")
            LOGGER.error(f"Dreamer Error: {e}", exc_info=True)
            raise RuntimeError(f"HARDFAIL: Dreamer failed: {e}") from e
    
    def run(self, days_limit=None, use_multipass=False):
        """Kör rebuild-processen."""
        _log("═══════════════════════════════════════════════")
        _log(f"  STAGED REBUILD - Fas: {self.phase.upper()}")
        _log("═══════════════════════════════════════════════")

        # Reset Dreamer counter to prevent daemon from triggering during rebuild
        try:
            from services.engines.ingestion_engine import reset_dreamer_counter
            reset_dreamer_counter()
            _log("   🔄 Dreamer-räknare nollställd")
        except ImportError:
            LOGGER.warning("Could not import reset_dreamer_counter")
        
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

                # Återställ dagens filer från staging till Assets
                _log("   📂 Återställer dagens filer...")
                self.file_manager.restore_files_for_date(date, files_by_date, self.staging_info)
                
                # DIREKT PROCESSING: Använd ingestion_engine.process_document()
                # Detta säkerställer samma pipeline som realtids-ingestion (OBJEKT-73)
                _log(f"   🔧 Processar {len(day_pending)} filer via IngestionEngine...")
                try:
                    from services.engines.ingestion_engine import process_document

                    # Ta lås på graph och vector för hela dagens batch
                    with resource_lock("graph", exclusive=True):
                        with resource_lock("vector", exclusive=True):
                            for f in day_pending:
                                if os.path.exists(f['path']):
                                    _log(f"      → {f['filename']}")
                                    try:
                                        # _lock_held=True eftersom vi redan har låsen
                                        process_document(f['path'], f['filename'], _lock_held=True)
                                    except Exception as doc_err:
                                        LOGGER.error(f"HARDFAIL: Fel vid processning av {f['filename']}: {doc_err}")
                                        raise RuntimeError(f"HARDFAIL: Document processing failed for {f['filename']}: {doc_err}") from doc_err
                except ImportError as e:
                    LOGGER.error(f"HARDFAIL: Kunde inte importera IngestionEngine: {e}")
                    raise RuntimeError(f"IngestionEngine import failed: {e}")

                # Vänta på att filer dyker upp i Lake (verifiering)
                try:
                    self.completion_watcher.wait_for_completion(day_files, date)
                except RuntimeError as e:
                    _log(f"\n❌ {e}")
                    raise

                # Städning (Dreamer) - körs med eget lås
                self._run_dreamer()
                
                _log(f"   ✅ Dag {date} klar!")
                
            _log(f"\n{'═' * 50}")
            _log("🎉 FAS KLAR!")

        finally:
            if self.staging_info:
                _log("\n📂 Återställer kvarvarande filer...")
                self.file_manager.restore_all_from_staging(self.staging_info)
            self.file_manager.cleanup_staging()

