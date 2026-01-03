import os
import sys
import yaml
from pathlib import Path

import logging
# 1. Konfigurera loggning till stderr OMEDELBART
# Detta måste ske innan SchemaValidator instansieras
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
# This assumes validator_mcp.py is in services/agents/
project_root = str(Path(__file__).parent.parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from mcp.server.fastmcp import FastMCP
from services.utils.schema_validator import SchemaValidator
from google import genai



mcp = FastMCP("DigitalistValidator")
validator = SchemaValidator()

def get_api_key():
    # Vi återanvänder logiken från SchemaValidator för att hitta config-sökvägen
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    config_path = os.path.join(base_dir, "config", "my_mem_config.yaml")
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            return config.get('ai_engine', {}).get('api_key')
    except Exception as e:
        print(f"Kunde inte ladda API-nyckel från config: {e}")
        return None

# --- LADDA PROMPTAR ---
def load_prompts():
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    prompt_path = os.path.join(base_dir, "config", "services_prompts.yaml")
    with open(prompt_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

PROMPTS = load_prompts()

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
def extract_and_validate_doc(raw_text: str, source_hint: str = "Document") -> dict:
    """
    Huvudverktyg som bygger kontext från schema, extraherar data via LLM 
    och loopar internt tills SchemaValidator ger grönt ljus.
    """
    # 1. Hämta grundprompt och schema-regler
    prompts = load_prompts()
    raw_prompt = prompts['doc_converter']['strict_entity_extraction']
    schema = validator.schema
    
    # --- DYNAMISK SCHEMA-KONTEXT (Flyttat från DocConverter) ---
    all_node_types = set(schema.get('nodes', {}).keys())
    valid_graph_nodes = all_node_types - {'Document', 'Source', 'File'}
    
    # Bygg nod-beskrivningar
    node_lines = []
    for k, v in schema.get('nodes', {}).items():
        if k == 'Document': continue
        desc = v.get('description', '')
        props = v.get('properties', {})
        constraints = [f"Namnregler: {props['name']['description']}"] if 'name' in props else []
        node_lines.append(f"- {k}: {desc} ({'; '.join(constraints)})")
    
    node_types_str = "\n".join(node_lines)

    # Bygg Relationer (White/Blacklists)
    filtered_edges = {k: v for k, v in schema.get('edges', {}).items() if k != 'MENTIONS'}
    whitelist, blacklist = [], []

    for k, v in filtered_edges.items():
        sources = set(v.get('source_type', []))
        targets = set(v.get('target_type', []))
        whitelist.append(f"- {k}: [{', '.join(sources)}] -> [{', '.join(targets)}] // {v.get('description', '')}")
        
        forbidden_src = valid_graph_nodes - sources
        forbidden_trg = valid_graph_nodes - targets
        if forbidden_src: blacklist.append(f"- {k}: ALDRIG från [{', '.join(forbidden_src)}]")
        if forbidden_trg: blacklist.append(f"- {k}: ALDRIG till [{', '.join(forbidden_trg)}]")

    edge_types_str = "WHITELIST:\n" + "\n".join(whitelist) + "\n\nBLACKLIST:\n" + "\n".join(blacklist)

    # 2. Anpassa efter Source Hint
    source_context = ""
    if "Slack" in source_hint:
        source_context = "KONTEXT: Slack-chatt. Extrahera deltagare som Person-noder."
    elif "Email" in source_hint or "Mail" in source_hint:
        source_context = "KONTEXT: E-post. Fokusera på avsändare/mottagare."

    # 3. Preparera loopen
    final_prompt = raw_prompt.format(
        text_chunk=raw_text[:25000], 
        node_types_context=node_types_str,
        edge_types_context=edge_types_str,
        known_entities_context=source_context
    )

    max_attempts = 10
    current_messages = [{"role": "user", "content": final_prompt}]

    for attempt in range(max_attempts):
        response = client.models.generate_content(
            model="gemini-2.0-flash-lite-preview",
            contents=current_messages,
            config={'response_mime_type': 'application/json'}
        )
        
        try:
            extracted_data = json.loads(response.text)
            errors = []
            
            # Validera noder via SchemaValidator 
            for i, node in enumerate(extracted_data.get('nodes', [])):
                is_valid, msg = validator.validate_node(node)
                if not is_valid:
                    errors.append(f"Node {i} ('{node.get('name')}'): {msg}")
            
            if not errors:
                return extracted_data 
            
            # Feedback-loop
            current_messages.append({"role": "model", "content": response.text})
            current_messages.append({
                "role": "user", 
                "content": f"VALIDERING MISSLYCKADES:\n{chr(10).join(errors)}\n\nKorrigera JSON och försök igen."
            })
            
        except Exception as e:
            current_messages.append({"role": "user", "content": f"Ogiltig JSON: {str(e)}. Försök igen."})
    return {"error": "Max retries reached", "partial": extracted_data}
