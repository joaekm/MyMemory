"""
Interactive Review System for Entity Validation.

Can be used both during rebuild and in regular chat usage.
"""

import os
import sys
import json
import yaml
import logging

# Lägg till project root i path för imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Försök importera readline (finns inte på alla system)
try:
    import readline
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False
    LOGGER = logging.getLogger('InteractiveReview')
    LOGGER.warning("readline inte tillgängligt - autokomplet kommer inte fungera")

from services.processors.dreamer import ReviewObject
from services.utils.graph_service import GraphStore, AVAILABLE_RELATIONS
from services.utils.json_parser import parse_llm_json

if not READLINE_AVAILABLE:
    LOGGER = logging.getLogger('InteractiveReview')
else:
    LOGGER = logging.getLogger('InteractiveReview')


# --- CONFIG LOADING ---

def _load_config():
    """Ladda config från my_mem_config.yaml. HARDFAIL om det misslyckas."""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config_path = os.path.join(project_root, "config", "my_mem_config.yaml")
    
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"HARDFAIL: Config saknas: {config_path}")
    
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    if not config:
        raise ValueError("HARDFAIL: Config är tom")
    
    return config


# --- DATA LOADING FUNCTIONS ---

def _load_master_nodes(taxonomy: dict) -> list[str]:
    """
    Hämta alla masternoder från taxonomin.
    
    Args:
        taxonomy: Taxonomi-dict
        
    Returns:
        Lista med masternod-namn
    """
    if not taxonomy:
        return []
    return sorted(list(taxonomy.keys()))


def _load_canonicals(graph: GraphStore) -> list[str]:
    """
    Hämta alla canonical entity-namn från grafen.
    
    Args:
        graph: GraphStore-instans
        
    Returns:
        Lista med canonical-namn
    """
    try:
        entities = graph.find_nodes_by_type("Entity")
        canonicals = [entity['id'] for entity in entities if entity.get('id')]
        return sorted(canonicals)
    except Exception as e:
        LOGGER.warning(f"Kunde inte hämta canonicals från grafen: {e}")
        return []


def _load_relation_types() -> list[str]:
    """
    Hämta alla tillgängliga relationstyper.
    
    Returns:
        Lista med relationstyp-namn
    """
    return sorted(list(AVAILABLE_RELATIONS.keys()))


def _entity_exists_anywhere(entity_name: str, taxonomy: dict, graph: GraphStore = None) -> tuple[bool, str | None]:
    """
    Kolla om en entitet finns i någon masternod i taxonomin.
    Kollar även grafen för canonicals och aliases.
    
    Args:
        entity_name: Entitetens namn att kolla
        taxonomy: Taxonomi-dict
        graph: GraphStore-instans (optional)
        
    Returns:
        (exists, master_node) där exists är True om entiteten finns, 
        och master_node är vilken masternod den finns i (eller None)
    """
    # 1. Kolla direkt i taxonomin (alla masternoder)
    for master_node, data in taxonomy.items():
        if entity_name in data.get("sub_nodes", []):
            return True, master_node
    
    # 2. Kolla grafen för canonicals och aliases
    if graph:
        try:
            # Kolla om det är en canonical
            node = graph.get_node(entity_name)
            if node and node.get("type") == "Entity":
                # Hitta vilken masternod denna canonical tillhör
                entity_type = node.get("properties", {}).get("entity_type", "")
                if entity_type and entity_type in taxonomy:
                    if entity_name in taxonomy[entity_type].get("sub_nodes", []):
                        return True, entity_type
            
            # Kolla om det är ett alias
            alias_matches = graph.find_nodes_by_alias(entity_name)
            if alias_matches:
                canonical_name = alias_matches[0]["id"]
                # Hitta vilken masternod canonical tillhör
                for master_node, data in taxonomy.items():
                    if canonical_name in data.get("sub_nodes", []):
                        return True, master_node
        except Exception as e:
            LOGGER.debug(f"Kunde inte kolla graf för entitet '{entity_name}': {e}")
    
    return False, None


# --- AUTOCOMPLETE CLASS ---

class AdjustmentCompleter:
    """
    Autokomplet-klass för justera-mode.
    Ger suggestions baserat på masternoder, canonicals och relationstyper.
    """
    
    def __init__(self, master_nodes: list[str], canonicals: list[str], relation_types: list[str]):
        """
        Args:
            master_nodes: Lista med masternod-namn
            canonicals: Lista med canonical entity-namn
            relation_types: Lista med relationstyp-namn
        """
        self.master_nodes = master_nodes
        self.canonicals = canonicals
        self.relation_types = relation_types
        
        # Bygg en kombinerad lista för autokomplet
        # Inkludera även vanliga kommandon
        self.all_completions = (
            self.master_nodes + 
            self.canonicals + 
            self.relation_types +
            ["Flytta till", "Byt namn till", "Alias till", "Koppla till", "Relation"]
        )
    
    def complete(self, text: str, state: int) -> str | None:
        """
        Readline completion-funktion.
        
        Args:
            text: Text att komplettera
            state: State från readline (0 = första gången, 1+ = nästa match)
            
        Returns:
            Matchande completion eller None
        """
        if state == 0:
            # Första gången - bygg lista med matches
            text_lower = text.lower()
            self.matches = [
                comp for comp in self.all_completions
                if comp.lower().startswith(text_lower)
            ]
        
        try:
            return self.matches[state]
        except IndexError:
            # Readline completion returnerar None när inga fler matches finns
            # Detta är förväntat beteende, inte ett fel
            LOGGER.debug(f"Readline completion: inga fler matches för state {state}")
            return None


def _setup_autocomplete(taxonomy: dict, graph: GraphStore):
    """
    Sätt upp autokomplet för justera-mode.
    
    Args:
        taxonomy: Taxonomi-dict
        graph: GraphStore-instans
    """
    if not READLINE_AVAILABLE:
        return
    
    try:
        master_nodes = _load_master_nodes(taxonomy)
        canonicals = _load_canonicals(graph)
        relation_types = _load_relation_types()
        
        completer = AdjustmentCompleter(master_nodes, canonicals, relation_types)
        readline.set_completer(completer.complete)
        
        # Aktivera tab completion
        readline.parse_and_bind("tab: complete")
        
        LOGGER.debug(f"Autokomplet aktiverad: {len(master_nodes)} masternoder, {len(canonicals)} canonicals, {len(relation_types)} relationstyper")
    except Exception as e:
        LOGGER.warning(f"Kunde inte sätta upp autokomplet: {e}")


def _disable_autocomplete():
    """Stäng av autokomplet."""
    if READLINE_AVAILABLE:
        try:
            readline.set_completer(None)
        except Exception as e:
            LOGGER.debug(f"Kunde inte stänga av autokomplet: {e}")


def _select_relation_type(source_entity: str, source_master_node: str, target_entity: str) -> str:
    """
    Presentera menyn för val av relationstyp från AVAILABLE_RELATIONS.
    
    Args:
        source_entity: Källentiteten
        source_master_node: Källentitetens masternod
        target_entity: Måletiteten (behöver hitta dess masternod)
        
    Returns:
        Vald relationstyp (t.ex. "WORKS_AT")
    """
    # Hämta target_entity's masternod från taxonomy
    try:
        config = _load_config()
        taxonomy_path = os.path.expanduser(config['paths']['taxonomy_file'])
    except KeyError as e:
        LOGGER.error(f"HARDFAIL: taxonomy_file saknas i config paths: {e}")
        taxonomy_path = None
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Kunde inte ladda config för taxonomy: {e}")
        taxonomy_path = None
    
    target_master_node = None
    if taxonomy_path and os.path.exists(taxonomy_path):
        try:
            with open(taxonomy_path, 'r', encoding='utf-8') as f:
                taxonomy = json.load(f)
            for mn, data in taxonomy.items():
                if target_entity in data.get("sub_nodes", []):
                    target_master_node = mn
                    break
        except Exception as e:
            LOGGER.warning(f"Kunde inte läsa taxonomy: {e}")
    
    # Filtrera relationstyper baserat på source och target masternoder
    available_options = []
    for rel_type, rel_info in AVAILABLE_RELATIONS.items():
        from_type = rel_info["from"]
        to_type = rel_info["to"]
        
        # Kolla om relationstypen passar
        if (from_type == "Valfri" or from_type == source_master_node) and \
           (to_type == "Valfri" or (target_master_node and to_type == target_master_node)):
            available_options.append((rel_type, rel_info))
    
    if not available_options:
        # Fallback: använd ASSOCIATED_WITH
        return "ASSOCIATED_WITH"
    
    # Presentera menyn
    print("\n" + "═" * 60)
    print("Vilken typ av relation?")
    print("═" * 60)
    for idx, (rel_type, rel_info) in enumerate(available_options, 1):
        print(f"{idx}: {rel_type} ({rel_info['from']} → {rel_info['to']}: {rel_info['description']})")
    print("═" * 60)
    
    # Validera input
    while True:
        try:
            choice = input(f"Välj (1-{len(available_options)}): ").strip()
            choice_num = int(choice)
            if 1 <= choice_num <= len(available_options):
                return available_options[choice_num - 1][0]
            else:
                print(f"❌ Välj ett nummer mellan 1 och {len(available_options)}")
        except ValueError:
            print("❌ Ange ett giltigt nummer")
            LOGGER.debug(f"Ogiltigt nummer angivet i _select_relation_type: {choice}")
        except (EOFError, KeyboardInterrupt):
            # Användaren avbröt - använd fallback
            return "ASSOCIATED_WITH"


def _parse_user_adjustment(user_input: str, entity: str, master_node: str) -> dict:
    """
    Tolka användarens fritext-input med LLM för att identifiera åtgärd.
    
    Args:
        user_input: Användarens fritext-input
        entity: Entitetens namn
        master_node: Masternodens namn
        
    Returns:
        Dict med action, new_name, new_master_node, target_entity, relation_type, split_entities, reason
    """
    try:
        from google import genai
        from google.genai import types
        
        # Ladda config
        config = _load_config()
        
        api_key = config.get('ai_engine', {}).get('api_key', '')
        model_fast = config.get('ai_engine', {}).get('models', {}).get('model_fast', 'models/gemini-flash-latest')
        
        if not api_key:
            LOGGER.error("HARDFAIL: API key saknas")
            return {"action": "REVIEW", "reason": "Kunde inte tolka - API key saknas"}
        
        ai_client = genai.Client(api_key=api_key)
        
        prompt = f"""Användaren sa: '{user_input}' för entiteten '{entity}' i kategorin '{master_node}'.

Tolka detta som en av följande åtgärder:

1. RENAME: Byt namn på entiteten (t.ex. "Kalla den X istället")
2. REMAP: Flytta till en ny masternod (t.ex. "Detta hör till Y-kategorin")
3. ALIAS: Koppla som alias till en befintlig entitet (t.ex. "Detta är samma som Z")
4. RELATE: Skapa en semantisk relation till en annan entitet (t.ex. "Koppla till Adda", "Joakim arbetar på")
5. SPLIT: Entiteten är egentligen två olika saker (t.ex. "Detta är faktiskt X och Y")

Extrahera även eventuell orsak/motivering från användarens input.

Returnera ENDAST JSON:
{{
    "action": "RENAME|REMAP|ALIAS|RELATE|SPLIT",
    "new_name": "...",  # För RENAME
    "new_master_node": "...",  # För REMAP
    "target_entity": "...",  # För ALIAS eller RELATE (extrahera från input)
    "relation_type": null,  # För RELATE - lämna null, väljs senare av användaren
    "split_entities": [...],  # För SPLIT (lista med nya entiteter)
    "reason": "..."  # Eventuell orsak/motivering
}}"""

        response = ai_client.models.generate_content(
            model=model_fast,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=500
            )
        )
        
        result_text = response.text.strip()
        result = parse_llm_json(result_text)
        
        if not result:
            LOGGER.warning(f"Kunde inte parsa LLM-svar för adjustment: {user_input}")
            return {"action": "REVIEW", "reason": "Kunde inte tolka input"}
        
        # Validera action
        valid_actions = ["RENAME", "REMAP", "ALIAS", "RELATE", "SPLIT"]
        if result.get("action") not in valid_actions:
            result["action"] = "REVIEW"
        
        return result
        
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Kunde inte tolka user adjustment: {e}")
        return {"action": "REVIEW", "reason": f"Fel vid tolkning: {e}"}


def run_interactive_review(review_list: list[ReviewObject], taxonomy: dict = None) -> dict:
    """
    Interaktiv granskning av entiteter med tydlig display och likhetsanalys.
    
    Args:
        review_list: Lista med ReviewObject som behöver granskas
        taxonomy: Taxonomi-dict för att kolla om entiteten redan finns exakt
        
    Returns:
        Dict med alla användarbeslut
    """
    if not review_list:
        return {}
    
    # Ladda config
    try:
        config = _load_config()
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Kunde inte ladda config: {e}")
        return {}
    
    # Ladda taxonomy om den inte skickades in
    if taxonomy is None:
        try:
            taxonomy_path = os.path.expanduser(config['paths']['taxonomy_file'])
            if os.path.exists(taxonomy_path):
                try:
                    with open(taxonomy_path, 'r', encoding='utf-8') as f:
                        taxonomy = json.load(f)
                except Exception as e:
                    LOGGER.error(f"HARDFAIL: Kunde inte läsa taxonomy från {taxonomy_path}: {e}")
                    taxonomy = {}
            else:
                LOGGER.warning(f"Taxonomy-fil saknas: {taxonomy_path}")
                taxonomy = {}
        except KeyError as e:
            LOGGER.error(f"HARDFAIL: taxonomy_file saknas i config paths: {e}")
            taxonomy = {}
    
    # Ladda graf för autokomplet
    graph = None
    try:
        graph_db_path = os.path.expanduser(config['paths']['graph_db'])
        graph = GraphStore(graph_db_path, read_only=True)
        # Sätt upp autokomplet en gång för hela sessionen
        _setup_autocomplete(taxonomy, graph)
    except Exception as e:
        LOGGER.warning(f"Kunde inte ladda graf för autokomplet: {e}")
        # Fortsätt utan autokomplet
    
    decisions = {}
    total = len(review_list)
    
    for idx, review_obj in enumerate(review_list, 1):
        entity = review_obj.entity_name
        master_node = review_obj.master_node
        similarity_score = review_obj.similarity_score
        suggested_action = review_obj.suggested_action
        closest_match = review_obj.closest_match
        
        # Automatisk hoppa över om entiteten redan finns i någon masternod
        # (den är redan reviewad och behöver inte visas igen)
        exists, existing_master_node = _entity_exists_anywhere(entity, taxonomy, graph)
        if exists:
            # Entiteten finns redan - hoppa över den helt
            decisions[entity] = {
                "decision": "APPROVED",
                "master_node": existing_master_node or master_node,
                "similarity_score": similarity_score
            }
            print(f"✅ \"{entity}\" automatiskt godkänd (finns redan i {existing_master_node or master_node})")
            continue
        
        # Automatisk godkännande för entiteter med mycket hög likhet
        if similarity_score >= 0.95 and suggested_action == "APPROVE":
            decisions[entity] = {
                "decision": "APPROVED",
                "master_node": master_node,
                "similarity_score": similarity_score
            }
            print(f"✅ \"{entity}\" automatiskt godkänd (likhet: {similarity_score:.2f})")
            continue
        
        # Bestäm likhetsindikator
        if similarity_score >= 0.7:
            similarity_label = f"Bekräftelse - Hög likhet"
        else:
            similarity_label = f"Avvikelse - Låg likhet"
        
        # Display
        print("\n" + "═" * 60)
        print(f"Granskning {idx}/{total}")
        print("═" * 60)
        print(f"Entitet: \"{entity}\"")
        print(f"Kategori: {master_node}")
        print(f"Likhetsgrad: {similarity_score:.2f} ({similarity_label})")
        if closest_match:
            print(f"Närmaste match: \"{closest_match}\"")
        print(f"Föreslagen åtgärd: {suggested_action}")
        if review_obj.reason:
            print(f"Motivering: {review_obj.reason}")
        print()
        print("(1) Behåll  (2) Justera  (3) Kasta")
        print("═" * 60)
        
        # Vänta på användarinput
        while True:
            try:
                choice = input("Ditt val (1-3): ").strip()
                if choice in ['1', '2', '3']:
                    break
                else:
                    print("❌ Välj 1, 2 eller 3")
            except (EOFError, KeyboardInterrupt):
                print("\n⏭️ Hoppar över resterande granskningar...")
                return decisions
        
        # Hantera val
        if choice == '1':  # Behåll
            decisions[entity] = {
                "decision": "APPROVED",
                "master_node": master_node,
                "similarity_score": similarity_score
            }
            print(f"✅ \"{entity}\" godkänd")
            
        elif choice == '2':  # Justera
            # Visa hints om tillgängliga masternoder, canonicals och relationstyper
            if graph:
                master_nodes = _load_master_nodes(taxonomy)
                canonicals = _load_canonicals(graph)
                relation_types = _load_relation_types()
                
                print("\n💡 Tips: Tryck TAB för autokomplet")
                if master_nodes:
                    print(f"   Masternoder ({len(master_nodes)}): {', '.join(master_nodes[:5])}{'...' if len(master_nodes) > 5 else ''}")
                if canonicals:
                    print(f"   Canonicals ({len(canonicals)}): {', '.join(canonicals[:5])}{'...' if len(canonicals) > 5 else ''}")
                if relation_types:
                    print(f"   Relationstyper ({len(relation_types)}): {', '.join(relation_types)}")
            
            user_input = input("Vad vill du ändra? (Flytta, Byt namn, Alias, Skapa relation...): ").strip()
            if not user_input:
                print("⏭️ Ingen ändring angiven. Hoppar över...")
                continue
            
            # Anropa SSOT-parser
            adjustment = _parse_user_adjustment(user_input, entity, master_node)
            
            # Om RELATE identifieras, presentera menyn för relationstyp
            if adjustment.get("action") == "RELATE":
                target_entity = adjustment.get("target_entity")
                if target_entity:
                    relation_type = _select_relation_type(entity, master_node, target_entity)
                    adjustment["relation_type"] = relation_type
                else:
                    print("⚠️ Kunde inte identifiera måletiteten för RELATE. Hoppar över...")
                    continue
            
            decisions[entity] = {
                "decision": "ADJUSTED",
                "master_node": master_node,
                "adjustment": adjustment,
                "similarity_score": similarity_score
            }
            print(f"✅ \"{entity}\" justerad: {adjustment.get('action')}")
            
        elif choice == '3':  # Kasta
            reason = ""
            while not reason.strip():
                try:
                    reason = input("Ange orsak (KRAV - systemet måste lära sig varför): ").strip()
                    if not reason.strip():
                        print("❌ Orsak är obligatorisk. Försök igen.")
                except (EOFError, KeyboardInterrupt):
                    print("\n⏭️ Avbruten. Hoppar över...")
                    return decisions
            
            decisions[entity] = {
                "decision": "REJECTED",
                "master_node": master_node,
                "reason": reason,
                "similarity_score": similarity_score
            }
            print(f"❌ \"{entity}\" kastad: {reason}")
    
    # Stäng av autokomplet när sessionen är klar
    _disable_autocomplete()
    
    # Stäng graf-anslutning om den öppnades
    if graph:
        try:
            graph.close()
        except Exception as e:
            LOGGER.debug(f"Kunde inte stänga graf-anslutning: {e}")
    
    return decisions


def apply_review_decisions(taxonomy: dict, decisions: dict, graph: GraphStore):
    """
    Applicera användarens beslut på taxonomin och grafen.
    
    Args:
        taxonomy: Taxonomi-dict
        decisions: Dict med användarbeslut från run_interactive_review()
        graph: GraphStore-instans för att spara validation rules
    """
    for entity_name, decision_data in decisions.items():
        decision = decision_data.get("decision")
        master_node = decision_data.get("master_node")
        similarity_score = decision_data.get("similarity_score", 0.0)
        
        if decision == "APPROVED":
            # Lägg till i taxonomy om den inte redan finns
            if master_node in taxonomy:
                if entity_name not in taxonomy[master_node].get("sub_nodes", []):
                    taxonomy[master_node].setdefault("sub_nodes", []).append(entity_name)
            
            # Spara validation rule
            graph.add_validation_rule(
                entity=entity_name,
                master_node=master_node,
                decision="APPROVED",
                similarity_score=similarity_score
            )
            
        elif decision == "ADJUSTED":
            adjustment = decision_data.get("adjustment", {})
            action = adjustment.get("action")
            
            if action == "RENAME":
                new_name = adjustment.get("new_name")
                if new_name and master_node in taxonomy:
                    # Ta bort gammalt namn, lägg till nytt
                    if entity_name in taxonomy[master_node].get("sub_nodes", []):
                        taxonomy[master_node]["sub_nodes"].remove(entity_name)
                    if new_name not in taxonomy[master_node].get("sub_nodes", []):
                        taxonomy[master_node]["sub_nodes"].append(new_name)
                
                graph.add_validation_rule(
                    entity=entity_name,
                    master_node=master_node,
                    decision="ADJUSTED",
                    adjusted_name=new_name,
                    reason=adjustment.get("reason", ""),
                    similarity_score=similarity_score
                )
                
            elif action == "REMAP":
                new_master_node = adjustment.get("new_master_node")
                if new_master_node and new_master_node in taxonomy:
                    # Ta bort från gammal masternod, lägg till i ny
                    if master_node in taxonomy and entity_name in taxonomy[master_node].get("sub_nodes", []):
                        taxonomy[master_node]["sub_nodes"].remove(entity_name)
                    if entity_name not in taxonomy[new_master_node].get("sub_nodes", []):
                        taxonomy[new_master_node]["sub_nodes"].append(entity_name)
                
                graph.add_validation_rule(
                    entity=entity_name,
                    master_node=master_node,
                    decision="ADJUSTED",
                    adjusted_master_node=new_master_node,
                    reason=adjustment.get("reason", ""),
                    similarity_score=similarity_score
                )
                
            elif action == "ALIAS":
                target_entity = adjustment.get("target_entity")
                if target_entity:
                    # Lägg till alias i grafen
                    try:
                        # Hämta target-noden och uppdatera dess aliases
                        target_node = graph.get_node(target_entity)
                        if target_node:
                            aliases = target_node.get("aliases", [])
                            if entity_name not in aliases:
                                aliases.append(entity_name)
                            graph.upsert_node(
                                id=target_entity,
                                type=target_node.get("type", "Entity"),
                                aliases=aliases,
                                properties=target_node.get("properties", {})
                            )
                        else:
                            # Skapa ny nod om den inte finns
                            graph.upsert_node(
                                id=target_entity,
                                type="Entity",
                                aliases=[entity_name],
                                properties={}
                            )
                    except Exception as e:
                        LOGGER.warning(f"Kunde inte lägga till alias {entity_name} -> {target_entity}: {e}")
                
                graph.add_validation_rule(
                    entity=entity_name,
                    master_node=master_node,
                    decision="ADJUSTED",
                    reason=adjustment.get("reason", ""),
                    similarity_score=similarity_score
                )
                
            elif action == "RELATE":
                target_entity = adjustment.get("target_entity")
                relation_type = adjustment.get("relation_type")
                if target_entity and relation_type:
                    # Skapa edge i grafen
                    try:
                        graph.upsert_edge(
                            source=entity_name,
                            target=target_entity,
                            edge_type=relation_type
                        )
                    except Exception as e:
                        LOGGER.warning(f"Kunde inte skapa relation {entity_name} -[{relation_type}]-> {target_entity}: {e}")
                
                graph.add_validation_rule(
                    entity=entity_name,
                    master_node=master_node,
                    decision="ADJUSTED",
                    reason=adjustment.get("reason", ""),
                    similarity_score=similarity_score
                )
                
            elif action == "SPLIT":
                split_entities = adjustment.get("split_entities", [])
                for new_entity in split_entities:
                    if master_node in taxonomy:
                        if new_entity not in taxonomy[master_node].get("sub_nodes", []):
                            taxonomy[master_node]["sub_nodes"].append(new_entity)
                
                graph.add_validation_rule(
                    entity=entity_name,
                    master_node=master_node,
                    decision="ADJUSTED",
                    reason=adjustment.get("reason", ""),
                    similarity_score=similarity_score
                )
        
        elif decision == "REJECTED":
            reason = decision_data.get("reason", "")
            graph.add_validation_rule(
                entity=entity_name,
                master_node=master_node,
                decision="REJECTED",
                reason=reason,
                similarity_score=similarity_score
            )
    
    # Deduplicera och sortera taxonomy
    for master_node, data in taxonomy.items():
        if "sub_nodes" in data:
            data["sub_nodes"] = sorted(list(set(data["sub_nodes"])))