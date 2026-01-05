"""
Validate Area C: Graph Ingestion & Provisional Write Strategy.
"""
import os
import sys
import unittest
import shutil
import tempfile
import json
import time

# Lägg till projektroten
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.utils.graph_service import GraphStore

class TestAreaC(unittest.TestCase):
    
    def setUp(self):
        # Skapa temporär databas
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_graph.duckdb")
        self.graph = GraphStore(self.db_path)
        
    def tearDown(self):
        self.graph.close()
        shutil.rmtree(self.temp_dir)

    def test_01_provisional_vs_verified(self):
        print("\n--- TEST 1: Provisional vs Verified ---")
        
        # 1. Skapa en VERIFIED nod
        self.graph.upsert_node(
            id="Jocke",
            type="Person",
            properties={"role": "CTO", "status": "VERIFIED", "department": "Tech"}
        )
        
        node = self.graph.get_node("Jocke")
        self.assertEqual(node['properties']['role'], "CTO")
        self.assertEqual(node['properties']['status'], "VERIFIED")
        print("✅ VERIFIED nod skapad.")

        # 2. Försök "sänka" den med PROVISIONAL data
        self.graph.merge_node(
            id="Jocke",
            type="Person",
            properties={"role": "Intern", "status": "PROVISIONAL"}
        )
        
        node = self.graph.get_node("Jocke")
        # SKA VARA KVAR SOM CTO (Skyddad)
        self.assertEqual(node['properties']['role'], "CTO") 
        self.assertEqual(node['properties']['status'], "VERIFIED")
        print("✅ VERIFIED data skyddad mot PROVISIONAL överskrivning.")

    def test_02_additive_update(self):
        print("\n--- TEST 2: Additive Update ---")
        
        # 1. Existerande nod
        self.graph.upsert_node(
            id="ProjektX",
            type="Project",
            properties={"status": "VERIFIED", "budget": 1000}
        )
        
        # 2. PROVISIONAL update med NY info (deadline) och KONFLIKTANDE info (budget)
        self.graph.merge_node(
            id="ProjektX",
            type="Project",
            properties={
                "status": "PROVISIONAL", 
                "budget": 500,       # Ska ignoreras
                "deadline": "2025"   # Ska läggas till
            }
        )
        
        node = self.graph.get_node("ProjektX")
        self.assertEqual(node['properties']['budget'], 1000)
        self.assertEqual(node['properties']['deadline'], "2025")
        print("✅ Additive Update fungerade (Ny info tillagd, gammal skyddad).")

    def test_03_create_edge(self):
        print("\n--- TEST 3: Edge Creation ---")
        
        self.graph.upsert_node(id="A", type="Person")
        self.graph.upsert_node(id="B", type="Project")
        
        self.graph.upsert_edge("A", "B", "LEADS", properties={"conf": 0.9})
        
        edges = self.graph.get_edges_from("A")
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]['type'], "LEADS")
        print("✅ Edge skapad korrekt.")

if __name__ == '__main__':
    unittest.main()







