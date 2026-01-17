#!/usr/bin/env python3
"""
Dreamer Engine (v2.0)

Batch refinement of the knowledge graph.
Phase 3 of the pipeline: Collect & Normalize -> Ingestion -> DREAMING

Responsibilities:
- Scan candidates for refinement (80/20 strategy)
- Structural analysis (SPLIT, RENAME, DELETE, RE-CATEGORIZE)
- Entity resolution (MERGE duplicates)
- Propagate changes back to Lake/Vector
"""

import logging
import json
import os
import re
import yaml
from typing import List, Dict, Any

from services.utils.graph_service import GraphService
from services.utils.vector_service import VectorService
from services.utils.lake_service import LakeService
from services.utils.llm_service import LLMService, TaskType
from services.utils.schema_validator import SchemaValidator

LOGGER = logging.getLogger("Dreamer")

_SCHEMA_VALIDATOR = None

def _get_schema_validator():
    """Get cached SchemaValidator instance."""
    global _SCHEMA_VALIDATOR
    if _SCHEMA_VALIDATOR is None:
        _SCHEMA_VALIDATOR = SchemaValidator()
    return _SCHEMA_VALIDATOR


def get_schema_validator():
    """Public accessor for schema validator (used in _prepare_node_for_llm)."""
    return _get_schema_validator()


def _load_dreamer_config() -> dict:
    """Load Dreamer config (thresholds and limits)."""
    config_path = os.path.join(
        os.path.dirname(__file__), '..', '..', 'config', 'my_mem_config.yaml'
    )
    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
        return config.get('dreamer', {})
    except Exception as e:
        LOGGER.warning(f"Could not load dreamer config: {e}. Using defaults.")
        return {}


DREAMER_CONFIG = _load_dreamer_config()


class Dreamer:
    """
    Dreamer Engine: Responsible for identity resolution and graph maintenance.
    Uses VectorService for semantic duplicate detection and LLM for evaluation.
    """

    def __init__(self, graph_service: GraphService, vector_service: VectorService,
                 config_path: str = "config/services_prompts.yaml"):
        self.graph_service = graph_service
        self.vector_service = vector_service
        self.llm_service = LLMService()
        self.prompts = self._load_prompts(config_path)

    def _load_prompts(self, path: str) -> dict:
        try:
            with open(path, "r") as f:
                data = yaml.safe_load(f)
                # Merge dreamer and entity_resolver sections
                prompts = data.get("dreamer", {})
                prompts.update(data.get("entity_resolver", {}))
                return prompts
        except Exception as e:
            LOGGER.error(f"Failed to load prompts from {path}: {e}")
            return {}

    def _get_node_type_description(self, node_type: str) -> str:
        """Get schema description for a node type."""
        schema = get_schema_validator().schema
        node_def = schema.get("nodes", {}).get(node_type, {})
        description = node_def.get("description", "")
        if not description:
            return f"(Ingen beskrivning tillgänglig för '{node_type}')"
        return description

    def _validate_edges_for_recategorize(self, node_id: str, new_type: str) -> tuple:
        """
        Validate that all edges remain valid after hypothetical type change.

        Returns:
            (all_valid: bool, invalid_edges: list of edge descriptions)
        """
        edges_out = self.graph_service.get_edges_from(node_id)
        edges_in = self.graph_service.get_edges_to(node_id)
        all_edges = edges_out + edges_in

        if not all_edges:
            return (True, [])

        validator = get_schema_validator()
        invalid_edges = []

        for edge in all_edges:
            # Build nodes_map with the NEW type for this node
            source_type = new_type if edge["source"] == node_id else self._get_node_type_for_id(edge["source"])
            target_type = new_type if edge["target"] == node_id else self._get_node_type_for_id(edge["target"])

            nodes_map = {edge["source"]: source_type, edge["target"]: target_type}
            ok, msg = validator.validate_edge(edge, nodes_map)

            if not ok:
                edge_desc = f"{edge['source']}-[{edge['type']}]->{edge['target']}: {msg}"
                invalid_edges.append(edge_desc)

        return (len(invalid_edges) == 0, invalid_edges)

    def _get_node_type_for_id(self, node_id: str) -> str:
        """Get type for a node from the graph."""
        node = self.graph_service.get_node(node_id)
        if node:
            return node.get("type", "Unknown")
        return "Unknown"

    def scan_candidates(self) -> List[Dict]:
        """
        Get candidates for refinement using 80/20 strategy.
        Delegates logic to GraphService to capture both 'Heat' (Relevance) and 'Deep Sleep' (Maintenance).
        """
        candidate_limit = DREAMER_CONFIG.get('candidate_limit', 50)
        candidates = self.graph_service.get_refinement_candidates(limit=candidate_limit)

        if candidates:
            LOGGER.info(f"Dreamer selected {len(candidates)} candidates via Relevance/Maintenance strategy.")

        return candidates

    def ensure_node_indexed(self, node: Dict):
        """Ensure node exists in vector index before searching."""
        self.vector_service.upsert_node(node)

    def find_potential_matches(self, node: Dict) -> List[Dict]:
        """Find potential duplicates for a given node using SEMANTIC SEARCH."""
        self.ensure_node_indexed(node)

        name = node.get("properties", {}).get("name", "")
        if not name:
            return []

        # Build search string (Name + Type + Context)
        search_text = f"{name} {node.get('type')}"
        node_context = node.get("properties", {}).get("node_context", [])
        if node_context and isinstance(node_context, list):
            ctx_texts = [c.get('text', '') for c in node_context if isinstance(c, dict)]
            search_text += " " + " ".join(ctx_texts)

        # Semantic search
        vector_limit = DREAMER_CONFIG.get('vector_search_limit', 10)
        results = self.vector_service.search(search_text, limit=vector_limit)

        valid_matches = []
        for res in results:
            match_id = res['id']
            if match_id == node["id"]:
                continue

            match_node = self.graph_service.get_node(match_id)
            if not match_node:
                continue

            if match_node["type"] != node["type"]:
                continue

            valid_matches.append(match_node)

        return valid_matches

    def _prepare_node_for_llm(self, node: Dict) -> Dict:
        """Clean node from technical metadata before sending to LLM."""
        if not node:
            return {}

        clean_node = {
            "type": node.get("type"),
            "aliases": node.get("aliases", []),
            "properties": node.get("properties", {}).copy()
        }

        # Remove system properties
        props = clean_node["properties"]
        schema = get_schema_validator().schema
        base_props = schema.get("base_properties", {}).get("properties", {})
        for key, key_def in base_props.items():
            if not key_def.get("include_in_vector", True):
                props.pop(key, None)

        return clean_node

    def evaluate_merge(self, primary: Dict, secondary: Dict) -> Dict:
        """Ask LLM: Are these the same entity?"""
        prompt_template = self.prompts.get("entity_resolution_prompt", "")
        if not prompt_template:
            LOGGER.error("Missing entity_resolution_prompt")
            return {"decision": "IGNORE", "confidence": 0.0}

        p_clean = self._prepare_node_for_llm(primary)
        s_clean = self._prepare_node_for_llm(secondary)

        prompt = prompt_template.format(
            node_a_json=json.dumps(p_clean, indent=2, ensure_ascii=False),
            node_b_json=json.dumps(s_clean, indent=2, ensure_ascii=False)
        )

        response = self.llm_service.generate(prompt, TaskType.ENTITY_RESOLUTION)
        if not response.success:
            LOGGER.error(f"LLM Evaluation failed: {response.error}")
            return {"decision": "IGNORE", "confidence": 0.0, "reason": "LLM Error"}

        try:
            cleaned_text = response.text.replace("```json", "").replace("```", "").strip()
            result = json.loads(cleaned_text)

            if isinstance(result, list):
                if result:
                    result = result[0]
                else:
                    return {"decision": "IGNORE", "confidence": 0.0, "reason": "Empty list from LLM"}

            if not isinstance(result, dict):
                return {"decision": "IGNORE", "confidence": 0.0, "reason": "Invalid format from LLM"}

            return result
        except Exception as e:
            LOGGER.error(f"LLM Evaluation parse failed: {e}")
            return {"decision": "IGNORE", "confidence": 0.0, "reason": "LLM Parse Error"}

    def prune_context(self, node_id: str):
        """Condense node_context for a node if list is too long."""
        node = self.graph_service.get_node(node_id)
        if not node:
            return

        node_context = node.get('properties', {}).get('node_context', [])
        if not isinstance(node_context, list) or len(node_context) < 15:
            return

        LOGGER.info(f"Pruning node_context for {node_id} ({len(node_context)} entries)...")

        ctx_texts = [c.get('text', '') for c in node_context if isinstance(c, dict)]

        prompt_template = self.prompts.get("context_pruning_prompt", "")
        if not prompt_template:
            LOGGER.warning("Missing context_pruning_prompt")
            return

        prompt = prompt_template.format(keywords=json.dumps(ctx_texts, ensure_ascii=False))

        response = self.llm_service.generate(prompt, TaskType.ENTITY_RESOLUTION)
        if not response.success:
            LOGGER.error(f"Context pruning LLM failed: {response.error}")
            return

        try:
            cleaned_text = response.text.replace("```json", "").replace("```", "").strip()

            try:
                result = json.loads(cleaned_text)
            except json.JSONDecodeError:
                match = re.search(r'\{.*\}', cleaned_text, re.DOTALL)
                if match:
                    result = json.loads(match.group(0))
                else:
                    raise

            if isinstance(result, dict) and "pruned_keywords" in result:
                pruned_texts = set(result["pruned_keywords"])
                new_context = [c for c in node_context if c.get('text') in pruned_texts]

                props = node.get('properties', {})
                props['node_context'] = new_context
                self.graph_service.upsert_node(node['id'], node['type'], node.get('aliases'), props)
                LOGGER.info(f"Pruned to {len(new_context)} context entries.")

        except Exception as e:
            LOGGER.error(f"Context pruning parse failed: {e}")

    def _is_weak_name(self, name: str) -> bool:
        """Identify UUIDs or generic placeholders."""
        patterns = [
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            r'^Talare \d+$',
            r'^Speaker \d+$',
            r'^Unknown$',
            r'^Unit_.*$'
        ]
        return any(re.match(p, name, re.I) for p in patterns)

    def run_resolution_cycle(self, dry_run: bool = False) -> Dict[str, int]:
        """Main loop for cognitive maintenance with causal updates."""
        candidates = self.scan_candidates()
        stats = {"merged": 0, "split": 0, "renamed": 0, "recat": 0, "deleted": 0}
        affected_units = set()

        thresholds = DREAMER_CONFIG.get('thresholds', {})
        THRESHOLD_DELETE = thresholds.get('delete', 0.95)
        THRESHOLD_SPLIT = thresholds.get('split', 0.90)
        THRESHOLD_RENAME_NORMAL = thresholds.get('rename_normal', 0.95)
        THRESHOLD_RENAME_WEAK = thresholds.get('rename_weak', 0.70)
        THRESHOLD_RECATEGORIZE = thresholds.get('recategorize', 0.90)
        THRESHOLD_MERGE = thresholds.get('merge', 0.90)

        for node in candidates:
            # 1. Structural Analysis (Split/Rename/Recat/Delete)
            analysis = self.check_structural_changes(node)
            action = analysis.get("action", "KEEP")
            conf = analysis.get("confidence", 0.0)

            # --- HEURISTIC GUARDS ---
            if action == "DELETE":
                if self.graph_service.get_node_degree(node["id"]) > 0:
                    action = "KEEP"
                elif conf < THRESHOLD_DELETE:
                    action = "KEEP"

            elif action == "RENAME":
                is_weak = self._is_weak_name(node["id"])
                target_threshold = THRESHOLD_RENAME_WEAK if is_weak else THRESHOLD_RENAME_NORMAL
                if conf < target_threshold:
                    action = "KEEP"

            # --- EXECUTION ---
            if action == "DELETE" and not dry_run:
                self.graph_service.delete_node(node["id"])
                self.vector_service.delete(node["id"])
                stats["deleted"] += 1
                continue

            elif action == "RENAME" and not dry_run:
                new_name = analysis.get("new_name")
                units = self.graph_service.get_related_unit_ids(node["id"])
                self.graph_service.rename_node(node["id"], new_name)
                affected_units.update(units)
                stats["renamed"] += 1
                node = self.graph_service.get_node(new_name)

            elif action == "RE-CATEGORIZE" and not dry_run:
                if conf >= THRESHOLD_RECATEGORIZE:
                    new_type = analysis.get("new_type")
                    # HARDFAIL: Validate edges before recategorize
                    edges_valid, invalid_edges = self._validate_edges_for_recategorize(node["id"], new_type)
                    if not edges_valid:
                        LOGGER.warning(
                            f"RE-CATEGORIZE blocked for {node['id']} -> {new_type}: "
                            f"{len(invalid_edges)} edges would become invalid. "
                            f"Details: {invalid_edges[:3]}{'...' if len(invalid_edges) > 3 else ''}"
                        )
                        continue  # Skip this operation (HARDFAIL)
                    self.graph_service.recategorize_node(node["id"], new_type)
                    affected_units.update(self.graph_service.get_related_unit_ids(node["id"]))
                    stats["recat"] += 1

            elif action == "SPLIT" and not dry_run:
                if conf >= THRESHOLD_SPLIT:
                    units = self.graph_service.get_related_unit_ids(node["id"])
                    self.graph_service.split_node(node["id"], analysis.get("split_clusters"))
                    affected_units.update(units)
                    stats["split"] += 1
                    continue

            # 2. Identity Resolution (Merge)
            matches = self.find_potential_matches(node)
            for match in matches:
                merge_eval = self.evaluate_merge(match, node)
                if merge_eval.get("decision") == "MERGE" and merge_eval.get("confidence", 0) >= THRESHOLD_MERGE:
                    if not dry_run:
                        units = self.graph_service.get_related_unit_ids(node["id"])
                        self.graph_service.merge_nodes(match["id"], node["id"])
                        affected_units.update(units)
                        # Prune context on target node after merge (may have grown large)
                        self.prune_context(match["id"])
                    stats["merged"] += 1
                    break

        # 3. Causal Semantic Update
        if affected_units and not dry_run:
            LOGGER.info(f"Triggering semantic update for {len(affected_units)} files...")
            self.propagate_changes(list(affected_units))

        return stats

    def check_structural_changes(self, node: Dict) -> Dict:
        """
        Call LLM to analyze if node requires structural changes.

        Actions:
        - KEEP: No change needed
        - DELETE: Node is noise and should be removed
        - RENAME: Node has weak name, change to suggested name
        - SPLIT: Node contains multiple distinct entities
        - RE-CATEGORIZE: Node type doesn't match content
        """
        context_list = node.get("properties", {}).get("node_context", [])

        if not context_list:
            return {"action": "KEEP", "confidence": 1.0, "reason": "No context available for analysis"}

        formatted_context = ""
        for i, ctx in enumerate(context_list[:40]):
            text = ctx.get("text", "No content")
            origin = ctx.get("origin", "Unknown source")
            formatted_context += f"[{i}] {text} (Source: {origin})\n"

        prompt_template = self.prompts.get("structural_analysis", "")
        if not prompt_template:
            LOGGER.error("Missing structural_analysis prompt in config")
            return {"action": "KEEP", "confidence": 0.0}

        node_type = node.get("type", "Unknown")
        prompt = prompt_template.format(
            id=node.get("id"),
            type=node_type,
            node_type_description=self._get_node_type_description(node_type),
            context_list=formatted_context,
            taxonomy_nodes="Person, Project, Organization, Group, Event, Roles, Business_relation"
        )

        response = self.llm_service.generate(prompt, TaskType.STRUCTURAL_ANALYSIS)
        if not response.success:
            LOGGER.error(f"Structural analysis LLM failed for {node.get('id')}: {response.error}")
            return {"action": "KEEP", "confidence": 0.0, "reason": f"LLM error: {response.error}"}

        try:
            cleaned_json = response.text.replace("```json", "").replace("```", "").strip()
            result = json.loads(cleaned_json)

            if "action" not in result:
                result["action"] = "KEEP"
            if "confidence" not in result:
                result["confidence"] = 0.0

            return result

        except Exception as e:
            LOGGER.error(f"Structural analysis parse failed for {node.get('id')}: {e}")
            return {"action": "KEEP", "confidence": 0.0, "reason": f"Parse error: {str(e)}"}

    def propagate_changes(self, unit_ids: List[str]) -> int:
        """
        Regenerate semantic metadata for Lake files affected by graph changes.

        Triggered after MERGE, SPLIT, RENAME, RE-CATEGORIZE operations.
        Updates context_summary, relations_summary and document_keywords
        based on new graph structure.

        Args:
            unit_ids: List of unit_id for files that need updating

        Returns:
            Number of files updated
        """
        if not unit_ids:
            return 0

        lake_path = self._get_lake_path()
        if not lake_path:
            LOGGER.error("Could not find Lake path in config")
            return 0

        lake_service = LakeService(lake_path)
        updated_count = 0

        for unit_id in unit_ids:
            filepath = self._find_lake_file(lake_path, unit_id)
            if not filepath:
                LOGGER.warning(f"Could not find Lake file for unit_id: {unit_id}")
                continue

            try:
                current_meta = lake_service.read_metadata(filepath)
                if not current_meta:
                    continue

                file_content = self._read_file_content(filepath)
                if not file_content:
                    continue

                graph_context = self._get_graph_context_for_unit(unit_id)

                new_semantics = self._regenerate_semantics_llm(
                    file_content,
                    current_meta,
                    graph_context
                )

                if not new_semantics:
                    continue

                success = lake_service.update_semantics(
                    filepath,
                    context_summary=new_semantics.get('context_summary'),
                    relations_summary=new_semantics.get('relations_summary'),
                    document_keywords=new_semantics.get('document_keywords'),
                    set_timestamp_updated=True
                )

                if success:
                    updated_count += 1
                    LOGGER.info(f"Semantic update: {os.path.basename(filepath)}")

            except Exception as e:
                LOGGER.error(f"Error during semantic update of {unit_id}: {e}")

        LOGGER.info(f"Semantic update complete: {updated_count}/{len(unit_ids)} files")
        return updated_count

    def _get_lake_path(self) -> str:
        """Get Lake path from config."""
        try:
            config_path = os.path.join(
                os.path.dirname(__file__), '..', '..', 'config', 'my_mem_config.yaml'
            )
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            return os.path.expanduser(config['paths']['lake_store'])
        except Exception as e:
            LOGGER.error(f"Could not read config: {e}")
            return ""

    def _find_lake_file(self, lake_path: str, unit_id: str) -> str:
        """Find Lake file based on unit_id."""
        try:
            for filename in os.listdir(lake_path):
                if unit_id in filename and filename.endswith('.md'):
                    return os.path.join(lake_path, filename)
        except Exception as e:
            LOGGER.error(f"Error searching for Lake file: {e}")
        return ""

    def _read_file_content(self, filepath: str) -> str:
        """Read content from Lake file (excluding frontmatter)."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()

            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    return parts[2].strip()
            return content
        except Exception as e:
            LOGGER.error(f"Could not read file {filepath}: {e}")
            return ""

    def _get_graph_context_for_unit(self, unit_id: str) -> str:
        """
        Get graph context for a specific unit.
        Returns a string with relevant entities and relations.
        """
        try:
            mentions = self.graph_service.get_nodes_mentioning_unit(unit_id)

            if not mentions:
                return "No known entities connected to this document."

            context_lines = ["KNOWN ENTITIES IN DOCUMENT:"]
            for node in mentions:
                node_type = node.get('type', 'Unknown')
                name = node.get('properties', {}).get('name', node.get('id'))
                context_lines.append(f"- [{node_type}] {name}")

            return "\n".join(context_lines)
        except Exception as e:
            LOGGER.warning(f"Could not get graph context for {unit_id}: {e}")
            return ""

    def _regenerate_semantics_llm(self, file_content: str, current_meta: Dict, graph_context: str) -> Dict:
        """
        Call LLM to regenerate semantic metadata.

        Args:
            file_content: Document content
            current_meta: Current frontmatter
            graph_context: Context from graph (known entities)

        Returns:
            Dict with context_summary, relations_summary, document_keywords
            or None on error
        """
        prompt_template = self.prompts.get("semantic_regeneration", "")
        if not prompt_template:
            LOGGER.warning("Missing semantic_regeneration prompt - using current metadata")
            return None

        truncated_content = file_content[:15000]

        prompt = prompt_template.format(
            file_content=truncated_content,
            current_summary=current_meta.get('context_summary', ''),
            current_relations=current_meta.get('relations_summary', ''),
            current_keywords=json.dumps(current_meta.get('document_keywords', []), ensure_ascii=False),
            graph_context=graph_context
        )

        response = self.llm_service.generate(prompt, TaskType.ENRICHMENT)
        if not response.success:
            LOGGER.error(f"Semantic regeneration LLM failed: {response.error}")
            return None

        try:
            cleaned_json = response.text.replace("```json", "").replace("```", "").strip()
            result = json.loads(cleaned_json)

            if not isinstance(result, dict):
                return None

            return {
                'context_summary': result.get('context_summary'),
                'relations_summary': result.get('relations_summary'),
                'document_keywords': result.get('document_keywords')
            }

        except Exception as e:
            LOGGER.error(f"Semantic regeneration parse failed: {e}")
            return None
