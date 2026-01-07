
import logging
import json
import os
import yaml
from typing import List, Dict, Any, Tuple
from services.utils.graph_service import GraphStore
from services.utils.vector_service import VectorService
from services.agents.validator_mcp import LLMClient # Reuse LLM client

# Setup logging
LOGGER = logging.getLogger("EntityResolver")

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
        # Hämta 50 kandidater (40 Relevans, 10 Underhåll)
        candidates = self.graph_store.get_refinement_candidates(limit=50)
        
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
        keywords = node.get("properties", {}).get("context_keywords", [])
        if keywords:
            search_text += " " + " ".join(keywords)

        # Semantisk sökning med KB-BERT
        results = self.vector_service.search(search_text, limit=10)
        
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
        
        # Ta bort brus från properties
        props = clean_node["properties"]
        keys_to_remove = ["status", "confidence", "created_at", "last_seen_at", "last_synced_at", "source_system"]
        for key in keys_to_remove:
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
        """Kondensera kontext-keywords för en nod om listan är för lång."""
        node = self.graph_store.get_node(node_id)
        if not node: return
        
        keywords = node.get('properties', {}).get('context_keywords', [])
        # Tröskel: 15 keywords
        if not isinstance(keywords, list) or len(keywords) < 15:
            return 
            
        LOGGER.info(f"🧹 Pruning context for {node_id} ({len(keywords)} keywords)...")
        
        prompt_template = self.prompts.get("context_pruning_prompt", "")
        if not prompt_template:
            LOGGER.warning("Missing context_pruning_prompt")
            return

        prompt = prompt_template.format(keywords=json.dumps(keywords, ensure_ascii=False))
        
        try:
            response_text = self.llm_client.generate(prompt)
            # Rensa markdown-block om det finns
            cleaned_text = response_text.replace("```json", "").replace("```", "").strip()
            
            try:
                result = json.loads(cleaned_text)
            except json.JSONDecodeError:
                # Fallback: Försök hitta JSON inuti texten om den är "pratig"
                import re
                match = re.search(r'\{.*\}', cleaned_text, re.DOTALL)
                if match:
                    result = json.loads(match.group(0))
                else:
                    raise

            if isinstance(result, dict) and "pruned_keywords" in result:
                new_keywords = result["pruned_keywords"]
                
                # Uppdatera noden med de nya, städade keywordsen
                props = node.get('properties', {})
                props['context_keywords'] = new_keywords
                self.graph_store.upsert_node(node['id'], node['type'], node['aliases'], props)
                LOGGER.info(f"   ✨ Pruned to {len(new_keywords)} keywords.")
                
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

        # Säkerhetströsklar
        THRESHOLD_DELETE = 0.95
        THRESHOLD_SPLIT = 0.90
        THRESHOLD_RENAME_NORMAL = 0.95
        THRESHOLD_RENAME_WEAK = 0.70

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
                if conf >= 0.90:
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
                if merge_eval.get("decision") == "MERGE" and merge_eval.get("confidence", 0) > 0.90:
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