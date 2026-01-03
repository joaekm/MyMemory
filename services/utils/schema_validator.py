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
        node_type = node_data.get("type")
        if not node_type: return False, "Missing field: 'type'"
        
        node_def = self.schema["nodes"].get(node_type)
        if not node_def:
            return False, f"Unknown node type: '{node_type}'"

        for field in ["confidence", "last_seen_at", "status", "context_keywords"]:
            if field not in node_data:
                return False, f"Missing system field: '{field}'"

        status = node_data.get("status")
        if status not in ["VERIFIED", "PROVISIONAL"]:
            return False, f"Invalid status: '{status}'"

        # Check name quality
        node_name = node_data.get('name', '')
        if not node_name: return False, "Missing 'name' field"
        
        # Rule: Name cannot be identical to Type (lazy LLM)
        if node_name.lower().replace(" ", "").replace("_", "") == node_type.lower().replace(" ", "").replace("_", ""):
             return False, f"Node name '{node_name}' is too generic (same as type '{node_type}'). Please provide a specific name."

        # Validate Properties against Schema
        properties_def = node_def.get("properties", {})
        for prop_name, prop_def in properties_def.items():
            # 1. Required Check
            if prop_def.get("required", False) and prop_name not in node_data:
                return False, f"Missing required property: '{prop_name}'"
            
            # 2. Enum Check
            if prop_name in node_data:
                value = node_data[prop_name]
                allowed_values = prop_def.get("values") # 'values' används i schemat för enums
                if allowed_values and value not in allowed_values:
                    return False, f"Invalid value for '{prop_name}': '{value}'. Allowed: {allowed_values}"

        return True, "OK"

    def get_healing_policy(self, node_type: str) -> Dict[str, Any]:
        return self.schema["nodes"].get(node_type, {}).get("healing_policy", {})

    def validate_edge(self, edge: Dict[str, Any], nodes_map: Dict[str, str]) -> Tuple[bool, str]:
        """
        Validerar en kant mot schemat.
        nodes_map: {node_name: node_type} - Mappning av nodnamn till typ för uppslagning.
        """
        rel_type = edge.get('type')
        source = edge.get('source')
        target = edge.get('target')

        if not rel_type or not source or not target:
            return False, "Malformed edge: missing type, source, or target"

        # Kolla att relationstypen finns
        edge_def = self.schema.get('edges', {}).get(rel_type)
        if not edge_def:
            return False, f"Unknown relation type: '{rel_type}'"

        # Kolla att noder finns i extraktionen
        s_type = nodes_map.get(source)
        t_type = nodes_map.get(target)

        if not s_type: return False, f"Source node '{source}' not found in extraction"
        if not t_type: return False, f"Target node '{target}' not found in extraction"

        # Validera riktning
        allowed_sources = edge_def.get('source_type', [])
        allowed_targets = edge_def.get('target_type', [])

        if s_type not in allowed_sources:
            return False, f"Invalid source type '{s_type}' for relation '{rel_type}'. Allowed: {allowed_sources}"
        
        if t_type not in allowed_targets:
            return False, f"Invalid target type '{t_type}' for relation '{rel_type}'. Allowed: {allowed_targets}"

        return True, "OK"