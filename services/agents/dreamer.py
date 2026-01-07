
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

    def run_resolution_cycle(self, dry_run: bool = False):
        """Kör en hel städ-cykel."""
        candidates = self.scan_candidates()
        if not candidates:
            return {"merged": 0, "reviewed": 0, "ignored": 0}
            
        LOGGER.info(f"🔍 Dreamer: Analyserar {len(candidates)} nya/osäkra noder...")
        
        stats = {"merged": 0, "reviewed": 0, "ignored": 0}
        
        processed_pairs = set()
        deleted_nodes = set()

        for node in candidates:
            # Hoppa över om noden redan har raderats i denna cykel
            if node["id"] in deleted_nodes:
                continue

            name = node.get('properties', {}).get('name', 'Okänd')
            node_type = node.get("type", "Unknown")
            
            # Använder nu Vector Search
            matches = self.find_potential_matches(node)
            if matches:
                LOGGER.info(f"   🔎 Söker dubbletter för '{name}' ({node_type})... Hittade {len(matches)} kandidater.")
            
            for match in matches:
                # Hoppa över om match-kandidaten har raderats
                if match["id"] in deleted_nodes:
                    continue

                match_name = match.get('properties', {}).get('name', 'Okänd')
                
                # Undvik A-B och B-A dubbletter
                pair_id = tuple(sorted([node["id"], match["id"]]))
                if pair_id in processed_pairs:
                    continue
                processed_pairs.add(pair_id)
                
                # LLM Judge
                evaluation = self.evaluate_merge(match, node) # Match is Candidate A (often Verified?), Node is B
                
                decision = evaluation.get("decision", "IGNORE").upper()
                confidence = evaluation.get("confidence", 0.0)
                reason = evaluation.get("reason", "No reason provided")
                
                if decision == "MERGE" and confidence > 0.9:
                    if dry_run:
                        LOGGER.info(f"      [DRY RUN] ✨ Skulle slå ihop '{name}' -> '{match_name}' ({int(confidence*100)}%)")
                    else:
                        LOGGER.info(f"      ✨ Slår ihop '{name}' -> '{match_name}' ({int(confidence*100)}%)")
                        self.graph_store.merge_nodes_into(match["id"], node["id"])
                        
                        # Markera noden som raderad
                        deleted_nodes.add(node["id"])
                        
                        # Uppdatera index efter merge (ta bort secondary, uppdatera primary)
                        self.vector_service.delete(node["id"])
                        
                        # Hämta den uppdaterade masternoden
                        master_node = self.graph_store.get_node(match["id"])
                        
                        # Städa kontexten om den blivit för fet
                        self._prune_context(master_node["id"])
                        
                        # Indexera om (med städad kontext)
                        # Hämta på nytt om vi ändrade i prune
                        master_node = self.graph_store.get_node(match["id"]) 
                        self.vector_service.upsert_node(master_node)
                    
                    stats["merged"] += 1
                    # Eftersom node är raderad, bryt den inre loopen (kan inte matcha mot fler)
                    break 
                    
                elif decision == "REVIEW" or (decision == "MERGE" and confidence <= 0.9):
                    node_type = node.get("type", "Unknown")
                    if dry_run:
                        LOGGER.info(f"      [DRY RUN] 🍺 ({node_type}) Skulle spara osäker match: '{name}' vs '{match_name}' ({int(confidence*100)}%)")
                    else:
                        LOGGER.info(f"      🍺 ({node_type}) Sparar osäker match: '{name}' vs '{match_name}' ({int(confidence*100)}%) - {reason}")
                        self.graph_store.add_pending_review(
                            entity=node["properties"].get("name", "Unknown"),
                            master_node=match["id"],
                            score=confidence,
                            reason=reason,
                            context={"candidate_id": node["id"], "match_id": match["id"]}
                        )
                    stats["reviewed"] += 1
                else:
                    # Tyst om det inte är en match, för att minska brus
                    stats["ignored"] += 1
                    
        return stats
