#!/usr/bin/env python3
"""
test_property_chain.py - End-to-end regressionstest för property-kedjan

╔══════════════════════════════════════════════════════════════════════════════╗
║  OBJEKT-63: Rigorös Metadata-testkedja                                       ║
║                                                                              ║
║  Testar att properties propagerar korrekt genom hela pipelinen:              ║
║  DocConverter → Lake → VectorIndexer → Dreamer → Graf                        ║
║                                                                              ║
║  HARDFAIL om någon property tappas eller läcker in odefinierat.              ║
╚══════════════════════════════════════════════════════════════════════════════╝

Användning:
    python tools/test_property_chain.py              # Kör fullständigt test
    python tools/test_property_chain.py --dry-run   # Visa vad som skulle testas
    python tools/test_property_chain.py --keep      # Behåll test-data efter körning
"""

import os
import sys
import json
import yaml
import uuid
import shutil
import argparse
import tempfile
from pathlib import Path
from typing import Dict, Set, List, Tuple, Any
from datetime import datetime

# Lägg till projektroten för imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# === SCHEMA LOADERS ===

def load_graph_schema() -> Dict[str, Any]:
    """Laddar graph_schema_template.json"""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    schema_path = os.path.join(base_dir, "config", "graph_schema_template.json")

    if not os.path.exists(schema_path):
        raise FileNotFoundError(f"HARDFAIL: Graf-schema saknas: {schema_path}")

    with open(schema_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_lake_schema() -> Dict[str, Any]:
    """Laddar lake_metadata_template.json"""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    schema_path = os.path.join(base_dir, "config", "lake_metadata_template.json")

    if not os.path.exists(schema_path):
        raise FileNotFoundError(f"HARDFAIL: Lake-schema saknas: {schema_path}")

    with open(schema_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_config() -> Dict[str, Any]:
    """Laddar huvudconfig för sökvägar"""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(base_dir, "config", "my_mem_config.yaml")

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"HARDFAIL: Config saknas: {config_path}")

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    for k, v in config.get('paths', {}).items():
        if isinstance(v, str):
            config['paths'][k] = os.path.expanduser(v)

    return config


# === SCHEMA HELPERS ===

def get_required_lake_properties(lake_schema: Dict) -> Set[str]:
    """Hämtar alla required Lake-properties"""
    required = set()
    for section in ['base_properties', 'semantic_properties']:
        for prop_name, prop_def in lake_schema.get(section, {}).get('properties', {}).items():
            if prop_def.get('required', False):
                required.add(prop_name)
    return required


def get_vector_properties(lake_schema: Dict, graph_schema: Dict) -> Tuple[Set[str], Set[str], Dict[str, str]]:
    """
    Hämtar properties som ska finnas i vektor-metadata.

    Returns:
        (lake_vector_props, graph_vector_props, key_mappings)
    """
    lake_props = set()
    graph_props = set()
    key_mappings = {}

    # Lake properties med include_in_vector=true
    for section in ['base_properties', 'semantic_properties']:
        for prop_name, prop_def in lake_schema.get(section, {}).get('properties', {}).items():
            if prop_def.get('include_in_vector', False):
                vector_key = prop_def.get('vector_key', prop_name)
                lake_props.add(vector_key)
                if vector_key != prop_name:
                    key_mappings[prop_name] = vector_key

    # Graf base_properties med include_in_vector=true
    for prop_name, prop_def in graph_schema.get('base_properties', {}).get('properties', {}).items():
        if prop_def.get('include_in_vector', False):
            vector_key = prop_def.get('vector_key', prop_name)
            graph_props.add(vector_key)
            if vector_key != prop_name:
                key_mappings[prop_name] = vector_key

    return lake_props, graph_props, key_mappings


def get_required_graph_base_properties(graph_schema: Dict) -> Set[str]:
    """Hämtar required base_properties för graf-noder"""
    required = set()
    for prop_name, prop_def in graph_schema.get('base_properties', {}).get('properties', {}).items():
        if prop_def.get('required', False):
            required.add(prop_name)
    return required


# === TEST DATA ===

TEST_DOCUMENT_CONTENT = """
DATUM_TID: 2026-01-15T10:00:00+01:00

# Test Document for Property Chain Validation

This is a test document created by test_property_chain.py.

It mentions a person named Test Person who works at Test Organization.
The project discussed is called Test Project.

This content is designed to trigger entity extraction and test the full pipeline.
"""


# === TEST STEPS ===

class PropertyChainTest:
    """End-to-end test för property chain"""

    def __init__(self, config: Dict, lake_schema: Dict, graph_schema: Dict, keep_data: bool = False):
        self.config = config
        self.lake_schema = lake_schema
        self.graph_schema = graph_schema
        self.keep_data = keep_data

        self.test_uuid = str(uuid.uuid4())
        self.test_filename = f"_test_property_chain_{self.test_uuid}.txt"
        self.violations = []
        self.info = []

        # Paths
        self.asset_path = None
        self.lake_path = None

    def log_info(self, msg: str):
        self.info.append(msg)
        print(f"  [INFO] {msg}")

    def log_violation(self, step: str, msg: str):
        self.violations.append({"step": step, "message": msg})
        print(f"  [FAIL] {step}: {msg}")

    def log_pass(self, step: str, msg: str):
        print(f"  [PASS] {step}: {msg}")

    # --- STEP 1: Create test file in Assets ---
    def step1_create_test_file(self) -> bool:
        """Skapar en test-fil i Assets/Documents"""
        self.log_info("Steg 1: Skapar test-fil i Assets...")

        asset_docs = self.config.get('paths', {}).get('asset_documents')
        if not asset_docs:
            self.log_violation("STEP1", "Config saknar paths.asset_documents")
            return False

        self.asset_path = os.path.join(asset_docs, self.test_filename)

        try:
            with open(self.asset_path, 'w', encoding='utf-8') as f:
                f.write(TEST_DOCUMENT_CONTENT)
            self.log_pass("STEP1", f"Skapade {self.test_filename}")
            return True
        except Exception as e:
            self.log_violation("STEP1", f"Kunde inte skapa fil: {e}")
            return False

    # --- STEP 2: Run DocConverter ---
    def step2_run_doc_converter(self) -> bool:
        """Kör DocConverter på test-filen"""
        self.log_info("Steg 2: Kör DocConverter...")

        try:
            from services.processors.doc_converter import processa_dokument

            processa_dokument(self.asset_path, self.test_filename)
            self.log_pass("STEP2", "DocConverter körde utan fel")
            return True
        except Exception as e:
            self.log_violation("STEP2", f"DocConverter kraschade: {e}")
            return False

    # --- STEP 3: Validate Lake file ---
    def step3_validate_lake(self) -> bool:
        """Validerar att Lake-filen har alla required properties"""
        self.log_info("Steg 3: Validerar Lake-fil...")

        lake_store = self.config.get('paths', {}).get('lake_store')
        if not lake_store:
            self.log_violation("STEP3", "Config saknar paths.lake_store")
            return False

        # Hitta Lake-filen (samma namn men .md)
        base_name = os.path.splitext(self.test_filename)[0]
        self.lake_path = os.path.join(lake_store, f"{base_name}.md")

        if not os.path.exists(self.lake_path):
            self.log_violation("STEP3", f"Lake-fil skapades inte: {self.lake_path}")
            return False

        # Läs frontmatter
        try:
            with open(self.lake_path, 'r', encoding='utf-8') as f:
                content = f.read()

            if not content.startswith('---'):
                self.log_violation("STEP3", "Lake-fil saknar frontmatter")
                return False

            end_idx = content.index('---', 3)
            yaml_content = content[3:end_idx]
            frontmatter = yaml.safe_load(yaml_content) or {}
        except Exception as e:
            self.log_violation("STEP3", f"Kunde inte läsa frontmatter: {e}")
            return False

        # Validera required properties
        required = get_required_lake_properties(self.lake_schema)
        missing = required - set(frontmatter.keys())

        if missing:
            self.log_violation("STEP3", f"Lake saknar required properties: {missing}")
            return False

        # Validera att inga okända properties finns
        allowed = set()
        for section in ['base_properties', 'semantic_properties']:
            allowed.update(self.lake_schema.get(section, {}).get('properties', {}).keys())

        unknown = set(frontmatter.keys()) - allowed
        if unknown:
            self.log_violation("STEP3", f"Lake har okända properties: {unknown}")
            return False

        self.log_pass("STEP3", f"Lake-fil har alla {len(required)} required properties")
        return True

    # --- STEP 4: Index to Vector ---
    def step4_run_vector_indexer(self) -> bool:
        """Indexera Lake-filen till VectorDB via VectorService"""
        self.log_info("Steg 4: Indexerar till VectorDB...")

        try:
            from services.utils.vector_service import VectorService
            vs = VectorService()

            # Läs Lake-fil och indexera
            with open(self.lake_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # Extrahera frontmatter för metadata
            end_idx = content.index('---', 3)
            yaml_content = content[3:end_idx]
            frontmatter = yaml.safe_load(yaml_content) or {}

            # Bygg metadata enligt schema
            lake_vector_props, _, key_mappings = get_vector_properties(
                self.lake_schema, self.graph_schema
            )

            metadata = {}
            for prop in lake_vector_props:
                # Kolla om det finns en reverse mapping
                original_key = prop
                for orig, mapped in key_mappings.items():
                    if mapped == prop:
                        original_key = orig
                        break

                if original_key in frontmatter:
                    metadata[prop] = frontmatter[original_key]

            vs.upsert(
                id=self.test_uuid,
                text=content,
                metadata=metadata
            )
            self.log_pass("STEP4", "Indexerade via VectorService")
            return True
        except Exception as e:
            self.log_violation("STEP4", f"Vektor-indexering kraschade: {e}")
            return False

    # --- STEP 5: Validate Vector metadata ---
    def step5_validate_vector(self) -> bool:
        """Validerar att vektor-metadata har rätt properties"""
        self.log_info("Steg 5: Validerar vektor-metadata...")

        try:
            from services.utils.vector_service import VectorService
            vs = VectorService()

            # Hämta test-dokumentet
            result = vs.collection.get(ids=[self.test_uuid])

            if not result['ids']:
                self.log_violation("STEP5", "Test-dokument finns inte i vektor-db")
                return False

            metadata = result['metadatas'][0] if result['metadatas'] else {}

            # Hämta expected properties
            lake_vector_props, _, _ = get_vector_properties(self.lake_schema, self.graph_schema)

            # Validera att alla include_in_vector=true finns
            missing = lake_vector_props - set(metadata.keys())
            if missing:
                self.log_violation("STEP5", f"Vektor saknar properties: {missing}")
                return False

            # Validera att inga okända properties finns
            unknown = set(metadata.keys()) - lake_vector_props
            if unknown:
                self.log_violation("STEP5", f"Vektor har okända properties: {unknown}")
                return False

            self.log_pass("STEP5", f"Vektor har alla {len(lake_vector_props)} förväntade properties")
            return True
        except Exception as e:
            self.log_violation("STEP5", f"Vektor-validering kraschade: {e}")
            return False

    # --- STEP 6: Run Dreamer (optional, if entities extracted) ---
    def step6_run_dreamer(self) -> bool:
        """Kör Dreamer för att förädla graf-noder"""
        self.log_info("Steg 6: Kör Dreamer (graf-förädling)...")

        try:
            from services.agents.dreamer import run_dreamer_cycle

            # Kör en Dreamer-cykel
            run_dreamer_cycle(max_nodes=10)
            self.log_pass("STEP6", "Dreamer körde utan fel")
            return True
        except ImportError:
            self.log_info("Dreamer har ingen run_dreamer_cycle, hoppar över...")
            return True  # Inte ett fel - Dreamer kanske inte har denna funktion
        except Exception as e:
            # Dreamer-fel är inte kritiska för property chain
            self.log_info(f"Dreamer-varning (ej kritiskt): {e}")
            return True

    # --- STEP 7: Validate Graph nodes ---
    def step7_validate_graph(self) -> bool:
        """Validerar att entiteter från test-dokumentet skrevs till grafen med korrekta properties"""
        self.log_info("Steg 7: Validerar graf-noder från test-dokumentet...")

        try:
            from services.utils.graph_service import GraphStore

            graph_path = self.config.get('paths', {}).get('graph_db')
            if not graph_path or not os.path.exists(graph_path):
                self.log_violation("STEP7", "Graf-db finns inte")
                return False

            graph = GraphStore(graph_path, read_only=True)

            required_base = get_required_graph_base_properties(self.graph_schema)

            with graph:
                # Hitta entiteter som har node_context med origin = test-dokumentets UUID
                # Detta verifierar att DocConverter skrev entiteter till grafen
                test_entities = []

                # Kolla alla noder och hitta de som refererar till vårt test-dokument
                all_nodes = graph.conn.execute("SELECT id, type, properties FROM nodes").fetchall()

                import json
                for row in all_nodes:
                    node_id, node_type, props_json = row
                    props = json.loads(props_json) if props_json else {}
                    node_context = props.get('node_context', [])

                    # Kolla om någon context-entry har origin = test_uuid
                    for ctx in node_context:
                        if isinstance(ctx, dict) and ctx.get('origin') == self.test_uuid:
                            test_entities.append({
                                'id': node_id,
                                'type': node_type,
                                'properties': props
                            })
                            break

                # Validera att entiteter skapades
                if not test_entities:
                    self.log_violation("STEP7",
                        f"Inga entiteter med origin={self.test_uuid} hittades i grafen. "
                        "DocConverter extraherar entiteter men skriver inte till grafen.")
                    return False

                self.log_info(f"Hittade {len(test_entities)} entiteter från test-dokumentet")

                violations_found = False

                for node in test_entities:
                    props = node.get('properties', {})
                    node_type = node.get('type')

                    # Kolla required base properties
                    check_props = required_base - {'name', 'type', 'id', 'source'}
                    missing = check_props - set(props.keys())

                    if missing:
                        self.log_violation("STEP7",
                            f"Nod {node.get('id', 'UNKNOWN')} ({node_type}) saknar: {missing}")
                        violations_found = True

                    # Kolla okända properties
                    allowed_base = set(self.graph_schema.get('base_properties', {}).get('properties', {}).keys())
                    node_type_props = set(self.graph_schema.get('nodes', {}).get(node_type, {}).get('properties', {}).keys())
                    allowed_all = allowed_base | node_type_props

                    unknown = set(props.keys()) - allowed_all
                    if unknown:
                        self.log_violation("STEP7",
                            f"Nod {node.get('id', 'UNKNOWN')} ({node_type}) har okända properties: {unknown}")
                        violations_found = True

            if not violations_found:
                self.log_pass("STEP7", f"Graf: {len(test_entities)} entiteter har korrekta properties")

            return not violations_found
        except Exception as e:
            self.log_violation("STEP7", f"Graf-validering kraschade: {e}")
            return False

    # --- CLEANUP ---
    def cleanup(self):
        """Tar bort test-data"""
        if self.keep_data:
            self.log_info(f"Behåller test-data (--keep). UUID: {self.test_uuid}")
            return

        self.log_info("Städar upp test-data...")

        # Ta bort Asset-fil
        if self.asset_path and os.path.exists(self.asset_path):
            os.remove(self.asset_path)

        # Ta bort Lake-fil
        if self.lake_path and os.path.exists(self.lake_path):
            os.remove(self.lake_path)

        # Ta bort från vektor-db
        try:
            from services.utils.vector_service import VectorService
            vs = VectorService()
            vs.delete(self.test_uuid)
        except Exception:
            pass

    # --- RUN ALL ---
    def run(self) -> bool:
        """Kör alla test-steg"""
        print("\n" + "=" * 60)
        print("PROPERTY CHAIN E2E TEST")
        print("=" * 60)
        print(f"Test UUID: {self.test_uuid}\n")

        steps = [
            ("1. Create test file", self.step1_create_test_file),
            ("2. DocConverter", self.step2_run_doc_converter),
            ("3. Validate Lake", self.step3_validate_lake),
            ("4. VectorIndexer", self.step4_run_vector_indexer),
            ("5. Validate Vector", self.step5_validate_vector),
            ("6. Dreamer", self.step6_run_dreamer),
            ("7. Validate Graph", self.step7_validate_graph),
        ]

        for step_name, step_func in steps:
            print(f"\n--- {step_name} ---")
            try:
                success = step_func()
                if not success:
                    print(f"\n[HARDFAIL] Testet avbröts vid: {step_name}")
                    self.cleanup()
                    return False
            except Exception as e:
                self.log_violation(step_name, f"Oväntat fel: {e}")
                self.cleanup()
                return False

        self.cleanup()

        print("\n" + "=" * 60)
        if self.violations:
            print(f"RESULT: FAIL ({len(self.violations)} violations)")
            for v in self.violations:
                print(f"  - {v['step']}: {v['message']}")
            print("=" * 60)
            return False
        else:
            print("RESULT: PASS - Property chain intact!")
            print("=" * 60)
            return True


# === MAIN ===

def main():
    parser = argparse.ArgumentParser(
        description="End-to-end test för property chain (Schema → Lake → Vector → Graf)"
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Visa vad som skulle testas utan att köra')
    parser.add_argument('--keep', action='store_true',
                        help='Behåll test-data efter körning')
    args = parser.parse_args()

    try:
        config = load_config()
        graph_schema = load_graph_schema()
        lake_schema = load_lake_schema()
    except FileNotFoundError as e:
        print(f"HARDFAIL: {e}")
        sys.exit(1)

    if args.dry_run:
        print("\n=== DRY RUN ===")
        print("\nLake required properties:")
        for prop in sorted(get_required_lake_properties(lake_schema)):
            print(f"  - {prop}")

        lake_vec, graph_vec, mappings = get_vector_properties(lake_schema, graph_schema)
        print("\nVector properties (Lake):")
        for prop in sorted(lake_vec):
            print(f"  - {prop}")

        print("\nVector properties (Graf):")
        for prop in sorted(graph_vec):
            print(f"  - {prop}")

        print("\nGraph required base_properties:")
        for prop in sorted(get_required_graph_base_properties(graph_schema)):
            print(f"  - {prop}")

        print("\nKey mappings:")
        for orig, mapped in mappings.items():
            print(f"  - {orig} → {mapped}")

        sys.exit(0)

    test = PropertyChainTest(config, lake_schema, graph_schema, keep_data=args.keep)
    success = test.run()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
