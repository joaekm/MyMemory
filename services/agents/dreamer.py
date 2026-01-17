
import logging
import json
import os
import yaml
from typing import List, Dict, Any, Tuple
from services.utils.graph_service import GraphStore
from services.utils.vector_service import VectorService
from services.utils.lake_service import LakeEditor
from services.agents.validator_mcp import LLMClient # Reuse LLM client
from services.utils.schema_validator import get_schema_validator

# Setup logging
LOGGER = logging.getLogger("EntityResolver")

def _load_dreamer_config() -> dict:
    """Ladda Dreamer-config (tröskelvärden och limits)."""
    config_path = os.path.join(
        os.path.dirname(__file__), '..', '..', 'config', 'my_mem_config.yaml'
    )
    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
        return config.get('dreamer', {})
    except Exception as e:
        LOGGER.warning(f"Kunde inte ladda dreamer-config: {e}. Använder defaults.")
        return {}

DREAMER_CONFIG = _load_dreamer_config()

class EntityResolver:
    """
    Dreamer Agent: Ansvarig för Identity Resolution och städning av grafen.
    Använder VectorService för att hitta semantiska dubbletter och LLM för att bedöma.
    """
    
    def __init__(self, graph_store: GraphStore, vector_service: VectorService, config_path: str = "config/services_prompts.yaml"):
        self.graph_store = graph_store
        self.vector_service = vector_service
        self.llm_client = LLMClient() # Reuse existing simple client wrapper
        self.prompts = self._load_prompts(config_path)
        
    def _load_prompts(self, path: str) -> dict:
        try:
            with open(path, "r") as f:
                data = yaml.safe_load(f)
                return data.get("entity_resolver", {})
        except Exception as e:
            LOGGER.error(f"Failed to load prompts from {path}: {e}")
            return {}

    def scan_candidates(self) -> List[Dict]:
        """
        Hämta kandidater för förädling enligt 80/20-strategin.
        Delegerar logiken till GraphStore för att fånga både 'Heat' (Relevans) och 'Deep Sleep' (Underhåll).
        """
        candidate_limit = DREAMER_CONFIG.get('candidate_limit', 50)
        candidates = self.graph_store.get_refinement_candidates(limit=candidate_limit)
        
        if candidates:
            LOGGER.info(f"Dreamer selected {len(candidates)} candidates via Relevance/Maintenance strategy.")
            
        return candidates

    def ensure_node_indexed(self, node: Dict):
        """Säkerställ att noden finns i vektorindexet innan sökning."""
        self.vector_service.upsert_node(node)

    def find_potential_matches(self, node: Dict) -> List[Dict]:
        """Hitta potentiella dubbletter för en given nod med SEMANTISK SÖKNING."""
        self.ensure_node_indexed(node)
        
        name = node.get("properties", {}).get("name", "")
        if not name:
            return []
            
        # Bygg söksträng (Namn + Typ + Kontext)
        search_text = f"{name} {node.get('type')}"
        node_context = node.get("properties", {}).get("node_context", [])
        if node_context and isinstance(node_context, list):
            ctx_texts = [c.get('text', '') for c in node_context if isinstance(c, dict)]
            search_text += " " + " ".join(ctx_texts)

        # Semantisk sökning med KB-BERT
        vector_limit = DREAMER_CONFIG.get('vector_search_limit', 10)
        results = self.vector_service.search(search_text, limit=vector_limit)
        
        valid_matches = []
        for res in results:
            match_id = res['id']
            if match_id == node["id"]:
                continue
            
            # Hämta hela noden från GraphStore för att verifiera och få data
            match_node = self.graph_store.get_node(match_id)
            if not match_node:
                continue

            # Typ-koll (kan vara valfritt beroende på hur strikta vi vill vara)
            if match_node["type"] != node["type"]:
                continue
                
            valid_matches.append(match_node)
            
        return valid_matches

    def _prepare_node_for_llm(self, node: Dict) -> Dict:
        """Tvätta noden från tekniskt metadata innan den skickas till LLM."""
        if not node: return {}
        
        # Kopiera för att inte modifiera originalet
        # OBS: Vi inkluderar INTE id här för att inte förvirra LLM
        clean_node = {
            "type": node.get("type"),
            "aliases": node.get("aliases", []),
            "properties": node.get("properties", {}).copy()
        }
        
        # Ta bort system-properties (include_in_vector: false) från jämförelse
        props = clean_node["properties"]
        schema = get_schema_validator().schema
        base_props = schema.get("base_properties", {}).get("properties", {})
        for key, key_def in base_props.items():
            if not key_def.get("include_in_vector", True):
                props.pop(key, None)
            
        return clean_node

    def evaluate_merge(self, primary: Dict, secondary: Dict) -> Dict:
        """Fråga LLM: Är dessa samma?"""
        prompt_template = self.prompts.get("entity_resolution_prompt", "")
        if not prompt_template:
            LOGGER.error("Missing entity_resolution_prompt")
            return {"decision": "IGNORE", "confidence": 0.0}
            
        # Använd tvättad data
        p_clean = self._prepare_node_for_llm(primary)
        s_clean = self._prepare_node_for_llm(secondary)
            
        prompt = prompt_template.format(
            node_a_json=json.dumps(p_clean, indent=2, ensure_ascii=False),
            node_b_json=json.dumps(s_clean, indent=2, ensure_ascii=False)
        )
        
        try:
            response_text = self.llm_client.generate(prompt) # Anta att denna metod finns/fungerar
            # Försök parsa JSON från svaret
            # Ofta svarar LLM med ```json ... ```
            cleaned_text = response_text.replace("```json", "").replace("```", "").strip()
            result = json.loads(cleaned_text)
            
            # FIX: Hantera om LLM returnerar en lista istället för dict
            if isinstance(result, list):
                if result:
                    result = result[0]
                else:
                    return {"decision": "IGNORE", "confidence": 0.0, "reason": "Empty list from LLM"}
                    
            if not isinstance(result, dict):
                 return {"decision": "IGNORE", "confidence": 0.0, "reason": "Invalid format from LLM"}
                 
            return result
        except Exception as e:
            LOGGER.error(f"LLM Evaluation failed: {e}")
            return {"decision": "IGNORE", "confidence": 0.0, "reason": "LLM Error"}

    def _prune_context(self, node_id: str):
        """Kondensera node_context för en nod om listan är för lång."""
        node = self.graph_store.get_node(node_id)
        if not node: return

        node_context = node.get('properties', {}).get('node_context', [])
        # Tröskel: 15 context entries
        if not isinstance(node_context, list) or len(node_context) < 15:
            return

        LOGGER.info(f"🧹 Pruning node_context for {node_id} ({len(node_context)} entries)...")

        # Extrahera text från context entries för LLM-analys
        ctx_texts = [c.get('text', '') for c in node_context if isinstance(c, dict)]

        prompt_template = self.prompts.get("context_pruning_prompt", "")
        if not prompt_template:
            LOGGER.warning("Missing context_pruning_prompt")
            return

        prompt = prompt_template.format(keywords=json.dumps(ctx_texts, ensure_ascii=False))

        try:
            response_text = self.llm_client.generate(prompt)
            cleaned_text = response_text.replace("```json", "").replace("```", "").strip()

            try:
                result = json.loads(cleaned_text)
            except json.JSONDecodeError:
                import re
                match = re.search(r'\{.*\}', cleaned_text, re.DOTALL)
                if match:
                    result = json.loads(match.group(0))
                else:
                    raise

            if isinstance(result, dict) and "pruned_keywords" in result:
                pruned_texts = set(result["pruned_keywords"])

                # Behåll endast de context entries vars text finns i pruned-listan
                new_context = [c for c in node_context if c.get('text') in pruned_texts]

                props = node.get('properties', {})
                props['node_context'] = new_context
                self.graph_store.upsert_node(node['id'], node['type'], node.get('aliases'), props)
                LOGGER.info(f"   ✨ Pruned to {len(new_context)} context entries.")

        except Exception as e:
            LOGGER.error(f"Context pruning failed: {e}")

    def _is_weak_name(self, name: str) -> bool:
        """Identifierar UUIDs eller generiska placeholders."""
        import re
        patterns = [
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', # UUID
            r'^Talare \d+$',
            r'^Unknown$',
            r'^Unit_.*$'
        ]
        return any(re.match(p, name, re.I) for p in patterns)

    def run_resolution_cycle(self, dry_run: bool = False):
        """Huvudloop för kognitivt underhåll med kausal uppdatering."""
        candidates = self.scan_candidates() # 80/20 urval
        stats = {"merged": 0, "split": 0, "renamed": 0, "recat": 0, "deleted": 0}
        affected_units = set()

        # Säkerhetströsklar från config
        thresholds = DREAMER_CONFIG.get('thresholds', {})
        THRESHOLD_DELETE = thresholds.get('delete', 0.95)
        THRESHOLD_SPLIT = thresholds.get('split', 0.90)
        THRESHOLD_RENAME_NORMAL = thresholds.get('rename_normal', 0.95)
        THRESHOLD_RENAME_WEAK = thresholds.get('rename_weak', 0.70)
        THRESHOLD_RECATEGORIZE = thresholds.get('recategorize', 0.90)
        THRESHOLD_MERGE = thresholds.get('merge', 0.90)

        for node in candidates:
            # 1. Strukturell Analys (Split/Rename/Recat/Delete)
            analysis = self.check_structural_changes(node)
            action = analysis.get("action", "KEEP")
            conf = analysis.get("confidence", 0.0)
            
            # --- HEURISTISKA SPÄRRAR ---
            if action == "DELETE":
                # Kolla om noden är isolerad i grafen
                if self.graph_store.get_node_degree(node["id"]) > 0:
                    action = "KEEP" # Veto: radera inte nav-noder
                elif conf < THRESHOLD_DELETE:
                    action = "KEEP"

            elif action == "RENAME":
                is_weak = self._is_weak_name(node["id"])
                target_threshold = THRESHOLD_RENAME_WEAK if is_weak else THRESHOLD_RENAME_NORMAL
                if conf < target_threshold:
                    action = "KEEP"

            # --- VERKSTÄLLANDE ---
            if action == "DELETE" and not dry_run:
                self.graph_store.delete_node(node["id"])
                self.vector_service.delete(node["id"])
                stats["deleted"] += 1
                continue # Gå till nästa kandidat

            elif action == "RENAME" and not dry_run:
                new_name = analysis.get("new_name")
                units = self.graph_store.get_related_unit_ids(node["id"])
                self.graph_store.rename_node(node["id"], new_name)
                affected_units.update(units)
                stats["renamed"] += 1
                node = self.graph_store.get_node(new_name) # Fortsätt analys med nya namnet

            elif action == "RE-CATEGORIZE" and not dry_run:
                if conf >= THRESHOLD_RECATEGORIZE:
                    self.graph_store.recategorize_node(node["id"], analysis.get("new_type"))
                    affected_units.update(self.graph_store.get_related_unit_ids(node["id"]))
                    stats["recat"] += 1

            elif action == "SPLIT" and not dry_run:
                if conf >= THRESHOLD_SPLIT:
                    units = self.graph_store.get_related_unit_ids(node["id"])
                    self.graph_store.split_node(node["id"], analysis.get("split_clusters"))
                    affected_units.update(units)
                    stats["split"] += 1
                    continue

            # 2. Identity Resolution (Merge)
            # Körs endast om noden inte raderats eller splittats
            matches = self.find_potential_matches(node)
            for match in matches:
                merge_eval = self.evaluate_merge(match, node)
                if merge_eval.get("decision") == "MERGE" and merge_eval.get("confidence", 0) >= THRESHOLD_MERGE:
                    if not dry_run:
                        units = self.graph_store.get_related_unit_ids(node["id"])
                        self.graph_store.merge_nodes(match["id"], node["id"]) # Robust merge
                        affected_units.update(units)
                    stats["merged"] += 1
                    break

        # 3. Kausal Semantisk Uppdatering
        if affected_units and not dry_run:
            LOGGER.info(f"Triggar semantisk uppdatering för {len(affected_units)} filer...")
            self.generate_semantic_update(list(affected_units))

        return stats

    def check_structural_changes(self, node: Dict) -> Dict:
        """
        Anropar LLM för att analysera om noden kräver strukturella ändringar.
        Returnerar ett beslutsobjekt med action, confidence och eventuella parametrar.

        Actions:
        - KEEP: Ingen ändring behövs
        - DELETE: Noden är brus och ska tas bort
        - RENAME: Noden har svagt namn, byt till föreslaget namn
        - SPLIT: Noden innehåller flera distinkta entiteter
        - RE-CATEGORIZE: Nodens typ matchar inte innehållet
        """
        # 1. Hämta node_context (bevisen)
        context_list = node.get("properties", {}).get("node_context", [])

        # Om ingen kontext finns kan vi inte göra en meningsfull analys
        if not context_list:
            return {"action": "KEEP", "confidence": 1.0, "reason": "No context available for analysis"}

        # 2. Formatera kontexten som en numrerad lista för LLM (så den kan referera till index vid SPLIT)
        # Format: [0] Text (Källa: UUID)
        formatted_context = ""
        for i, ctx in enumerate(context_list[:40]):  # Begränsa till 40 bevis för att spara tokens
            text = ctx.get("text", "Inget innehåll")
            origin = ctx.get("origin", "Okänd källa")
            formatted_context += f"[{i}] {text} (Källa: {origin})\n"

        # 3. Hämta prompt och formatera
        prompt_template = self.prompts.get("structural_analysis", "")
        if not prompt_template:
            LOGGER.error("Missing structural_analysis prompt in config")
            return {"action": "KEEP", "confidence": 0.0}

        # Vi kan skicka med existerande taxonomi-noder om vi vill styra RE-CATEGORIZE
        prompt = prompt_template.format(
            id=node.get("id"),
            type=node.get("type"),
            context_list=formatted_context,
            taxonomy_nodes="Person, Project, Organization, Group, Event, Roles, Business_relation"
        )

        # 4. Exekvera LLM-anrop
        try:
            response_text = self.llm_client.generate(prompt)

            # Städa upp Markdown-formatering
            cleaned_json = response_text.replace("```json", "").replace("```", "").strip()
            result = json.loads(cleaned_json)

            # Säkerställ att vi har de fält som krävs för logiken i run_resolution_cycle
            if "action" not in result:
                result["action"] = "KEEP"
            if "confidence" not in result:
                result["confidence"] = 0.0

            return result

        except Exception as e:
            LOGGER.error(f"Structural analysis failed for {node.get('id')}: {e}")
            # Fallback till säker åtgärd vid fel
            return {"action": "KEEP", "confidence": 0.0, "reason": f"Analysis error: {str(e)}"}

    def generate_semantic_update(self, unit_ids: List[str]) -> int:
        """
        Regenererar semantisk metadata för Lake-filer som påverkats av graf-ändringar.

        Triggas efter MERGE, SPLIT, RENAME, RE-CATEGORIZE operationer.
        Uppdaterar context_summary, relations_summary och document_keywords
        baserat på den nya graf-strukturen.

        Sätter automatiskt timestamp_updated via LakeEditor.update_semantics().

        Args:
            unit_ids: Lista med unit_id för filer som behöver uppdateras

        Returns:
            Antal filer som uppdaterades
        """
        if not unit_ids:
            return 0

        # Hämta Lake-sökväg från config
        lake_path = self._get_lake_path()
        if not lake_path:
            LOGGER.error("Kunde inte hitta Lake-sökväg i config")
            return 0

        lake_editor = LakeEditor(lake_path)
        updated_count = 0

        for unit_id in unit_ids:
            # Hitta Lake-filen för detta unit_id
            filepath = self._find_lake_file(lake_path, unit_id)
            if not filepath:
                LOGGER.warning(f"Kunde inte hitta Lake-fil för unit_id: {unit_id}")
                continue

            try:
                # Läs nuvarande metadata och innehåll
                current_meta = lake_editor.read_metadata(filepath)
                if not current_meta:
                    continue

                # Läs filinnehållet för LLM-analys
                file_content = self._read_file_content(filepath)
                if not file_content:
                    continue

                # Hämta uppdaterad graf-kontext för denna fil
                graph_context = self._get_graph_context_for_unit(unit_id)

                # Generera ny semantisk metadata via LLM
                new_semantics = self._regenerate_semantics_llm(
                    file_content,
                    current_meta,
                    graph_context
                )

                if not new_semantics:
                    continue

                # Uppdatera filen - timestamp_updated sätts automatiskt
                success = lake_editor.update_semantics(
                    filepath,
                    context_summary=new_semantics.get('context_summary'),
                    relations_summary=new_semantics.get('relations_summary'),
                    document_keywords=new_semantics.get('document_keywords'),
                    set_timestamp_updated=True
                )

                if success:
                    updated_count += 1
                    LOGGER.info(f"✨ Semantisk uppdatering: {os.path.basename(filepath)}")

            except Exception as e:
                LOGGER.error(f"Fel vid semantisk uppdatering av {unit_id}: {e}")

        LOGGER.info(f"Semantisk uppdatering klar: {updated_count}/{len(unit_ids)} filer")
        return updated_count

    def _get_lake_path(self) -> str:
        """Hämtar Lake-sökväg från config."""
        try:
            config_path = os.path.join(
                os.path.dirname(__file__), '..', '..', 'config', 'my_mem_config.yaml'
            )
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            return os.path.expanduser(config['paths']['lake_store'])
        except Exception as e:
            LOGGER.error(f"Kunde inte läsa config: {e}")
            return ""

    def _find_lake_file(self, lake_path: str, unit_id: str) -> str:
        """Hittar Lake-fil baserat på unit_id."""
        try:
            for filename in os.listdir(lake_path):
                if unit_id in filename and filename.endswith('.md'):
                    return os.path.join(lake_path, filename)
        except Exception as e:
            LOGGER.error(f"Fel vid sökning efter Lake-fil: {e}")
        return ""

    def _read_file_content(self, filepath: str) -> str:
        """Läser innehållet från en Lake-fil (exklusive frontmatter)."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()

            # Hoppa över frontmatter
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    return parts[2].strip()
            return content
        except Exception as e:
            LOGGER.error(f"Kunde inte läsa fil {filepath}: {e}")
            return ""

    def _get_graph_context_for_unit(self, unit_id: str) -> str:
        """
        Hämtar graf-kontext för en specifik unit.
        Returnerar en sträng med relevanta entiteter och relationer.
        """
        try:
            # Hitta alla noder som nämner denna unit
            mentions = self.graph_store.get_nodes_mentioning_unit(unit_id)

            if not mentions:
                return "Inga kända entiteter kopplade till detta dokument."

            context_lines = ["KÄNDA ENTITETER I DOKUMENTET:"]
            for node in mentions:
                node_type = node.get('type', 'Unknown')
                name = node.get('properties', {}).get('name', node.get('id'))
                context_lines.append(f"- [{node_type}] {name}")

            return "\n".join(context_lines)
        except Exception as e:
            LOGGER.warning(f"Kunde inte hämta graf-kontext för {unit_id}: {e}")
            return ""

    def _regenerate_semantics_llm(self, file_content: str, current_meta: Dict, graph_context: str) -> Dict:
        """
        Anropar LLM för att regenerera semantisk metadata.

        Args:
            file_content: Dokumentets innehåll
            current_meta: Nuvarande frontmatter
            graph_context: Kontext från grafen (kända entiteter)

        Returns:
            Dict med context_summary, relations_summary, document_keywords
            eller None vid fel
        """
        prompt_template = self.prompts.get("semantic_regeneration", "")
        if not prompt_template:
            LOGGER.warning("Missing semantic_regeneration prompt - using current metadata")
            return None

        # Begränsa innehållet för att spara tokens
        truncated_content = file_content[:15000]

        prompt = prompt_template.format(
            file_content=truncated_content,
            current_summary=current_meta.get('context_summary', ''),
            current_relations=current_meta.get('relations_summary', ''),
            current_keywords=json.dumps(current_meta.get('document_keywords', []), ensure_ascii=False),
            graph_context=graph_context
        )

        try:
            response_text = self.llm_client.generate(prompt)

            # Städa upp Markdown-formatering
            cleaned_json = response_text.replace("```json", "").replace("```", "").strip()
            result = json.loads(cleaned_json)

            # Validera att vi fick de förväntade fälten
            if not isinstance(result, dict):
                return None

            return {
                'context_summary': result.get('context_summary'),
                'relations_summary': result.get('relations_summary'),
                'document_keywords': result.get('document_keywords')
            }

        except Exception as e:
            LOGGER.error(f"Semantic regeneration failed: {e}")
            return None