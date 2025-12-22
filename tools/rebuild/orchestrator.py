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
from services.review.interactive_review import run_interactive_review, apply_review_decisions
from services.utils.graph_service import GraphStore

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
    
    def _run_graph_builder(self):
        """Kör graf-byggning direkt i samma process för att ha kontroll över GraphStore-anslutningar."""
        _log("  🧠 Kör Graf-byggning...")
        
        # Importera och kör direkt istället för subprocess
        # Detta löser DuckDB-låskonflikter eftersom alla GraphStore-anslutningar sker i samma process
        try:
            from services.indexers.graph_builder import process_lake_batch
            process_lake_batch()
            _log("  ✅ Graf-byggning klar")
        except Exception as e:
            _log(f"  ❌ Graf-byggning fel: {e}")
            LOGGER.error(f"Graph Builder Error: {e}", exc_info=True)
            raise RuntimeError(f"Graf-byggning misslyckades: {e}") from e
    
    def _run_dreamer(self):
        """Kör dreamer för konsolidering direkt i samma process."""
        _log("  💭 Kör Dreamer...")
        
        # Importera och kör consolidate() direkt istället för subprocess
        # Detta ger oss tillgång till review_list i returvärdet
        try:
            from services.processors.dreamer import consolidate
            result = consolidate()
            
            # consolidate() returnerar en dict med stats inklusive review_list
            review_list = result.get("review_list", [])
            status = result.get("status", "OK")
            
            if status == "OK":
                _log(f"  ✅ Dreamer klar: {len(review_list)} entiteter behöver granskning")
            elif status == "NO_AI":
                _log("  ⚠️ Dreamer klar men AI-klient saknas")
            else:
                _log(f"  ⚠️ Dreamer status: {status}")
            
            return {
                "status": status,
                "review_list": review_list,
                "stats": result
            }
        except ImportError as e:
            error_msg = f"Dreamer import fel: {e}"
            _log(f"  ⚠️ {error_msg}")
            LOGGER.error(f"HARDFAIL: {error_msg}")
            return {"status": "ERROR", "error": str(e), "review_list": []}
        except Exception as e:
            error_msg = f"Dreaming misslyckades: {e}"
            _log(f"  ⚠️ {error_msg}")
            LOGGER.error(f"HARDFAIL: {error_msg}", exc_info=True)
            return {"status": "ERROR", "error": str(e), "review_list": []}
    
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
        
        if days_limit:
            sorted_dates = sorted_dates[:days_limit]
            _log(f"   Begränsat till {days_limit} dagar.")

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
                
                LOGGER.info(f"DEBUG: Tjänster startade, väntar nu på filprocessering...")
                
                # Vänta på completion
                try:
                    self.completion_watcher.wait_for_completion(day_files, date)
                except RuntimeError as e:
                    _log(f"\n❌ {e}")
                    self.service_manager.stop()
                    raise
                
                self.service_manager.stop()
                
                # Konsolidering
                self._run_graph_builder()
                dreamer_result = self._run_dreamer()
                
                # Interaktiv granskning om det finns entiteter att granska
                review_list = dreamer_result.get("review_list", [])
                LOGGER.info(f"DEBUG: Dreamer returnerade {len(review_list)} entiteter för granskning")
                if review_list:
                    _log("   🔍 Granskning av förslag...")
                    # Läs taxonomy_path från config (Princip 8)
                    taxonomy_path = os.path.expanduser(self.config['paths']['taxonomy_file'])
                    
                    # Ladda taxonomy
                    if os.path.exists(taxonomy_path):
                        with open(taxonomy_path, 'r', encoding='utf-8') as f:
                            taxonomy = json.load(f)
                    else:
                        taxonomy = {}
                    
                    # Öppna GraphStore för att spara validation rules
                    graph_path = os.path.expanduser(self.config['paths']['graph_db'])
                    graph = GraphStore(graph_path, read_only=False)
                    try:
                        # Kör interaktiv granskning (skicka med taxonomy för automatisk godkännande)
                        review_decisions = run_interactive_review(review_list, taxonomy)
                        
                        # Applicera beslut
                        if review_decisions:
                            apply_review_decisions(taxonomy, review_decisions, graph)
                            
                            # Spara uppdaterad taxonomy
                            if taxonomy:
                                with open(taxonomy_path, 'w', encoding='utf-8') as f:
                                    json.dump(taxonomy, f, ensure_ascii=False, indent=2)
                            
                            _log(f"   ✅ {len(review_decisions)} beslut applicerade")
                    finally:
                        graph.close()
                
                _log(f"   ✅ Dag {date} klar!")
                
            _log(f"\n{'═' * 50}")
            _log("🎉 FAS KLAR!")
            
        finally:
            self.service_manager.stop()
            if self.staging_info:
                _log("\n📂 Återställer kvarvarande filer...")
                self.file_manager.restore_all_from_staging(self.staging_info)
            self.file_manager.cleanup_staging()

