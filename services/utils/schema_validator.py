"""
Schema Validator
----------------
Central valideringsmodul som säkerställer att all data som skrivs till grafen
följer det strikta schemat (config/graph_schema_template.json).

Principer:
1. HARDFAIL > Silent Fallback: Felaktig data ska avvisas, inte gissas.
2. Config som Sanning: Schemat definierar sanningen.
3. Strip Unknown: Okända fält rensas bort.

Användning:
    validator = SchemaValidator()
    is_valid, data, error = validator.validate_node("Person", props, "Slack")
"""

import json
import re
import os
import logging
import yaml
from datetime import datetime
from typing import Tuple, Dict, Any, Optional

LOGGER = logging.getLogger('SchemaValidator')

class SchemaValidator:
    """
    Validerar noder och relationer mot JSON-schemat.
    """
    
    def __init__(self):
        self.config = self._load_config()
        self.schema = self._load_schema()
        
        # Cacha patterns för prestanda
        self._compiled_regexes = {}
        
    def _load_config(self) -> dict:
        """Ladda huvudkonfigurationen."""
        # Försök hitta config-filen relativt denna fil
        current_dir = os.path.dirname(os.path.abspath(__file__))
        # services/utils/ -> config/my_mem_config.yaml
        possible_paths = [
            os.path.join(current_dir, '..', '..', 'config', 'my_mem_config.yaml'),
            os.path.join(current_dir, '..', 'config', 'my_mem_config.yaml'),
            'config/my_mem_config.yaml'
        ]
        
        for path in possible_paths:
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        return yaml.safe_load(f)
                except Exception as e:
                    LOGGER.error(f"HARDFAIL: Kunde inte läsa config vid {path}: {e}")
                    raise RuntimeError(f"Config load failed: {e}")
                    
        LOGGER.error("HARDFAIL: Config-fil hittades inte")
        raise FileNotFoundError("config/my_mem_config.yaml missing")

    def _load_schema(self) -> dict:
        """Ladda och parsa graf-schemat."""
        try:
            schema_rel_path = self.config.get('paths', {}).get('graph_schema')
            if not schema_rel_path:
                # Fallback om path saknas i config (migration safety)
                schema_rel_path = "config/graph_schema_template.json"
                LOGGER.warning("graph_schema saknas i config, använder default")

            # Expandera ~ om det finns
            if schema_rel_path.startswith('~'):
                schema_path = os.path.expanduser(schema_rel_path)
            else:
                # Antag relativt workspace root om inte absolut
                if os.path.isabs(schema_rel_path):
                    schema_path = schema_rel_path
                else:
                    # Hitta workspace root (där config-mappen ligger)
                    # Vi vet att config laddades, så vi kan gissa root relativt config-filen vi hittade?
                    # Enklare: försök hitta filen
                    current_dir = os.path.dirname(os.path.abspath(__file__))
                    schema_path = os.path.abspath(os.path.join(current_dir, '..', '..', schema_rel_path))
            
            if not os.path.exists(schema_path):
                 # Prova direkt path
                if os.path.exists(schema_rel_path):
                    schema_path = schema_rel_path
                else:
                    LOGGER.error(f"HARDFAIL: Schema-fil saknas: {schema_path}")
                    raise FileNotFoundError(f"Schema missing: {schema_path}")

            with open(schema_path, 'r', encoding='utf-8') as f:
                schema = json.load(f)
                LOGGER.info(f"Schema laddat: {schema.get('meta', {}).get('version', 'Unknown')}")
                return schema
                
        except Exception as e:
            LOGGER.error(f"HARDFAIL: Kunde inte ladda schema: {e}")
            raise RuntimeError(f"Schema load failed: {e}")

    def validate_node(self, node_type: str, properties: Dict[str, Any], source_system: str) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
            """
            Validera en nod mot schemat.
            
            Returns:
                (is_valid, validated_data, error_message)
            """
            # Steg 1: Kontrollera nodtyp
            node_def = self.schema.get('nodes', {}).get(node_type)
            if not node_def:
                return False, None, f"Unknown node type: {node_type}"

            # Förbered scheman
            schema_props = node_def.get('properties', {})
            base_props = self.schema.get('base_properties', {}).get('properties', {})
            all_allowed_props = {**base_props, **schema_props}
            
            # Extrahera status tidigt för att styra käll-validering
            # Default till PROVISIONAL om det saknas (enligt schema-default, men här är vi explicita)
            node_status = properties.get('status', 'PROVISIONAL')
            
            # Steg 2: Kontrollera källa med "Self-Healing Logic"
            allowed_sources = node_def.get('allowed_sources', [])
            is_trusted_source = source_system in allowed_sources
            
            if not is_trusted_source:
                # UNTRUSTED SOURCE (t.ex. DocConverter)
                if node_status == 'VERIFIED':
                    return False, None, f"Source '{source_system}' is NOT allowed to create VERIFIED nodes of type {node_type}. Must be PROVISIONAL."
                elif node_status == 'PROVISIONAL':
                    # Tillåt untrusted source att skapa PROVISIONAL noder
                    pass
                else:
                    return False, None, f"Invalid status '{node_status}' for untrusted source."
            else:
                # TRUSTED SOURCE (t.ex. Slack, AD)
                # Får skapa både VERIFIED och PROVISIONAL
                pass

            # Steg 3: Validera properties
            validated_data = {}
            
            # Loopa igenom INPUT properties (för att STRIPPA okända)
            for key, value in properties.items():
                if key not in all_allowed_props:
                    # STRIP unknown
                    continue
                    
                prop_def = all_allowed_props[key]
                
                # Validera Typ & Värde
                is_valid_prop, safe_val, err = self._validate_property(key, value, prop_def)
                if not is_valid_prop:
                    return False, None, f"Property '{key}' invalid: {err}"
                
                validated_data[key] = safe_val

            # Kontrollera REQUIRED fields (från schemat)
            for key, prop_def in all_allowed_props.items():
                if prop_def.get('required', False):
                    # Specialfall: status har en default i vår logik ovan, men om den saknas i input
                    # och vi inte lagt till den i validated_data än:
                    if key == 'status' and key not in validated_data:
                        validated_data['status'] = 'PROVISIONAL'
                        continue

                    if key not in validated_data or validated_data[key] in (None, ""):
                        return False, None, f"Missing required property: {key}"

            return True, validated_data, None

    def validate_edge(self, edge_type: str, source_type: str, target_type: str) -> Tuple[bool, Optional[str]]:
        """
        Validera att en relation är tillåten mellan två nodtyper.
        
        Returns:
            (is_valid, error_message)
        """
        edge_def = self.schema.get('edges', {}).get(edge_type)
        if not edge_def:
            return False, f"Unknown edge type: {edge_type}"
            
        if source_type not in edge_def.get('source_type', []):
            return False, f"Invalid source type '{source_type}' for edge '{edge_type}'. Expected: {edge_def['source_type']}"
            
        if target_type not in edge_def.get('target_type', []):
            return False, f"Invalid target type '{target_type}' for edge '{edge_type}'. Expected: {edge_def['target_type']}"
            
        return True, None

    def get_lookup_key(self, node_type: str) -> Optional[str]:
        """Hämta Natural Key field för en nodtyp."""
        node_def = self.schema.get('nodes', {}).get(node_type)
        if node_def:
            return node_def.get('lookup_key')
        return None

    def _validate_property(self, key: str, value: Any, prop_def: dict) -> Tuple[bool, Any, Optional[str]]:
        """Intern helper för att validera ett enskilt värde."""
        expected_type = prop_def.get('type')
        
        # Typhantering
        if expected_type == 'string':
            if not isinstance(value, str):
                return False, None, f"Expected string, got {type(value)}"
            # Regex validering
            regex_pattern = prop_def.get('regex')
            if regex_pattern:
                if key not in self._compiled_regexes:
                    self._compiled_regexes[key] = re.compile(regex_pattern)
                if not self._compiled_regexes[key].match(value):
                    return False, None, f"Value '{value}' does not match regex '{regex_pattern}'"
        
        elif expected_type == 'integer':
            if not isinstance(value, int):
                # Försök konvertera sträng till int om det ser ut som en siffra?
                # Strikt schema säger nej, men praktiskt kanske?
                # Vi kör strikt.
                return False, None, f"Expected integer, got {type(value)}"
                
        elif expected_type == 'float' or expected_type == 'number':
            if not isinstance(value, (float, int)):
                return False, None, f"Expected number, got {type(value)}"
            
            # Min/Max checks
            if 'min' in prop_def and value < prop_def['min']:
                return False, None, f"Value {value} < min {prop_def['min']}"
            if 'max' in prop_def and value > prop_def['max']:
                return False, None, f"Value {value} > max {prop_def['max']}"

        elif expected_type == 'boolean':
            if not isinstance(value, bool):
                 return False, None, f"Expected boolean, got {type(value)}"

        elif expected_type == 'list':
            if not isinstance(value, list):
                return False, None, f"Expected list, got {type(value)}"
        
        elif expected_type == 'enum':
            allowed_values = prop_def.get('values', [])
            if value not in allowed_values:
                return False, None, f"Value '{value}' not in enum {allowed_values}"

        elif expected_type == 'timestamp' or expected_type == 'date':
            # Förväntar sig ISO-sträng
            if not isinstance(value, str):
                 return False, None, f"Expected ISO string for {expected_type}, got {type(value)}"
            try:
                # Basic ISO check (datetime.fromisoformat är tillgänglig i 3.7+)
                # Hanterar 'YYYY-MM-DD' eller 'YYYY-MM-DDTHH:MM:SS'
                datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                return False, None, f"Invalid ISO format: '{value}'"

        elif expected_type == 'uuid':
             if not isinstance(value, str):
                 return False, None, f"Expected UUID string, got {type(value)}"
             # Enkel UUID-koll
             uuid_regex = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
             if not re.match(uuid_regex, value.lower()):
                  return False, None, f"Invalid UUID format: '{value}'"

        return True, value, None




