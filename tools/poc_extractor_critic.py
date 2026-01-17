#!/usr/bin/env python3
"""
POC: Extractor + Critic Pattern för Entity Extraction

Syfte: Testa hypotesen att ett tvåstegs-LLM-anrop (Extractor föreslår,
Critic filtrerar) minskar brus i entity-extraktion.

NY FEATURE: Simulerar hela ingestion-flödet och visar vad som skulle
skapats i Lake, Graf och Vektor - med kanoniska namn från Gatekeeper.

VIKTIGT: Denna POC påverkar INTE befintliga filer eller databaser.
All output går till stdout/fil för analys.

Användning:
    python tools/poc_extractor_critic.py                    # Kör på testdokument
    python tools/poc_extractor_critic.py --file <path>      # Kör på specifik fil
    python tools/poc_extractor_critic.py --compare          # Jämför med/utan Critic
    python tools/poc_extractor_critic.py --full-pipeline    # Simulera hela flödet
"""

import os
import sys
import json
import yaml
import argparse
from datetime import datetime
from typing import Dict, Any, List, Optional

# Lägg till projektroten
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import difflib
import uuid

from google import genai
from google.genai import types
from services.utils.json_parser import parse_llm_json
from services.utils.schema_validator import SchemaValidator
from services.utils.graph_service import GraphStore

# --- CONFIG ---
def _load_config():
    config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'my_mem_config.yaml')
    prompts_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'services_prompts.yaml')

    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    with open(prompts_path, 'r') as f:
        prompts = yaml.safe_load(f)

    return config, prompts

CONFIG, PROMPTS = _load_config()
API_KEY = CONFIG['ai_engine']['api_key']
MODEL_LITE = CONFIG.get('ai_engine', {}).get('models', {}).get('model_lite', 'gemini-2.0-flash-lite')
MODEL_PRO = CONFIG.get('ai_engine', {}).get('models', {}).get('model_pro', 'gemini-2.0-flash')

CLIENT = genai.Client(api_key=API_KEY)


# =============================================================================
# STEG 1: EXTRACTOR (Återanvänder befintlig logik från doc_converter)
# =============================================================================

def build_extraction_prompt(text: str, source_hint: str = "") -> str:
    """
    Bygger extraktions-prompten baserat på schema.
    Kopierad logik från doc_converter.strict_entity_extraction_mcp()
    """
    raw_prompt = PROMPTS.get('doc_converter', {}).get('strict_entity_extraction')
    if not raw_prompt:
        raise ValueError("HARDFAIL: strict_entity_extraction prompt saknas i config")

    validator = SchemaValidator()
    schema = validator.schema

    # Bygg nodtyp-kontext
    all_node_types = set(schema.get('nodes', {}).keys())
    valid_graph_nodes = all_node_types - {'Document', 'Source', 'File'}

    filtered_nodes = {k: v for k, v in schema.get('nodes', {}).items() if k not in {'Document'}}
    node_lines = []
    for k, v in filtered_nodes.items():
        desc = v.get('description', '')
        props = v.get('properties', {})

        prop_info = []
        for prop_name, prop_def in props.items():
            if prop_name in ['id', 'created_at', 'last_synced_at', 'last_seen_at',
                           'confidence', 'status', 'source_system', 'distinguishing_context',
                           'uuid', 'version']:
                continue

            req_marker = "*" if prop_def.get('required') else ""
            if 'values' in prop_def:
                enums = ", ".join(prop_def['values'])
                prop_info.append(f"{prop_name}{req_marker} [{enums}]")
            else:
                p_type = prop_def.get('type', 'string')
                prop_info.append(f"{prop_name}{req_marker} ({p_type})")

        info = f"- {k}: {desc}"
        if prop_info:
            info += f" | Egenskaper: {', '.join(prop_info)}"
        node_lines.append(info)

    node_types_str = "\n".join(node_lines)

    # Bygg relationskontext
    filtered_edges = {k: v for k, v in schema.get('edges', {}).items() if k != 'MENTIONS'}
    edge_names = list(filtered_edges.keys())
    whitelist, blacklist = [], []

    for k, v in filtered_edges.items():
        desc = v.get('description', '')
        sources = set(v.get('source_type', []))
        targets = set(v.get('target_type', []))
        whitelist.append(f"- {k}: [{', '.join(sources)}] -> [{', '.join(targets)}]  // {desc}")

    edge_types_str = (
        f"TILLÅTNA RELATIONSNAMN:\n[{', '.join(edge_names)}]\n\n"
        f"TILLÅTNA KOPPLINGAR (WHITELIST):\n" + "\n".join(whitelist)
    )

    source_context_instruction = ""
    if "Slack" in source_hint:
        source_context_instruction = "KONTEXT: Detta är en Slack-chatt."
    elif "Mail" in source_hint:
        source_context_instruction = "KONTEXT: Detta är ett email."

    return raw_prompt.format(
        text_chunk=text[:25000],
        node_types_context=node_types_str,
        edge_types_context=edge_types_str,
        known_entities_context=source_context_instruction
    )


def run_extractor(text: str, source_hint: str = "") -> Dict[str, Any]:
    """
    Kör Extractor-steget: LLM extraherar entiteter från text.
    Returnerar rå output utan filtrering.
    """
    prompt = build_extraction_prompt(text, source_hint)

    response = CLIENT.models.generate_content(
        model=MODEL_LITE,
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )

    return parse_llm_json(response.text)


# =============================================================================
# STEG 2: CRITIC (Ny komponent)
# =============================================================================

CRITIC_PROMPT = """
Du är en strikt kvalitetsgranskare för Knowledge Graph-entiteter.

Din uppgift är att granska en lista med extraherade entiteter och FILTRERA BORT de som är:
1. **Brus** - Generiska termer, verb, adjektiv, eller meningslösa fraser
2. **Felkategoriserade** - Entiteter som har fel typ enligt definitionen
3. **Systemkomponenter** - Tekniska funktioner, kod-moduler, eller interna verktygsnamn
4. **Dubbletter** - Varianter av samma entitet

=== DEFINITIONER (Läs noga!) ===

**Project**: Ett tidsbegränsat initiativ med TYDLIGT MÅL och BESTÄLLARE.
- ✅ BRA: "Digitalist AI-PoC", "Kundprojekt Acme Q1"
- ❌ DÅLIGT: "Dashboard", "React SPA", "Versionshantering", "OBJEKT-42"

**Roles**: En FORMELL yrkestitel som någon kan ha.
- ✅ BRA: "Projektledare", "VD", "Utvecklare", "Säljchef"
- ❌ DÅLIGT: "semester", "Chat", "Påstridig Agent", "DropZone"

**Person**: En IDENTIFIERBAR fysisk individ med namn.
- ✅ BRA: "Anna Andersson", "Erik Svensson"
- ❌ DÅLIGT: "Talare 1", "användaren", "kunden"

**Organization**: En JURIDISK PERSON eller formell organisation.
- ✅ BRA: "Digitalist Sweden AB", "Skatteverket", "Google"
- ❌ DÅLIGT: "teamet", "leverantören", "kunden"

**Group**: En NAMNGIVEN organisatorisk enhet.
- ✅ BRA: "HR-avdelningen", "Team Alpha", "Ledningsgruppen"
- ❌ DÅLIGT: "mötesdeltagarna", "de som var där"

=== ENTITETER ATT GRANSKA ===
{entities_json}

=== INSTRUKTION ===
För VARJE entitet, bedöm:
1. Passar den definitionen för sin typ?
2. Är den specifik nog (inte generisk)?
3. Är den en verklig entitet (inte en systemkomponent/funktion)?

RETURNERA JSON med två listor:
{{
  "approved": [
    {{"name": "Namn", "type": "Typ", "reason": "Varför godkänd"}}
  ],
  "rejected": [
    {{"name": "Namn", "type": "Typ", "reason": "Varför avvisad"}}
  ]
}}

Var STRIKT. Om du är osäker, avvisa.
"""


# =============================================================================
# STEG 2.5: GATEKEEPER SIMULATOR (Ny - returnerar canonical_name)
# =============================================================================

class GatekeeperSimulator:
    """
    Simulerar EntityGatekeeper med canonical_name-retur.

    Skillnad mot produktion:
    - resolve_entity() returnerar ÄVEN canonical_name vid LINK
    - Detta möjliggör normalisering i semantic metadata
    """

    def __init__(self):
        graph_path = os.path.expanduser(CONFIG['paths']['graph_db'])
        self.graph = GraphStore(graph_path, read_only=True)
        self.indices = self._build_indices()
        self.schema_validator = SchemaValidator()

    def _build_indices(self) -> Dict[str, Dict[str, Dict[str, List[str]]]]:
        """Bygg lookup-index från grafen."""
        indices = {}

        # Hämta alla nodtyper vi bryr oss om
        node_types = ["Person", "Organization", "Group", "Project", "Event", "Roles"]

        for node_type in node_types:
            nodes = self.graph.find_nodes_by_type(node_type)
            if node_type not in indices:
                indices[node_type] = {"name": {}, "email": {}, "original_names": {}}

            for node in nodes:
                props = node.get('properties', {}) or {}
                name = (props.get('name') or '').strip()
                email = (props.get('email') or '').strip()
                node_uuid = node.get('id')

                if name and node_uuid:
                    name_lower = name.lower()
                    if name_lower not in indices[node_type]["name"]:
                        indices[node_type]["name"][name_lower] = []
                    indices[node_type]["name"][name_lower].append(node_uuid)
                    # Spara original-namn (för canonical) - bevara case
                    indices[node_type]["original_names"][name_lower] = name

                if email and node_uuid:
                    email_lower = email.lower()
                    if email_lower not in indices[node_type]["email"]:
                        indices[node_type]["email"][email_lower] = []
                    indices[node_type]["email"][email_lower].append(node_uuid)

        return indices

    def _fuzzy_match(self, type_str: str, value: str) -> Optional[tuple]:
        """Fuzzy-matchning som returnerar (uuid, canonical_name)."""
        if type_str not in self.indices or "name" not in self.indices[type_str]:
            return None

        value_lower = value.lower()
        candidates = list(self.indices[type_str]["name"].keys())

        matches = difflib.get_close_matches(value_lower, candidates, n=1, cutoff=0.85)

        if matches:
            matched_name_lower = matches[0]
            uuids = self.indices[type_str]["name"][matched_name_lower]
            # Hämta kanoniskt namn (med rätt case)
            canonical = self.indices[type_str].get("original_names", {}).get(matched_name_lower, value)
            return (uuids[0], canonical)

        return None

    def resolve_entity(self, type_str: str, value: str, context_props: Dict = None) -> Dict:
        """
        Matchar entitet mot grafen.

        Returns:
            Dict med action, target_uuid, confidence, och NYA fältet canonical_name
        """
        lookup_key = "name"
        if "@" in value and type_str == "Person":
            lookup_key = "email"

        # 1. Exakt matchning
        uuid_hit = None
        canonical_name = value  # Default: använd ursprungligt namn

        if type_str in self.indices and lookup_key in self.indices[type_str]:
            hits = self.indices[type_str][lookup_key].get(value.strip().lower())
            if hits and isinstance(hits, list) and len(hits) == 1:
                uuid_hit = hits[0]
                # Hämta kanoniskt namn
                if lookup_key == "name":
                    canonical_name = self.indices[type_str].get("original_names", {}).get(
                        value.strip().lower(), value
                    )

        # 2. Fuzzy matchning
        if not uuid_hit and lookup_key == "name":
            fuzzy_result = self._fuzzy_match(type_str, value)
            if fuzzy_result:
                uuid_hit, canonical_name = fuzzy_result

        if uuid_hit:
            return {
                "action": "LINK",
                "target_uuid": uuid_hit,
                "target_type": type_str,
                "confidence": 1.0,
                "source_text": value,
                "canonical_name": canonical_name  # NY!
            }

        # 3. CREATE
        new_uuid = str(uuid.uuid4())
        props = context_props.copy() if context_props else {}
        props['name'] = value
        props['status'] = 'PROVISIONAL'

        return {
            "action": "CREATE",
            "target_uuid": new_uuid,
            "target_type": type_str,
            "confidence": props.get('confidence', 0.5),
            "properties": props,
            "source_text": value,
            "canonical_name": value  # Vid CREATE = samma som input
        }


def run_critic(entities: List[Dict]) -> Dict[str, Any]:
    """
    Kör Critic-steget: LLM granskar och filtrerar entiteter.
    """
    if not entities:
        return {"approved": [], "rejected": []}

    # Förbered entiteter för granskning
    entities_for_review = [
        {"name": e.get("name"), "type": e.get("type"), "confidence": e.get("confidence", 0.5)}
        for e in entities
    ]

    prompt = CRITIC_PROMPT.format(entities_json=json.dumps(entities_for_review, indent=2, ensure_ascii=False))

    response = CLIENT.models.generate_content(
        model=MODEL_PRO,  # Använd starkare modell för kritisk granskning
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )

    return parse_llm_json(response.text)


# =============================================================================
# STEG 3: PIPELINE (Extractor -> Critic)
# =============================================================================

def run_pipeline(text: str, source_hint: str = "", use_critic: bool = True) -> Dict[str, Any]:
    """
    Kör hela pipelinen: Extractor -> (Critic) -> Resultat

    Args:
        text: Dokument-text att analysera
        source_hint: Typ av källa (Slack, Mail, etc.)
        use_critic: Om True, kör Critic-steget

    Returns:
        Dict med extractor_output, critic_output (om använd), och final_entities
    """
    result = {
        "timestamp": datetime.now().isoformat(),
        "source_hint": source_hint,
        "use_critic": use_critic,
        "text_length": len(text),
    }

    # Steg 1: Extractor
    print("  [1/2] Kör Extractor...")
    extractor_output = run_extractor(text, source_hint)
    result["extractor_output"] = extractor_output
    result["extractor_node_count"] = len(extractor_output.get("nodes", []))
    result["extractor_edge_count"] = len(extractor_output.get("edges", []))

    if not use_critic:
        result["final_entities"] = extractor_output.get("nodes", [])
        result["final_edges"] = extractor_output.get("edges", [])
        return result

    # Steg 2: Critic
    print("  [2/2] Kör Critic...")
    nodes = extractor_output.get("nodes", [])
    critic_output = run_critic(nodes)
    result["critic_output"] = critic_output

    # Bygg final lista baserat på approved
    approved_names = {e["name"] for e in critic_output.get("approved", [])}
    final_nodes = [n for n in nodes if n.get("name") in approved_names]

    result["final_entities"] = final_nodes
    result["final_node_count"] = len(final_nodes)
    result["rejected_count"] = len(critic_output.get("rejected", []))

    # Filtrera edges baserat på approved nodes
    final_edges = [
        e for e in extractor_output.get("edges", [])
        if e.get("source") in approved_names and e.get("target") in approved_names
    ]
    result["final_edges"] = final_edges
    result["final_edge_count"] = len(final_edges)

    return result


# =============================================================================
# STEG 3.5: BASELINE PIPELINE (Nuvarande produktionsflöde)
# =============================================================================

def generate_baseline_semantic_metadata(text: str) -> Dict[str, Any]:
    """
    Genererar semantic metadata UTAN injicerade entiteter (nuvarande produktionsflöde).
    Använder EXAKT samma prompt som produktionen (doc_summary_prompt från services_prompts.yaml).
    """
    # Hämta EXAKT samma prompt som produktionen använder
    prompt_template = PROMPTS.get('doc_converter', {}).get('doc_summary_prompt')
    if not prompt_template:
        raise ValueError("HARDFAIL: doc_summary_prompt saknas i services_prompts.yaml")

    prompt = prompt_template.format(text=text[:15000])
    lite_model = CONFIG.get('ai_engine', {}).get('models', {}).get('model_lite', 'gemini-2.0-flash-lite')

    response = CLIENT.models.generate_content(
        model=lite_model,
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )

    data = parse_llm_json(response.text)
    return {
        "context_summary": data.get("context_summary", ""),
        "relations_summary": data.get("relations_summary", ""),
        "document_keywords": data.get("document_keywords", []),
        "ai_model": lite_model
    }


def run_baseline_pipeline(text: str, filename: str, source_hint: str = "") -> Dict[str, Any]:
    """
    Simulerar NUVARANDE produktionsflöde (baseline):

    1. Semantic Metadata FÖRST (utan entitetskontext)
    2. Extractor (LLM extraherar entiteter)
    3. Gatekeeper (matchar mot graf) - UTAN canonical_name tillbaka

    Detta är för att jämföra med det nya flödet.
    """
    result = {
        "filename": filename,
        "timestamp": datetime.now().isoformat(),
        "source_hint": source_hint,
        "text_length": len(text),
        "pipeline_type": "BASELINE",
        "pipeline_steps": []
    }

    # --- STEG 1: Semantic Metadata FÖRST (utan entitetskontext) ---
    print("  [1/3] Semantic Metadata (baseline - utan entiteter)...")
    semantic_metadata = generate_baseline_semantic_metadata(text)
    result["pipeline_steps"].append({
        "step": "semantic_metadata",
        "summary_length": len(semantic_metadata.get("context_summary", "")),
        "keywords_count": len(semantic_metadata.get("document_keywords", []))
    })

    # --- STEG 2: Extractor (UTAN Critic) ---
    print("  [2/3] Extractor (utan Critic)...")
    extractor_output = run_extractor(text, source_hint)
    nodes = extractor_output.get("nodes", [])
    edges = extractor_output.get("edges", [])
    result["pipeline_steps"].append({
        "step": "extractor",
        "nodes_count": len(nodes),
        "edges_count": len(edges)
    })

    # --- STEG 3: Gatekeeper (utan canonical_name-normalisering) ---
    print("  [3/3] Gatekeeper...")
    gatekeeper = GatekeeperSimulator()

    resolved_entities = []
    graph_operations = {"links": [], "creates": []}

    for node in nodes:
        name = node.get("name")
        node_type = node.get("type")

        resolution = gatekeeper.resolve_entity(
            type_str=node_type,
            value=name,
            context_props={"confidence": node.get("confidence", 0.5)}
        )

        resolved_entities.append(resolution)

        if resolution["action"] == "LINK":
            graph_operations["links"].append({
                "original": name,
                "canonical": resolution["canonical_name"],
                "uuid": resolution["target_uuid"]
            })
        else:
            graph_operations["creates"].append({
                "name": name,
                "type": node_type,
                "uuid": resolution["target_uuid"]
            })

    result["pipeline_steps"].append({
        "step": "gatekeeper",
        "links": len(graph_operations["links"]),
        "creates": len(graph_operations["creates"])
    })

    # --- BYGG SIMULERAD OUTPUT (baseline) ---

    # Lake output - notera att relations_summary använder LLM:ens egna namn
    result["lake_output"] = {
        "file_path": f"~/MyMemory/Lake/{filename}.md",
        "frontmatter": {
            "unit_id": str(uuid.uuid4()),
            "original_filename": filename,
            "timestamp_ingestion": datetime.now().isoformat(),
            "source_type": source_hint or "Document",
            "context_summary": semantic_metadata.get("context_summary", ""),
            "relations_summary": semantic_metadata.get("relations_summary", ""),
            "document_keywords": semantic_metadata.get("document_keywords", []),
            "ai_model": semantic_metadata.get("ai_model", "unknown")
        }
    }

    # Graf output
    result["graph_output"] = {
        "operations": graph_operations,
        "edges": [
            {
                "source": e.get("source"),
                "target": e.get("target"),
                "type": e.get("type")
            }
            for e in edges
        ]
    }

    # Vektor output
    result["vector_output"] = {
        "document_text_length": len(text),
        "metadata_for_embedding": {
            "context_summary": semantic_metadata.get("context_summary", ""),
            "keywords": semantic_metadata.get("document_keywords", [])
        }
    }

    # Baseline har inga normaliseringar (vi visar vad SOM BORDE ha normaliserats)
    potential_normalizations = []
    for op in graph_operations["links"]:
        if op["original"] != op["canonical"]:
            potential_normalizations.append({
                "original": op["original"],
                "canonical": op["canonical"],
                "note": "MISSED - relations_summary använder fortfarande originalet"
            })

    result["missed_normalizations"] = potential_normalizations

    return result


def print_baseline_report(result: Dict[str, Any]):
    """Skriv ut rapport för baseline pipeline."""
    print("\n" + "="*70)
    print(f"BASELINE RESULTAT: {result['filename']}")
    print("="*70)

    print("\n📊 PIPELINE STEG (nuvarande ordning):")
    for step in result.get("pipeline_steps", []):
        step_name = step.get("step", "?")
        if step_name == "semantic_metadata":
            print(f"  1. Semantic: {step['summary_length']} tecken summary (UTAN entitetskontext)")
        elif step_name == "extractor":
            print(f"  2. Extractor: {step['nodes_count']} noder, {step['edges_count']} edges (utan Critic)")
        elif step_name == "gatekeeper":
            print(f"  3. Gatekeeper: {step['links']} LINK, {step['creates']} CREATE")

    # Missade normaliseringar
    missed = result.get("missed_normalizations", [])
    if missed:
        print("\n⚠️ MISSADE NORMALISERINGAR (skulle ha använts i relations_summary):")
        for m in missed:
            print(f"  '{m['original']}' borde vara '{m['canonical']}'")
    else:
        print("\n✅ Inga missade normaliseringar")

    # Lake output
    lake = result.get("lake_output", {})
    print("\n📁 LAKE OUTPUT:")
    print(f"  Fil: {lake.get('file_path', '?')}")
    fm = lake.get("frontmatter", {})
    print(f"  context_summary: {fm.get('context_summary', '')[:100]}...")
    print(f"  relations_summary: {fm.get('relations_summary', '')[:100]}...")
    print(f"  keywords: {fm.get('document_keywords', [])}")

    # Graf output
    graph = result.get("graph_output", {})
    ops = graph.get("operations", {})
    print("\n🕸️ GRAPH OUTPUT:")
    print(f"  LINKs: {len(ops.get('links', []))}")
    for link in ops.get("links", [])[:5]:
        marker = " ⚠️" if link['original'] != link['canonical'] else ""
        print(f"    • {link['original']} → {link['canonical']}{marker}")
    print(f"  CREATEs: {len(ops.get('creates', []))}")
    for create in ops.get("creates", [])[:5]:
        print(f"    • {create['name']} ({create['type']})")


# =============================================================================
# STEG 4: FULL PIPELINE MED CANONICAL NAMES (NY!)
# =============================================================================

SEMANTIC_PROMPT_WITH_ENTITIES = """
Analysera följande dokument och skapa metadata för sökbarhet.

=== DOKUMENT ===
{text}

=== KÄNDA ENTITETER I DOKUMENTET ===
Följande entiteter har redan identifierats och validerats. Använd EXAKT dessa namn
(inte varianter) när du refererar till dem i sammanfattningarna:

{entities_list}

=== INSTRUKTION ===
Returnera JSON med:
{{
  "context_summary": "2-3 meningar som beskriver dokumentets huvudsakliga innehåll och syfte.",
  "relations_summary": "1-2 meningar som beskriver relationer mellan aktörer. Använd de EXAKTA namnen ovan.",
  "document_keywords": ["nyckelord1", "nyckelord2", ...]
}}

VIKTIGT: I relations_summary, använd ENDAST namnen från listan ovan - inte varianter eller förkortningar.
"""


def generate_semantic_metadata_with_entities(text: str, canonical_entities: List[Dict]) -> Dict[str, Any]:
    """
    Genererar semantic metadata MED injicerade kanoniska entitetsnamn.

    Args:
        text: Dokumenttext
        canonical_entities: Lista med {"name": "Kanoniskt Namn", "type": "Person", ...}

    Returns:
        Dict med context_summary, relations_summary, document_keywords
    """
    # Bygg entitetslista för prompten
    if canonical_entities:
        entity_lines = []
        for e in canonical_entities:
            entity_type = e.get('target_type') or e.get('type', 'Unknown')
            entity_lines.append(f"- {e['canonical_name']} ({entity_type})")
        entities_list = "\n".join(entity_lines)
    else:
        entities_list = "(Inga entiteter identifierade)"

    prompt = SEMANTIC_PROMPT_WITH_ENTITIES.format(
        text=text[:15000],  # Begränsa textlängd
        entities_list=entities_list
    )

    lite_model = CONFIG.get('ai_engine', {}).get('models', {}).get('model_lite', 'gemini-2.0-flash-lite')

    response = CLIENT.models.generate_content(
        model=lite_model,
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )

    data = parse_llm_json(response.text)
    return {
        "context_summary": data.get("context_summary", ""),
        "relations_summary": data.get("relations_summary", ""),
        "document_keywords": data.get("document_keywords", []),
        "ai_model": lite_model
    }


def run_full_pipeline(text: str, filename: str, source_hint: str = "") -> Dict[str, Any]:
    """
    Simulerar HELA ingestion-flödet med ny ordning:

    1. Extractor (LLM extraherar entiteter)
    2. Critic (LLM filtrerar brus) [optional]
    3. Gatekeeper (matchar mot graf, returnerar canonical_name)
    4. Semantic Metadata (LLM genererar med kanoniska namn injicerade)

    Returnerar simulerad output för Lake, Graf och Vektor.
    """
    result = {
        "filename": filename,
        "timestamp": datetime.now().isoformat(),
        "source_hint": source_hint,
        "text_length": len(text),
        "pipeline_steps": []
    }

    # --- STEG 1: Extractor ---
    print("  [1/4] Extractor...")
    extractor_output = run_extractor(text, source_hint)
    nodes = extractor_output.get("nodes", [])
    edges = extractor_output.get("edges", [])
    result["pipeline_steps"].append({
        "step": "extractor",
        "nodes_count": len(nodes),
        "edges_count": len(edges)
    })

    # --- STEG 2: Critic ---
    print("  [2/4] Critic...")
    critic_output = run_critic(nodes)
    approved_names = {e["name"] for e in critic_output.get("approved", [])}
    filtered_nodes = [n for n in nodes if n.get("name") in approved_names]
    result["pipeline_steps"].append({
        "step": "critic",
        "approved": len(filtered_nodes),
        "rejected": len(critic_output.get("rejected", []))
    })

    # --- STEG 3: Gatekeeper ---
    print("  [3/4] Gatekeeper...")
    gatekeeper = GatekeeperSimulator()

    resolved_entities = []
    graph_operations = {"links": [], "creates": []}
    name_to_canonical = {}  # Mappning: LLM-namn -> kanoniskt namn

    for node in filtered_nodes:
        name = node.get("name")
        node_type = node.get("type")

        resolution = gatekeeper.resolve_entity(
            type_str=node_type,
            value=name,
            context_props={"confidence": node.get("confidence", 0.5)}
        )

        resolved_entities.append(resolution)
        name_to_canonical[name] = resolution["canonical_name"]

        if resolution["action"] == "LINK":
            graph_operations["links"].append({
                "original": name,
                "canonical": resolution["canonical_name"],
                "uuid": resolution["target_uuid"]
            })
        else:
            graph_operations["creates"].append({
                "name": name,
                "type": node_type,
                "uuid": resolution["target_uuid"]
            })

    result["pipeline_steps"].append({
        "step": "gatekeeper",
        "links": len(graph_operations["links"]),
        "creates": len(graph_operations["creates"])
    })

    # --- STEG 4: Semantic Metadata (med kanoniska namn) ---
    print("  [4/4] Semantic Metadata...")
    semantic_metadata = generate_semantic_metadata_with_entities(text, resolved_entities)
    result["pipeline_steps"].append({
        "step": "semantic_metadata",
        "summary_length": len(semantic_metadata.get("context_summary", "")),
        "keywords_count": len(semantic_metadata.get("document_keywords", []))
    })

    # --- BYGG SIMULERAD OUTPUT ---

    # Lake output (frontmatter + text)
    result["lake_output"] = {
        "file_path": f"~/MyMemory/Lake/{filename}.md",
        "frontmatter": {
            "unit_id": str(uuid.uuid4()),
            "original_filename": filename,
            "timestamp_ingestion": datetime.now().isoformat(),
            "source_type": source_hint or "Document",
            "context_summary": semantic_metadata.get("context_summary", ""),
            "relations_summary": semantic_metadata.get("relations_summary", ""),
            "document_keywords": semantic_metadata.get("document_keywords", []),
            "ai_model": semantic_metadata.get("ai_model", "unknown")
        }
    }

    # Graf output (noder och edges)
    result["graph_output"] = {
        "operations": graph_operations,
        "edges": [
            {
                "source": name_to_canonical.get(e.get("source"), e.get("source")),
                "target": name_to_canonical.get(e.get("target"), e.get("target")),
                "type": e.get("type")
            }
            for e in edges
            if e.get("source") in approved_names and e.get("target") in approved_names
        ]
    }

    # Vektor output (vad som skulle indexeras)
    result["vector_output"] = {
        "document_text_length": len(text),
        "metadata_for_embedding": {
            "context_summary": semantic_metadata.get("context_summary", ""),
            "keywords": semantic_metadata.get("document_keywords", [])
        }
    }

    # Sammanställ canonical name-normalisering
    result["canonical_normalizations"] = [
        {"original": orig, "canonical": canon}
        for orig, canon in name_to_canonical.items()
        if orig != canon
    ]

    return result


def print_full_pipeline_report(result: Dict[str, Any]):
    """Skriv ut rapport för full pipeline."""
    print("\n" + "="*70)
    print(f"FULL PIPELINE RESULTAT: {result['filename']}")
    print("="*70)

    # Pipeline steg
    print("\n📊 PIPELINE STEG:")
    for step in result.get("pipeline_steps", []):
        step_name = step.get("step", "?")
        if step_name == "extractor":
            print(f"  1. Extractor: {step['nodes_count']} noder, {step['edges_count']} edges")
        elif step_name == "critic":
            print(f"  2. Critic: {step['approved']} godkända, {step['rejected']} avvisade")
        elif step_name == "gatekeeper":
            print(f"  3. Gatekeeper: {step['links']} LINK, {step['creates']} CREATE")
        elif step_name == "semantic_metadata":
            print(f"  4. Semantic: {step['summary_length']} tecken summary, {step['keywords_count']} keywords")

    # Canonical normalizations
    normalizations = result.get("canonical_normalizations", [])
    if normalizations:
        print("\n🔄 CANONICAL NORMALIZATIONS:")
        for n in normalizations:
            print(f"  '{n['original']}' → '{n['canonical']}'")
    else:
        print("\n🔄 CANONICAL NORMALIZATIONS: (inga)")

    # Lake output
    lake = result.get("lake_output", {})
    print("\n📁 LAKE OUTPUT:")
    print(f"  Fil: {lake.get('file_path', '?')}")
    fm = lake.get("frontmatter", {})
    print(f"  context_summary: {fm.get('context_summary', '')[:100]}...")
    print(f"  relations_summary: {fm.get('relations_summary', '')[:100]}...")
    print(f"  keywords: {fm.get('document_keywords', [])}")

    # Graf output
    graph = result.get("graph_output", {})
    ops = graph.get("operations", {})
    print("\n🕸️ GRAPH OUTPUT:")
    print(f"  LINKs: {len(ops.get('links', []))}")
    for link in ops.get("links", [])[:5]:
        print(f"    • {link['original']} → {link['canonical']} ({link['uuid'][:8]}...)")
    print(f"  CREATEs: {len(ops.get('creates', []))}")
    for create in ops.get("creates", [])[:5]:
        print(f"    • {create['name']} ({create['type']})")

    edges = graph.get("edges", [])
    if edges:
        print(f"  Edges: {len(edges)}")
        for edge in edges[:3]:
            print(f"    • {edge['source']} --[{edge['type']}]--> {edge['target']}")

    # Vektor output
    vector = result.get("vector_output", {})
    print("\n🔍 VECTOR OUTPUT:")
    print(f"  Text längd: {vector.get('document_text_length', 0)} tecken")
    meta = vector.get("metadata_for_embedding", {})
    print(f"  Embedding metadata: {len(meta.get('context_summary', ''))} tecken summary")


# =============================================================================
# TESTHARNESS
# =============================================================================

def load_test_document(path: str) -> str:
    """Ladda ett testdokument från Lake."""
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Ta bort YAML frontmatter om den finns
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            return parts[2].strip()

    return content


def find_test_documents(limit: int = 3) -> List[str]:
    """Hitta testdokument från Lake."""
    lake_path = os.path.expanduser(CONFIG['paths']['lake_store'])

    if not os.path.exists(lake_path):
        print(f"VARNING: Lake-mappen finns inte: {lake_path}")
        return []

    files = []
    for f in os.listdir(lake_path):
        if f.endswith('.md') and not f.startswith('.'):
            files.append(os.path.join(lake_path, f))

    # Sortera på storlek (större filer = mer intressanta)
    files.sort(key=lambda x: os.path.getsize(x), reverse=True)

    return files[:limit]


def print_comparison_report(result: Dict[str, Any]):
    """Skriv ut en jämförelserapport."""
    print("\n" + "="*60)
    print("RESULTAT")
    print("="*60)

    print(f"\nText-längd: {result['text_length']} tecken")
    print(f"Extractor hittade: {result['extractor_node_count']} noder, {result['extractor_edge_count']} edges")

    if result.get('use_critic'):
        print(f"\nEfter Critic:")
        print(f"  - Godkända: {result['final_node_count']} noder")
        print(f"  - Avvisade: {result['rejected_count']} noder")

        reduction = 1 - (result['final_node_count'] / max(result['extractor_node_count'], 1))
        print(f"  - Reduktion: {reduction:.0%}")

        print("\n--- AVVISADE ENTITETER ---")
        for r in result.get('critic_output', {}).get('rejected', []):
            print(f"  ❌ {r['name']} ({r['type']}): {r['reason']}")

        print("\n--- GODKÄNDA ENTITETER ---")
        for a in result.get('critic_output', {}).get('approved', []):
            print(f"  ✅ {a['name']} ({a['type']})")
    else:
        print("\n--- EXTRAHERADE ENTITETER (utan Critic) ---")
        for n in result.get('final_entities', []):
            print(f"  • {n.get('name')} ({n.get('type')}, conf={n.get('confidence', '?')})")


def main():
    parser = argparse.ArgumentParser(description="POC: Extractor + Critic Pattern")
    parser.add_argument('--file', type=str, help="Specifik fil att testa")
    parser.add_argument('--compare', action='store_true', help="Jämför med/utan Critic")
    parser.add_argument('--full-pipeline', action='store_true', help="Simulera hela flödet (Lake, Graf, Vektor)")
    parser.add_argument('--baseline', action='store_true', help="Kör nuvarande produktionsflöde (baseline)")
    parser.add_argument('--compare-pipelines', action='store_true', help="Jämför baseline vs POC full-pipeline")
    parser.add_argument('--limit', type=int, default=1, help="Antal dokument att testa")
    parser.add_argument('--output', type=str, help="Spara resultat till JSON-fil")

    args = parser.parse_args()

    print("="*60)
    if args.compare_pipelines:
        print("POC: Jämförelse BASELINE vs FULL-PIPELINE")
    elif args.baseline:
        print("POC: Baseline (nuvarande produktionsflöde)")
    elif args.full_pipeline:
        print("POC: Full Pipeline Simulation (Extractor → Critic → Gatekeeper → Semantic)")
    else:
        print("POC: Extractor + Critic Pattern")
    print("="*60)

    # Hitta testdokument
    if args.file:
        test_files = [args.file]
    else:
        test_files = find_test_documents(args.limit)

    if not test_files:
        print("Inga testdokument hittades!")
        return

    all_results = []

    for filepath in test_files:
        filename = os.path.basename(filepath)
        print(f"\n📄 Testar: {filename}")

        text = load_test_document(filepath)
        if len(text) < 100:
            print(f"  Hoppar över (för kort: {len(text)} tecken)")
            continue

        # Bestäm source_hint
        source_hint = "Document"
        if "slack" in filepath.lower():
            source_hint = "Slack Log"
        elif "mail" in filepath.lower():
            source_hint = "Email Thread"

        if args.compare_pipelines:
            # Kör båda pipelines och jämför
            print("\n" + "-"*40)
            print("  [A] BASELINE (nuvarande flöde)")
            print("-"*40)
            baseline_result = run_baseline_pipeline(text, filename, source_hint)
            print_baseline_report(baseline_result)

            print("\n" + "-"*40)
            print("  [B] FULL-PIPELINE (nytt flöde)")
            print("-"*40)
            poc_result = run_full_pipeline(text, filename, source_hint)
            print_full_pipeline_report(poc_result)

            # Sammanfattning
            print("\n" + "="*70)
            print("📊 JÄMFÖRELSE SAMMANFATTNING")
            print("="*70)
            baseline_nodes = baseline_result["pipeline_steps"][1]["nodes_count"]
            poc_nodes = poc_result["pipeline_steps"][0]["nodes_count"]
            poc_approved = poc_result["pipeline_steps"][1]["approved"]
            baseline_creates = baseline_result["pipeline_steps"][2]["creates"]
            poc_creates = poc_result["pipeline_steps"][2]["creates"]

            print(f"\n  Entiteter:")
            print(f"    Baseline: {baseline_nodes} extraherade → {baseline_creates} nya noder")
            print(f"    POC:      {poc_nodes} extraherade → {poc_approved} efter Critic → {poc_creates} nya noder")

            missed = len(baseline_result.get("missed_normalizations", []))
            applied = len(poc_result.get("canonical_normalizations", []))
            print(f"\n  Normaliseringar:")
            print(f"    Baseline: {missed} missade (inte tillämpade i relations_summary)")
            print(f"    POC:      {applied} tillämpade i relations_summary")

            all_results.append({
                "file": filename,
                "baseline": baseline_result,
                "poc": poc_result
            })

        elif args.baseline:
            # Bara baseline
            result = run_baseline_pipeline(text, filename, source_hint)
            print_baseline_report(result)
            all_results.append({"file": filename, "result": result})

        elif args.full_pipeline:
            # Bara POC full-pipeline
            result = run_full_pipeline(text, filename, source_hint)
            print_full_pipeline_report(result)
            all_results.append({"file": filename, "result": result})

        elif args.compare:
            print("\n  [A] UTAN Critic:")
            result_without = run_pipeline(text, source_hint, use_critic=False)
            print_comparison_report(result_without)

            print("\n  [B] MED Critic:")
            result_with = run_pipeline(text, source_hint, use_critic=True)
            print_comparison_report(result_with)

            all_results.append({
                "file": filename,
                "without_critic": result_without,
                "with_critic": result_with
            })
        else:
            result = run_pipeline(text, source_hint, use_critic=True)
            print_comparison_report(result)
            all_results.append({"file": filename, "result": result})

    # Spara resultat om önskat
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)
        print(f"\nResultat sparat till: {args.output}")


if __name__ == "__main__":
    main()
