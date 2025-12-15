"""
GraphStore - DuckDB-baserad graf-lagring.

Ersätter KuzuDB för stabilitet. Implementerar graf-semantik
via relationella tabeller (nodes, edges) med SQL.

Migration: LÖST-54 (The DuckDB Pivot)
"""

import duckdb
import json
import os
import logging
from typing import Optional

# Logger
LOGGER = logging.getLogger('GraphStore')


class GraphStore:
    """
    DuckDB-baserad graf-lagring med nodes/edges-tabeller.
    
    Användning:
        # Skriv-mode (GraphBuilder)
        store = GraphStore("/path/to/graph.duckdb")
        store.upsert_node("Joakim Ekman", "Entity", aliases=["Jocke"])
        
        # Läs-mode (ContextBuilder) - för concurrency
        store = GraphStore("/path/to/graph.duckdb", read_only=True)
    """
    
    def __init__(self, db_path: str, read_only: bool = False):
        """
        Initiera GraphStore.
        
        Args:
            db_path: Sökväg till DuckDB-filen
            read_only: True för läs-only anslutning (concurrency-säker)
        """
        self.db_path = db_path
        self.read_only = read_only
        
        if not read_only:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        self.conn = duckdb.connect(db_path, read_only=read_only)
        
        if not read_only:
            self._init_schema()
    
    def _init_schema(self):
        """Skapa tabeller och index om de inte finns."""
        # Nodes-tabell
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                aliases VARCHAR[],
                properties JSON,
                updated_at TIMESTAMP DEFAULT current_timestamp
            )
        """)
        
        # Edges-tabell
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS edges (
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                type TEXT NOT NULL,
                properties JSON,
                created_at TIMESTAMP DEFAULT current_timestamp,
                PRIMARY KEY (source, target, type)
            )
        """)
        
        # Index för prestanda
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target)")
        
        LOGGER.info(f"GraphStore schema initierat: {self.db_path}")
    
    # === NODE OPERATIONS ===
    
    def upsert_node(self, id: str, type: str, aliases: list = None, properties: dict = None):
        """
        Skapa eller uppdatera en nod.
        
        Args:
            id: Unikt ID (t.ex. "Joakim Ekman", "uuid-123")
            type: Nodtyp ("Entity", "Unit", "Concept")
            aliases: Lista med alias-namn
            properties: Extra metadata som JSON
        """
        if self.read_only:
            raise RuntimeError("HARDFAIL: Försöker skriva i read_only mode")
        
        props_json = json.dumps(properties) if properties else None
        
        self.conn.execute("""
            INSERT OR REPLACE INTO nodes (id, type, aliases, properties)
            VALUES (?, ?, ?, ?)
        """, [id, type, aliases, props_json])
    
    def get_node(self, id: str) -> Optional[dict]:
        """
        Hämta en nod via ID.
        
        Returns:
            dict med {id, type, aliases, properties} eller None
        """
        result = self.conn.execute(
            "SELECT id, type, aliases, properties FROM nodes WHERE id = ?",
            [id]
        ).fetchone()
        
        if result:
            return {
                "id": result[0],
                "type": result[1],
                "aliases": result[2] or [],
                "properties": json.loads(result[3]) if result[3] else {}
            }
        return None
    
    def find_nodes_by_type(self, node_type: str) -> list[dict]:
        """Hämta alla noder av en viss typ."""
        results = self.conn.execute(
            "SELECT id, type, aliases, properties FROM nodes WHERE type = ?",
            [node_type]
        ).fetchall()
        
        return [
            {
                "id": r[0],
                "type": r[1],
                "aliases": r[2] or [],
                "properties": json.loads(r[3]) if r[3] else {}
            }
            for r in results
        ]
    
    def find_nodes_by_alias(self, alias: str) -> list[dict]:
        """
        Hitta noder som har ett visst alias.
        
        Args:
            alias: Alias att söka efter
            
        Returns:
            Lista med matchande noder
        """
        results = self.conn.execute(
            "SELECT id, type, aliases, properties FROM nodes WHERE list_contains(aliases, ?)",
            [alias]
        ).fetchall()
        
        return [
            {
                "id": r[0],
                "type": r[1],
                "aliases": r[2] or [],
                "properties": json.loads(r[3]) if r[3] else {}
            }
            for r in results
        ]
    
    def find_nodes_fuzzy(self, term: str, threshold: float = 0.8) -> list[dict]:
        """
        Fuzzy-sök efter noder via ID eller alias.
        
        Använder CONTAINS för enkel matching (snabbt).
        Kan utökas med jaro_winkler_similarity för mer avancerad fuzzy-match.
        
        Args:
            term: Sökterm
            threshold: (reserved för framtida jaro-winkler)
            
        Returns:
            Lista med matchande noder
        """
        # Enkel CONTAINS-matching för nu
        results = self.conn.execute("""
            SELECT id, type, aliases, properties FROM nodes
            WHERE id ILIKE ? 
               OR EXISTS (
                   SELECT 1 FROM unnest(aliases) AS a(alias)
                   WHERE a.alias ILIKE ?
               )
            LIMIT 10
        """, [f"%{term}%", f"%{term}%"]).fetchall()
        
        return [
            {
                "id": r[0],
                "type": r[1],
                "aliases": r[2] or [],
                "properties": json.loads(r[3]) if r[3] else {}
            }
            for r in results
        ]
    
    def delete_node(self, id: str) -> bool:
        """Ta bort en nod."""
        if self.read_only:
            raise RuntimeError("HARDFAIL: Försöker skriva i read_only mode")
        
        result = self.conn.execute("DELETE FROM nodes WHERE id = ?", [id])
        return result.rowcount > 0
    
    # === EDGE OPERATIONS ===
    
    def upsert_edge(self, source: str, target: str, edge_type: str, properties: dict = None):
        """
        Skapa eller uppdatera en kant.
        
        Args:
            source: Käll-nod ID
            target: Mål-nod ID
            edge_type: Kanttyp ("DEALS_WITH", "UNIT_MENTIONS", etc.)
            properties: Extra metadata
        """
        if self.read_only:
            raise RuntimeError("HARDFAIL: Försöker skriva i read_only mode")
        
        props_json = json.dumps(properties) if properties else None
        
        self.conn.execute("""
            INSERT OR REPLACE INTO edges (source, target, type, properties)
            VALUES (?, ?, ?, ?)
        """, [source, target, edge_type, props_json])
    
    def get_edges_from(self, source: str, edge_type: str = None) -> list[dict]:
        """Hämta alla kanter från en nod."""
        if edge_type:
            results = self.conn.execute(
                "SELECT source, target, type, properties FROM edges WHERE source = ? AND type = ?",
                [source, edge_type]
            ).fetchall()
        else:
            results = self.conn.execute(
                "SELECT source, target, type, properties FROM edges WHERE source = ?",
                [source]
            ).fetchall()
        
        return [
            {
                "source": r[0],
                "target": r[1],
                "type": r[2],
                "properties": json.loads(r[3]) if r[3] else {}
            }
            for r in results
        ]
    
    def get_edges_to(self, target: str, edge_type: str = None) -> list[dict]:
        """Hämta alla kanter till en nod."""
        if edge_type:
            results = self.conn.execute(
                "SELECT source, target, type, properties FROM edges WHERE target = ? AND type = ?",
                [target, edge_type]
            ).fetchall()
        else:
            results = self.conn.execute(
                "SELECT source, target, type, properties FROM edges WHERE target = ?",
                [target]
            ).fetchall()
        
        return [
            {
                "source": r[0],
                "target": r[1],
                "type": r[2],
                "properties": json.loads(r[3]) if r[3] else {}
            }
            for r in results
        ]
    
    # === ENTITY-SPECIFIKA OPERATIONER ===
    
    def upgrade_canonical(self, old_id: str, new_id: str) -> bool:
        """
        Uppgradera Entity: 'Jocke' -> 'Joakim Ekman'
        
        KRITISKT: Uppdaterar alla kanter FÖRE nod-radering!
        
        Args:
            old_id: Gammalt ID (t.ex. "Jocke")
            new_id: Nytt ID (t.ex. "Joakim Ekman")
            
        Returns:
            True om lyckad
        """
        if self.read_only:
            raise RuntimeError("HARDFAIL: Försöker skriva i read_only mode")
        
        # 1. Hämta gamla noden
        old_node = self.get_node(old_id)
        if not old_node:
            LOGGER.warning(f"upgrade_canonical: Nod '{old_id}' finns inte")
            return False
        
        # 2. Bygg nya aliases (gamla ID + befintliga aliases, exkludera nya ID)
        new_aliases = (old_node.get('aliases') or []) + [old_id]
        new_aliases = [a for a in new_aliases if a != new_id]  # Undvik duplicat
        
        # 3. Skapa/uppdatera nya noden
        self.upsert_node(new_id, old_node['type'], aliases=new_aliases, properties=old_node.get('properties'))
        
        # 4. Uppdatera alla kanter som pekar på gamla noden
        self.conn.execute("UPDATE edges SET source = ? WHERE source = ?", [new_id, old_id])
        self.conn.execute("UPDATE edges SET target = ? WHERE target = ?", [new_id, old_id])
        
        # 5. Ta bort gamla noden
        self.conn.execute("DELETE FROM nodes WHERE id = ?", [old_id])
        
        LOGGER.info(f"Uppgraderade canonical: '{old_id}' -> '{new_id}'")
        return True
    
    def add_alias(self, node_id: str, alias: str) -> bool:
        """
        Lägg till ett alias för en nod.
        
        Args:
            node_id: Nod-ID
            alias: Alias att lägga till
            
        Returns:
            True om lyckad
        """
        if self.read_only:
            raise RuntimeError("HARDFAIL: Försöker skriva i read_only mode")
        
        node = self.get_node(node_id)
        if not node:
            LOGGER.warning(f"add_alias: Nod '{node_id}' finns inte")
            return False
        
        aliases = node.get('aliases') or []
        if alias not in aliases:
            aliases.append(alias)
            self.conn.execute(
                "UPDATE nodes SET aliases = ?, updated_at = current_timestamp WHERE id = ?",
                [aliases, node_id]
            )
            LOGGER.info(f"Lade till alias '{alias}' för '{node_id}'")
        
        return True
    
    # === STATISTIK & UTILITY ===
    
    def get_stats(self) -> dict:
        """Hämta statistik över grafen."""
        node_counts = self.conn.execute(
            "SELECT type, COUNT(*) FROM nodes GROUP BY type"
        ).fetchall()
        
        edge_counts = self.conn.execute(
            "SELECT type, COUNT(*) FROM edges GROUP BY type"
        ).fetchall()
        
        return {
            "nodes": {r[0]: r[1] for r in node_counts},
            "edges": {r[0]: r[1] for r in edge_counts},
            "total_nodes": sum(r[1] for r in node_counts),
            "total_edges": sum(r[1] for r in edge_counts)
        }
    
    def close(self):
        """Stäng anslutningen."""
        if self.conn:
            self.conn.close()
            self.conn = None
            LOGGER.info(f"GraphStore stängd: {self.db_path}")
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# === TEST BLOCK ===

if __name__ == "__main__":
    import tempfile
    
    print("=== GraphStore Test ===\n")
    
    # Skapa temporär databas (ta bort om den finns först)
    test_db = tempfile.mktemp(suffix=".duckdb")
    
    try:
        # Test 1: Skapa store och noder
        print("1. Skapar GraphStore...")
        store = GraphStore(test_db)
        print(f"   ✅ Databas skapad: {test_db}")
        
        # Test 2: Upsert noder
        print("\n2. Skapar noder...")
        store.upsert_node("Joakim Ekman", "Entity", aliases=["Jocke", "JE"])
        store.upsert_node("Adda", "Entity", aliases=["Adda AB"])
        store.upsert_node("doc-123", "Unit", properties={"title": "Mötesanteckningar"})
        store.upsert_node("AI", "Concept")
        print("   ✅ 4 noder skapade")
        
        # Test 3: Upsert kanter
        print("\n3. Skapar kanter...")
        store.upsert_edge("doc-123", "Joakim Ekman", "UNIT_MENTIONS")
        store.upsert_edge("doc-123", "Adda", "UNIT_MENTIONS")
        store.upsert_edge("doc-123", "AI", "DEALS_WITH")
        print("   ✅ 3 kanter skapade")
        
        # Test 4: Läs noder
        print("\n4. Läser noder...")
        node = store.get_node("Joakim Ekman")
        print(f"   Node: {node}")
        assert node['aliases'] == ["Jocke", "JE"], "Alias-fel"
        print("   ✅ Nod hämtad korrekt")
        
        # Test 5: Alias-sökning
        print("\n5. Söker på alias...")
        matches = store.find_nodes_by_alias("Jocke")
        print(f"   Hittade: {[m['id'] for m in matches]}")
        assert len(matches) == 1 and matches[0]['id'] == "Joakim Ekman"
        print("   ✅ Alias-sökning fungerar")
        
        # Test 6: Fuzzy-sökning
        print("\n6. Fuzzy-sökning...")
        matches = store.find_nodes_fuzzy("Joakim")
        print(f"   Hittade: {[m['id'] for m in matches]}")
        assert len(matches) >= 1
        print("   ✅ Fuzzy-sökning fungerar")
        
        # Test 7: Statistik
        print("\n7. Statistik...")
        stats = store.get_stats()
        print(f"   {stats}")
        assert stats['total_nodes'] == 4
        assert stats['total_edges'] == 3
        print("   ✅ Statistik korrekt")
        
        # Test 8: Upgrade canonical
        print("\n8. Testar upgrade_canonical...")
        store.upsert_node("Sänk", "Entity", aliases=[])
        store.upsert_edge("doc-123", "Sänk", "UNIT_MENTIONS")
        store.upgrade_canonical("Sänk", "Cenk Bisgen")
        
        # Verifiera
        old = store.get_node("Sänk")
        new = store.get_node("Cenk Bisgen")
        assert old is None, "Gamla noden borde vara borta"
        assert new is not None, "Nya noden borde finnas"
        assert "Sänk" in new['aliases'], "Gamla ID borde vara i aliases"
        
        # Verifiera att kanten uppdaterats
        edges = store.get_edges_to("Cenk Bisgen")
        assert len(edges) == 1 and edges[0]['source'] == "doc-123"
        print("   ✅ upgrade_canonical fungerar (nod + kanter)")
        
        # Test 9: Read-only mode
        print("\n9. Testar read_only mode...")
        store.close()
        ro_store = GraphStore(test_db, read_only=True)
        node = ro_store.get_node("Joakim Ekman")
        assert node is not None
        try:
            ro_store.upsert_node("Test", "Entity")
            raise AssertionError("Borde ha kastat RuntimeError")
        except RuntimeError as e:
            # Förväntat beteende - read_only mode ska kasta RuntimeError
            LOGGER.info(f"Read-only test OK: {e}")
            print(f"   ✅ Korrekt: {e}")
        ro_store.close()
        
        print("\n" + "="*40)
        print("✅ ALLA TESTER PASSERADE!")
        print("="*40)
        
    finally:
        # Cleanup
        import os
        for ext in ['', '.wal']:
            path = test_db + ext
            if os.path.exists(path):
                os.remove(path)
        print(f"\nStädade upp: {test_db}")

