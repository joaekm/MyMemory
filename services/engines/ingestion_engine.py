#!/usr/bin/env python3
"""
Ingestion Engine (v12.0)

Integrates normalized data into the knowledge system.
Phase 2 of the pipeline: Collect & Normalize -> INGESTION -> Dreaming

Responsibilities:
- Generate semantic metadata (summary, keywords)
- Extract entities via MCP
- Resolve entities against existing graph
- Write to Lake, Graph, Vector
"""

import os
import sys
import time
import yaml
import logging
import datetime
import threading
import re
import uuid
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from services.utils.json_parser import parse_llm_json
from services.utils.llm_service import LLMService, TaskType
from services.utils.graph_service import GraphService
from services.utils.schema_validator import SchemaValidator, normalize_value
from services.processors.text_extractor import extract_text
from services.utils.shared_lock import resource_lock

try:
    from services.utils.date_service import get_timestamp as date_service_timestamp
except ImportError:
    sys.stderr.write("[INFO] date_service not available, using datetime.now()\n")
    date_service_timestamp = lambda x: datetime.datetime.now()


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
            with open(p, 'r') as f:
                main_conf = yaml.safe_load(f)
            for k, v in main_conf.get('paths', {}).items():
                main_conf['paths'][k] = os.path.expanduser(v)

            config_dir = os.path.dirname(p)
            prompts_conf = {}
            for name in ['services_prompts.yaml', 'service_prompts.yaml']:
                pp = os.path.join(config_dir, name)
                if os.path.exists(pp):
                    with open(pp, 'r') as f:
                        prompts_conf = yaml.safe_load(f)
                    break

            return main_conf, prompts_conf

    raise FileNotFoundError("HARDFAIL: Config missing")


CONFIG, PROMPTS_RAW = _load_config()


def get_prompt(agent: str, key: str) -> str:
    """Get prompt from config."""
    if 'prompts' in PROMPTS_RAW:
        return PROMPTS_RAW['prompts'].get(agent, {}).get(key)
    return PROMPTS_RAW.get(agent, {}).get(key)


# Settings
LAKE_STORE = os.path.expanduser(CONFIG['paths']['lake_store'])
FAILED_FOLDER = os.path.expanduser(CONFIG['paths']['asset_failed'])
GRAPH_DB_PATH = os.path.expanduser(CONFIG['paths']['graph_db'])

# Dreamer daemon state file (OBJEKT-76)
DREAMER_STATE_FILE = os.path.expanduser(
    CONFIG.get('dreamer', {}).get('daemon', {}).get(
        'state_file', '~/MyMemory/Index/.dreamer_state.json'
    )
)

# LLMService singleton (lazy init)
_LLM_SERVICE = None

def _get_llm_service():
    global _LLM_SERVICE
    if _LLM_SERVICE is None:
        _LLM_SERVICE = LLMService()
    return _LLM_SERVICE

# Processing limits from config
PROCESSING_CONFIG = CONFIG.get('processing', {})
SUMMARY_MAX_CHARS = PROCESSING_CONFIG.get('summary_max_chars', 30000)
HEADER_SCAN_CHARS = PROCESSING_CONFIG.get('header_scan_chars', 3000)

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - INGESTION - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('IngestionEngine')

UUID_SUFFIX_PATTERN = re.compile(
    r'_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(txt|md|pdf|docx|csv|xlsx)$',
    re.IGNORECASE
)
STANDARD_TIMESTAMP_PATTERN = re.compile(r'^DATUM_TID:\s+(.+)$', re.MULTILINE)
TRANSCRIBER_DATE_PATTERN = re.compile(r'^DATUM:\s+(\d{4}-\d{2}-\d{2})$', re.MULTILINE)
TRANSCRIBER_START_PATTERN = re.compile(r'^START:\s+(\d{2}:\d{2})$', re.MULTILINE)

PROCESSED_FILES = set()
PROCESS_LOCK = threading.Lock()

# Dreamer state lock (OBJEKT-76)
DREAMER_STATE_LOCK = threading.Lock()


def _increment_dreamer_node_counter(nodes_added: int):
    """
    Increment the Dreamer daemon node counter (OBJEKT-76).

    This signals to the daemon that new graph nodes have been created,
    allowing threshold-based triggering of Dreamer resolution cycles.
    """
    if nodes_added <= 0:
        return

    import json

    with DREAMER_STATE_LOCK:
        try:
            # Load existing state
            state = {'nodes_since_last_run': 0, 'last_run_timestamp': None}
            if os.path.exists(DREAMER_STATE_FILE):
                with open(DREAMER_STATE_FILE, 'r') as f:
                    state = json.load(f)

            # Increment counter
            state['nodes_since_last_run'] = state.get('nodes_since_last_run', 0) + nodes_added

            # Save state
            os.makedirs(os.path.dirname(DREAMER_STATE_FILE), exist_ok=True)
            with open(DREAMER_STATE_FILE, 'w') as f:
                json.dump(state, f, indent=2, default=str)

            LOGGER.debug(f"Dreamer counter: +{nodes_added} -> {state['nodes_since_last_run']} total")

        except (OSError, json.JSONDecodeError) as e:
            LOGGER.error(f"HARDFAIL: Could not update Dreamer state: {e}")
            raise RuntimeError(f"Failed to update Dreamer counter: {e}") from e


def reset_dreamer_counter():
    """
    Reset the Dreamer daemon node counter to zero.

    Called by rebuild orchestrator to prevent daemon from triggering
    during rebuild (since orchestrator runs Dreamer manually after each day).
    """
    import json

    with DREAMER_STATE_LOCK:
        try:
            state = {'nodes_since_last_run': 0, 'last_run_timestamp': None}
            if os.path.exists(DREAMER_STATE_FILE):
                with open(DREAMER_STATE_FILE, 'r') as f:
                    state = json.load(f)

            state['nodes_since_last_run'] = 0

            os.makedirs(os.path.dirname(DREAMER_STATE_FILE), exist_ok=True)
            with open(DREAMER_STATE_FILE, 'w') as f:
                json.dump(state, f, indent=2, default=str)

            LOGGER.info("Dreamer counter reset to 0")

        except (OSError, json.JSONDecodeError) as e:
            LOGGER.warning(f"Could not reset Dreamer state: {e}")


# Global Schema Validator (Lazy load)
_SCHEMA_VALIDATOR = None


def _get_schema_validator():
    global _SCHEMA_VALIDATOR
    if _SCHEMA_VALIDATOR is None:
        try:
            _SCHEMA_VALIDATOR = SchemaValidator()
        except Exception as e:
            LOGGER.error(f"Could not load SchemaValidator: {e}")
            raise
    return _SCHEMA_VALIDATOR


# MCP Server Configuration
VALIDATOR_PARAMS = StdioServerParameters(
    command=sys.executable,
    args=[os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "agents", "validator_mcp.py"))]
)


def extract_content_date(text: str) -> str:
    """
    Extract timestamp_content - when the content actually happened.

    Strict extraction without fallbacks:
    1. DATUM_TID header (from collectors: Slack, Calendar, Gmail) -> ISO string
    2. Transcriber format DATUM + START -> combined to ISO string
    3. Otherwise -> "UNKNOWN"

    Returns:
        ISO format string or "UNKNOWN"
    """
    header_section = text[:HEADER_SCAN_CHARS]

    # 1. Try DATUM_TID (collectors: Slack, Calendar, Gmail)
    match = STANDARD_TIMESTAMP_PATTERN.search(header_section)
    if match:
        ts_str = match.group(1).strip()
        try:
            dt = datetime.datetime.fromisoformat(ts_str)
            LOGGER.debug(f"extract_content_date: DATUM_TID -> {dt.isoformat()}")
            return dt.isoformat()
        except ValueError:
            LOGGER.warning(f"extract_content_date: Invalid DATUM_TID '{ts_str}'")

    # 2. Try Transcriber format (DATUM + START)
    date_match = TRANSCRIBER_DATE_PATTERN.search(header_section)
    start_match = TRANSCRIBER_START_PATTERN.search(header_section)

    if date_match and start_match:
        date_str = date_match.group(1)
        time_str = start_match.group(1)
        try:
            dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            LOGGER.debug(f"extract_content_date: Transcriber -> {dt.isoformat()}")
            return dt.isoformat()
        except ValueError:
            LOGGER.warning(f"extract_content_date: Invalid Transcriber format '{date_str} {time_str}'")
    elif date_match:
        date_str = date_match.group(1)
        try:
            dt = datetime.datetime.strptime(f"{date_str} 12:00", "%Y-%m-%d %H:%M")
            LOGGER.debug(f"extract_content_date: Transcriber (date only) -> {dt.isoformat()}")
            return dt.isoformat()
        except ValueError as e:
            LOGGER.debug(f"extract_content_date: Could not parse date '{date_str}': {e}")

    # 3. No source found
    LOGGER.info("extract_content_date: No date source -> UNKNOWN")
    return "UNKNOWN"


def generate_semantic_metadata(text: str, resolved_entities: List[Dict] = None) -> Dict[str, Any]:
    """
    Generate summary and keywords using a lightweight model.
    Used for vector indexing and searchability.

    If resolved_entities is provided, canonical names are injected into
    the prompt for better relations_summary quality.
    """
    prompt_template = get_prompt('doc_converter', 'doc_summary_prompt')
    if not prompt_template:
        LOGGER.warning("Summary prompt missing in config, returning empty metadata")
        return {"context_summary": "", "relations_summary": "", "document_keywords": []}

    # Bygg entity-kontext om tillgänglig
    entity_context = ""
    if resolved_entities:
        entity_lines = []
        for e in resolved_entities:
            if e.get("action") in ["LINK", "CREATE"]:
                canonical = e.get("canonical_name", e.get("label", ""))
                etype = e.get("type", "Unknown")
                if canonical:
                    entity_lines.append(f"- {canonical} ({etype})")
        if entity_lines:
            entity_context = "\n\nKÄNDA ENTITETER I DOKUMENTET:\n" + "\n".join(entity_lines)

    # Injicera entity-kontext i slutet av texten
    safe_text = text[:SUMMARY_MAX_CHARS] + entity_context
    final_prompt = prompt_template.format(text=safe_text)

    llm = _get_llm_service()
    response = llm.generate(final_prompt, TaskType.ENRICHMENT)

    if not response.success:
        LOGGER.warning(f"Semantic metadata generation failed: {response.error}")
        return {"context_summary": "", "relations_summary": "", "document_keywords": [], "ai_model": "FAILED"}

    data = parse_llm_json(response.text)
    return {
        "context_summary": data.get("context_summary", ""),
        "relations_summary": data.get("relations_summary", ""),
        "document_keywords": data.get("document_keywords", []) or data.get("keywords", []),
        "ai_model": response.model
    }


async def _call_mcp_validator(initial_prompt: str, reference_timestamp: str, anchors: dict = None):
    """Internal async helper to communicate with MCP server."""
    async with stdio_client(VALIDATOR_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool(
                "extract_and_validate_doc",
                arguments={
                    "initial_prompt": initial_prompt,
                    "reference_timestamp": reference_timestamp,
                    "anchors": anchors or {}
                }
            )
            return result.content[0].text if result.content else "{}"


def extract_entities_mcp(text: str, source_hint: str = "") -> Dict[str, Any]:
    """
    Extract entities via MCP server.
    Builds the prompt (order), MCP executes and validates.
    """
    LOGGER.info(f"Preparing MCP prompt for {source_hint}...")

    raw_prompt = get_prompt('doc_converter', 'strict_entity_extraction')
    if not raw_prompt:
        return {"nodes": [], "edges": []}

    validator = _get_schema_validator()
    schema = validator.schema

    # --- SCHEMA CONTEXT ---
    all_node_types = set(schema.get('nodes', {}).keys())
    valid_graph_nodes = all_node_types - {'Document', 'Source', 'File'}

    filtered_nodes = {k: v for k, v in schema.get('nodes', {}).items() if k not in {'Document'}}
    node_lines = []
    for k, v in filtered_nodes.items():
        desc = v.get('description', '')
        props = v.get('properties', {})

        prop_info = []
        for prop_name, prop_def in props.items():
            if prop_name in ['id', 'created_at', 'last_synced_at', 'last_seen_at', 'confidence', 'status',
                             'source_system', 'distinguishing_context', 'uuid', 'version']:
                continue

            req_marker = "*" if prop_def.get('required') else ""

            if 'values' in prop_def:
                enums = ", ".join(prop_def['values'])
                prop_info.append(f"{prop_name}{req_marker} [{enums}]")
            else:
                p_type = prop_def.get('type', 'string')
                prop_info.append(f"{prop_name}{req_marker} ({p_type})")

        constraints = []
        if 'name' in props and props['name'].get('description'):
            constraints.append(f"Name rules: {props['name']['description']}")

        info = f"- {k}: {desc}"
        if prop_info:
            info += f" | Properties: {', '.join(prop_info)}"
        if constraints:
            info += f" ({'; '.join(constraints)})"

        node_lines.append(info)
    node_types_str = "\n".join(node_lines)

    filtered_edges = {k: v for k, v in schema.get('edges', {}).items() if k != 'MENTIONS'}
    edge_names = list(filtered_edges.keys())
    whitelist, blacklist = [], []

    for k, v in filtered_edges.items():
        desc = v.get('description', '')
        sources = set(v.get('source_type', []))
        targets = set(v.get('target_type', []))
        whitelist.append(f"- {k}: [{', '.join(sources)}] -> [{', '.join(targets)}]  // {desc}")

        forbidden_sources = valid_graph_nodes - sources
        forbidden_targets = valid_graph_nodes - targets
        if forbidden_sources:
            blacklist.append(f"- {k}: NEVER starts from [{', '.join(forbidden_sources)}]")
        if forbidden_targets:
            blacklist.append(f"- {k}: NEVER points to [{', '.join(forbidden_targets)}]")

    edge_types_str = (
        f"ALLOWED RELATION NAMES:\n[{', '.join(edge_names)}]\n\n"
        f"ALLOWED CONNECTIONS (WHITELIST):\n" + "\n".join(whitelist) + "\n\n"
        f"FORBIDDEN CONNECTIONS (BLACKLIST - AUTO-GENERATED):\n" + "\n".join(blacklist)
    )

    source_context_instruction = ""
    if "Slack" in source_hint:
        source_context_instruction = "CONTEXT: This is a Slack chat. Format is often 'Name: Message'. Treat senders as strong Person candidates."
    elif "Mail" in source_hint:
        source_context_instruction = "CONTEXT: This is an email. Sender (From) and recipients (To) are important Person nodes."

    final_prompt = raw_prompt.format(
        text_chunk=text[:25000],
        node_types_context=node_types_str,
        edge_types_context=edge_types_str,
        known_entities_context=source_context_instruction
    )

    try:
        reference_timestamp = datetime.datetime.now().isoformat()
        anchors = {}
        response_json = asyncio.run(_call_mcp_validator(final_prompt, reference_timestamp, anchors))
        return parse_llm_json(response_json)
    except Exception as e:
        LOGGER.error(f"HARDFAIL: MCP Extraction failed: {e}")
        raise RuntimeError(f"MCP Entity Extraction failed: {e}") from e


def critic_filter_entities(nodes: List[Dict]) -> List[Dict]:
    """
    LLM-baserad filtrering av extraherade entiteter.
    Returnerar endast godkända noder.

    Fallback: Returnerar alla noder om prompt saknas eller LLM misslyckas.
    """
    import json

    if not nodes:
        return []

    # Förbered för granskning
    entities_for_review = [
        {"name": n.get("name"), "type": n.get("type"), "confidence": n.get("confidence", 0.5)}
        for n in nodes if n.get("name") and n.get("type")
    ]

    if not entities_for_review:
        return []

    prompt_template = get_prompt('doc_converter', 'entity_critic')
    if not prompt_template:
        LOGGER.warning("entity_critic prompt saknas - hoppar över Critic-steget")
        return nodes  # Fallback: returnera alla

    prompt = prompt_template.format(
        entities_json=json.dumps(entities_for_review, indent=2, ensure_ascii=False)
    )

    llm = _get_llm_service()
    response = llm.generate(prompt, TaskType.VALIDATION)

    if not response.success:
        LOGGER.error(f"Critic LLM failed: {response.error}")
        return nodes  # Fallback vid fel

    result = parse_llm_json(response.text)
    approved_names = {e["name"] for e in result.get("approved", [])}

    # Logga statistik
    rejected = result.get("rejected", [])
    if rejected:
        LOGGER.info(f"Critic: {len(approved_names)} godkända, {len(rejected)} avvisade")
        for rej in rejected[:5]:  # Logga max 5 avvisade
            LOGGER.debug(f"  Avvisad: {rej.get('name')} ({rej.get('type')}) - {rej.get('reason', 'N/A')}")

    # Filtrera original-noder baserat på godkända namn
    return [n for n in nodes if n.get("name") in approved_names]


def resolve_entities(nodes: List[Dict], edges: List[Dict], source_type: str, filename: str) -> List[Dict]:
    """
    Resolve extracted entities against existing graph.
    Returns list of mentions (actions to perform).

    Includes canonical_name for each entity:
    - LINK: canonical_name from graph (the authoritative name)
    - CREATE: canonical_name = input name
    """
    mentions = []
    name_to_uuid = {}
    name_to_canonical = {}  # Maps input name -> canonical name

    # Check if database exists before opening in read-only mode
    # After hard reset, graph is empty - all entities will be CREATE
    if not os.path.exists(GRAPH_DB_PATH):
        LOGGER.info(f"Graph DB not found at {GRAPH_DB_PATH}, all entities will be CREATE")
        graph = None
    else:
        graph = GraphService(GRAPH_DB_PATH, read_only=True)
    seen_candidates = set()

    for node in nodes:
        name = node.get('name')
        type_str = node.get('type')
        confidence = node.get('confidence', 0.5)
        # Extract text from node_context (validator_mcp normalizes to [{text, origin}])
        nc = node.get('node_context', '')
        if isinstance(nc, list) and nc and isinstance(nc[0], dict):
            node_context_text = nc[0].get('text', '')
        else:
            node_context_text = normalize_value(nc, 'string') or ''

        if not name or not type_str:
            continue

        # Noise filter based on confidence
        if confidence < 0.3:
            continue

        key = f"{name}|{type_str}"
        if key in seen_candidates:
            continue
        seen_candidates.add(key)

        # Boost confidence for trusted sources
        if source_type in ["Slack Log", "Email Thread"]:
            confidence = max(confidence, 0.8)

        # Entity resolution: LINK if exists, CREATE if new
        existing_uuid = None
        if graph is not None:
            existing_uuid = graph.find_node_by_name(type_str, name, fuzzy=True)

        if existing_uuid:
            action = "LINK"
            target_uuid = existing_uuid
            # Hämta kanoniskt namn från grafen
            existing_node = graph.get_node(existing_uuid)
            canonical_name = existing_node.get("name", name) if existing_node else name
        else:
            action = "CREATE"
            target_uuid = str(uuid.uuid4())
            canonical_name = name  # Vid CREATE = input-namn

        name_to_uuid[name] = target_uuid
        name_to_canonical[name] = canonical_name

        mentions.append({
            "action": action,
            "target_uuid": target_uuid,
            "type": type_str,
            "label": name,
            "canonical_name": canonical_name,
            "node_context_text": node_context_text,
            "confidence": confidence
        })

    if graph is not None:
        graph.close()

    # Handle relations - use canonical names for source_text
    for edge in edges:
        source_name = edge.get('source')
        target_name = edge.get('target')
        rel_type = edge.get('type')
        rel_conf = edge.get('confidence', 0.5)

        if source_name in name_to_uuid and target_name in name_to_uuid:
            source_uuid = name_to_uuid[source_name]
            target_uuid = name_to_uuid[target_name]
            # Använd canonical names i source_text
            source_canonical = name_to_canonical.get(source_name, source_name)
            target_canonical = name_to_canonical.get(target_name, target_name)

            mentions.append({
                "action": "CREATE_EDGE",
                "source_uuid": source_uuid,
                "target_uuid": target_uuid,
                "edge_type": rel_type,
                "confidence": rel_conf,
                "source_text": f"{source_canonical} -> {target_canonical}"
            })

    return mentions


def write_lake(unit_id: str, filename: str, raw_text: str, source_type: str,
               semantic_metadata: Dict, ingestion_payload: List) -> str:
    """Write document to Lake with frontmatter."""
    base_name = os.path.splitext(filename)[0]
    lake_file = os.path.join(LAKE_STORE, f"{base_name}.md")

    timestamp_content = extract_content_date(raw_text)
    default_access_level = CONFIG.get('security', {}).get('default_access_level', 5)

    frontmatter = {
        "unit_id": unit_id,
        "source_ref": lake_file,
        "original_filename": filename,
        "timestamp_ingestion": datetime.datetime.now().isoformat(),
        "timestamp_content": timestamp_content,
        "timestamp_updated": None,
        "source_type": source_type,
        "access_level": default_access_level,
        "context_summary": semantic_metadata.get("context_summary", ""),
        "relations_summary": semantic_metadata.get("relations_summary", ""),
        "document_keywords": semantic_metadata.get("document_keywords", []),
        "ai_model": semantic_metadata.get("ai_model", "unknown"),
    }

    fm_str = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True)
    with open(lake_file, 'w', encoding='utf-8') as f:
        f.write(f"---\n{fm_str}---\n\n# {filename}\n\n{raw_text}")

    LOGGER.info(f"Lake: {filename} ({source_type}) -> {len(ingestion_payload)} mentions")
    return lake_file


def write_graph(unit_id: str, filename: str, ingestion_payload: List) -> tuple:
    """Write entities and edges to graph."""
    graph = GraphService(GRAPH_DB_PATH)

    nodes_written = 0
    edges_written = 0

    for entity in ingestion_payload:
        action = entity.get("action")

        if action in ["CREATE", "LINK"]:
            target_uuid = entity.get("target_uuid")
            node_type = entity.get("type")
            label = entity.get("label", "")
            confidence = entity.get("confidence", 0.5)
            node_context_text = entity.get("node_context_text", "")

            if not target_uuid or not node_type:
                continue

            node_context_entry = {
                "text": node_context_text or f"Mentioned in {filename}",
                "origin": unit_id
            }

            props = {
                "name": label,
                "status": "PROVISIONAL",
                "confidence": confidence,
                "node_context": [node_context_entry],
                "source_system": "IngestionEngine"
            }

            graph.upsert_node(
                id=target_uuid,
                type=node_type,
                properties=props
            )
            nodes_written += 1

            graph.upsert_edge(
                source=unit_id,
                target=target_uuid,
                edge_type="MENTIONS",
                properties={"confidence": confidence}
            )
            edges_written += 1

        elif action == "CREATE_EDGE":
            source_uuid = entity.get("source_uuid")
            target_uuid = entity.get("target_uuid")
            edge_type = entity.get("edge_type")
            edge_conf = entity.get("confidence", 0.5)

            if source_uuid and target_uuid and edge_type:
                graph.upsert_edge(
                    source=source_uuid,
                    target=target_uuid,
                    edge_type=edge_type,
                    properties={"confidence": edge_conf}
                )
                edges_written += 1

    LOGGER.info(f"Graph: {filename} -> {nodes_written} nodes, {edges_written} edges")
    return nodes_written, edges_written


def write_vector(unit_id: str, filename: str, raw_text: str, source_type: str,
                 semantic_metadata: Dict, timestamp_ingestion: str):
    """Write document to vector index."""
    from services.utils.vector_service import get_vector_service
    vector_service = get_vector_service("knowledge_base")

    ctx_summary = semantic_metadata.get("context_summary", "")
    rel_summary = semantic_metadata.get("relations_summary", "")

    vector_text = f"FILENAME: {filename}\nSUMMARY: {ctx_summary}\nRELATIONS: {rel_summary}\n\nCONTENT:\n{raw_text[:8000]}"

    vector_service.upsert(
        id=unit_id,
        text=vector_text,
        metadata={
            "timestamp": timestamp_ingestion,
            "filename": filename,
            "source_type": source_type
        }
    )
    LOGGER.info(f"Vector: {filename} -> ChromaDB")


def process_document(filepath: str, filename: str, _lock_held: bool = False):
    """
    Main document processing function.
    Orchestrates the full ingestion pipeline.

    Args:
        filepath: Full path to source file
        filename: Filename (used for UUID extraction)
        _lock_held: If True, caller already holds resource locks (e.g., rebuild).
                    If False, this function acquires locks per document.
    """
    with PROCESS_LOCK:
        if filename in PROCESSED_FILES:
            return
        PROCESSED_FILES.add(filename)

    match = UUID_SUFFIX_PATTERN.search(filename)
    if not match:
        return
    unit_id = match.group(1)
    base_name = os.path.splitext(filename)[0]
    lake_file = os.path.join(LAKE_STORE, f"{base_name}.md")

    if os.path.exists(lake_file):
        return  # Idempotent

    LOGGER.debug(f"Processing: {filename}")

    def _do_process():
        """Inner processing logic."""
        # 1. Extract text (via text_extractor)
        raw_text = extract_text(filepath)

        if not raw_text or len(raw_text) < 10:
            LOGGER.debug(f"File {filename} appears incomplete ({len(raw_text) if raw_text else 0} chars). Waiting for on_modified.")
            with PROCESS_LOCK:
                PROCESSED_FILES.discard(filename)
            return

        # 2. Determine source type
        source_type = "Document"
        if "slack" in filepath.lower():
            source_type = "Slack Log"
        elif "mail" in filepath.lower():
            source_type = "Email Thread"
        elif "calendar" in filepath.lower():
            source_type = "Calendar Event"

        # 3. Extract entities via MCP
        entity_data = extract_entities_mcp(raw_text, source_hint=source_type)
        nodes = entity_data.get('nodes', [])
        edges = entity_data.get('edges', [])

        # 4. Critic-filtrering (LLM filtrerar brus)
        filtered_nodes = critic_filter_entities(nodes)
        LOGGER.info(f"Critic: {len(nodes)} → {len(filtered_nodes)} noder")

        # 5. Resolve entities against graph (returnerar canonical_name)
        ingestion_payload = resolve_entities(filtered_nodes, edges, source_type, filename)

        # 6. Generate semantic metadata MED entity-kontext (FLYTTAD hit)
        semantic_metadata = generate_semantic_metadata(raw_text, ingestion_payload)

        # 7. Write to Lake
        write_lake(unit_id, filename, raw_text, source_type, semantic_metadata, ingestion_payload)

        # 8. Write to Graph
        nodes_written, edges_written = write_graph(unit_id, filename, ingestion_payload)

        # 8b. Update Dreamer daemon counter (OBJEKT-76)
        _increment_dreamer_node_counter(nodes_written)

        # 9. Write to Vector
        timestamp_ingestion = datetime.datetime.now().isoformat()
        write_vector(unit_id, filename, raw_text, source_type, semantic_metadata, timestamp_ingestion)

    try:
        if _lock_held:
            # Caller holds locks (rebuild scenario)
            _do_process()
        else:
            # Acquire locks for this document (realtime scenario)
            with resource_lock("graph", exclusive=True):
                with resource_lock("vector", exclusive=True):
                    _do_process()

    except Exception as e:
        LOGGER.error(f"HARDFAIL {filename}: {e}")
        with PROCESS_LOCK:
            PROCESSED_FILES.discard(filename)
        raise RuntimeError(f"HARDFAIL: Document processing failed for {filename}: {e}") from e


class DocumentHandler:
    """Watchdog event handler for new documents."""

    def on_created(self, event):
        if event.is_directory:
            return
        process_document(event.src_path, os.path.basename(event.src_path))


# --- INIT & WATCHDOG ---
if __name__ == "__main__":
    os.makedirs(LAKE_STORE, exist_ok=True)
    print("IngestionEngine (v12.0) online.")

    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    class WatchdogHandler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory:
                return
            process_document(event.src_path, os.path.basename(event.src_path))

    folders = [
        CONFIG['paths']['asset_documents'],
        CONFIG['paths']['asset_slack'],
        CONFIG.get('paths', {}).get('asset_mail'),
        CONFIG['paths']['asset_transcripts']
    ]

    with ThreadPoolExecutor(max_workers=5) as executor:
        for folder in folders:
            if folder and os.path.exists(folder):
                for f in os.listdir(folder):
                    if UUID_SUFFIX_PATTERN.search(f):
                        executor.submit(process_document, os.path.join(folder, f), f)

    observer = Observer()
    for folder in folders:
        if folder and os.path.exists(folder):
            observer.schedule(WatchdogHandler(), folder, recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
