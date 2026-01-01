import json
import os
import logging
import yaml  # Kräver PyYAML. Fallback finns nedan om den saknas.
from typing import Dict, Any, Tuple

LOGGER = logging.getLogger(__name__)

class SchemaValidator:
    def __init__(self, schema_path: str = None):
        # 1. Om ingen sökväg ges, slå upp den i config-filen
        if not schema_path:
            self.schema_path = self._resolve_schema_path_from_config()
        else:
            self.schema_path = schema_path
            
        self.schema = self._load_and_merge_schema()
        LOGGER.info(f"SchemaValidator loaded from {self.schema_path}")

    def _resolve_schema_path_from_config(self) -> str:
        """Läser config/my_mem_config.yaml för att hitta rätt schema-fil."""
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__))) # Root: MyMemory/
        config_path = os.path.join(base_dir, "config", "my_mem_config.yaml")
        
        default_template = os.path.join(base_dir, "config", "graph_schema_template.json")

        if not os.path.exists(config_path):
            LOGGER.warning(f"Config file not found at {config_path}. Using default: {default_template}")
            return default_template

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                # Försök använda PyYAML, annars enkel parsing
                try:
                    config = yaml.safe_load(f)
                    relative_path = config.get("graph_schema")
                except NameError:
                    # Fallback om yaml inte är importerat/installerat
                    content = f.read()
                    import re
                    match = re.search(r'graph_schema:\s*["\']?([^"\']+)["\']?', content)
                    relative_path = match.group(1) if match else None

                if relative_path:
                    # Hantera om sökvägen i config är relativ till root
                    return os.path.join(base_dir, relative_path)
                    
        except Exception as e:
            LOGGER.error(f"Failed to read config file: {e}")

        return default_template

    def _load_and_merge_schema(self) -> Dict[str, Any]:
        """Laddar JSON-filen och slår ihop 'base_properties' med noder."""
        if not os.path.exists(self.schema_path):
            raise FileNotFoundError(f"Schema file not found at {self.schema_path}")

        try:
            with open(self.schema_path, 'r', encoding='utf-8') as f:
                raw_schema = json.load(f)
        except json.JSONDecodeError as e:
            LOGGER.error(f"Invalid JSON in schema file: {e}")
            raise

        # Hämta base properties
        base_props = raw_schema.get("base_properties", {}).get("properties", {})
        
        # Merge logic
        nodes = raw_schema.get("nodes", {})
        for node_name, node_def in nodes.items():
            merged_props = base_props.copy()
            merged_props.update(node_def.get("properties", {}))
            node_def["properties"] = merged_props
            
            # Healing policy fallback
            if "healing_policy" not in node_def:
                node_def["healing_policy"] = {
                    "max_days_as_provisional": 90,
                    "min_connections_to_survive": 2
                }

        raw_schema["nodes"] = nodes
        return raw_schema

    def validate_node(self, node_data: Dict[str, Any]) -> Tuple[bool, str]:
        # ... (Samma valideringslogik som tidigare) ...
        node_type = node_data.get("type")
        if not node_type: return False, "Missing field: 'type'"
        
        if node_type not in self.schema["nodes"]:
            return False, f"Unknown node type: '{node_type}'"

        for field in ["confidence", "last_seen_at", "status"]:
            if field not in node_data:
                return False, f"Missing system field: '{field}'"

        status = node_data.get("status")
        if status not in ["VERIFIED", "PROVISIONAL"]:
            return False, f"Invalid status: '{status}'"

        return True, "OK"

    def get_healing_policy(self, node_type: str) -> Dict[str, Any]:
        return self.schema["nodes"].get(node_type, {}).get("healing_policy", {})