"""
GraphStore - DuckDB-baserad grafdatabas.

Relationell graf-modell med nodes/edges-tabeller.
Ersätter KuzuDB (LÖST-54).
"""

import os
import json
import logging
import threading
import duckdb
from datetime import datetime

# --- LOGGING ---
LOGGER = logging.getLogger('GraphStore')


class GraphStore:
    """
    Thread-safe grafdatabas med DuckDB backend.
    
    Schema:
        nodes(id, type, aliases, properties)
        edges(source, target, edge_type, properties)
    """
    
    def __init__(self, db_path: str, read_only: bool = False):
        """
        Öppna eller skapa en grafdatabas.
        
        Args:
            db_path: Sökväg till DuckDB-filen
            read_only: Om True, öppna i read-only läge
        """
        self.db_path = db_path
        self.read_only = read_only
        self._lock = threading.Lock()
        
        # Skapa mappen om den inte finns
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        # Öppna anslutning
        if read_only:
            self.conn = duckdb.connect(db_path, read_only=True)
        else:
            self.conn = duckdb.connect(db_path)
            self._init_schema()
        
        LOGGER.info(f"GraphStore öppnad: {db_path} (read_only={read_only})")
    
    def _init_schema(self):
        """Skapa tabeller om de inte finns."""
        with self._lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    aliases TEXT,
                    properties TEXT
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS edges (
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    edge_type TEXT NOT NULL,
                    properties TEXT,
                    PRIMARY KEY (source, target, edge_type)
                )
            """)
            # Index för snabbare sökningar
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target)")
    
    def close(self):
        """Stäng databasanslutningen."""
        with self._lock:
            if self.conn:
                self.conn.close()
                self.conn = None
                LOGGER.info(f"GraphStore stängd: {self.db_path}")
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
    
    # --- NODE OPERATIONS ---
    
    def get_node(self, node_id: str) -> dict | None:
        """
        Hämta en nod med givet ID.
        
        Returns:
            dict med {id, type, aliases, properties} eller None
        """
        with self._lock:
            result = self.conn.execute(
                "SELECT id, type, aliases, properties FROM nodes WHERE id = ?",
                [node_id]
            ).fetchone()
        
        if not result:
            return None
        
        return {
            "id": result[0],
            "type": result[1],
            "aliases": json.loads(result[2]) if result[2] else [],
            "properties": json.loads(result[3]) if result[3] else {}
        }
    
    def find_nodes_by_type(self, node_type: str) -> list[dict]:
        """
        Hitta alla noder av en viss typ.
        
        Args:
            node_type: Nodtyp att söka efter
        
        Returns:
            Lista med noder
        """
        with self._lock:
            results = self.conn.execute(
                "SELECT id, type, aliases, properties FROM nodes WHERE type = ?",
                [node_type]
            ).fetchall()
        
        nodes = []
        for row in results:
            nodes.append({
                "id": row[0],
                "type": row[1],
                "aliases": json.loads(row[2]) if row[2] else [],
                "properties": json.loads(row[3]) if row[3] else {}
            })
        return nodes
    
    def find_nodes_by_alias(self, alias: str) -> list[dict]:
        """
        Hitta noder där alias matchar.
        
        Söker i aliases-arrayen (JSON).
        
        Args:
            alias: Alias att söka efter
        
        Returns:
            Lista med matchande noder
        """
        # DuckDB stöder JSON-funktioner
        with self._lock:
            results = self.conn.execute("""
                SELECT id, type, aliases, properties 
                FROM nodes 
                WHERE aliases IS NOT NULL 
                  AND list_contains(aliases::TEXT[]::TEXT[], ?)
            """, [alias]).fetchall()
        
        nodes = []
        for row in results:
            nodes.append({
                "id": row[0],
                "type": row[1],
                "aliases": json.loads(row[2]) if row[2] else [],
                "properties": json.loads(row[3]) if row[3] else {}
            })
        return nodes
    
def upsert_node(self, id: str, type: str, aliases: list = None, properties: dict = None):
        """
        Skapa eller uppdatera en nod.
        Hanterar merge av properties för att bevara system-metadata.
        
        Args:
            id: Unikt nod-ID
            type: Nodtyp (Unit, Entity, Concept, Person)
            aliases: Lista med alternativa namn
            properties: Dict med extra egenskaper
        """
        if self.read_only:
            raise RuntimeError("HARDFAIL: Försöker skriva i read_only mode")
        
        new_props = properties or {}
        
        with self._lock:
            # 1. Hämta existerande egenskaper för att bevara systemfält
            existing = self.conn.execute(
                "SELECT properties FROM nodes WHERE id = ?", [id]
            ).fetchone()
            
            final_props = {}
            
            if existing:
                # Noden finns - bevara existerande data, skriv över med nytt
                try:
                    current_props = json.loads(existing[0]) if existing[0] else {}
                except:
                    current_props = {}
                
                final_props = current_props.copy()
                final_props.update(new_props)
                
            else:
                # Ny nod - Initiera systemfält
                now_ts = datetime.now().isoformat()
                defaults = {
                    "last_retrieved_at": now_ts,
                    "retrieved_times": 0,
                    "last_refined_at": "never"
                }
                final_props = defaults
                final_props.update(new_props)

            aliases_json = json.dumps(aliases or [], ensure_ascii=False)
            properties_json = json.dumps(final_props, ensure_ascii=False)

            # 2. Skriv till DB (UPSERT)
            self.conn.execute("""
                INSERT INTO nodes (id, type, aliases, properties)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    type = EXCLUDED.type,
                    aliases = EXCLUDED.aliases,
                    properties = EXCLUDED.properties
            """, [id, type, aliases_json, properties_json])

def register_usage(self, node_ids: list):
        """
        Registrera att noder har använts i ett svar (Relevans).
        Ökar retrieved_times och sätter last_retrieved_at till nu.
        """
        if not node_ids: return
        
        now_ts = datetime.now().isoformat()
        
        with self._lock:
            # Batch-uppdatering via Read-Modify-Write för säkerhet
            placeholders = ','.join(['?'] * len(node_ids))
            rows = self.conn.execute(
                f"SELECT id, properties FROM nodes WHERE id IN ({placeholders})", 
                node_ids
            ).fetchall()
            
            for r in rows:
                nid = r[0]
                try:
                    props = json.loads(r[1]) if r[1] else {}
                except:
                    props = {}
                
                # Uppdatera räknare
                count = props.get('retrieved_times', 0)
                if not isinstance(count, int): count = 0
                
                props['retrieved_times'] = count + 1
                props['last_retrieved_at'] = now_ts
                
                # Skriv tillbaka
                self.conn.execute(
                    "UPDATE nodes SET properties = ? WHERE id = ?",
                    [json.dumps(props, ensure_ascii=False), nid]
                )
        
        LOGGER.info(f"Registered usage for {len(node_ids)} nodes")

    def get_refinement_candidates(self, limit: int = 50) -> list[dict]:
        """
        Hämta kandidater för Dreamer-underhåll enligt 80/20-principen.
        
        - 80% Relevans: Heta noder (nyligen använda).
        - 20% Underhåll: Glömda noder (aldrig städade eller gamla).
        """
        relevance_limit = int(limit * 0.8)
        maintenance_limit = limit - relevance_limit
        
        candidates = []
        
        with self._lock:
            # 1. Relevans (Heta noder) - Sortera på last_retrieved_at DESC
            rel_rows = self.conn.execute(f"""
                SELECT id, type, aliases, properties 
                FROM nodes 
                ORDER BY json_extract_string(properties, '$.last_retrieved_at') DESC
                LIMIT ?
            """, [relevance_limit]).fetchall()
            
            # 2. Underhåll (Glömda noder)
            # Prioritera 'never' (ostädade) först, sedan äldsta datum
            maint_rows = self.conn.execute(f"""
                SELECT id, type, aliases, properties 
                FROM nodes 
                ORDER BY 
                    CASE WHEN json_extract_string(properties, '$.last_refined_at') = 'never' THEN 0 ELSE 1 END,
                    json_extract_string(properties, '$.last_refined_at') ASC
                LIMIT ?
            """, [maintenance_limit]).fetchall()
            
            # Slå ihop och deduplicera
            seen_ids = set()
            for r in rel_rows + maint_rows:
                if r[0] not in seen_ids:
                    candidates.append({
                        "id": r[0],
                        "type": r[1],
                        "aliases": json.loads(r[2]) if r[2] else [],
                        "properties": json.loads(r[3]) if r[3] else {}
                    })
                    seen_ids.add(r[0])
                    
        return candidates
    
    def delete_node(self, node_id: str) -> bool:
        """
        Ta bort en nod och alla dess kanter.
        
        Args:
            node_id: ID på noden att ta bort
        
        Returns:
            True om noden fanns och togs bort
        """
        if self.read_only:
            raise RuntimeError("HARDFAIL: Försöker skriva i read_only mode")
        
        with self._lock:
            # Ta bort kanter först
            self.conn.execute(
                "DELETE FROM edges WHERE source = ? OR target = ?",
                [node_id, node_id]
            )
            # Ta bort noden
            result = self.conn.execute(
                "DELETE FROM nodes WHERE id = ? RETURNING id",
                [node_id]
            ).fetchone()
            
            return result is not None
    
    # --- EDGE OPERATIONS ---
    
    def get_edges_from(self, node_id: str) -> list[dict]:
        """
        Hämta alla utgående kanter från en nod.
        
        Returns:
            Lista med {source, target, type, properties}
        """
        with self._lock:
            results = self.conn.execute(
                "SELECT source, target, edge_type, properties FROM edges WHERE source = ?",
                [node_id]
            ).fetchall()
        
        edges = []
        for row in results:
            edges.append({
                "source": row[0],
                "target": row[1],
                "type": row[2],
                "properties": json.loads(row[3]) if row[3] else {}
            })
        return edges
    
    def get_edges_to(self, node_id: str) -> list[dict]:
        """
        Hämta alla inkommande kanter till en nod.
        
        Returns:
            Lista med {source, target, type, properties}
        """
        with self._lock:
            results = self.conn.execute(
                "SELECT source, target, edge_type, properties FROM edges WHERE target = ?",
                [node_id]
            ).fetchall()
        
        edges = []
        for row in results:
            edges.append({
                "source": row[0],
                "target": row[1],
                "type": row[2],
                "properties": json.loads(row[3]) if row[3] else {}
            })
        return edges
    
    def upsert_edge(self, source: str, target: str, edge_type: str, properties: dict = None):
        """
        Skapa eller uppdatera en kant.
        
        Args:
            source: Käll-nod ID
            target: Mål-nod ID
            edge_type: Typ av relation (DEALS_WITH, CREATED_BY, etc.)
            properties: Extra egenskaper
        """
        if self.read_only:
            raise RuntimeError("HARDFAIL: Försöker skriva i read_only mode")
        
        properties_json = json.dumps(properties or {}, ensure_ascii=False)
        
        with self._lock:
            self.conn.execute("""
                INSERT INTO edges (source, target, edge_type, properties)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (source, target, edge_type) DO UPDATE SET
                    properties = EXCLUDED.properties
            """, [source, target, edge_type, properties_json])
    
    def delete_edge(self, source: str, target: str, edge_type: str) -> bool:
        """
        Ta bort en specifik kant.
        
        Returns:
            True om kanten fanns och togs bort
        """
        if self.read_only:
            raise RuntimeError("HARDFAIL: Försöker skriva i read_only mode")
        
        with self._lock:
            result = self.conn.execute(
                "DELETE FROM edges WHERE source = ? AND target = ? AND edge_type = ? RETURNING source",
                [source, target, edge_type]
            ).fetchone()
            
            return result is not None
    
    # --- STATISTICS ---
    
    def get_stats(self) -> dict:
        """
        Hämta statistik om grafen.
        
        Returns:
            dict med total_nodes, total_edges, nodes per typ, edges per typ
        """
        with self._lock:
            # Räkna noder per typ
            node_counts = self.conn.execute(
                "SELECT type, COUNT(*) FROM nodes GROUP BY type"
            ).fetchall()
            
            # Räkna kanter per typ
            edge_counts = self.conn.execute(
                "SELECT edge_type, COUNT(*) FROM edges GROUP BY edge_type"
            ).fetchall()
        
        nodes_dict = {row[0]: row[1] for row in node_counts}
        edges_dict = {row[0]: row[1] for row in edge_counts}
        
        return {
            "total_nodes": sum(nodes_dict.values()),
            "total_edges": sum(edges_dict.values()),
            "nodes": nodes_dict,
            "edges": edges_dict
        }
    
    # --- SEARCH HELPERS ---
    
    def find_nodes_fuzzy(self, term: str, limit: int = 10) -> list[dict]:
        """
        Fuzzy-sök efter noder baserat på ID eller alias.
        
        Args:
            term: Sökterm
            limit: Max antal resultat
        
        Returns:
            Lista med matchande noder
        """
        # Sök i id och aliases
        with self._lock:
            results = self.conn.execute("""
                SELECT id, type, aliases, properties 
                FROM nodes 
                WHERE id ILIKE ? 
                   OR (aliases IS NOT NULL AND aliases ILIKE ?)
                LIMIT ?
            """, [f"%{term}%", f"%{term}%", limit]).fetchall()
        
        nodes = []
        for row in results:
            nodes.append({
                "id": row[0],
                "type": row[1],
                "aliases": json.loads(row[2]) if row[2] else [],
                "properties": json.loads(row[3]) if row[3] else {}
            })
        return nodes
    
    def get_related_units(self, entity_id: str, limit: int = 10) -> list[str]:
        """
        Hitta alla Units som nämner en viss Entity.
        
        Args:
            entity_id: Entity-nodens ID
            limit: Max antal resultat
        
        Returns:
            Lista med Unit-IDs
        """
        with self._lock:
            results = self.conn.execute("""
                SELECT DISTINCT source 
                FROM edges 
                WHERE target = ? AND edge_type = 'UNIT_MENTIONS'
                LIMIT ?
            """, [entity_id, limit]).fetchall()
        
        return [row[0] for row in results]

    # --- DREAMER SUPPORT ---

    def add_pending_review(self, entity: str, master_node: str, score: float, reason: str, context: dict):
        """
        Lägg till en manuell granskning (för Dreamer).
        """
        import uuid
        
        review_id = str(uuid.uuid4())
        context_json = json.dumps(context, ensure_ascii=False)
        
        with self._lock:
            # Skapa tabellen om den saknas
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_reviews (
                    id TEXT PRIMARY KEY,
                    entity TEXT,
                    master_node TEXT,
                    score FLOAT,
                    reason TEXT,
                    context TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            self.conn.execute("""
                INSERT INTO pending_reviews (id, entity, master_node, score, reason, context)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [review_id, entity, master_node, score, reason, context_json])
            
            LOGGER.info(f"Saved pending review: {entity} vs {master_node} ({score})")

    def merge_nodes_into(self, target_id: str, source_id: str):
        """
        Slå ihop source_id in i target_id.
        Flytta alla relationer och alias. Radera source_id.
        """
        if self.read_only:
            raise RuntimeError("HARDFAIL: Read-only mode")

        with self._lock:
            # 1. Flytta UTGÅENDE kanter (source -> X) till (target -> X)
            # Uppdatera bara om relationen inte redan finns
            self.conn.execute("""
                UPDATE edges 
                SET source = ? 
                WHERE source = ? 
                  AND NOT EXISTS (
                      SELECT 1 FROM edges e2 
                      WHERE e2.source = ? AND e2.target = edges.target AND e2.edge_type = edges.edge_type
                  )
            """, [target_id, source_id, target_id])
            
            # 2. Flytta INKOMMANDE kanter (X -> source) till (X -> target)
            self.conn.execute("""
                UPDATE edges 
                SET target = ? 
                WHERE target = ? 
                  AND NOT EXISTS (
                      SELECT 1 FROM edges e2 
                      WHERE e2.source = edges.source AND e2.target = ? AND e2.edge_type = edges.edge_type
                  )
            """, [target_id, source_id, target_id])
            
            # 3. Radera kvarvarande kanter (dubbletter som inte flyttades)
            self.conn.execute("DELETE FROM edges WHERE source = ? OR target = ?", [source_id, source_id])
            
            # 4. Flytta ALIASES
            res_source = self.conn.execute("SELECT aliases FROM nodes WHERE id = ?", [source_id]).fetchone()
            source_aliases = json.loads(res_source[0]) if res_source and res_source[0] else []
            
            res_target = self.conn.execute("SELECT aliases FROM nodes WHERE id = ?", [target_id]).fetchone()
            target_aliases = json.loads(res_target[0]) if res_target and res_target[0] else []
            
            # Slå ihop och deduplicera
            new_aliases = list(set(target_aliases + source_aliases + [source_id]))
            
            self.conn.execute("UPDATE nodes SET aliases = ? WHERE id = ?", [json.dumps(new_aliases, ensure_ascii=False), target_id])
            
            # 5. Radera source nod
            self.conn.execute("DELETE FROM nodes WHERE id = ?", [source_id])
            
            LOGGER.info(f"Merged {source_id} into {target_id}")
