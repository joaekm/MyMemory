"""
MyMem Dreamer - Konsoliderar grafens synapser till taxonomin.

Analogin: Grafen är synapserna, Dreaming konsoliderar dem.
Läser inte om Lake (råa minnen), utan arbetar på den redan abstraherade representationen.

Körs vid uppstart om >24h sedan senaste körning.
"""

import os
import sys
import json
import yaml
import logging
import datetime
import zoneinfo
from typing import Optional
from dataclasses import dataclass
from google import genai
from google.genai import types

# Lägg till projektroten i sys.path för att hitta services-paketet
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.utils.graph_service import GraphStore
from services.utils.json_parser import parse_llm_json
from services.indexers.graph_builder import get_canonical_from_graph
from services.processors.similarity_review_service import _calculate_similarity
from glob import glob
import re

# --- CONFIG LOADER ---
def _load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths_to_check = [
        os.path.join(script_dir, '..', '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, 'config', 'my_mem_config.yaml'),
    ]
    for p in paths_to_check:
        if os.path.exists(p):
            with open(p, 'r') as f:
                config = yaml.safe_load(f)
            for k, v in config['paths'].items():
                config['paths'][k] = os.path.expanduser(v)
            config['logging']['log_file_path'] = os.path.expanduser(config['logging']['log_file_path'])
            return config
    raise RuntimeError("HARDFAIL: Config not found")


def _load_prompts():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', '..', 'config', 'services_prompts.yaml'),
        os.path.join(script_dir, '..', 'config', 'services_prompts.yaml'),
    ]
    for prompts_path in paths:
        if os.path.exists(prompts_path):
            with open(prompts_path, 'r') as f:
                return yaml.safe_load(f)
    raise RuntimeError("HARDFAIL: Prompts not found")


CONFIG = _load_config()
PROMPTS = _load_prompts()

# --- PATHS ---
GRAPH_PATH = CONFIG['paths']['graph_db']
TAXONOMY_FILE = CONFIG['paths'].get('taxonomy_file')
LOG_FILE = CONFIG['logging']['log_file_path']
# Timestamp-fil ligger i samma mapp som taxonomin
_taxonomy_dir = os.path.dirname(TAXONOMY_FILE)
TIMESTAMP_FILE = os.path.join(_taxonomy_dir, ".dreamer_last_run")

# --- TIMEZONE ---
TZ_NAME = CONFIG.get('system', {}).get('timezone', 'UTC')
try:
    SYSTEM_TZ = zoneinfo.ZoneInfo(TZ_NAME)
except Exception as e:
    raise ValueError(f"HARDFAIL: Ogiltig timezone '{TZ_NAME}': {e}") from e

# --- LOGGING ---
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - DREAMER - %(levelname)s - %(message)s'
)
LOGGER = logging.getLogger('Dreamer')

# --- AI CLIENT ---
API_KEY = CONFIG.get('ai_engine', {}).get('api_key', '')
MODEL_FAST = CONFIG.get('ai_engine', {}).get('models', {}).get('model_fast', 'models/gemini-flash-latest')
MODEL_LITE = CONFIG.get('ai_engine', {}).get('models', {}).get('model_lite', 'models/gemini-flash-latest')
MODEL_PRO = CONFIG.get('ai_engine', {}).get('models', {}).get('model_pro', 'models/gemini-pro-latest')
AI_CLIENT = genai.Client(api_key=API_KEY) if API_KEY else None

LAKE_STORE = CONFIG['paths']['lake_store']

LOGGER.info(f"Dreamer initierad: MODEL={MODEL_PRO}, GRAPH={GRAPH_PATH}, TAXONOMY={TAXONOMY_FILE}")

# --- DREAMING INTERVAL ---
DREAMING_INTERVAL_HOURS = 24


def _ts():
    return datetime.datetime.now(SYSTEM_TZ).strftime("[%H:%M:%S]")


@dataclass
class ReviewObject:
    """Dataclass för entiteter som behöver granskas av användaren."""
    entity_name: str
    master_node: str
    similarity_score: float
    suggested_action: str  # 'APPROVE', 'REVIEW', 'REJECT'
    reason: str
    closest_match: str | None = None


def should_run_dreaming() -> bool:
    """Kolla om dreaming ska köras (>24h sedan senaste)."""
    if not os.path.exists(TIMESTAMP_FILE):
        return True
    
    try:
        with open(TIMESTAMP_FILE, 'r') as f:
            last_run_str = f.read().strip()
        last_run = datetime.datetime.fromisoformat(last_run_str)
        now = datetime.datetime.now(SYSTEM_TZ)
        hours_since = (now - last_run).total_seconds() / 3600
        return hours_since >= DREAMING_INTERVAL_HOURS
    except Exception as e:
        LOGGER.warning(f"Kunde inte läsa timestamp-fil: {e}")
        return True


def _save_timestamp():
    """Spara tidsstämpel för senaste dreaming-körning."""
    os.makedirs(os.path.dirname(TIMESTAMP_FILE), exist_ok=True)
    with open(TIMESTAMP_FILE, 'w') as f:
        f.write(datetime.datetime.now(SYSTEM_TZ).isoformat())


def _load_taxonomy() -> dict:
    """Ladda befintlig taxonomi."""
    LOGGER.info(f"Laddar taxonomi från: {TAXONOMY_FILE}")
    if not os.path.exists(TAXONOMY_FILE):
        LOGGER.error(f"HARDFAIL: Taxonomi-fil saknas: {TAXONOMY_FILE}")
        raise RuntimeError(f"HARDFAIL: Taxonomi-fil saknas: {TAXONOMY_FILE}")
    
    with open(TAXONOMY_FILE, 'r', encoding='utf-8') as f:
        taxonomy = json.load(f)
    
    total_sub_nodes = sum(len(v.get("sub_nodes", [])) for v in taxonomy.values())
    LOGGER.info(f"Taxonomi laddad: {len(taxonomy)} masternoder, {total_sub_nodes} sub_nodes totalt")
    return taxonomy


def _save_taxonomy(taxonomy: dict):
    """Spara uppdaterad taxonomi."""
    with open(TAXONOMY_FILE, 'w', encoding='utf-8') as f:
        json.dump(taxonomy, f, ensure_ascii=False, indent=2)
    LOGGER.info(f"Taxonomi uppdaterad: {TAXONOMY_FILE}")


def backpropagate_to_lake(unit_id: str, context_summary: str):
    """
    Uppdatera Lake-filens frontmatter med grafkontext för att trigga re-indexering.
    """
    try:
        pattern = os.path.join(LAKE_STORE, f"*_{unit_id}.md")
        matches = glob(pattern)
        if not matches:
            LOGGER.warning(f"Hittade ingen Lake-fil för unit_id={unit_id}")
            return False
        target = matches[0]

        with open(target, 'r', encoding='utf-8') as f:
            content = f.read()

        if not content.startswith("---"):
            LOGGER.warning(f"Frontmatter saknas i {target}")
            return False

        parts = content.split("---", 2)
        if len(parts) < 3:
            LOGGER.warning(f"Kunde inte parsa frontmatter i {target}")
            return False

        fm = yaml.safe_load(parts[1]) or {}
        fm["graph_context_updated_at"] = datetime.datetime.now(SYSTEM_TZ).isoformat()
        fm["graph_context_summary"] = context_summary

        new_frontmatter = yaml.dump(fm, allow_unicode=True, sort_keys=False)
        new_content = f"---\n{new_frontmatter}---{parts[2]}"

        with open(target, 'w', encoding='utf-8') as f:
            f.write(new_content)

        LOGGER.info(f"Backpropagerade grafkontext till {target}")
        return True
    except Exception as e:
        LOGGER.error(f"Fel vid backpropagate_to_lake för {unit_id}: {e}")
        return False


def collect_from_graph() -> dict:
    """
    Samla alla noder från grafen (synapserna).
    
    Returns:
        dict med:
        - concepts: Lista med alla Concept-noder
        - entities: Dict med type -> [names]
        - aliases: Lista med (alias, canonical) tuples
    """
    result = {
        "concepts": [],
        "entities": {},
        "aliases": []
    }
    
    graph = None
    try:
        # Använd read_write istället för read_only för att undvika konflikter med andra processer
        # read_write kan användas för både läsning och skrivning
        graph = GraphStore(GRAPH_PATH, read_only=False)
        
        # 1. Hämta alla Concept-noder
        try:
            concepts = graph.find_nodes_by_type("Concept")
            for concept in concepts:
                if concept.get('id'):
                    result["concepts"].append(concept['id'])
        except Exception as e:
            LOGGER.warning(f"Kunde inte hämta Concept-noder: {e}")
        
        # 2. Hämta alla Entity-noder med typ
        try:
            entities = graph.find_nodes_by_type("Entity")
            for entity in entities:
                entity_id = entity.get('id')
                if not entity_id:
                    continue
                
                entity_type = entity.get('properties', {}).get('entity_type', 'Unknown')
                entity_aliases = entity.get('aliases', [])
                
                if entity_type not in result["entities"]:
                    result["entities"][entity_type] = []
                result["entities"][entity_type].append(entity_id)
                
                # Samla alias-relationer
                for alias in entity_aliases:
                    result["aliases"].append((alias, entity_id))
        except Exception as e:
            LOGGER.warning(f"Kunde inte hämta Entity-noder: {e}")
        
        # 3. Hämta Person-noder (legacy)
        try:
            persons = graph.find_nodes_by_type("Person")
            for person in persons:
                person_id = person.get('id')
                if person_id:
                    if "Person" not in result["entities"]:
                        result["entities"]["Person"] = []
                    if person_id not in result["entities"]["Person"]:
                        result["entities"]["Person"].append(person_id)
        except Exception as e:
            LOGGER.warning(f"Kunde inte hämta Person-noder: {e}")
        
    except Exception as e:
        LOGGER.error(f"Fel vid graf-anslutning: {e}")
        raise RuntimeError(f"HARDFAIL: Kunde inte ansluta till grafen: {e}") from e
    finally:
        if graph:
            graph.close()
    
    LOGGER.info(f"Samlade från graf: {len(result['concepts'])} concepts, "
                f"{sum(len(v) for v in result['entities'].values())} entities, "
                f"{len(result['aliases'])} aliases")
    
    return result


def _resolve_canonical_entity(graph: GraphStore, entity_name: str) -> tuple[str, dict | None]:
    """Returnera (canonical_name, node) för ett namn eller alias."""
    node = graph.get_node(entity_name)
    if node and node.get("type") == "Entity":
        return entity_name, node

    try:
        alias_matches = graph.find_nodes_by_alias(entity_name)
    except Exception as exc:
        LOGGER.warning(f"Kunde inte slå upp alias för '{entity_name}': {exc}")
        alias_matches = []

    if alias_matches:
        canonical_name = alias_matches[0]["id"]
        node = graph.get_node(canonical_name)
        if node and node.get("type") == "Entity":
            LOGGER.debug(f"Alias '{entity_name}' -> canonical '{canonical_name}'")
            return canonical_name, node
    return entity_name, node


def _filter_deterministic_noise(node_name: str, master_node: str) -> bool:
    """
    Deterministisk filter för att avvisa tekniskt brus.
    
    Returns:
        False om noden ska avvisas, True om den ska behållas.
    """
    # Ladda dokumentändelser från config
    doc_extensions = CONFIG.get('processing', {}).get('document_extensions', [])
    extensions_lower = [ext.lower() for ext in doc_extensions]
    
    node_lower = node_name.lower()
    
    # 1. Filnamnsmönster: Avvisa noder som slutar på dokumentändelser
    for ext in extensions_lower:
        if node_lower.endswith(ext):
            LOGGER.info(f"Filter: Avvisade '{node_name}' (filnamnsändelse: {ext})")
            return False
    
    # 2. UUID-mönster: Avvisa noder som matchar UUID-regex
    uuid_pattern = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
    if uuid_pattern.search(node_name):
        LOGGER.info(f"Filter: Avvisade '{node_name}' (UUID-mönster)")
        return False
    
    # 3. Aktör-specifik: Avvisa "Digitalist" i Aktör-masternoden
    if master_node == "Aktör" and "digitalist" in node_lower:
        LOGGER.info(f"Filter: Avvisade '{node_name}' (Digitalist i Aktör)")
        return False
    
    return True


def _enforce_canonical(node_name: str, master_node: str, taxonomy: dict, graph: GraphStore) -> tuple[str | None, bool]:
    """
    Tvinga canonical truth från grafen.
    
    Args:
        node_name: Nodnamn att validera
        master_node: Masternod där noden ska läggas till
        taxonomy: Taxonomi-dict
        graph: GraphStore-instans
    
    Returns:
        (canonical_name, should_reject) där:
        - canonical_name: Canonical namn att använda (eller None om ska avvisas)
        - should_reject: True om noden ska avvisas helt
    """
    # Hämta canonical från grafen
    canonical = get_canonical_from_graph(node_name)
    
    if canonical and canonical != node_name:
        # Canonical finns och skiljer sig från original
        # Kolla om canonical redan finns i taxonomin
        for mn, data in taxonomy.items():
            if canonical in data.get("sub_nodes", []):
                LOGGER.info(f"Canonical: Avvisade '{node_name}' (canonical '{canonical}' finns redan i {mn})")
                return None, True
        
        LOGGER.info(f"Canonical: '{node_name}' -> '{canonical}'")
        return canonical, False
    
    elif canonical and canonical == node_name:
        # Detta är redan canonical - kolla om det finns i taxonomin
        for mn, data in taxonomy.items():
            if canonical in data.get("sub_nodes", []):
                LOGGER.info(f"Canonical: Avvisade '{node_name}' (finns redan i {mn})")
                return None, True
        return node_name, False
    
    else:
        # Ingen canonical hittades - behåll original om det inte redan finns
        for mn, data in taxonomy.items():
            if node_name in data.get("sub_nodes", []):
                LOGGER.info(f"Canonical: Avvisade '{node_name}' (finns redan i {mn})")
                return None, True
        return node_name, False


def consolidate_with_evidence(taxonomy: dict, graph_data: dict) -> dict:
    """
    Konsolidera entiteter mot taxonomi med Evidence Layer.
    
    - Hämtar alla unika entity_name direkt från evidence-tabellen (inte från grafen)
    - För varje entity: räkna master_node_candidate-frekvens och välj högsta (tie-break på confidence-medel)
    - Lägg till entiteten i taxonomin under vald masternod om den saknas
    - Flytta entiteten om grafens canonical truth eller stark evidence visar annan masternod
    
    Returns:
        {"entities_added": int, "entities_moved": int}
    """
    stats = {"entities_added": 0, "entities_moved": 0}

    # Använd read_write istället för read_only för att undvika konflikter med andra processer
    graph = GraphStore(GRAPH_PATH, read_only=False)
    try:
        # Hämta alla unika entity_name direkt från evidence-tabellen
        # (Multipass-entities finns i evidence, inte i grafen ännu)
        unique_entities = graph.conn.execute("""
            SELECT DISTINCT entity_name 
            FROM evidence
            WHERE entity_name IS NOT NULL AND entity_name != ''
        """).fetchall()
        
        all_entities = [row[0] for row in unique_entities]
        
        LOGGER.info(f"Konsoliderar {len(all_entities)} entities från Evidence Layer")
        
        entity_to_masters: dict[str, set[str]] = {}
        for master_node, data in taxonomy.items():
            for sub_node in data.get("sub_nodes", []):
                entity_to_masters.setdefault(sub_node, set()).add(master_node)
        
        for entity_name in all_entities:
            evidences = graph.get_evidence_for_entity(entity_name, limit=200)
            if not evidences:
                continue

            # Räkna master_node_candidate-frekvenser och medelconfidence
            counts = {}
            confidence_sum = {}
            for ev in evidences:
                master = ev.get("master_node_candidate")
                if not master:
                    continue
                counts[master] = counts.get(master, 0) + 1
                conf = ev.get("confidence")
                if conf is not None:
                    try:
                        conf_f = float(conf)
                    except Exception as e:
                        LOGGER.debug(f"Confidence parse misslyckades för {entity_name}: {e}")
                        conf_f = 0.0
                    confidence_sum[master] = confidence_sum.get(master, 0.0) + conf_f

            if not counts:
                continue

            # Välj master med flest evidence, tie-break på högst medelconfidence
            def score(item):
                master, cnt = item
                avg_conf = confidence_sum.get(master, 0.0) / cnt if cnt else 0.0
                return (cnt, avg_conf)

            best_master = max(counts.items(), key=score)[0]

            canonical_name, graph_node = _resolve_canonical_entity(graph, entity_name)
            graph_master = None
            if graph_node:
                graph_master = graph_node.get("properties", {}).get("entity_type")

            correct_master = best_master
            reason = f"{counts[best_master]} evidence"

            if graph_master:
                if graph_master in taxonomy:
                    correct_master = graph_master
                    reason = "grafens canonical entity_type"
                    if graph_master != best_master:
                        LOGGER.info(
                            f"Graf överstyr evidence för '{canonical_name}': "
                            f"graf={graph_master}, evidence={best_master}"
                        )
                else:
                    LOGGER.warning(
                        f"Graf pekar på okänd masternod '{graph_master}' för '{canonical_name}'. "
                        f"Faller tillbaka till evidence '{best_master}'."
                    )

            if correct_master not in taxonomy:
                LOGGER.warning(
                    f"Skippar entity '{canonical_name}': okänd masternod '{correct_master}'"
                )
                continue

            current_masters = entity_to_masters.get(canonical_name, set()).copy()

            if not current_masters:
                # Entitet saknas i taxonomin – validera innan läggning
                # 1. Deterministisk filter
                if not _filter_deterministic_noise(canonical_name, correct_master):
                    LOGGER.info(f"Filter: Avvisade '{canonical_name}' från {correct_master} (deterministisk filter)")
                    continue
                
                # 2. Canonical enforcement
                final_name, should_reject = _enforce_canonical(canonical_name, correct_master, taxonomy, graph)
                if should_reject or final_name is None:
                    LOGGER.info(f"Canonical: Avvisade '{canonical_name}' från {correct_master} (duplikat eller ogiltig)")
                    continue
                
                # Lägg till med canonical namn
                taxonomy[correct_master].setdefault("sub_nodes", []).append(final_name)
                taxonomy[correct_master]["sub_nodes"] = sorted(
                    list(set(taxonomy[correct_master]["sub_nodes"]))
                )
                entity_to_masters.setdefault(final_name, set()).add(correct_master)
                stats["entities_added"] += 1
                if final_name != canonical_name:
                    LOGGER.info(f"Lade till '{final_name}' (från '{canonical_name}') i {correct_master} ({reason})")
                else:
                    LOGGER.info(f"Lade till '{canonical_name}' i {correct_master} ({reason})")
                continue

            if correct_master in current_masters and len(current_masters) == 1:
                # Redan placerad rätt – inget att göra
                continue

            # Graf saknas → använd evidence-tröskel innan flytt
            evidence_reason = reason
            if not graph_master:
                master_count = counts.get(correct_master, 0)
                avg_conf = (
                    confidence_sum.get(correct_master, 0.0) / master_count
                    if master_count
                    else 0.0
                )
                evidence_strong = master_count >= 3 or avg_conf >= 0.7
                if not evidence_strong:
                    LOGGER.debug(
                        f"Behåller '{canonical_name}' i {sorted(current_masters)}: "
                        f"evidence för '{correct_master}' är för svagt "
                        f"(count={master_count}, avg_conf={avg_conf:.2f})"
                    )
                    continue
                evidence_reason = f"{master_count} evidence (avg_conf {avg_conf:.2f})"

            # Flytta: ta bort från samtliga gamla masternoder
            for old_master in sorted(current_masters):
                if canonical_name in taxonomy[old_master].get("sub_nodes", []):
                    taxonomy[old_master]["sub_nodes"] = sorted(
                        list(
                            set(
                                node
                                for node in taxonomy[old_master]["sub_nodes"]
                                if node != canonical_name
                            )
                        )
                    )
                    LOGGER.info(
                        f"Tog bort '{canonical_name}' från {old_master} "
                        f"(flyttas till {correct_master})"
                    )
            entity_to_masters[canonical_name] = {correct_master}

            # Lägg till i korrekt masternod
            taxonomy[correct_master].setdefault("sub_nodes", []).append(canonical_name)
            taxonomy[correct_master]["sub_nodes"] = sorted(
                list(set(taxonomy[correct_master]["sub_nodes"]))
            )
            stats["entities_moved"] += 1
            LOGGER.info(
                f"Flyttade '{canonical_name}' till {correct_master} "
                f"(källa: {reason if graph_master else evidence_reason})"
            )
    finally:
        graph.close()

    LOGGER.info(
        f"Evidence-konsolidering: {stats['entities_added']} entities tillagda, "
        f"{stats['entities_moved']} entities flyttade"
    )
    return stats


def get_evidence_for_entity(entity_name: str, limit: int = 200) -> list[dict]:
    """Hämta evidence för en entitet."""
    # Använd read_write istället för read_only för att undvika konflikter med andra processer
    graph = GraphStore(GRAPH_PATH, read_only=False)
    try:
        return graph.get_evidence_for_entity(entity_name, limit=limit)
    finally:
        graph.close()


def _synchronize_taxonomy_with_graph(taxonomy: dict, graph: GraphStore) -> dict:
    """
    Synkronisera taxonomin med grafens canonical truth.
    
    Validerar varje sub_node i taxonomin mot grafen:
    - Om noden finns som canonical: behåll den
    - Om noden är ett alias: ersätt med canonical
    - Om noden inte finns: ta bort den (prune)
    
    Args:
        taxonomy: Taxonomi-dict att validera
        graph: GraphStore-instans (read-only)
    
    Returns:
        dict med statistik: {"stale_removed": X, "aliases_replaced": Y, "pruned": Z}
    """
    stats = {
        "stale_removed": 0,
        "aliases_replaced": 0,
        "pruned": 0
    }
    
    try:
        for master_node, data in taxonomy.items():
            sub_nodes = data.get("sub_nodes", [])
            if not sub_nodes:
                continue
            
            cleaned_sub_nodes = []
            replacements = {}  # Map: stale_name -> canonical_name
            node_pruned_count = 0  # Per-master-node counter for logging
            
            for sub_node in sub_nodes:
                # 1. Check if it exists as canonical node
                canonical_node = graph.get_node(sub_node)
                if canonical_node:
                    # Exists as canonical - keep it
                    cleaned_sub_nodes.append(sub_node)
                    continue
                
                # 2. Check if it's an alias
                alias_matches = graph.find_nodes_by_alias(sub_node)
                if alias_matches:
                    # It's an alias - get the canonical
                    canonical_id = alias_matches[0]['id']
                    if canonical_id not in cleaned_sub_nodes:
                        cleaned_sub_nodes.append(canonical_id)
                        replacements[sub_node] = canonical_id
                        stats["aliases_replaced"] += 1
                        LOGGER.info(f"Synkroniserade alias: '{sub_node}' -> '{canonical_id}' i {master_node}")
                    else:
                        # Canonical already in list, just remove the alias
                        stats["stale_removed"] += 1
                        LOGGER.info(f"Tog bort stale alias '{sub_node}' (canonical '{canonical_id}' finns redan) i {master_node}")
                    continue
                
                # 3. Node doesn't exist in graph - prune it
                stats["pruned"] += 1
                node_pruned_count += 1
                LOGGER.warning(f"Prunade nod '{sub_node}' från {master_node} (finns inte i graf)")
            
            # Deduplicate and sort
            data["sub_nodes"] = sorted(list(set(cleaned_sub_nodes)))
            
            # Log summary for this master node
            if replacements or node_pruned_count > 0:
                changes = []
                if replacements:
                    changes.append(f"{len(replacements)} alias ersatta")
                if node_pruned_count > 0:
                    changes.append(f"{node_pruned_count} prunade")
                LOGGER.info(f"Synkroniserade {master_node}: {', '.join(changes)}")
        
        return stats
    
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Fel vid taxonomi-synkronisering: {e}")
        raise RuntimeError(f"HARDFAIL: Fel vid taxonomi-synkronisering: {e}") from e


def _find_unrecognized_nodes(graph_data: dict, taxonomy: dict) -> dict:
    """
    Hitta noder i grafen som inte finns i taxonomin.
    
    Returns:
        dict med:
        - new_concepts: Concepts som inte matchar någon sub_node
        - new_entities: Entities som inte finns i motsvarande typ-kategori
    """
    # Bygg lookup för alla kända sub_nodes
    known_sub_nodes = set()
    for master_node, data in taxonomy.items():
        known_sub_nodes.add(master_node)
        for sub in data.get("sub_nodes", []):
            known_sub_nodes.add(sub)
    
    # Hitta okända concepts
    new_concepts = []
    for concept in graph_data["concepts"]:
        if concept not in known_sub_nodes:
            new_concepts.append(concept)
    
    # Hitta okända entities
    new_entities = {}
    for entity_type, names in graph_data["entities"].items():
        if entity_type in taxonomy:
            known_in_type = set(taxonomy[entity_type].get("sub_nodes", []))
            for name in names:
                if name not in known_in_type:
                    if entity_type not in new_entities:
                        new_entities[entity_type] = []
                    new_entities[entity_type].append(name)
    
    return {
        "new_concepts": new_concepts,
        "new_entities": new_entities
    }


def _prune_taxonomy(taxonomy: dict) -> int:
    """
    Deterministisk städning av taxonomin (Grovtvätt).
    
    1. Tar bort självreferenser (sub_node == master_node).
    2. Tar bort dubbletter (deduplicering).
    """
    total_pruned = 0
    for master_node, data in taxonomy.items():
        sub_nodes = data.get("sub_nodes", [])
        if not sub_nodes:
            continue

        original_count = len(sub_nodes)
        clean_nodes = []
        seen = set()

        for node in sub_nodes:
            # 1. Självreferens check (case-insensitive)
            if node.lower() == master_node.lower():
                LOGGER.info(f"Pruning: Tog bort självreferens '{node}' från {master_node}")
                continue
            
            # 2. Deduplicering (case-insensitive check men behåll original casing)
            if node.lower() in seen:
                continue
            
            seen.add(node.lower())
            clean_nodes.append(node)
        
        data["sub_nodes"] = sorted(clean_nodes)
        pruned_here = original_count - len(clean_nodes)
        total_pruned += pruned_here
        
        if pruned_here > 0:
            LOGGER.info(f"Pruning: {pruned_here} noder borttagna från {master_node}")

    return total_pruned


def consolidate() -> dict:
    """
    Huvudfunktion: Konsolidera grafens noder till taxonomin.
    
    Steg:
    0. Pruning: Grovtvätt av taxonomin.
    1. Samla noder från grafen
    2. Ladda taxonomin
    2.5. Synkronisera taxonomin med grafens canonical truth (validera aliases, pruna stale noder)
    3. Jämför med taxonomin (hitta nya noder)
    4. Skicka noder (både nya och befintliga) till LLM för kategorisering/finstädning
    5. Uppdatera taxonomin
    
    Returns:
        dict med statistik över konsolideringen
    """
    print(f"{_ts()} 💭 Dreaming startar...")
    LOGGER.info("Dreaming startar")
    
    stats = {
        "concepts_added": 0,
        "entities_added": 0,
        "aliases_found": 0,
        "pruned_count": 0,
        "status": "OK",
        "evidence_entities_added": 0
    }
    
    try:
        # 2. Ladda taxonomin (Gör detta tidigt för att pruna)
        taxonomy = _load_taxonomy()
        
        # 0. Pruning (Grovtvätt)
        pruned_deterministic = _prune_taxonomy(taxonomy)
        stats["pruned_count"] += pruned_deterministic
        if pruned_deterministic > 0:
             print(f"{_ts()} 🧹 Grovtvätt: {pruned_deterministic} noder raderade")

        # 1. Samla från grafen
        graph_data = collect_from_graph()
        
        # 2.5. Synkronisera taxonomin med grafens canonical truth (NYTT)
        # Öppna graf-anslutning för synkronisering
        graph = None
        try:
            # Använd read_write istället för read_only för att undvika konflikter med andra processer
            # read_write kan användas för både läsning och skrivning
            graph = GraphStore(GRAPH_PATH, read_only=False)
            sync_stats = _synchronize_taxonomy_with_graph(taxonomy, graph)
            
            if sync_stats["aliases_replaced"] > 0 or sync_stats["pruned"] > 0 or sync_stats["stale_removed"] > 0:
                total_changes = sync_stats["aliases_replaced"] + sync_stats["pruned"] + sync_stats["stale_removed"]
                print(f"{_ts()} 🔄 Synkroniserade taxonomi: {sync_stats['aliases_replaced']} alias ersatta, "
                      f"{sync_stats['pruned']} prunade, {sync_stats['stale_removed']} stale borttagna")
                LOGGER.info(f"Taxonomi-synkronisering: {total_changes} ändringar")
        except Exception as e:
            LOGGER.error(f"HARDFAIL: Kunde inte synkronisera taxonomi: {e}")
            raise RuntimeError(f"HARDFAIL: Kunde inte synkronisera taxonomi: {e}") from e
        finally:
            if graph:
                graph.close()
        
        # 3. Evidence-konsolidering: använd Evidence Layer för att placera entiteter
        ev_stats = consolidate_with_evidence(taxonomy, graph_data)
        stats["evidence_entities_added"] = ev_stats.get("entities_added", 0)

        # 4. Hitta nya noder (efter evidence-konsolidering)
        unrecognized = _find_unrecognized_nodes(graph_data, taxonomy)
        
        new_concepts = unrecognized["new_concepts"]
        new_entities = unrecognized["new_entities"]
        
        # VIKTIGT: Skicka BARA nya noder till LLM för granskning
        # Befintliga noder i taxonomin behöver inte granskas om de redan finns
        # Detta förhindrar att samma entiteter skickas om och om igen
        
        all_nodes_to_review = new_concepts.copy()
        
        # Lägg till nya entities
        for entity_list in new_entities.values():
            all_nodes_to_review.extend(entity_list)
        
        # Deduplicera
        all_nodes_to_review = list(set(all_nodes_to_review))

        if not all_nodes_to_review:
            print(f"{_ts()} ✅ Dreaming klar: Inget att granska")
            stats["review_list"] = []
            stats["review_count"] = 0
            _save_taxonomy(taxonomy)
            _save_timestamp()
            return stats
        
        print(f"{_ts()} 🔍 Granskar {len(all_nodes_to_review)} nya noder via LLM...")
        
        # 5. Skicka till LLM för kategorisering OCH städning
        # Samla ReviewObject-lista istället för att direkt uppdatera taxonomy
        review_list = []
        
        if AI_CLIENT:
            # Öppna graf-anslutning för similarity-beräkningar
            # Använd read_write istället för read_only för att undvika konflikter med andra processer
            # read_write kan användas för både läsning och skrivning
            graph = GraphStore(GRAPH_PATH, read_only=False)
            try:
                # Använd _llm_categorize för att få kategoriseringar
                review_result = _llm_categorize(all_nodes_to_review, taxonomy)
                
                if review_result:
                    # Hantera 'categorized' (Lägg till/Flytta) - skapa ReviewObject för varje
                    categorized = review_result.get("categorized", {})
                    
                    for master_node, items in categorized.items():
                        if master_node not in taxonomy:
                            continue
                        current_subs = set(taxonomy[master_node].get("sub_nodes", []))
                        for item in items:
                            if item in current_subs:
                                continue
                            
                            # 1. Deterministisk filter
                            if not _filter_deterministic_noise(item, master_node):
                                LOGGER.info(f"Filter: Avvisade '{item}' från {master_node} (deterministisk filter)")
                                continue
                            
                            # 2. Canonical enforcement
                            final_name, should_reject = _enforce_canonical(item, master_node, taxonomy, graph)
                            if should_reject or final_name is None:
                                LOGGER.info(f"Canonical: Avvisade '{item}' från {master_node} (duplikat eller ogiltig)")
                                continue
                            
                            # 3. Beräkna similarity och skapa ReviewObject
                            similarity_result = _calculate_similarity(final_name, master_node, graph)
                            
                            closest_match_str = None
                            if similarity_result.get("closest_match"):
                                closest_match_str = similarity_result["closest_match"].get("entity_name")
                            
                            review_obj = ReviewObject(
                                entity_name=final_name,
                                master_node=master_node,
                                similarity_score=similarity_result.get("similarity_score", 0.0),
                                suggested_action=similarity_result.get("suggested_action", "REVIEW"),
                                reason=similarity_result.get("reason", ""),
                                closest_match=closest_match_str
                            )
                            review_list.append(review_obj)
                            
                            LOGGER.info(f"ReviewObject skapad: {final_name} -> {master_node} (similarity: {review_obj.similarity_score}, action: {review_obj.suggested_action})")
                
                # Hantera 'pruned' (Ta bort) - skapa ReviewObject med REJECT
                pruned_list = review_result.get("pruned", [])
                for pruned_item in pruned_list:
                    node_to_remove = pruned_item.get("node")
                    reason = pruned_item.get("reason", "Ingen orsak")
                    
                    # Hitta masternod
                    found_master = None
                    for master_node, data in taxonomy.items():
                        if node_to_remove in data.get("sub_nodes", []):
                            found_master = master_node
                            break
                    
                    if found_master:
                        # Beräkna similarity även för prunade noder
                        similarity_result = _calculate_similarity(node_to_remove, found_master, graph)
                        
                        review_obj = ReviewObject(
                            entity_name=node_to_remove,
                            master_node=found_master,
                            similarity_score=similarity_result.get("similarity_score", 0.0),
                            suggested_action="REJECT",
                            reason=reason,
                            closest_match=None
                        )
                        review_list.append(review_obj)

                # Hantera 'merged' (Sammanslagning/Rename) - skapa ReviewObject för både old och new
                merged_list = review_result.get("merged", [])
                for merge_item in merged_list:
                    old_name = merge_item.get("old")
                    new_name = merge_item.get("new")
                    
                    if old_name and new_name:
                        # Hitta masternod för old_name
                        found_master = None
                        for master_node, data in taxonomy.items():
                            if old_name in data.get("sub_nodes", []):
                                found_master = master_node
                                break
                        
                        if found_master:
                            # Skapa ReviewObject för new_name (RENAME-åtgärd)
                            similarity_result = _calculate_similarity(new_name, found_master, graph)
                            
                            closest_match_str = None
                            if similarity_result.get("closest_match"):
                                closest_match_str = similarity_result["closest_match"].get("entity_name")
                            
                            review_obj = ReviewObject(
                                entity_name=new_name,
                                master_node=found_master,
                                similarity_score=similarity_result.get("similarity_score", 0.0),
                                suggested_action="REVIEW",  # Merged behöver alltid granskning
                                reason=f"Merged från '{old_name}'",
                                closest_match=closest_match_str
                            )
                            review_list.append(review_obj)
            finally:
                graph.close()
            
            # Deduplicera och sortera taxonomy (behåll för backward compatibility)
            for master_node, data in taxonomy.items():
                if "sub_nodes" in data:
                    data["sub_nodes"] = sorted(list(set(data["sub_nodes"])))
            
            # Lägg till review_list i stats
            stats["review_list"] = review_list
            stats["review_count"] = len(review_list)
            
            print(f"{_ts()} ✅ Dreaming klar: {len(review_list)} entiteter behöver granskning")
        else:
            LOGGER.warning("AI-klient saknas, kan inte kategorisera nya noder")
            stats["status"] = "NO_AI"
            stats["review_list"] = []
            stats["review_count"] = 0
        
        # Spara taxonomy endast om review_list är tom (backward compatibility)
        # Annars väntar vi på användarens beslut via interactive review
        if not review_list:
            _save_taxonomy(taxonomy)
        
        _save_timestamp()
        
    except Exception as e:
        LOGGER.error(f"Fel under dreaming: {e}")
        stats["status"] = "ERROR"
        stats["error"] = str(e)
        stats["review_list"] = []
        stats["review_count"] = 0
        print(f"{_ts()} ❌ Dreaming misslyckades: {e}")
    
    return stats


def _llm_categorize(nodes_to_review: list, taxonomy: dict) -> Optional[dict]:
    """
    Använd LLM för att granska, kategorisera och städa noder.
    """
    if not nodes_to_review:
        return None
    
    prompt_template = PROMPTS.get('dreamer', {}).get('consolidation_prompt', '')
    if not prompt_template:
        LOGGER.error("HARDFAIL: dreamer.consolidation_prompt saknas")
        raise RuntimeError("HARDFAIL: dreamer.consolidation_prompt saknas")
    
    # Bygg master node info med definitioner
    master_nodes_info = {}
    for k, v in taxonomy.items():
        desc = v.get("description", "")
        defi = v.get("multipass_definition", "")
        master_nodes_info[k] = f"{desc} | Definition: {defi}"

    # Batching om det är extremt många noder (safety catch)
    # För nu: skicka allt men trunkera listan om den är för stor för context
    # En enkel string-dump
    nodes_str = json.dumps(nodes_to_review, ensure_ascii=False)
    
    prompt = prompt_template
    prompt = prompt.replace("{master_nodes}", json.dumps(master_nodes_info, ensure_ascii=False, indent=2))
    prompt = prompt.replace("{sub_nodes_to_review}", nodes_str)
    
    try:
        LOGGER.info(f"Skickar {len(nodes_to_review)} noder till LLM för granskning")
        
        # DEBUG: Spara prompt
        debug_prompt_file = os.path.join(os.path.dirname(TAXONOMY_FILE), "dreamer_review_prompt.txt")
        with open(debug_prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt)
            
        response = AI_CLIENT.models.generate_content(
            model=MODEL_PRO,
            contents=[
                types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
            ],
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        
        raw_text = response.text.replace('```json', '').replace('```', '').strip()
        result = json.loads(raw_text)
        return result
        
    except Exception as e:
        LOGGER.error(f"LLM-granskning misslyckades: {e}")
        return None


# --- CLI ---
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--force":
        print("Forcerar dreaming...")
        result = consolidate()
        print(f"Resultat: {result}")
    elif should_run_dreaming():
        result = consolidate()
        print(f"Resultat: {result}")
    else:
        print("Dreaming behöver inte köras (senaste körning var för < 24h sedan)")
