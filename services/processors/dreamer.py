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
from google import genai
from google.genai import types

# Lägg till projektroten i sys.path för att hitta services-paketet
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.utils.graph_service import GraphStore

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
AI_CLIENT = genai.Client(api_key=API_KEY) if API_KEY else None

LOGGER.info(f"Dreamer initierad: MODEL={MODEL_FAST}, GRAPH={GRAPH_PATH}, TAXONOMY={TAXONOMY_FILE}")

# --- DREAMING INTERVAL ---
DREAMING_INTERVAL_HOURS = 24


def _ts():
    return datetime.datetime.now(SYSTEM_TZ).strftime("[%H:%M:%S]")


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
        graph = GraphStore(GRAPH_PATH, read_only=True)
        
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


def consolidate() -> dict:
    """
    Huvudfunktion: Konsolidera grafens noder till taxonomin.
    
    Steg:
    1. Samla noder från grafen
    2. Ladda taxonomin
    2.5. Synkronisera taxonomin med grafens canonical truth (validera aliases, pruna stale noder)
    3. Jämför med taxonomin (hitta nya noder)
    4. Skicka nya noder till LLM för kategorisering
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
        "status": "OK"
    }
    
    try:
        # 1. Samla från grafen
        graph_data = collect_from_graph()
        
        # 2. Ladda taxonomin
        taxonomy = _load_taxonomy()
        
        # 2.5. Synkronisera taxonomin med grafens canonical truth (NYTT)
        # Öppna graf-anslutning för synkronisering
        graph = None
        try:
            graph = GraphStore(GRAPH_PATH, read_only=True)
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
        
        # 3. Hitta nya noder
        unrecognized = _find_unrecognized_nodes(graph_data, taxonomy)
        
        new_concepts = unrecognized["new_concepts"]
        new_entities = unrecognized["new_entities"]
        
        if not new_concepts and not new_entities:
            print(f"{_ts()} ✅ Dreaming klar: Inga nya noder att konsolidera")
            LOGGER.info("Inga nya noder att konsolidera")
            # Spara även om inga nya noder (synkroniseringen kan ha ändrat taxonomin)
            _save_taxonomy(taxonomy)
            _save_timestamp()
            return stats
        
        print(f"{_ts()} 🔍 Hittade {len(new_concepts)} nya concepts, "
              f"{sum(len(v) for v in new_entities.values())} nya entities")
        
        # 4. Skicka till LLM för kategorisering
        if AI_CLIENT:
            categorized = _llm_categorize(new_concepts, new_entities, taxonomy)
            
            # 5. Uppdatera taxonomin
            if categorized:
                for master_node, additions in categorized.items():
                    if master_node in taxonomy:
                        current_subs = set(taxonomy[master_node].get("sub_nodes", []))
                        for item in additions:
                            if item not in current_subs:
                                taxonomy[master_node]["sub_nodes"].append(item)
                                stats["concepts_added"] += 1
                
                # Deduplicera och sortera efter att ha lagt till nya noder
                for master_node, data in taxonomy.items():
                    if "sub_nodes" in data:
                        data["sub_nodes"] = sorted(list(set(data["sub_nodes"])))
                
                _save_taxonomy(taxonomy)
                print(f"{_ts()} ✅ Dreaming klar: {stats['concepts_added']} noder tillagda i taxonomin")
        else:
            LOGGER.warning("AI-klient saknas, kan inte kategorisera nya noder")
            stats["status"] = "NO_AI"
            # Spara även om AI saknas (synkroniseringen kan ha ändrat taxonomin)
            _save_taxonomy(taxonomy)
        
        _save_timestamp()
        
    except Exception as e:
        LOGGER.error(f"Fel under dreaming: {e}")
        stats["status"] = "ERROR"
        stats["error"] = str(e)
        print(f"{_ts()} ❌ Dreaming misslyckades: {e}")
    
    return stats


def _llm_categorize(new_concepts: list, new_entities: dict, taxonomy: dict) -> Optional[dict]:
    """
    Använd LLM för att kategorisera nya noder under rätt masternode.
    
    Returns:
        dict med {masternode: [items to add]} eller None vid fel
    """
    if not new_concepts and not new_entities:
        return None
    
    prompt_template = PROMPTS.get('dreamer', {}).get('consolidation_prompt', '')
    if not prompt_template:
        LOGGER.error("HARDFAIL: dreamer.consolidation_prompt saknas i services_prompts.yaml")
        raise RuntimeError("HARDFAIL: dreamer.consolidation_prompt saknas")
    
    # Bygg input för LLM (använd replace istället för format för att undvika {}-konflikter)
    master_nodes_info = {
        k: v.get("description", "") for k, v in taxonomy.items()
    }
    
    prompt = prompt_template
    prompt = prompt.replace("{master_nodes}", json.dumps(master_nodes_info, ensure_ascii=False, indent=2))
    prompt = prompt.replace("{new_concepts}", json.dumps(new_concepts, ensure_ascii=False))
    prompt = prompt.replace("{new_entities}", json.dumps(new_entities, ensure_ascii=False))
    
    try:
        LOGGER.info(f"Skickar {len(new_concepts)} concepts och {len(new_entities)} entities till LLM")
        
        # DEBUG: Spara prompten till fil för inspektion
        debug_prompt_file = os.path.join(os.path.dirname(TAXONOMY_FILE), "dreamer_debug_prompt.txt")
        with open(debug_prompt_file, "w", encoding="utf-8") as f:
            f.write(f"=== DREAMER PROMPT ({len(prompt)} tecken) ===\n\n")
            f.write(prompt)
        LOGGER.info(f"Prompt sparad till: {debug_prompt_file}")
        print(f"{_ts()} 📝 Prompt sparad till: {debug_prompt_file}")
        
        response = AI_CLIENT.models.generate_content(
            model=MODEL_FAST,
            contents=[
                types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
            ],
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        
        raw_text = response.text.replace('```json', '').replace('```', '').strip()
        LOGGER.info(f"LLM svarade: {raw_text[:500]}...")  # Logga första 500 tecken
        
        result = json.loads(raw_text)
        LOGGER.info(f"LLM kategoriserade {len(result)} noder")
        return result
        
    except json.JSONDecodeError as e:
        LOGGER.error(f"JSON-parse fel: {e}. Råsvar: {response.text[:200] if response else 'inget svar'}")
        return None
    except Exception as e:
        LOGGER.error(f"LLM-kategorisering misslyckades: {e}")
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
