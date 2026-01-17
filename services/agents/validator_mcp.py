import os
import sys
import yaml
import json
import uuid
from pathlib import Path

import logging
# 1. Konfigurera loggning till stderr OMEDELBART
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format='%(levelname)s:%(name)s:%(message)s'
)

# 2. Tysta alla existerande loggers som kan ha skapats
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

# Add the project root to sys.path so 'services' can be found
project_root = str(Path(__file__).parent.parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from google.genai import types

from mcp.server.fastmcp import FastMCP
from services.utils.schema_validator import SchemaValidator
from services.utils.json_parser import parse_llm_json
from services.utils.llm_service import LLMService, TaskType

mcp = FastMCP("DigitalistValidator")
validator = SchemaValidator()

# Load config for validation settings
def _load_config():
    config_path = os.path.join(project_root, 'config', 'my_mem_config.yaml')
    try:
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logging.warning(f"Could not load config: {e}")
        return {}

_CONFIG = _load_config()

# Centralized LLM access via LLMService
_llm_service = None

def _get_llm_service():
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service

@mcp.tool()
def validate_extraction(data: dict) -> str:
    """
    Manuellt verktyg för att validera en JSON-struktur direkt mot schemat.
    Bra för felsökning i MCP Inspector.
    """
    errors = []
    nodes = data.get("nodes", [])
    for i, node in enumerate(nodes):
        is_valid, msg = validator.validate_node(node)
        if not is_valid:
            node_name = node.get('name', f"Index {i}")
            errors.append(f"Node '{node_name}': {msg}")

    if not errors:
        return "VALID"
    return "VALIDATION_ERROR:\n" + "\n".join(errors)

@mcp.tool()
def extract_and_validate_doc(initial_prompt: str, reference_timestamp: str = None, anchors: dict = None) -> dict:
    """
    Huvudverktyg som exekverar en färdig prompt, validerar svaret mot schemat,
    och loopar internt tills validering lyckas.

    anchors: Dict[str, str] = Mappning { "Namn": "UUID" } för kända entiteter som SKA återanvändas.
    """
    llm = _get_llm_service()
    if not llm.client:
        return {"error": "Server configuration error: No LLM client available"}

    # Fallback för timestamp om den inte skickas
    if not reference_timestamp:
        import datetime
        reference_timestamp = datetime.datetime.now().isoformat()

    # Normalisera anchors
    anchor_map = anchors or {}

    # Max attempts from config (default 15)
    max_attempts = _CONFIG.get('validation', {}).get('entity_extraction_max_attempts', 15)
    current_messages = [
        types.Content(role="user", parts=[types.Part.from_text(text=initial_prompt)])
    ]

    model = llm.models.get('lite')

    for attempt in range(max_attempts):
        try:
            response = llm.client.models.generate_content(
                model=model,
                contents=current_messages,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            
            # ANVÄND ROBUST PARSER
            extracted_data = parse_llm_json(response.text, context="validator_mcp")
            errors = []
            
            # --- AUTO-FIX: Inject System Fields & Anchors ---
            # Vi hjälper LLM med fält den inte kan veta eller ofta glömmer
            for node in extracted_data.get('nodes', []):
                # 1. System fields
                if reference_timestamp:
                    if 'last_seen_at' not in node: node['last_seen_at'] = reference_timestamp
                    if 'created_at' not in node: node['created_at'] = reference_timestamp
                    if 'last_synced_at' not in node: node['last_synced_at'] = reference_timestamp
                
                if 'status' not in node:
                    node['status'] = 'PROVISIONAL'
                if 'confidence' not in node:
                    node['confidence'] = 0.5

                # Användnings- och underhållsräknare (initialiseras vid skapande)
                if 'last_retrieved_at' not in node:
                    node['last_retrieved_at'] = reference_timestamp
                if 'retrieved_times' not in node:
                    node['retrieved_times'] = 0
                if 'last_refined_at' not in node:
                    node['last_refined_at'] = "never"

                # 2. Fixa ID (UUID) om det saknas (Krävs av schemat)
                if 'id' not in node:
                    if 'uuid' in node:
                        node['id'] = node['uuid']
                    else:
                        new_id = str(uuid.uuid4())
                        node['id'] = new_id
                        # Sätt även uuid-fältet om det saknas, för konsekvens
                        node['uuid'] = new_id

                # 3. Anchors (Kända entiteter)
                name = node.get('name')
                if name and name in anchor_map:
                    known_uuid = anchor_map[name]
                    current_uuid = node.get('id') # Använd 'id' som primärnyckel

                    # Om id/uuid genererades nyss, skriv över med anchor
                    if current_uuid != known_uuid:
                         # Här kan vi antingen tvinga (auto-fix) eller klaga.
                         # Givet att vi nyss genererade ett random ID, bör vi skriva över det med anchor.
                         node['id'] = known_uuid
                         node['uuid'] = known_uuid # Legacy support

                # 4. Normalize node_context to schema format [{text, origin}]
                # LLM returns string or [{text}], we need [{text, origin}]
                nc = node.get('node_context')
                if nc is not None:
                    if isinstance(nc, str):
                        node['node_context'] = [{"text": nc, "origin": "PENDING"}]
                    elif isinstance(nc, list):
                        normalized = []
                        for item in nc:
                            if isinstance(item, str):
                                normalized.append({"text": item, "origin": "PENDING"})
                            elif isinstance(item, dict):
                                if 'origin' not in item:
                                    item['origin'] = "PENDING"
                                normalized.append(item)
                        node['node_context'] = normalized

            # Validera noder via SchemaValidator 
            for i, node in enumerate(extracted_data.get('nodes', [])):
                is_valid, msg = validator.validate_node(node)
                if not is_valid:
                    errors.append(f"Node {i} ('{node.get('name')}'): {msg}")
            
            # Validera kanter via SchemaValidator (NYTT)
            nodes_map = {n.get('name'): n.get('type') for n in extracted_data.get('nodes', [])}
            for i, edge in enumerate(extracted_data.get('edges', [])):
                is_valid, msg = validator.validate_edge(edge, nodes_map)
                if not is_valid:
                    errors.append(f"Edge {i} ('{edge.get('source')} -> {edge.get('target')}'): {msg}")

            if not errors:
                return extracted_data 
            
            # Feedback-loop
            logging.info(f"Attempt {attempt+1} failed validation. Errors:\n{chr(10).join(errors)}")
            current_messages.append(types.Content(role="model", parts=[types.Part.from_text(text=response.text)]))
            current_messages.append(types.Content(
                role="user", 
                parts=[types.Part.from_text(text=f"VALIDERING MISSLYCKADES:\n{chr(10).join(errors)}\n\nKorrigera JSON och försök igen.")]
            ))
            
        except Exception as e:
            logging.error(f"Error in LLM loop: {e}")
            current_messages.append(types.Content(role="user", parts=[types.Part.from_text(text=f"Ogiltig JSON eller systemfel: {str(e)}. Försök igen.")]))

    return {"error": "Max retries reached", "partial": extracted_data if 'extracted_data' in locals() else {}}

if __name__ == "__main__":
    try:
        mcp.run()
    except Exception as e:
        logging.critical(f"MCP Server CRASHED: {e}")
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
