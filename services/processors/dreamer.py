#!/usr/bin/env python3
"""
MyMem Dreamer (v9.5) - Rebuild Compatible

Nyheter:
- Återställt ReviewObject för kompatibilitet med interactive_review.py.
- consolidate() returnerar nu data (review_list) till orchestratorn.
- Batch-optimering och Aggregerad Konfidens kvarstår.
"""

import os
import sys
import json
import yaml
import logging
import datetime
import zoneinfo
import asyncio
from collections import defaultdict
from typing import List, Dict, Optional
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google import genai
from google.genai import types
from services.utils.graph_service import GraphStore
from services.utils.json_parser import parse_llm_json

# --- CONFIG LOADER ---
def _load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, 'config', 'my_mem_config.yaml'),
    ]
    main_conf = {}
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f: main_conf = yaml.safe_load(f)
            for k, v in main_conf.get('paths', {}).items():
                main_conf['paths'][k] = os.path.expanduser(v)
            
            config_dir = os.path.dirname(p)
            prompts_conf = {}
            for name in ['services_prompts.yaml', 'service_prompts.yaml']:
                pp = os.path.join(config_dir, name)
                if os.path.exists(pp):
                    with open(pp, 'r') as f: prompts_conf = yaml.safe_load(f)
                    break
            
            return main_conf, prompts_conf
    raise FileNotFoundError("HARDFAIL: Config saknas")

CONFIG, PROMPTS_RAW = _load_config()

# Helper Functions
def get_prompt(agent, key):
    if 'prompts' in PROMPTS_RAW:
        return PROMPTS_RAW['prompts'].get(agent, {}).get(key)
    return PROMPTS_RAW.get(agent, {}).get(key)

def get_setting(agent, key, default):
    val = PROMPTS_RAW.get('settings', {}).get(agent, {}).get(key)
    if val is not None: return val
    return default

# Settings
GRAPH_PATH = CONFIG['paths']['graph_db']
LAKE_STORE = CONFIG['paths']['lake_store']
TAXONOMY_FILE = CONFIG['paths']['taxonomy_file']
LOG_FILE = CONFIG['logging']['log_file_path']

API_KEY = CONFIG['ai_engine']['api_key']
MODEL_FAST = CONFIG.get('ai_engine', {}).get('models', {}).get('model_fast')

# Batch Settings
BATCH_SIZE = 15
MAX_CONCURRENT_REQUESTS = 5 
MAX_NODE_LENGTH = get_setting('dreamer', 'max_node_name_length', 60)
MAX_CONTEXT_ITEMS_PER_ENTITY = 3

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - DREAMER - %(levelname)s - %(message)s'
)
LOGGER = logging.getLogger('Dreamer')
AI_CLIENT = genai.Client(api_key=API_KEY)

def _ts():
    return datetime.datetime.now().strftime("[%H:%M:%S]")

# --- DATA STRUCTURES ---

@dataclass
class ReviewObject:
    """Dataclass för entiteter som behöver granskas av användaren (Krävs av interactive_review)."""
    entity_name: str
    master_node: str
    similarity_score: float
    suggested_action: str  # 'APPROVE', 'REVIEW', 'REJECT'
    reason: str
    closest_match: str | None = None

# --- CORE LOGIC CLASS ---
class EvidenceConsolidator:
    def __init__(self):
        self.graph = GraphStore(GRAPH_PATH, read_only=False)
        self.valid_master_nodes = self._load_valid_nodes()

    def _load_valid_nodes(self) -> List[str]:
        if not os.path.exists(TAXONOMY_FILE): return []
        with open(TAXONOMY_FILE, 'r', encoding='utf-8') as f:
            return list(json.load(f).keys())

    def fetch_pending_evidence(self) -> Dict[str, List[Dict]]:
        try:
            results = self.graph.conn.execute("""
                SELECT entity_name, master_node_candidate, context_description, source_file, confidence
                FROM evidence
            """).fetchall()
        except Exception as e:
            LOGGER.error(f"DB Read Error: {e}")
            return {}

        grouped = defaultdict(list)
        for row in results:
            grouped[row[0]].append({
                "master_node": row[1],
                "context": row[2],
                "source_file": row[3],
                "confidence": row[4]
            })
        return grouped

    def _calculate_aggregated_confidence(self, evidence_list: List[Dict]) -> float:
        if not evidence_list: return 0.0
        failure_probability = 1.0
        unique_sources = set()
        for ev in evidence_list:
            src = ev['source_file']
            base_conf = ev.get('confidence', 0.5)
            weight = 0.2 if src in unique_sources else 1.0
            unique_sources.add(src)
            local_risk = 1.0 - (base_conf * weight)
            failure_probability *= local_risk
        return min(1.0 - failure_probability, 0.99)

    def _analyze_batch_sync(self, batch_items: List[tuple]) -> List[Dict]:
        """Analyserar en lista med (entity_name, evidence_list)."""
        raw_prompt = get_prompt('dreamer', 'consolidate_batch')
        if not raw_prompt:
            LOGGER.error("HARDFAIL: Prompt 'consolidate_batch' saknas")
            return []

        candidates_parts = []
        for name, ev_list in batch_items:
            contexts = [f"- [{e['master_node']}] {e['context']}" for e in ev_list[:MAX_CONTEXT_ITEMS_PER_ENTITY]]
            context_str = "\n".join(contexts)
            candidates_parts.append(f"---\nITEM: \"{name}\"\nCONTEXT:\n{context_str}")
        
        candidates_str = "\n".join(candidates_parts)
        valid_nodes_str = ", ".join([f'"{n}"' for n in self.valid_master_nodes])

        prompt = raw_prompt.format(
            candidates_str=candidates_str,
            valid_nodes_str=valid_nodes_str
        )

        try:
            response = AI_CLIENT.models.generate_content(
                model=MODEL_FAST,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            parsed = parse_llm_json(response.text, context="Dreamer_Batch")
            results = parsed.get("results", [])
            
            validated_results = []
            for res in results:
                if res.get('master_node') not in self.valid_master_nodes:
                    continue
                node_id = res.get('suggested_node_id') or res.get('entity')
                if res.get('is_atomic_node') and len(node_id) > MAX_NODE_LENGTH:
                    res['is_atomic_node'] = False
                    res['suggested_node_id'] = None
                validated_results.append(res)
            return validated_results

        except Exception as e:
            LOGGER.error(f"Batch LLM Error: {e}")
            return []

    async def analyze_batch_async(self, batch: List[tuple], semaphore: asyncio.Semaphore) -> List[Dict]:
        async with semaphore:
            return await asyncio.to_thread(self._analyze_batch_sync, batch)

    def commit_to_graph(self, analysis: Dict, source_files: List[str]) -> tuple[bool, str]:
        master_node = analysis['master_node']
        desc = analysis.get('canonical_summary', '')
        
        try:
            self.graph.upsert_node(id=master_node, type="Concept")

            if analysis.get('is_atomic_node'):
                node_id = analysis['suggested_node_id']
                self.graph.upsert_node(
                    id=node_id,
                    type="Entity",
                    aliases=analysis.get('aliases', []),
                    properties={
                        "entity_type": master_node,
                        "description": desc,
                        "last_consolidated": datetime.datetime.now().isoformat()
                    }
                )
                msg = f"🟢 Nod: {node_id} ({master_node})"
                target = node_id
                edge = "UNIT_MENTIONS"
            else:
                msg = f"🔵 Tema: Kopplat till {master_node}"
                target = master_node
                edge = "DEALS_WITH"

            for src in list(set(source_files)):
                self.graph.upsert_edge(
                    source=src, target=target, edge_type=edge, 
                    properties={"context": desc}
                )
            return True, msg

        except Exception as e:
            LOGGER.error(f"Graph Write Error: {e}")
            return False, str(e)

    def clear_processed_evidence(self, entity_name: str):
        try:
            self.graph.conn.execute("DELETE FROM evidence WHERE entity_name = ?", [entity_name])
        except Exception as e:
            LOGGER.error(f"Cleanup Error: {e}")

    def close(self):
        self.graph.close()

# --- TAXONOMY SYNC ---
def _sync_taxonomy_file(taxonomy_path: str, graph_path: str):
    """
    Läser alla Entity-noder från grafen och uppdaterar sub_nodes i taxonomi-filen.
    Detta gör att taxonomifilen alltid speglar grafens innehåll (Terrängen -> Kartan).
    """
    if not os.path.exists(taxonomy_path):
        LOGGER.error(f"Taxonomy file not found: {taxonomy_path}")
        return

    try:
        # 1. Läs nuvarande taxonomi för att behålla struktur/beskrivningar
        with open(taxonomy_path, 'r', encoding='utf-8') as f:
            taxonomy_data = json.load(f)
        
        # 2. Hämta alla entities från grafen
        # Vi öppnar en temporär read-only connection för att inte störa Dreamer
        # OBS: Vi måste hantera låsning om Dreamer redan kör
        # I detta kontext körs sync EFTER att Dreamer stängt sin connection
        graph = GraphStore(graph_path, read_only=True)
        all_entities = graph.find_nodes_by_type("Entity")
        graph.close()

        # 3. Gruppera entities per masternod
        # { "Person": ["Alice", "Bob"], "Projekt": ["P1", "P2"] }
        graph_entities = defaultdict(set)
        for node in all_entities:
            props = node.get('properties', {})
            entity_type = props.get('entity_type') # Detta är masternoden (t.ex. "Person")
            entity_name = node.get('id')
            
            if entity_type and entity_name:
                graph_entities[entity_type].add(entity_name)

        # 4. Uppdatera taxonomi-strukturen
        updates_count = 0
        for master_node, entities in graph_entities.items():
            if master_node in taxonomy_data:
                current_sub_nodes = set(taxonomy_data[master_node].get('sub_nodes', []))
                # Union av filens och grafens noder för att inte tappa bort manuella
                new_sub_nodes = sorted(list(current_sub_nodes.union(entities)))
                
                if new_sub_nodes != taxonomy_data[master_node].get('sub_nodes', []):
                    taxonomy_data[master_node]['sub_nodes'] = new_sub_nodes
                    updates_count += 1
            else:
                LOGGER.warning(f"Grafen innehåller okänd masternod: {master_node}")

        # 5. Skriv tillbaka till fil om ändringar gjordes
        if updates_count > 0:
            with open(taxonomy_path, 'w', encoding='utf-8') as f:
                json.dump(taxonomy_data, f, indent=2, ensure_ascii=False)
            LOGGER.info(f"Uppdaterade {updates_count} masternoder i taxonomifilen.")
        else:
            LOGGER.info("Taxonomifilen är redan synkad.")

    except Exception as e:
        LOGGER.error(f"Kunde inte synka taxonomi-fil: {e}")
        raise # Kasta vidare för att loggas i anroparen

# --- BATCH ORCHESTRATION ---
async def process_all_evidence_batch() -> Dict:
    """
    Returnerar en dict med status och review_list för orkestratorn.
    """
    print(f"{_ts()} 💭 Dreamer Batch Startar...")
    dreamer = EvidenceConsolidator()
    evidence_groups = dreamer.fetch_pending_evidence()
    
    if not evidence_groups:
        print(f"{_ts()} 💤 Inga bevis att bearbeta.")
        dreamer.close()
        return {"status": "OK", "review_list": []}

    candidates = []
    min_evidence = get_setting('dreamer', 'min_evidence_count', 2)
    for name, ev_list in evidence_groups.items():
        if len(ev_list) >= min_evidence or any(e['confidence'] > 0.8 for e in ev_list):
            candidates.append((name, ev_list))
    
    if not candidates:
        dreamer.close()
        return {"status": "OK", "review_list": []}

    print(f"{_ts()} 🔍 Hittade {len(candidates)} kandidater. Batchar...")
    batches = [candidates[i:i + BATCH_SIZE] for i in range(0, len(candidates), BATCH_SIZE)]
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    tasks = [dreamer.analyze_batch_async(batch, semaphore) for batch in batches]
    
    results_list_of_lists = await asyncio.gather(*tasks)
    all_results = [item for sublist in results_list_of_lists for item in sublist]
    
    stats = {"auto_nodes": 0, "themes": 0, "skipped_uncertain": 0}
    evidence_map = {name: ev_list for name, ev_list in candidates}
    review_list = [] # Här samlar vi manuella reviews
    
    for res in all_results:
        entity_name = res.get('entity')
        evidence_list = evidence_map.get(entity_name)
        
        if not evidence_list: continue

        # --- VALIDATION RULES CHECK ---
        val_rule = dreamer.graph.get_validation_rule(entity_name)
        force_approved = False

        if val_rule:
            decision = val_rule.get('decision')
            if decision == 'REJECTED':
                # Tyst kastas (REJECTED) -> Rensa evidence så det inte loopar
                dreamer.clear_processed_evidence(entity_name)
                continue
            
            elif decision in ['APPROVED', 'ADJUSTED']:
                # Hantera APPROVED/ADJUSTED automatiskt
                force_approved = True
                
                if decision == 'ADJUSTED':
                    # Applicera justeringar
                    if val_rule.get('adjusted_name'):
                        res['suggested_node_id'] = val_rule['adjusted_name']
                        res['is_atomic_node'] = True
                    if val_rule.get('adjusted_master_node'):
                        res['master_node'] = val_rule['adjusted_master_node']
                
                elif decision == 'APPROVED':
                    res['is_atomic_node'] = True
            
        is_node = res.get('is_atomic_node')
        # Om validerad -> 1.0 konfidens, annars beräkna
        agg_conf = 1.0 if force_approved else dreamer._calculate_aggregated_confidence(evidence_list)
        
        should_commit = False
        if not is_node:
            should_commit = True
            stats["themes"] += 1
        elif agg_conf >= 0.9:
            should_commit = True
            stats["auto_nodes"] += 1
        else:
            # Osäker Nod -> Skicka till Pending Reviews (DB)
            stats["skipped_uncertain"] += 1
            
            # Hitta ett exempel på kontext för att hjälpa granskaren
            example_context = evidence_list[0]['context'] if evidence_list else "Ingen kontext"
            
            dreamer.graph.add_pending_review(
                entity=res.get('suggested_node_id') or entity_name,
                master_node=res.get('master_node'),
                score=agg_conf,
                reason=f"Konfidens ({agg_conf:.2f}) < 0.9",
                context={"snippet": example_context, "source": evidence_list[0]['source_file']}
            )
            
        if should_commit:
            source_files = [e['source_file'] for e in evidence_list]
            success, msg = dreamer.commit_to_graph(res, source_files)
            if success:
                # topic = res.get('suggested_node_id') if is_node else res.get('master_node')
                # backpropagate_to_lake removed (Graph is SSOT)
                dreamer.clear_processed_evidence(entity_name)
                print(f"   {msg}")

    dreamer.close()

    # --- AUTO SYNC ---
    try:
        _sync_taxonomy_file(TAXONOMY_FILE, GRAPH_PATH)
        print(f"{_ts()} 🔄 Taxonomi-fil synkad med Grafen.")
    except Exception as e:
        LOGGER.error(f"Auto Sync failed: {e}")

    print(f"{_ts()} ✨ Klar. {stats['auto_nodes']} noder, {stats['themes']} teman sparade.")
    if stats["skipped_uncertain"] > 0:
        print(f"{_ts()} 📥 {stats['skipped_uncertain']} noder skickade till granskningskön (Shadowgraph).")
        
    return {
        "status": "OK",
        "stats": stats
    }

def consolidate():
    """Wrapper som returnerar resultatet (synkront)."""
    return asyncio.run(process_all_evidence_batch())

if __name__ == "__main__":
    consolidate()