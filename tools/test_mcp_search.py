#!/usr/bin/env python3
"""
test_mcp_search.py - Validerar index_search_mcp.py och dess MCP-verktyg.

╔══════════════════════════════════════════════════════════════════════════════╗
║  Testar att alla MCP-verktyg i index_search_mcp.py fungerar korrekt.         ║
║                                                                              ║
║  Verktyg som testas:                                                         ║
║  1. search_graph_nodes      - Graf-sökning                                   ║
║  2. query_vector_memory     - Vektor-sökning                                 ║
║  3. search_by_date_range    - Datumsökning                                   ║
║  4. search_lake_metadata    - Lake metadata-sökning                          ║
║  5. get_neighbor_network    - Relationsutforskning                           ║
║  6. get_entity_summary      - Entitetssammanfattning                         ║
║  7. get_graph_statistics    - Graf-statistik                                 ║
║  8. parse_relative_date     - Relativ datumparsning                          ║
║  9. read_document_content   - Dokumentläsning                                ║
║                                                                              ║
║  HARDFAIL om något verktyg inte fungerar.                                    ║
╚══════════════════════════════════════════════════════════════════════════════╝

Användning:
    python tools/test_mcp_search.py              # Kör alla tester
    python tools/test_mcp_search.py --verbose    # Visa detaljer
    python tools/test_mcp_search.py --tool X     # Testa specifikt verktyg
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta

# Lägg till projektroten för imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class MCPSearchTest:
    """Testar alla MCP-verktyg i index_search_mcp.py"""

    # Felmarkörer som indikerar misslyckad operation
    ERROR_MARKERS = ["FEL:", "Error:", "misslyckades", "HARDFAIL", "Exception", "Traceback"]

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.results = {}
        self.passed = 0
        self.failed = 0

        # Importera MCP-modulen
        try:
            from services.agents import index_search_mcp as mcp_module
            self.mcp = mcp_module
            self.log_info("MCP-modul importerad")
        except ImportError as e:
            print(f"HARDFAIL: Kunde inte importera index_search_mcp: {e}")
            sys.exit(1)

    def _contains_error(self, result: str) -> bool:
        """Kontrollerar om resultatet innehåller felmarkörer."""
        for marker in self.ERROR_MARKERS:
            if marker in result:
                return True
        return False

    def log_info(self, msg: str):
        if self.verbose:
            print(f"  [INFO] {msg}")

    def log_pass(self, test_name: str, msg: str):
        self.results[test_name] = {"status": "PASS", "message": msg}
        self.passed += 1
        print(f"  [PASS] {test_name}: {msg}")

    def log_fail(self, test_name: str, msg: str):
        self.results[test_name] = {"status": "FAIL", "message": msg}
        self.failed += 1
        print(f"  [FAIL] {test_name}: {msg}")

    # --- TEST 1: search_graph_nodes ---
    def test_search_graph_nodes(self) -> bool:
        """Testar graf-sökning"""
        print("\n--- Test: search_graph_nodes ---")

        try:
            # Test 1: Sök efter vanligt namn
            result = self.mcp.search_graph_nodes("Joakim")
            self.log_info(f"Resultat (Joakim): {result[:100]}...")

            if self._contains_error(result):
                self.log_fail("graph_basic", f"Sökning returnerade fel: {result[:150]}")
                return False
            elif "GRAF" in result:
                self.log_pass("graph_basic", "Sökning lyckades utan fel")
            else:
                self.log_fail("graph_basic", f"Oväntat resultat: {result[:100]}")
                return False

            # Test 2: Sök med node_type filter
            result_typed = self.mcp.search_graph_nodes("test", node_type="Person")
            self.log_info(f"Resultat (typ-filtrerad): {result_typed[:100]}...")

            if self._contains_error(result_typed):
                self.log_fail("graph_filtered", f"Typ-filtrering returnerade fel: {result_typed[:150]}")
                return False
            elif "GRAF" in result_typed:
                self.log_pass("graph_filtered", "Typ-filtrering fungerar utan fel")
            else:
                self.log_fail("graph_filtered", "Typ-filtrering misslyckades")
                return False

            # Test 3: Tom sökning (ska ge tomt resultat, inte krasch)
            result_empty = self.mcp.search_graph_nodes("")
            if self._contains_error(result_empty) and "Exception" in result_empty:
                self.log_fail("graph_empty", f"Tom sökning kraschade: {result_empty[:100]}")
                return False
            else:
                self.log_pass("graph_empty", "Hanterar tom sökning utan krasch")

            return True

        except Exception as e:
            self.log_fail("graph_exception", f"Exception: {e}")
            return False

    # --- TEST 2: query_vector_memory ---
    def test_query_vector_memory(self) -> bool:
        """Testar vektor-sökning"""
        print("\n--- Test: query_vector_memory ---")

        try:
            # Test 1: Basic semantisk sökning
            result = self.mcp.query_vector_memory("projekt möte diskussion")
            self.log_info(f"Resultat: {result[:150]}...")

            if self._contains_error(result):
                self.log_fail("vector_basic", f"Vektorsökning returnerade fel: {result[:150]}")
                return False
            elif "VEKTOR" in result and "Modell:" in result:
                self.log_pass("vector_basic", "Vektorsökning lyckades utan fel")
            else:
                self.log_fail("vector_basic", f"Oväntat format (saknar VEKTOR/Modell): {result[:100]}")
                return False

            # Test 2: Begränsat antal resultat
            result_limited = self.mcp.query_vector_memory("test", n_results=2)
            self.log_info(f"Begränsad sökning: {result_limited[:100]}...")

            if self._contains_error(result_limited):
                self.log_fail("vector_limited", f"Begränsad sökning returnerade fel: {result_limited[:150]}")
                return False
            elif "VEKTOR" in result_limited:
                self.log_pass("vector_limited", "Resultatbegränsning fungerar utan fel")
            else:
                self.log_fail("vector_limited", "Resultatbegränsning fungerar inte")
                return False

            return True

        except Exception as e:
            self.log_fail("vector_exception", f"Exception: {e}")
            return False

    # --- TEST 3: search_by_date_range ---
    def test_search_by_date_range(self) -> bool:
        """Testar datumsökning"""
        print("\n--- Test: search_by_date_range ---")

        try:
            # Test 1: Giltig datumintervall
            today = datetime.now()
            start = (today - timedelta(days=30)).strftime("%Y-%m-%d")
            end = today.strftime("%Y-%m-%d")

            result = self.mcp.search_by_date_range(start, end)
            self.log_info(f"Resultat (30 dagar): {result[:150]}...")

            if self._contains_error(result):
                self.log_fail("date_basic", f"Datumsökning returnerade fel: {result[:150]}")
                return False
            elif "DATUM" in result:
                self.log_pass("date_basic", "Datumsökning lyckades utan fel")
            else:
                self.log_fail("date_basic", f"Oväntat format: {result[:100]}")
                return False

            # Test 2: Olika date_field
            result_ingestion = self.mcp.search_by_date_range(start, end, date_field="ingestion")
            if self._contains_error(result_ingestion):
                self.log_fail("date_field", f"date_field returnerade fel: {result_ingestion[:150]}")
                return False
            elif "DATUM" in result_ingestion:
                self.log_pass("date_field", "date_field parameter fungerar utan fel")
            else:
                self.log_fail("date_field", "date_field fungerar inte")
                return False

            # Test 3: Ogiltigt datum (ska ge tydligt felmeddelande, inte krasch)
            result_invalid = self.mcp.search_by_date_range("invalid", "also-invalid")
            if "Ogiltigt" in result_invalid or "datumformat" in result_invalid.lower():
                self.log_pass("date_invalid", "Felhantering för ogiltigt datum")
            else:
                self.log_fail("date_invalid", f"Hanterar inte ogiltigt datum korrekt: {result_invalid[:100]}")
                return False

            # Test 4: Ogiltigt date_field (ska ge tydligt felmeddelande)
            result_bad_field = self.mcp.search_by_date_range(start, end, date_field="invalid_field")
            if "Ogiltigt date_field" in result_bad_field:
                self.log_pass("date_bad_field", "Validerar date_field korrekt")
            else:
                self.log_fail("date_bad_field", f"Validerar inte date_field: {result_bad_field[:100]}")
                return False

            return True

        except Exception as e:
            self.log_fail("date_exception", f"Exception: {e}")
            return False

    # --- TEST 4: search_lake_metadata ---
    def test_search_lake_metadata(self) -> bool:
        """Testar Lake metadata-sökning"""
        print("\n--- Test: search_lake_metadata ---")

        try:
            # Test 1: Sök efter nyckelord
            result = self.mcp.search_lake_metadata("Digitalist")
            self.log_info(f"Resultat: {result[:150]}...")

            if self._contains_error(result):
                self.log_fail("lake_basic", f"Lake-sökning returnerade fel: {result[:150]}")
                return False
            elif "LAKE" in result:
                self.log_pass("lake_basic", "Lake-sökning lyckades utan fel")
            else:
                self.log_fail("lake_basic", f"Oväntat format: {result[:100]}")
                return False

            # Test 2: Fältspecifik sökning
            result_field = self.mcp.search_lake_metadata("Document", field="source_type")
            if self._contains_error(result_field):
                self.log_fail("lake_field", f"Fältfiltrering returnerade fel: {result_field[:150]}")
                return False
            elif "LAKE" in result_field:
                self.log_pass("lake_field", "Fältfiltrering fungerar utan fel")
            else:
                self.log_fail("lake_field", "Fältfiltrering fungerar inte")
                return False

            return True

        except Exception as e:
            self.log_fail("lake_exception", f"Exception: {e}")
            return False

    # --- TEST 5: get_neighbor_network ---
    def test_get_neighbor_network(self) -> bool:
        """Testar relationsutforskning"""
        print("\n--- Test: get_neighbor_network ---")

        try:
            # Hämta en nod-ID från grafen först
            result_search = self.mcp.search_graph_nodes("Digitalist")
            self.log_info(f"Sökte efter nod: {result_search[:100]}...")

            # Extrahera ett ID (om det finns)
            import re
            id_match = re.search(r'ID:\s*([a-f0-9-]+)', result_search)

            if id_match:
                node_id = id_match.group(1)
                result = self.mcp.get_neighbor_network(node_id)
                self.log_info(f"Nätverk för {node_id[:8]}: {result[:150]}...")

                if self._contains_error(result):
                    self.log_fail("network_basic", f"Nätverksutforskning returnerade fel: {result[:150]}")
                    return False
                elif "NÄTVERK" in result or "kopplingar" in result.lower():
                    self.log_pass("network_basic", "Nätverksutforskning fungerar utan fel")
                else:
                    self.log_fail("network_basic", f"Oväntat format: {result[:100]}")
                    return False
            else:
                self.log_info("Ingen nod hittad - hoppar över network_basic test")

            # Test med icke-existerande nod (ska ge tydligt "hittades inte", inte krasch)
            result_fake = self.mcp.get_neighbor_network("fake-node-id-12345")
            if "hittades inte" in result_fake.lower():
                self.log_pass("network_notfound", "Hanterar icke-existerande nod korrekt")
            elif self._contains_error(result_fake) and "Exception" in result_fake:
                self.log_fail("network_notfound", f"Icke-existerande nod orsakade krasch: {result_fake[:100]}")
                return False
            else:
                self.log_fail("network_notfound", f"Fel vid icke-existerande nod: {result_fake[:100]}")
                return False

            return True

        except Exception as e:
            self.log_fail("network_exception", f"Exception: {e}")
            return False

    # --- TEST 6: get_entity_summary ---
    def test_get_entity_summary(self) -> bool:
        """Testar entitetssammanfattning"""
        print("\n--- Test: get_entity_summary ---")

        try:
            # Hitta en riktig nod att testa på
            result_search = self.mcp.search_graph_nodes("Digitalist")
            import re
            id_match = re.search(r'ID:\s*([a-f0-9-]+)', result_search)

            if id_match:
                node_id = id_match.group(1)
                result = self.mcp.get_entity_summary(node_id)
                self.log_info(f"Sammanfattning för {node_id[:8]}: {result[:150]}...")

                if self._contains_error(result):
                    self.log_fail("summary_basic", f"Sammanfattning returnerade fel: {result[:150]}")
                    return False
                elif "ENTITET" in result or "Namn:" in result:
                    self.log_pass("summary_basic", "Entitetssammanfattning fungerar utan fel")
                else:
                    self.log_fail("summary_basic", f"Oväntat format: {result[:100]}")
                    return False
            else:
                self.log_info("Ingen nod hittad - hoppar över summary_basic test")

            # Test med fake ID (ska returnera "hittades inte", inte krasch)
            result_fake = self.mcp.get_entity_summary("non-existent-id-xyz")
            self.log_info(f"Resultat (fake ID): {result_fake[:100]}...")

            if "hittades inte" in result_fake.lower():
                self.log_pass("summary_notfound", "Hanterar icke-existerande nod korrekt")
            elif self._contains_error(result_fake) and "Exception" in result_fake:
                self.log_fail("summary_notfound", f"Icke-existerande nod orsakade krasch: {result_fake[:100]}")
                return False
            else:
                self.log_fail("summary_notfound", f"Oväntat beteende: {result_fake[:100]}")
                return False

            return True

        except Exception as e:
            self.log_fail("summary_exception", f"Exception: {e}")
            return False

    # --- TEST 7: get_graph_statistics ---
    def test_get_graph_statistics(self) -> bool:
        """Testar graf-statistik"""
        print("\n--- Test: get_graph_statistics ---")

        try:
            result = self.mcp.get_graph_statistics()
            self.log_info(f"Statistik: {result[:200]}...")

            # Först: kontrollera att det inte är ett fel
            if self._contains_error(result):
                self.log_fail("stats_error_check", f"Statistik returnerade fel: {result[:150]}")
                return False

            # Validera format
            checks = [
                ("STATISTIK" in result, "Rubrik finns"),
                ("noder" in result.lower(), "Visar noder"),
                ("kanter" in result.lower(), "Visar kanter"),
            ]

            all_passed = True
            for check, desc in checks:
                if check:
                    self.log_pass(f"stats_{desc.replace(' ', '_')}", desc)
                else:
                    self.log_fail(f"stats_{desc.replace(' ', '_')}", f"{desc} saknas")
                    all_passed = False

            return all_passed

        except Exception as e:
            self.log_fail("stats_exception", f"Exception: {e}")
            return False

    # --- TEST 8: parse_relative_date ---
    def test_parse_relative_date(self) -> bool:
        """Testar relativ datumparsning"""
        print("\n--- Test: parse_relative_date ---")

        test_cases = [
            ("idag", "start_date"),
            ("igår", "start_date"),
            ("förra veckan", "start_date"),
            ("denna veckan", "start_date"),
            ("3 dagar sedan", "start_date"),
            ("2 veckor sedan", "start_date"),
            ("nyligen", "start_date"),
            ("gibberish_xyz", "Okänt"),
        ]

        all_passed = True

        for expr, expected_key in test_cases:
            try:
                result = self.mcp.parse_relative_date(expr)
                parsed = json.loads(result)

                if expected_key == "Okänt":
                    if "Okänt" in parsed.get("description", ""):
                        self.log_pass(f"date_{expr[:10]}", f"'{expr}' hanteras korrekt")
                    else:
                        self.log_fail(f"date_{expr[:10]}", f"'{expr}' borde ge Okänt")
                        all_passed = False
                else:
                    if parsed.get(expected_key):
                        self.log_pass(f"date_{expr[:10]}", f"'{expr}' → {parsed.get(expected_key)}")
                    else:
                        self.log_fail(f"date_{expr[:10]}", f"'{expr}' gav inget {expected_key}")
                        all_passed = False

            except Exception as e:
                self.log_fail(f"date_{expr[:10]}", f"Exception: {e}")
                all_passed = False

        return all_passed

    # --- TEST 9: read_document_content ---
    def test_read_document_content(self) -> bool:
        """Testar dokumentläsning"""
        print("\n--- Test: read_document_content ---")

        try:
            # Test med icke-existerande dokument (ska ge "EJ HITTAT", inte krasch)
            result = self.mcp.read_document_content("fake-doc-id-xyz")
            self.log_info(f"Resultat (fake): {result[:100]}...")

            if "EJ HITTAT" in result or "Kunde inte hitta" in result:
                self.log_pass("doc_notfound", "Hanterar icke-existerande dokument korrekt")
            elif self._contains_error(result) and "Exception" in result:
                self.log_fail("doc_notfound", f"Icke-existerande dokument orsakade krasch: {result[:100]}")
                return False
            else:
                self.log_fail("doc_notfound", f"Fel vid icke-existerande dokument: {result[:100]}")
                return False

            # Hitta ett faktiskt dokument att testa
            lake_path = self.mcp.LAKE_PATH
            if os.path.exists(lake_path):
                files = [f for f in os.listdir(lake_path) if f.endswith('.md')]
                if files:
                    test_file = files[0]
                    # Extrahera UUID från filnamn (om möjligt)
                    import re
                    uuid_match = re.search(r'([a-f0-9-]{36})', test_file)
                    if uuid_match:
                        doc_id = uuid_match.group(1)
                    else:
                        doc_id = test_file.replace('.md', '')

                    result_real = self.mcp.read_document_content(doc_id)
                    self.log_info(f"Verkligt dokument ({test_file[:20]}): {result_real[:100]}...")

                    if self._contains_error(result_real):
                        self.log_fail("doc_real", f"Dokumentläsning returnerade fel: {result_real[:150]}")
                        return False
                    elif "DOKUMENT" in result_real or "---" in result_real:
                        self.log_pass("doc_real", "Läser verkligt dokument utan fel")
                    else:
                        self.log_fail("doc_real", f"Oväntat format: {result_real[:100]}")
                        return False

                    # Test smart trunkering
                    result_smart = self.mcp.read_document_content(doc_id, max_length=500, section="smart")
                    if self._contains_error(result_smart):
                        self.log_fail("doc_truncate", f"Smart trunkering returnerade fel: {result_smart[:150]}")
                        return False
                    elif "LÄGE: smart" in result_smart or len(result_smart) <= 600:
                        self.log_pass("doc_truncate", "Smart trunkering fungerar utan fel")
                    else:
                        self.log_info("Trunkering aktiverades inte (dokument kort)")

            return True

        except Exception as e:
            self.log_fail("doc_exception", f"Exception: {e}")
            return False

    # --- RUN ALL ---
    def run_all(self, specific_tool: str = None) -> bool:
        """Kör alla tester"""
        print("\n" + "=" * 60)
        print("MCP SEARCH TOOL VALIDATION")
        print("=" * 60)

        tests = [
            ("search_graph_nodes", self.test_search_graph_nodes),
            ("query_vector_memory", self.test_query_vector_memory),
            ("search_by_date_range", self.test_search_by_date_range),
            ("search_lake_metadata", self.test_search_lake_metadata),
            ("get_neighbor_network", self.test_get_neighbor_network),
            ("get_entity_summary", self.test_get_entity_summary),
            ("get_graph_statistics", self.test_get_graph_statistics),
            ("parse_relative_date", self.test_parse_relative_date),
            ("read_document_content", self.test_read_document_content),
        ]

        if specific_tool:
            tests = [(name, func) for name, func in tests if specific_tool.lower() in name.lower()]
            if not tests:
                print(f"Inget test matchar '{specific_tool}'")
                return False

        for test_name, test_func in tests:
            try:
                test_func()
            except Exception as e:
                self.log_fail(test_name, f"Kritiskt fel: {e}")

        # Summering
        print("\n" + "=" * 60)
        total = self.passed + self.failed
        if self.failed == 0:
            print(f"RESULT: PASS - Alla {total} tester godkända!")
        else:
            print(f"RESULT: FAIL - {self.failed}/{total} tester misslyckades")

        print("=" * 60)
        return self.failed == 0


def main():
    parser = argparse.ArgumentParser(
        description="Validerar index_search_mcp.py MCP-verktyg"
    )
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Visa detaljerad output')
    parser.add_argument('--tool', '-t', type=str,
                        help='Testa specifikt verktyg (delvis namn)')
    args = parser.parse_args()

    test = MCPSearchTest(verbose=args.verbose)
    success = test.run_all(specific_tool=args.tool)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
