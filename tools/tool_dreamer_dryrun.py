#!/usr/bin/env python3
"""
Dreamer Dry-Run Tool
====================
Kör Dreamer-logiken utan att skriva till grafen.
Loggar alla beslut till fil för analys.

Användning:
    python tools/tool_dreamer_dryrun.py
    python tools/tool_dreamer_dryrun.py --limit 10
    python tools/tool_dreamer_dryrun.py --node-id <uuid>
"""

import argparse
import json
import os
import sys
import yaml
from datetime import datetime
from typing import Dict, List, Any

# Lägg till projekt-root i path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.utils.graph_service import GraphService
from services.utils.vector_service import VectorService
from services.utils.llm_service import LLMService, TaskType
from services.utils.schema_validator import SchemaValidator


def load_config() -> dict:
    """Ladda konfiguration."""
    config_path = os.path.join(
        os.path.dirname(__file__), '..', 'config', 'my_mem_config.yaml'
    )
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def load_prompts() -> dict:
    """Ladda prompts från både dreamer och entity_resolver sektioner."""
    prompts_path = os.path.join(
        os.path.dirname(__file__), '..', 'config', 'services_prompts.yaml'
    )
    with open(prompts_path, 'r') as f:
        data = yaml.safe_load(f)
    # Kombinera prompts från båda sektioner
    prompts = {}
    prompts.update(data.get('dreamer', {}))
    prompts.update(data.get('entity_resolver', {}))
    return prompts


def format_node_context(context_list: List[Dict], max_items: int = 40) -> str:
    """Formatera node_context för logg."""
    if not context_list:
        return "  (ingen kontext)"

    lines = []
    for i, ctx in enumerate(context_list[:max_items]):
        text = ctx.get("text", "Inget innehåll")
        origin = ctx.get("origin", "Okänd källa")
        lines.append(f"  [{i}] {text}")
        lines.append(f"      Källa: {origin}")

    if len(context_list) > max_items:
        lines.append(f"  ... (+{len(context_list) - max_items} fler)")

    return "\n".join(lines)


def is_weak_name(name: str) -> bool:
    """Identifierar UUIDs eller generiska placeholders."""
    import re
    patterns = [
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
        r'^Talare \d+$',
        r'^Unknown$',
        r'^Unit_.*$'
    ]
    return any(re.match(p, name, re.I) for p in patterns)


def prepare_node_for_llm(node: Dict) -> Dict:
    """Tvätta noden från tekniskt metadata innan den skickas till LLM."""
    if not node:
        return {}

    clean_node = {
        "type": node.get("type"),
        "aliases": node.get("aliases", []),
        "properties": node.get("properties", {}).copy()
    }

    props = clean_node["properties"]
    keys_to_remove = ["status", "confidence", "created_at", "last_seen_at", "last_synced_at", "source_system"]
    for key in keys_to_remove:
        props.pop(key, None)

    return clean_node


def get_log_path(config: dict) -> str:
    """Hämta loggkatalog från config och returnera dryrun-loggfilens sökväg."""
    log_file = config.get('logging', {}).get('log_file_path', '~/MyMemory/Logs/my_mem_system.log')
    log_dir = os.path.dirname(os.path.expanduser(log_file))
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, 'dreamer_dryrun.log')


class DreamerDryRun:
    """Dry-run av Dreamers beslutsmekanism."""

    def __init__(self):
        self.config = load_config()
        self.prompts = load_prompts()
        self.dreamer_config = self.config.get('dreamer', {})
        self.thresholds = self.dreamer_config.get('thresholds', {})

        # Ladda tröskelvärden
        self.THRESHOLD_DELETE = self.thresholds.get('delete', 0.95)
        self.THRESHOLD_SPLIT = self.thresholds.get('split', 0.90)
        self.THRESHOLD_RENAME_NORMAL = self.thresholds.get('rename_normal', 0.95)
        self.THRESHOLD_RENAME_WEAK = self.thresholds.get('rename_weak', 0.70)
        self.THRESHOLD_RECATEGORIZE = self.thresholds.get('recategorize', 0.90)
        self.THRESHOLD_MERGE = self.thresholds.get('merge', 0.90)

        # Initiera tjänster
        graph_path = os.path.expanduser(self.config['paths']['graph_db'])

        self.graph_store = GraphService(graph_path)
        self.vector_service = VectorService()  # Läser config internt
        self.llm_service = LLMService()

        # Ladda schemat för nodtyp-beskrivningar
        self.schema = self._load_schema()

        # SchemaValidator för kant-validering
        self.schema_validator = SchemaValidator()

        # Loggfil - append mode
        self.log_path = get_log_path(self.config)
        self.log_file = open(self.log_path, 'a', encoding='utf-8')

    def log(self, text: str):
        """Skriv till loggfil."""
        self.log_file.write(text + "\n")
        self.log_file.flush()

    def close(self):
        """Stäng loggfil."""
        self.log_file.close()

    def _load_schema(self) -> Dict:
        """Ladda graf-schemat för nodtyp-beskrivningar."""
        schema_path = os.path.join(
            os.path.dirname(__file__), '..', 'config', 'graph_schema_template.json'
        )
        try:
            with open(schema_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"VARNING: Kunde inte ladda schema: {e}")
            return {"nodes": {}}

    def _get_node_type_description(self, node_type: str) -> str:
        """Hämta beskrivningen för en nodtyp från schemat."""
        node_def = self.schema.get("nodes", {}).get(node_type, {})
        description = node_def.get("description", "")
        if not description:
            return f"(Ingen beskrivning tillgänglig för '{node_type}')"
        return description

    def _get_node_type(self, node_id: str) -> str:
        """Hämta typ för en nod från grafen."""
        node = self.graph_store.get_node(node_id)
        if node:
            return node.get("type", "Unknown")
        return "Unknown"

    def _log_edge_validation(self, node_id: str, old_type: str, new_type: str):
        """Logga schema-validering av kanter vid hypotetiskt typbyte."""
        self.log("")
        self.log("  KANT-VALIDERING (SchemaValidator):")
        self.log(f"    Hypotetiskt typbyte: {old_type} -> {new_type}")

        # Hämta alla kanter
        edges_out = self.graph_store.get_edges_from(node_id)
        edges_in = self.graph_store.get_edges_to(node_id)
        all_edges = edges_out + edges_in

        if not all_edges:
            self.log("    (Inga kanter att validera)")
            return

        valid_count = 0
        invalid_count = 0

        for edge in all_edges:
            # Bygg nodes_map med den NYA typen för denna nod
            source_type = new_type if edge["source"] == node_id else self._get_node_type(edge["source"])
            target_type = new_type if edge["target"] == node_id else self._get_node_type(edge["target"])

            nodes_map = {edge["source"]: source_type, edge["target"]: target_type}
            ok, msg = self.schema_validator.validate_edge(edge, nodes_map)

            edge_str = f"{edge['source']}-[{edge['type']}]->{edge['target']}"
            if ok:
                self.log(f"    OK {edge_str}")
                valid_count += 1
            else:
                self.log(f"    OGILTIG {edge_str}: {msg}")
                invalid_count += 1

        self.log(f"    Sammanfattning: {valid_count} giltiga, {invalid_count} ogiltiga kanter")

        if invalid_count > 0:
            self.log(f"    VARNING: {invalid_count} kanter skulle bli ogiltiga vid typbyte!")

    def _log_prune_simulation(self, source_node: Dict, target_node: Dict):
        """Simulera context-pruning efter merge och logga resultatet."""
        source_context = source_node.get("properties", {}).get("node_context", [])
        target_context = target_node.get("properties", {}).get("node_context", [])

        # Simulera merge: kombinera och deduplicera
        combined = target_context + source_context
        seen = set()
        unique_list = []
        for item in combined:
            if isinstance(item, dict):
                try:
                    item_key = tuple(sorted((k, str(v)) for k, v in item.items()))
                    if item_key not in seen:
                        seen.add(item_key)
                        unique_list.append(item)
                except Exception:
                    unique_list.append(item)

        self.log("")
        self.log("  CONTEXT-PRUNING SIMULERING:")
        self.log(f"    Source entries:   {len(source_context)}")
        self.log(f"    Target entries:   {len(target_context)}")
        self.log(f"    Efter merge:      {len(unique_list)} (efter dedup)")

        # Prune triggas vid 15+ entries
        if len(unique_list) < 15:
            self.log(f"    Pruning:          EJ NÖDVÄNDIG (< 15 entries)")
            return

        self.log(f"    Pruning:          SKULLE TRIGGAS (>= 15 entries)")

        # Anropa LLM med pruning-prompt
        prompt_template = self.prompts.get("dreamer", {}).get("context_pruning_prompt", "")
        if not prompt_template:
            self.log("    VARNING: context_pruning_prompt saknas i config")
            return

        ctx_texts = [c.get('text', '') for c in unique_list if isinstance(c, dict)]
        prompt = prompt_template.format(keywords=json.dumps(ctx_texts, ensure_ascii=False))

        response = self.llm_service.generate(prompt, TaskType.ENTITY_RESOLUTION)
        if not response.success:
            self.log(f"    LLM FEL: {response.error}")
            return

        try:
            cleaned_text = response.text.replace("```json", "").replace("```", "").strip()
            import re
            try:
                result = json.loads(cleaned_text)
            except json.JSONDecodeError:
                match = re.search(r'\{.*\}', cleaned_text, re.DOTALL)
                if match:
                    result = json.loads(match.group(0))
                else:
                    raise

            if isinstance(result, dict) and "pruned_keywords" in result:
                pruned = result["pruned_keywords"]
                self.log(f"    LLM föreslår:     {len(pruned)} entries (från {len(unique_list)})")
                self.log(f"    Reducering:       {len(unique_list) - len(pruned)} entries tas bort")

                # Visa vilka som behålls
                self.log("    Behålls:")
                for text in pruned[:5]:
                    self.log(f"      - {text[:60]}...")
                if len(pruned) > 5:
                    self.log(f"      ... och {len(pruned) - 5} till")
            else:
                self.log(f"    LLM svar (oväntat format): {result}")

        except Exception as e:
            self.log(f"    Parse-fel: {e}")

    def run(self, limit: int = None, node_id: str = None):
        """Kör dry-run."""
        self.log("=" * 80)
        self.log(f"DREAMER DRY-RUN")
        self.log(f"Starttid: {datetime.now().isoformat()}")
        self.log("=" * 80)
        self.log("")

        # Visa tröskelvärden
        self.log("KONFIGURERADE TRÖSKELVÄRDEN:")
        self.log(f"  DELETE:           {self.THRESHOLD_DELETE}")
        self.log(f"  SPLIT:            {self.THRESHOLD_SPLIT}")
        self.log(f"  RENAME (normal):  {self.THRESHOLD_RENAME_NORMAL}")
        self.log(f"  RENAME (svagt):   {self.THRESHOLD_RENAME_WEAK}")
        self.log(f"  RECATEGORIZE:     {self.THRESHOLD_RECATEGORIZE}")
        self.log(f"  MERGE:            {self.THRESHOLD_MERGE}")
        self.log("")
        self.log("=" * 80)

        # Hämta kandidater
        if node_id:
            node = self.graph_store.get_node(node_id)
            if not node:
                self.log(f"ERROR: Nod {node_id} hittades inte")
                return
            candidates = [node]
        else:
            candidate_limit = limit or self.dreamer_config.get('candidate_limit', 50)
            candidates = self.graph_store.get_refinement_candidates(limit=candidate_limit)

        self.log(f"\nANTAL KANDIDATER: {len(candidates)}")
        self.log("")

        total = len(candidates)

        # === FAS 1: BATCH STRUKTURELL ANALYS ===
        print(f"[Fas 1/{3}] Strukturell analys för {total} kandidater (batch)...")
        structural_prompts = []
        structural_meta = []  # Håller koll på node + context per prompt

        for node in candidates:
            context_list = node.get("properties", {}).get("node_context", [])
            prompt = self._build_structural_prompt(node, context_list)
            if prompt:
                structural_prompts.append(prompt)
                structural_meta.append({"node": node, "context_list": context_list})
            else:
                structural_meta.append({"node": node, "context_list": [], "skip": True})

        # Kör batch
        total_prompts = len(structural_prompts)
        batch_size = self.llm_service.max_parallel
        num_batches = (total_prompts + batch_size - 1) // batch_size
        print(f"  Kör {total_prompts} LLM-anrop i ~{num_batches} batches (max {batch_size} parallellt)...")

        structural_responses = self.llm_service.batch_generate(
            structural_prompts, TaskType.STRUCTURAL_ANALYSIS, parallel=True
        )
        print(f"  ✓ {len(structural_responses)} svar mottagna")

        # Parsa resultat
        structural_results = []
        resp_idx = 0
        for meta in structural_meta:
            if meta.get("skip"):
                structural_results.append({"action": "KEEP", "confidence": 1.0, "reason": "Ingen kontext"})
            else:
                structural_results.append(self._parse_structural_response(structural_responses[resp_idx]))
                resp_idx += 1

        print(f"[Fas 1/{3}] Klar!")

        # === FAS 2: SAMLA ALLA MERGE-PAR ===
        print(f"[Fas 2/{3}] Samlar merge-kandidater...")

        merge_pairs = []  # Lista med (candidate_idx, node, match)
        for i, node in enumerate(candidates):
            structural = structural_results[i]
            action = structural.get("action", "KEEP")

            # Skippa noder som ska DELETE/SPLIT
            if action in ["DELETE", "SPLIT"]:
                continue

            matches = self.find_potential_matches(node)
            for match in matches:
                merge_pairs.append((i, node, match))

        print(f"  Hittade {len(merge_pairs)} merge-par att utvärdera")

        # === FAS 3: BATCH MERGE-UTVÄRDERING ===
        print(f"[Fas 3/{3}] Batch merge-utvärdering ({len(merge_pairs)} par)...")

        merge_prompts = []
        for _, node, match in merge_pairs:
            prompt = self._build_merge_prompt(node, match)
            if prompt:
                merge_prompts.append(prompt)

        if merge_prompts:
            # Progress-indikator
            total_prompts = len(merge_prompts)
            batch_size = self.llm_service.max_parallel
            num_batches = (total_prompts + batch_size - 1) // batch_size
            print(f"  Kör {total_prompts} LLM-anrop i ~{num_batches} batches (max {batch_size} parallellt)...")

            merge_responses = self.llm_service.batch_generate(
                merge_prompts, TaskType.ENTITY_RESOLUTION, parallel=True
            )
            print(f"  ✓ {len(merge_responses)} svar mottagna")
        else:
            merge_responses = []

        # Mappa responses tillbaka till merge_pairs
        merge_results = {}  # {(node_id, match_id): result}
        resp_idx = 0
        for _, node, match in merge_pairs:
            if resp_idx < len(merge_responses):
                result = self._parse_merge_response(merge_responses[resp_idx])
                merge_results[(node["id"], match["id"])] = result
                resp_idx += 1

        print(f"[Fas 3/{3}] Klar!")

        # === LOGGA RESULTAT ===
        print(f"Loggar resultat...")

        for i, node in enumerate(candidates, 1):
            name = node.get("properties", {}).get("name", node.get("id", "?"))
            print(f"  [{i}/{total}] {name}")
            self._log_candidate_analysis(i, node, structural_results[i-1], merge_results)

        self.log("")
        self.log("=" * 80)
        self.log(f"DRY-RUN KLAR: {datetime.now().isoformat()}")
        self.log(f"Loggen sparad: {self.log_path}")
        self.log("=" * 80)

    def analyze_candidate(self, num: int, node: Dict):
        """Analysera en kandidat."""
        node_id = node.get("id", "?")
        node_type = node.get("type", "?")
        props = node.get("properties", {})
        name = props.get("name", node_id)
        status = props.get("status", "?")
        confidence = props.get("confidence", 0.0)

        self.log("")
        self.log("=" * 80)
        self.log(f"[{num}] KANDIDAT: {name}")
        self.log("=" * 80)

        # Grundläggande info
        self.log("")
        self.log("NOD-INFO:")
        self.log(f"  ID:          {node_id}")
        self.log(f"  Typ:         {node_type}")
        self.log(f"  Status:      {status}")
        self.log(f"  Confidence:  {confidence}")

        # Grad i grafen
        degree = self.graph_store.get_node_degree(node_id)
        self.log(f"  Grad:        {degree} (antal relationer)")

        # Svagt namn?
        weak = is_weak_name(name)
        self.log(f"  Svagt namn:  {'JA' if weak else 'NEJ'}")

        # Aliases
        aliases = node.get("aliases", [])
        if aliases:
            self.log(f"  Aliases:     {', '.join(aliases)}")

        # Node context (bevisen)
        self.log("")
        self.log("NODE CONTEXT (beviskedja):")
        context_list = props.get("node_context", [])
        self.log(format_node_context(context_list))

        # === STEG 1: STRUKTURELL ANALYS ===
        self.log("")
        self.log("-" * 40)
        self.log("STRUKTURELL ANALYS (LLM)")
        self.log("-" * 40)

        structural_result = self.do_structural_analysis(node, context_list)

        if structural_result:
            action = structural_result.get("action", "KEEP")
            conf = structural_result.get("confidence", 0.0)
            reason = structural_result.get("reason", "")

            self.log("")
            self.log("LLM SVAR (rått):")
            self.log(f"  {json.dumps(structural_result, ensure_ascii=False, indent=2)}")

            self.log("")
            self.log("PARSAD DOM:")
            self.log(f"  Action:      {action}")
            self.log(f"  Confidence:  {conf}")
            self.log(f"  Reason:      {reason}")

            if action == "RENAME":
                self.log(f"  Nytt namn:   {structural_result.get('new_name', '?')}")
            elif action == "RE-CATEGORIZE":
                self.log(f"  Ny typ:      {structural_result.get('new_type', '?')}")
            elif action == "SPLIT":
                clusters = structural_result.get("split_clusters", [])
                self.log(f"  Kluster:     {json.dumps(clusters, ensure_ascii=False)}")

            # Heuristiska spärrar
            self.log("")
            self.log("HEURISTISKA SPÄRRAR:")
            final_action = action

            if action == "DELETE":
                if degree > 0:
                    self.log(f"  ❌ BLOCKERAD: Noden har {degree} relationer (får ej raderas)")
                    final_action = "KEEP"
                elif conf < self.THRESHOLD_DELETE:
                    self.log(f"  ❌ BLOCKERAD: Confidence {conf} < {self.THRESHOLD_DELETE} (DELETE-tröskel)")
                    final_action = "KEEP"
                else:
                    self.log(f"  ✓ PASSERAR: Isolerad nod, confidence {conf} >= {self.THRESHOLD_DELETE}")

            elif action == "RENAME":
                target_threshold = self.THRESHOLD_RENAME_WEAK if weak else self.THRESHOLD_RENAME_NORMAL
                if conf < target_threshold:
                    self.log(f"  ❌ BLOCKERAD: Confidence {conf} < {target_threshold} ({'svagt' if weak else 'normalt'} namn)")
                    final_action = "KEEP"
                else:
                    self.log(f"  ✓ PASSERAR: Confidence {conf} >= {target_threshold}")

            elif action == "RE-CATEGORIZE":
                if conf < self.THRESHOLD_RECATEGORIZE:
                    self.log(f"  ❌ BLOCKERAD: Confidence {conf} < {self.THRESHOLD_RECATEGORIZE}")
                    final_action = "KEEP"
                else:
                    self.log(f"  ✓ PASSERAR: Confidence {conf} >= {self.THRESHOLD_RECATEGORIZE}")

            elif action == "SPLIT":
                if conf < self.THRESHOLD_SPLIT:
                    self.log(f"  ❌ BLOCKERAD: Confidence {conf} < {self.THRESHOLD_SPLIT}")
                    final_action = "KEEP"
                else:
                    self.log(f"  ✓ PASSERAR: Confidence {conf} >= {self.THRESHOLD_SPLIT}")

            elif action == "KEEP":
                self.log(f"  ✓ KEEP: Ingen åtgärd krävs")

            self.log("")
            self.log("TRÖSKELKONTROLL:")
            self.log(f"  Ursprunglig action: {action}")
            self.log(f"  Slutlig action:     {final_action}")

            # Om DELETE eller SPLIT, skippa merge-analys
            if final_action in ["DELETE", "SPLIT"]:
                self.log("")
                self.log("(Skippar merge-analys - noden ska raderas/splittas)")
                return

        # === STEG 2: MERGE-ANALYS ===
        self.log("")
        self.log("-" * 40)
        self.log("MERGE-ANALYS (Identity Resolution)")
        self.log("-" * 40)

        # Hitta potentiella dubbletter
        matches = self.find_potential_matches(node)

        if not matches:
            self.log("  Inga potentiella dubbletter hittades via semantisk sökning.")
            return

        self.log(f"  Hittade {len(matches)} potentiella matcher")

        for j, match in enumerate(matches, 1):
            match_id = match.get("id", "?")
            match_name = match.get("properties", {}).get("name", match_id)

            self.log("")
            self.log(f"  --- Match #{j}: {match_name} ---")
            self.log(f"  Match ID: {match_id}")

            # Match node context
            match_context = match.get("properties", {}).get("node_context", [])
            self.log(f"  Match kontext:")
            self.log(format_node_context(match_context, max_items=10))

            # LLM-bedömning
            merge_result = self.do_merge_evaluation(node, match)

            if merge_result:
                decision = merge_result.get("decision", "IGNORE")
                conf = merge_result.get("confidence", 0.0)
                reason = merge_result.get("reason", "")

                self.log("")
                self.log("  LLM SVAR (rått):")
                self.log(f"    {json.dumps(merge_result, ensure_ascii=False, indent=2)}")

                self.log("")
                self.log("  PARSAD DOM:")
                self.log(f"    Decision:    {decision}")
                self.log(f"    Confidence:  {conf}")
                self.log(f"    Reason:      {reason}")

                self.log("")
                self.log("  TRÖSKELKONTROLL:")
                if decision == "MERGE" and conf >= self.THRESHOLD_MERGE:
                    self.log(f"    ✓ MERGE GODKÄND: {conf} >= {self.THRESHOLD_MERGE}")
                    self.log(f"    → {name} skulle slås ihop med {match_name}")
                    # Vid godkänd merge, avbryt matchning
                    break
                elif decision == "MERGE":
                    self.log(f"    ❌ MERGE BLOCKERAD: {conf} < {self.THRESHOLD_MERGE}")
                else:
                    self.log(f"    ✗ IGNORE: Ingen åtgärd")

    def _build_structural_prompt(self, node: Dict, context_list: List[Dict]) -> str:
        """Bygg prompt för strukturell analys (returnerar None om ej möjligt)."""
        if not context_list:
            return None

        formatted_context = ""
        for i, ctx in enumerate(context_list[:40]):
            text = ctx.get("text", "Inget innehåll")
            origin = ctx.get("origin", "Okänd källa")
            formatted_context += f"[{i}] {text} (Källa: {origin})\n"

        prompt_template = self.prompts.get("structural_analysis", "")
        if not prompt_template:
            return None

        node_type = node.get("type", "Unknown")
        return prompt_template.format(
            id=node.get("id"),
            type=node_type,
            node_type_description=self._get_node_type_description(node_type),
            context_list=formatted_context,
            taxonomy_nodes="Person, Project, Organization, Group, Event, Roles, Business_relation"
        )

    def _parse_structural_response(self, response) -> Dict:
        """Parsa LLM-svar till strukturellt resultat."""
        if not response.success:
            return {"action": "KEEP", "confidence": 0.0, "reason": f"LLM-fel: {response.error}"}

        try:
            cleaned_json = response.text.replace("```json", "").replace("```", "").strip()
            result = json.loads(cleaned_json)

            if "action" not in result:
                result["action"] = "KEEP"
            if "confidence" not in result:
                result["confidence"] = 0.0

            return result
        except Exception as e:
            return {"action": "KEEP", "confidence": 0.0, "reason": f"Parse-fel: {e}"}

    def analyze_candidate_with_result(self, num: int, node: Dict, structural_result: Dict):
        """Analysera kandidat med förberäknat strukturellt resultat."""
        node_id = node.get("id", "?")
        node_type = node.get("type", "?")
        props = node.get("properties", {})
        name = props.get("name", node_id)
        status = props.get("status", "?")
        confidence = props.get("confidence", 0.0)
        context_list = props.get("node_context", [])

        self.log("")
        self.log("=" * 80)
        self.log(f"[{num}] KANDIDAT: {name}")
        self.log("=" * 80)

        # Grundläggande info
        self.log("")
        self.log("NOD-INFO:")
        self.log(f"  ID:          {node_id}")
        self.log(f"  Typ:         {node_type}")
        self.log(f"  Status:      {status}")
        self.log(f"  Confidence:  {confidence}")

        degree = self.graph_store.get_node_degree(node_id)
        self.log(f"  Grad:        {degree} (antal relationer)")

        weak = is_weak_name(name)
        self.log(f"  Svagt namn:  {'JA' if weak else 'NEJ'}")

        aliases = node.get("aliases", [])
        if aliases:
            self.log(f"  Aliases:     {', '.join(aliases)}")

        self.log("")
        self.log("NODE CONTEXT (beviskedja):")
        self.log(format_node_context(context_list))

        # Strukturell analys (redan beräknad)
        self.log("")
        self.log("-" * 40)
        self.log("STRUKTURELL ANALYS (LLM)")
        self.log("-" * 40)

        action = structural_result.get("action", "KEEP")
        conf = structural_result.get("confidence", 0.0)
        reason = structural_result.get("reason", "")

        self.log("")
        self.log("LLM SVAR (rått):")
        self.log(f"  {json.dumps(structural_result, ensure_ascii=False, indent=2)}")

        self.log("")
        self.log("PARSAD DOM:")
        self.log(f"  Action:      {action}")
        self.log(f"  Confidence:  {conf}")
        self.log(f"  Reason:      {reason}")

        if action == "RENAME":
            self.log(f"  Nytt namn:   {structural_result.get('new_name', '?')}")
        elif action == "RE-CATEGORIZE":
            self.log(f"  Ny typ:      {structural_result.get('new_type', '?')}")
        elif action == "SPLIT":
            clusters = structural_result.get("split_clusters", [])
            self.log(f"  Kluster:     {json.dumps(clusters, ensure_ascii=False)}")

        # Heuristiska spärrar
        self.log("")
        self.log("HEURISTISKA SPÄRRAR:")
        final_action = action

        if action == "DELETE":
            if degree > 0:
                self.log(f"  ❌ BLOCKERAD: Noden har {degree} relationer (får ej raderas)")
                final_action = "KEEP"
            elif conf < self.THRESHOLD_DELETE:
                self.log(f"  ❌ BLOCKERAD: Confidence {conf} < {self.THRESHOLD_DELETE} (DELETE-tröskel)")
                final_action = "KEEP"
            else:
                self.log(f"  ✓ PASSERAR: Isolerad nod, confidence {conf} >= {self.THRESHOLD_DELETE}")

        elif action == "RENAME":
            target_threshold = self.THRESHOLD_RENAME_WEAK if weak else self.THRESHOLD_RENAME_NORMAL
            if conf < target_threshold:
                self.log(f"  ❌ BLOCKERAD: Confidence {conf} < {target_threshold} ({'svagt' if weak else 'normalt'} namn)")
                final_action = "KEEP"
            else:
                self.log(f"  ✓ PASSERAR: Confidence {conf} >= {target_threshold}")

        elif action == "RE-CATEGORIZE":
            if conf < self.THRESHOLD_RECATEGORIZE:
                self.log(f"  ❌ BLOCKERAD: Confidence {conf} < {self.THRESHOLD_RECATEGORIZE}")
                final_action = "KEEP"
            else:
                self.log(f"  ✓ PASSERAR: Confidence {conf} >= {self.THRESHOLD_RECATEGORIZE}")
                # Validera kanter med nya typen
                new_type = structural_result.get("new_type")
                if new_type:
                    self._log_edge_validation(node_id, node_type, new_type)

        elif action == "SPLIT":
            if conf < self.THRESHOLD_SPLIT:
                self.log(f"  ❌ BLOCKERAD: Confidence {conf} < {self.THRESHOLD_SPLIT}")
                final_action = "KEEP"
            else:
                self.log(f"  ✓ PASSERAR: Confidence {conf} >= {self.THRESHOLD_SPLIT}")

        elif action == "KEEP":
            self.log(f"  ✓ KEEP: Ingen åtgärd krävs")

        self.log("")
        self.log("TRÖSKELKONTROLL:")
        self.log(f"  Ursprunglig action: {action}")
        self.log(f"  Slutlig action:     {final_action}")

        # Om DELETE eller SPLIT, skippa merge-analys
        if final_action in ["DELETE", "SPLIT"]:
            self.log("")
            self.log("(Skippar merge-analys - noden ska raderas/splittas)")
            return

        # Merge-analys
        self._do_merge_analysis(node, name)

    def _do_merge_analysis(self, node: Dict, name: str):
        """Kör merge-analys för en nod."""
        self.log("")
        self.log("-" * 40)
        self.log("MERGE-ANALYS (Identity Resolution)")
        self.log("-" * 40)

        matches = self.find_potential_matches(node)

        if not matches:
            self.log("  Inga potentiella dubbletter hittades via semantisk sökning.")
            return

        self.log(f"  Hittade {len(matches)} potentiella matcher")

        # Batch merge-utvärdering
        merge_prompts = []
        for match in matches:
            prompt = self._build_merge_prompt(node, match)
            if prompt:
                merge_prompts.append(prompt)

        if merge_prompts:
            merge_responses = self.llm_service.batch_generate(
                merge_prompts, TaskType.ENTITY_RESOLUTION, parallel=True
            )
        else:
            merge_responses = []

        # Logga resultat
        for j, (match, response) in enumerate(zip(matches, merge_responses), 1):
            match_id = match.get("id", "?")
            match_name = match.get("properties", {}).get("name", match_id)

            self.log("")
            self.log(f"  --- Match #{j}: {match_name} ---")
            self.log(f"  Match ID: {match_id}")

            match_context = match.get("properties", {}).get("node_context", [])
            self.log(f"  Match kontext:")
            self.log(format_node_context(match_context, max_items=10))

            merge_result = self._parse_merge_response(response)

            decision = merge_result.get("decision", "IGNORE")
            conf = merge_result.get("confidence", 0.0)
            reason = merge_result.get("reason", "")

            self.log("")
            self.log("  LLM SVAR (rått):")
            self.log(f"    {json.dumps(merge_result, ensure_ascii=False, indent=2)}")

            self.log("")
            self.log("  PARSAD DOM:")
            self.log(f"    Decision:    {decision}")
            self.log(f"    Confidence:  {conf}")
            self.log(f"    Reason:      {reason}")

            self.log("")
            self.log("  TRÖSKELKONTROLL:")
            if decision == "MERGE" and conf >= self.THRESHOLD_MERGE:
                self.log(f"    ✓ MERGE GODKÄND: {conf} >= {self.THRESHOLD_MERGE}")
                self.log(f"    → {name} skulle slås ihop med {match_name}")
                # Simulera context-pruning efter merge
                self._log_prune_simulation(node, match)
                break
            elif decision == "MERGE":
                self.log(f"    ❌ MERGE BLOCKERAD: {conf} < {self.THRESHOLD_MERGE}")
            else:
                self.log(f"    ✗ IGNORE: Ingen åtgärd")

    def _log_candidate_analysis(self, num: int, node: Dict, structural_result: Dict, merge_results: Dict):
        """Logga kandidatanalys med förberäknade resultat."""
        node_id = node.get("id", "?")
        node_type = node.get("type", "?")
        props = node.get("properties", {})
        name = props.get("name", node_id)
        status = props.get("status", "?")
        confidence = props.get("confidence", 0.0)
        context_list = props.get("node_context", [])

        self.log("")
        self.log("=" * 80)
        self.log(f"[{num}] KANDIDAT: {name}")
        self.log("=" * 80)

        # Grundläggande info
        self.log("")
        self.log("NOD-INFO:")
        self.log(f"  ID:          {node_id}")
        self.log(f"  Typ:         {node_type}")
        self.log(f"  Status:      {status}")
        self.log(f"  Confidence:  {confidence}")

        degree = self.graph_store.get_node_degree(node_id)
        self.log(f"  Grad:        {degree} (antal relationer)")

        weak = is_weak_name(name)
        self.log(f"  Svagt namn:  {'JA' if weak else 'NEJ'}")

        aliases = node.get("aliases", [])
        if aliases:
            self.log(f"  Aliases:     {', '.join(aliases)}")

        self.log("")
        self.log("NODE CONTEXT (beviskedja):")
        self.log(format_node_context(context_list))

        # Strukturell analys
        self.log("")
        self.log("-" * 40)
        self.log("STRUKTURELL ANALYS (LLM)")
        self.log("-" * 40)

        action = structural_result.get("action", "KEEP")
        conf = structural_result.get("confidence", 0.0)
        reason = structural_result.get("reason", "")

        self.log("")
        self.log("LLM SVAR (rått):")
        self.log(f"  {json.dumps(structural_result, ensure_ascii=False, indent=2)}")

        self.log("")
        self.log("PARSAD DOM:")
        self.log(f"  Action:      {action}")
        self.log(f"  Confidence:  {conf}")
        self.log(f"  Reason:      {reason}")

        if action == "RENAME":
            self.log(f"  Nytt namn:   {structural_result.get('new_name', '?')}")
        elif action == "RE-CATEGORIZE":
            self.log(f"  Ny typ:      {structural_result.get('new_type', '?')}")
        elif action == "SPLIT":
            clusters = structural_result.get("split_clusters", [])
            self.log(f"  Kluster:     {json.dumps(clusters, ensure_ascii=False)}")

        # Heuristiska spärrar
        self.log("")
        self.log("HEURISTISKA SPÄRRAR:")
        final_action = action

        if action == "DELETE":
            if degree > 0:
                self.log(f"  ❌ BLOCKERAD: Noden har {degree} relationer (får ej raderas)")
                final_action = "KEEP"
            elif conf < self.THRESHOLD_DELETE:
                self.log(f"  ❌ BLOCKERAD: Confidence {conf} < {self.THRESHOLD_DELETE} (DELETE-tröskel)")
                final_action = "KEEP"
            else:
                self.log(f"  ✓ PASSERAR: Isolerad nod, confidence {conf} >= {self.THRESHOLD_DELETE}")

        elif action == "RENAME":
            target_threshold = self.THRESHOLD_RENAME_WEAK if weak else self.THRESHOLD_RENAME_NORMAL
            if conf < target_threshold:
                self.log(f"  ❌ BLOCKERAD: Confidence {conf} < {target_threshold} ({'svagt' if weak else 'normalt'} namn)")
                final_action = "KEEP"
            else:
                self.log(f"  ✓ PASSERAR: Confidence {conf} >= {target_threshold}")

        elif action == "RE-CATEGORIZE":
            if conf < self.THRESHOLD_RECATEGORIZE:
                self.log(f"  ❌ BLOCKERAD: Confidence {conf} < {self.THRESHOLD_RECATEGORIZE}")
                final_action = "KEEP"
            else:
                self.log(f"  ✓ PASSERAR: Confidence {conf} >= {self.THRESHOLD_RECATEGORIZE}")
                # Validera kanter med nya typen
                new_type = structural_result.get("new_type")
                if new_type:
                    self._log_edge_validation(node_id, node_type, new_type)

        elif action == "SPLIT":
            if conf < self.THRESHOLD_SPLIT:
                self.log(f"  ❌ BLOCKERAD: Confidence {conf} < {self.THRESHOLD_SPLIT}")
                final_action = "KEEP"
            else:
                self.log(f"  ✓ PASSERAR: Confidence {conf} >= {self.THRESHOLD_SPLIT}")

        elif action == "KEEP":
            self.log(f"  ✓ KEEP: Ingen åtgärd krävs")

        self.log("")
        self.log("TRÖSKELKONTROLL:")
        self.log(f"  Ursprunglig action: {action}")
        self.log(f"  Slutlig action:     {final_action}")

        # Om DELETE eller SPLIT, skippa merge-analys
        if final_action in ["DELETE", "SPLIT"]:
            self.log("")
            self.log("(Skippar merge-analys - noden ska raderas/splittas)")
            return

        # Merge-analys med förberäknade resultat
        self.log("")
        self.log("-" * 40)
        self.log("MERGE-ANALYS (Identity Resolution)")
        self.log("-" * 40)

        # Hitta merge-resultat för denna nod
        node_merges = [(match_id, result) for (nid, match_id), result in merge_results.items() if nid == node_id]

        if not node_merges:
            self.log("  Inga potentiella dubbletter hittades via semantisk sökning.")
            return

        self.log(f"  Hittade {len(node_merges)} potentiella matcher")

        for j, (match_id, merge_result) in enumerate(node_merges, 1):
            match_node = self.graph_store.get_node(match_id)
            match_name = match_node.get("properties", {}).get("name", match_id) if match_node else match_id

            self.log("")
            self.log(f"  --- Match #{j}: {match_name} ---")
            self.log(f"  Match ID: {match_id}")

            if match_node:
                match_context = match_node.get("properties", {}).get("node_context", [])
                self.log(f"  Match kontext:")
                self.log(format_node_context(match_context, max_items=10))

            decision = merge_result.get("decision", "IGNORE")
            m_conf = merge_result.get("confidence", 0.0)
            m_reason = merge_result.get("reason", "")

            self.log("")
            self.log("  LLM SVAR (rått):")
            self.log(f"    {json.dumps(merge_result, ensure_ascii=False, indent=2)}")

            self.log("")
            self.log("  PARSAD DOM:")
            self.log(f"    Decision:    {decision}")
            self.log(f"    Confidence:  {m_conf}")
            self.log(f"    Reason:      {m_reason}")

            self.log("")
            self.log("  TRÖSKELKONTROLL:")
            if decision == "MERGE" and m_conf >= self.THRESHOLD_MERGE:
                self.log(f"    ✓ MERGE GODKÄND: {m_conf} >= {self.THRESHOLD_MERGE}")
                self.log(f"    → {name} skulle slås ihop med {match_name}")
                # Simulera context-pruning efter merge
                if match_node:
                    self._log_prune_simulation(node, match_node)
                break
            elif decision == "MERGE":
                self.log(f"    ❌ MERGE BLOCKERAD: {m_conf} < {self.THRESHOLD_MERGE}")
            else:
                self.log(f"    ✗ IGNORE: Ingen åtgärd")

    def _build_merge_prompt(self, primary: Dict, secondary: Dict) -> str:
        """Bygg prompt för merge-utvärdering."""
        prompt_template = self.prompts.get("entity_resolution_prompt", "")
        if not prompt_template:
            return None

        p_clean = prepare_node_for_llm(primary)
        s_clean = prepare_node_for_llm(secondary)

        return prompt_template.format(
            node_a_json=json.dumps(p_clean, indent=2, ensure_ascii=False),
            node_b_json=json.dumps(s_clean, indent=2, ensure_ascii=False)
        )

    def _parse_merge_response(self, response) -> Dict:
        """Parsa LLM-svar till merge-resultat."""
        if not response.success:
            return {"decision": "IGNORE", "confidence": 0.0, "reason": f"LLM-fel: {response.error}"}

        try:
            cleaned_text = response.text.replace("```json", "").replace("```", "").strip()
            result = json.loads(cleaned_text)

            if isinstance(result, list):
                result = result[0] if result else {"decision": "IGNORE", "confidence": 0.0}

            if not isinstance(result, dict):
                return {"decision": "IGNORE", "confidence": 0.0, "reason": "Ogiltigt format"}

            return result
        except Exception as e:
            return {"decision": "IGNORE", "confidence": 0.0, "reason": f"Parse-fel: {e}"}

    def do_structural_analysis(self, node: Dict, context_list: List[Dict]) -> Dict:
        """Kör strukturell analys via LLM (legacy, används av analyze_candidate)."""
        prompt = self._build_structural_prompt(node, context_list)
        if not prompt:
            return {"action": "KEEP", "confidence": 1.0, "reason": "Ingen kontext tillgänglig"}

        response = self.llm_service.generate(prompt, TaskType.STRUCTURAL_ANALYSIS)
        return self._parse_structural_response(response)

    def find_potential_matches(self, node: Dict) -> List[Dict]:
        """Hitta potentiella dubbletter via semantisk sökning."""
        self.vector_service.upsert_node(node)

        name = node.get("properties", {}).get("name", "")
        if not name:
            return []

        search_text = f"{name} {node.get('type')}"
        keywords = node.get("properties", {}).get("context_keywords", [])
        if keywords:
            search_text += " " + " ".join(keywords)

        vector_limit = self.dreamer_config.get('vector_search_limit', 10)
        results = self.vector_service.search(search_text, limit=vector_limit)

        valid_matches = []
        for res in results:
            match_id = res['id']
            if match_id == node["id"]:
                continue

            match_node = self.graph_store.get_node(match_id)
            if not match_node:
                continue

            if match_node["type"] != node["type"]:
                continue

            valid_matches.append(match_node)

        return valid_matches

    def do_merge_evaluation(self, primary: Dict, secondary: Dict) -> Dict:
        """Bedöm om två noder ska slås ihop."""
        prompt_template = self.prompts.get("entity_resolution_prompt", "")
        if not prompt_template:
            return {"decision": "IGNORE", "confidence": 0.0, "reason": "Prompt saknas"}

        p_clean = prepare_node_for_llm(primary)
        s_clean = prepare_node_for_llm(secondary)

        prompt = prompt_template.format(
            node_a_json=json.dumps(p_clean, indent=2, ensure_ascii=False),
            node_b_json=json.dumps(s_clean, indent=2, ensure_ascii=False)
        )

        response = self.llm_service.generate(prompt, TaskType.ENTITY_RESOLUTION)

        if not response.success:
            return {"decision": "IGNORE", "confidence": 0.0, "reason": f"LLM-fel: {response.error}"}

        try:
            cleaned_text = response.text.replace("```json", "").replace("```", "").strip()
            result = json.loads(cleaned_text)

            if isinstance(result, list):
                result = result[0] if result else {"decision": "IGNORE", "confidence": 0.0}

            if not isinstance(result, dict):
                return {"decision": "IGNORE", "confidence": 0.0, "reason": "Ogiltigt format"}

            return result
        except Exception as e:
            return {"decision": "IGNORE", "confidence": 0.0, "reason": f"Parse-fel: {e}"}


def main():
    parser = argparse.ArgumentParser(description="Dreamer Dry-Run - analysera utan att skriva")
    parser.add_argument("--limit", type=int, help="Max antal kandidater")
    parser.add_argument("--node-id", type=str, help="Analysera specifik nod")
    args = parser.parse_args()

    print(f"Startar Dreamer Dry-Run...")

    dryrun = DreamerDryRun()
    print(f"Loggar till: {dryrun.log_path}")
    print()

    try:
        dryrun.run(limit=args.limit, node_id=args.node_id)
    finally:
        dryrun.close()

    print(f"\nKlar! Loggen finns på: {dryrun.log_path}")


if __name__ == "__main__":
    main()
