"""
GraphStore - DuckDB-baserad grafdatabas.

Relationell graf-modell med nodes/edges-tabeller.
Ersätter KuzuDB (LÖST-54).
"""

import os
import json
import uuid
import datetime
import logging
import threading
import time
import duckdb

# --- LOGGING ---
LOGGER = logging.getLogger('GraphStore')

# --- KANONISKA RELATIONER ---
AVAILABLE_RELATIONS = {
    "WORKS_AT": {
        "from": "Person",
        "to": "Aktör",
        "description": "Anställning eller konsultuppdrag hos ett bolag."
    },
    "MEMBER_OF": {
        "from": "Person",
        "to": "Organisation",
        "description": "Medlemskap i internt team, styrelse eller förening."
    },
    "LEADS": {
        "from": "Person",
        "to": "Projekt",
        "description": "Ansvarig ledare, projektledare eller PO."
    },
    "PART_OF": {
        "from": "Projekt",
        "to": "Aktör",
        "description": "Ett projekt som tillhör en kund eller organisation."
    },
    "USES": {
        "from": "Projekt",
        "to": "Teknologier",
        "description": "Verktyg eller språk som används i ett specifikt projekt."
    },
    "REPORTS_TO": {
        "from": "Person",
        "to": "Person",
        "description": "Hierarkisk koppling (t.ex. chef/medarbetare)."
    },
    "ASSOCIATED_WITH": {
        "from": "Valfri",
        "to": "Valfri",
        "description": "Fallback: Allmän koppling när ingen annan passar."
    }
}


class GraphStore:
    """
    Thread-safe grafdatabas med DuckDB backend.
    
        Schema:
        nodes(id, type, aliases, properties)
        edges(source, target, edge_type, properties)
        evidence(id, entity_name, master_node_candidate, context_description, source_file,
                 source_timestamp, extraction_pass, confidence, created_at)
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
        # DuckDB tillåter inte att öppna samma databasfil med olika konfigurationer samtidigt
        # Lösning: Använd alltid read_write om det finns en konflikt, eftersom read_write kan användas för både läsning och skrivning
        connection_attempted = False
        if read_only:
            try:
                self.conn = duckdb.connect(db_path, read_only=True)
                connection_attempted = True
            except Exception as e:
                error_str = str(e).lower()
                # Om det finns en konflikt, försök med read_write istället
                # Detta kan hända om en annan process redan har öppnat med read_write
                if "different configuration" in error_str or "can't open" in error_str or "connection error" in error_str:
                    LOGGER.warning(f"Kunde inte öppna med read_only=True (konflikt med befintlig anslutning), försöker med read_write: {e}")
                    # Vänta lite för att låta den andra anslutningen stängas om den gör det
                    import time
                    time.sleep(0.1)
                    try:
                        self.conn = duckdb.connect(db_path)
                        self.read_only = False  # Uppdatera flaggan
                        self._init_schema()
                        connection_attempted = True
                    except Exception as e2:
                        # Om det fortfarande misslyckas, kan det vara att read_only-anslutningen fortfarande är öppen
                        # I detta fall, vänta lite längre och försök igen
                        error_str2 = str(e2).lower()
                        if "different configuration" in error_str2 or "can't open" in error_str2 or "connection error" in error_str2:
                            LOGGER.warning(f"Första read_write-försöket misslyckades, väntar 0.5s och försöker igen: {e2}")
                            time.sleep(0.5)
                            try:
                                self.conn = duckdb.connect(db_path)
                                self.read_only = False
                                self._init_schema()
                                connection_attempted = True
                            except Exception as e3:
                                LOGGER.error(f"HARDFAIL: Kunde inte öppna GraphStore även efter retry: {e3}")
                                raise
                        else:
                            LOGGER.error(f"HARDFAIL: Kunde inte öppna GraphStore: {e2}")
                            raise
                else:
                    raise
        else:
            try:
                self.conn = duckdb.connect(db_path)
                self._init_schema()
                connection_attempted = True
            except Exception as e:
                error_str = str(e).lower()
                if "different configuration" in error_str or "can't open" in error_str or "connection error" in error_str:
                    # Det finns redan en read_only-anslutning, vänta lite och försök igen
                    LOGGER.warning(f"Kunde inte öppna med read_write (konflikt med read_only-anslutning), väntar 0.1s och försöker igen: {e}")
                    import time
                    time.sleep(0.1)
                    try:
                        self.conn = duckdb.connect(db_path)
                        self.read_only = False
                        self._init_schema()
                        connection_attempted = True
                    except Exception as e2:
                        error_str2 = str(e2).lower()
                        if "different configuration" in error_str2 or "can't open" in error_str2 or "connection error" in error_str2:
                            LOGGER.warning(f"Första retry misslyckades, väntar 0.5s och försöker igen: {e2}")
                            time.sleep(0.5)
                            try:
                                self.conn = duckdb.connect(db_path)
                                self.read_only = False
                                self._init_schema()
                                connection_attempted = True
                            except Exception as e3:
                                LOGGER.error(f"HARDFAIL: Kunde inte öppna GraphStore även efter retry: {e3}")
                                raise
                        else:
                            LOGGER.error(f"HARDFAIL: Kunde inte öppna GraphStore: {e2}")
                            raise
                else:
                    raise
        
        if not connection_attempted:
            raise RuntimeError("HARDFAIL: GraphStore connection failed - no connection attempted")
        
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
            # Evidence-tabell (lagrar LLM-bevis per entitet/masternod)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,
                    entity_name TEXT NOT NULL,
                    master_node_candidate TEXT NOT NULL,
                    context_description TEXT NOT NULL,
                    source_file TEXT NOT NULL,
                    source_timestamp TEXT,
                    extraction_pass TEXT,
                    confidence DOUBLE,
                    created_at TEXT
                )
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_entity ON evidence(entity_name)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_master ON evidence(master_node_candidate)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence(source_file)")
            
            # Pending Reviews-tabell (Kö för MCP-granskning)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_reviews (
                    entity_name TEXT PRIMARY KEY,
                    master_node TEXT NOT NULL,
                    similarity_score DOUBLE,
                    reason TEXT,
                    context JSON,
                    created_at TEXT
                )
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_reviews_score ON pending_reviews(similarity_score)")
        
        # Validation rules-tabell (anropas utanför lock för att undvika deadlock)
        # _init_validation_table hanterar sin egen lock
        self._init_validation_table()
    
    def _init_validation_table(self):
        """Skapa validation_rules tabell för att spara användarens beslut."""
        # Använd filbaserad lock för att säkerställa att bara en process skapar tabellen
        lock_file = self.db_path + ".validation_table.lock"
        max_retries = 10
        retry_delay = 0.2
        
        lock_acquired = False
        lock_fd = None
        
        try:
            # Försök få filbaserad lock
            for attempt in range(max_retries):
                try:
                    lock_fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    lock_acquired = True
                    break
                except FileExistsError:
                    # HARDFAIL: Lock finns redan (förväntat beteende vid concurrent access)
                    # Vänta och försök igen - detta är intentional för thread-safety
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                    else:
                        # Kunde inte få lock efter alla försök, antag att tabellen redan finns
                        LOGGER.debug(f"Kunde inte få lock för validation table (försök {attempt + 1}/{max_retries}), antag att tabellen redan finns")
                        return
            
            if lock_acquired:
                try:
                    # Försök skapa tabellen (IF NOT EXISTS hanterar concurrent creation)
                    try:
                        self.conn.execute("""
                            CREATE TABLE IF NOT EXISTS validation_rules (
                                id TEXT PRIMARY KEY,
                                entity_name TEXT NOT NULL,
                                master_node TEXT NOT NULL,
                                decision TEXT NOT NULL,
                                reason TEXT,
                                adjusted_name TEXT,
                                adjusted_master_node TEXT,
                                created_at TEXT,
                                similarity_score DOUBLE
                            )
                        """)
                    except Exception as db_error:
                        # Om tabellen redan finns eller databasen är låst, det är OK
                        error_msg = str(db_error).lower()
                        if "already exists" in error_msg or "duplicate" in error_msg or "locked" in error_msg:
                            # Tabellen finns redan eller DB är låst, fortsätt med index
                            pass
                        else:
                            raise  # Kasta vidare om det är ett annat fel
                    
                    # Skapa index (IF NOT EXISTS är säkert även om de redan finns)
                    self.conn.execute("CREATE INDEX IF NOT EXISTS idx_validation_entity ON validation_rules(entity_name)")
                    self.conn.execute("CREATE INDEX IF NOT EXISTS idx_validation_master ON validation_rules(master_node)")
                    self.conn.execute("CREATE INDEX IF NOT EXISTS idx_validation_decision ON validation_rules(decision)")
                finally:
                    # Frigör filbaserad lock
                    if lock_fd is not None:
                        try:
                            os.close(lock_fd)
                            os.unlink(lock_file)
                        except Exception as e:
                            # HARDFAIL: Logga men fortsätt (cleanup-fel ska inte krascha)
                            LOGGER.debug(f"Kunde inte frigöra lock-fil {lock_file}: {e}")
        except Exception as e:
            error_str = str(e)
            # Om tabellen redan finns (från annan process), det är OK
            if "already exists" in error_str.lower() or "duplicate" in error_str.lower():
                LOGGER.debug(f"Validation table already exists (from another process), continuing...")
                return
            
            # Om det är sista försöket, kasta felet
            LOGGER.error(f"HARDFAIL: _init_validation_table failed: {e}", exc_info=True)
            raise
    
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
        
        Args:
            id: Unikt nod-ID
            type: Nodtyp (Unit, Entity, Concept, Person)
            aliases: Lista med alternativa namn
            properties: Dict med extra egenskaper
        """
        if self.read_only:
            raise RuntimeError("HARDFAIL: Försöker skriva i read_only mode")
        
        aliases_json = json.dumps(aliases or [], ensure_ascii=False)
        properties_json = json.dumps(properties or {}, ensure_ascii=False)
        
        with self._lock:
            self.conn.execute("""
                INSERT INTO nodes (id, type, aliases, properties)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    type = EXCLUDED.type,
                    aliases = EXCLUDED.aliases,
                    properties = EXCLUDED.properties
            """, [id, type, aliases_json, properties_json])
    
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

    # --- EVIDENCE LAYER ---

    def _row_to_evidence(self, row) -> dict:
        """Intern helper för att mappa evidence-rad till dict."""
        if not row:
            return {}
        return {
            "id": row[0],
            "entity_name": row[1],
            "master_node_candidate": row[2],
            "context_description": row[3],
            "source_file": row[4],
            "source_timestamp": row[5],
            "extraction_pass": row[6],
            "confidence": row[7],
            "created_at": row[8],
        }

    def add_evidence(
        self,
        id: str,
        entity_name: str,
        master_node_candidate: str,
        context_description: str,
        source_file: str,
        source_timestamp: str = None,
        extraction_pass: str = None,
        confidence: float = None,
        created_at: str = None,
    ):
        """
        Spara eller uppdatera ett evidence-item.
        """
        if self.read_only:
            raise RuntimeError("HARDFAIL: Försöker skriva i read_only mode")

        if not created_at:
            created_at = datetime.datetime.now().isoformat()

        with self._lock:
            try:
                self.conn.execute(
                    """
                    INSERT INTO evidence (
                        id, entity_name, master_node_candidate, context_description,
                        source_file, source_timestamp, extraction_pass, confidence, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET
                        entity_name = EXCLUDED.entity_name,
                        master_node_candidate = EXCLUDED.master_node_candidate,
                        context_description = EXCLUDED.context_description,
                        source_file = EXCLUDED.source_file,
                        source_timestamp = EXCLUDED.source_timestamp,
                        extraction_pass = EXCLUDED.extraction_pass,
                        confidence = EXCLUDED.confidence,
                        created_at = EXCLUDED.created_at
                    """,
                    [
                        id,
                        entity_name,
                        master_node_candidate,
                        context_description,
                        source_file,
                        source_timestamp,
                        extraction_pass,
                        confidence,
                        created_at,
                    ],
                )
                self.conn.commit()
            except Exception as e:
                LOGGER.error(f"HARDFAIL: Kunde inte spara evidence {id}: {e}")
                raise

    def get_evidence_for_entity(self, entity_name: str, limit: int = 200) -> list[dict]:
        """
        Hämta evidence för en given entitet.
        """
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, entity_name, master_node_candidate, context_description,
                       source_file, source_timestamp, extraction_pass, confidence, created_at
                FROM evidence
                WHERE entity_name = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [entity_name, limit],
            ).fetchall()
        return [self._row_to_evidence(r) for r in rows]

    def get_evidence_by_masternode(self, master_node: str, limit: int = 200) -> list[dict]:
        """
        Hämta evidence för en viss masternod-kandidat.
        """
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, entity_name, master_node_candidate, context_description,
                       source_file, source_timestamp, extraction_pass, confidence, created_at
                FROM evidence
                WHERE master_node_candidate = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [master_node, limit],
            ).fetchall()
        return [self._row_to_evidence(r) for r in rows]
    
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
    
    def get_extraction_context(self, master_node: str) -> dict:
        """
        Hämta extraction context för en masternod (GODKÄNDA_REFERENSER och TIDIGARE_AVFÄRDADE_EXEMPEL).
        
        Args:
            master_node: Masternodens namn
            
        Returns:
            Dict med approved_references och rejected_examples
        """
        with self._lock:
            # GODKÄNDA_REFERENSER: De 20 vanligaste APPROVED-entiteterna
            approved_rows = self.conn.execute("""
                SELECT entity_name, COUNT(*) as count, MAX(created_at) as last_seen
                FROM validation_rules
                WHERE master_node = ? AND decision = 'APPROVED'
                GROUP BY entity_name
                ORDER BY count DESC, last_seen DESC
                LIMIT 20
            """, [master_node]).fetchall()
            
            approved_references = [
                {
                    "entity_name": row[0],
                    "count": row[1],
                    "last_seen": row[2]
                }
                for row in approved_rows
            ]
            
            # TIDIGARE_AVFÄRDADE_EXEMPEL: Alla REJECTED-entiteter med reason
            rejected_rows = self.conn.execute("""
                SELECT DISTINCT entity_name, reason
                FROM validation_rules
                WHERE master_node = ? AND decision = 'REJECTED'
            """, [master_node]).fetchall()
            
            rejected_examples = [
                {
                    "entity_name": row[0],
                    "reason": row[1] or ""
                }
                for row in rejected_rows
            ]
            
            return {
                "approved_references": approved_references,
                "rejected_examples": rejected_examples
            }
    
    def add_validation_rule(
        self,
        entity: str,
        master_node: str,
        decision: str,
        reason: str = None,
        adjusted_name: str = None,
        adjusted_master_node: str = None,
        similarity_score: float = None
    ):
        """
        Lägg till en validation rule (användarens beslut).
        
        Args:
            entity: Entitetens namn
            master_node: Masternodens namn
            decision: 'APPROVED', 'REJECTED', eller 'ADJUSTED'
            reason: Orsak (obligatorisk för REJECTED)
            adjusted_name: Nytt namn om decision är 'ADJUSTED'
            adjusted_master_node: Ny masternod om decision är 'ADJUSTED'
            similarity_score: Likhetsgrad mot referenser (0.0-1.0)
        """
        if not entity or not master_node or not decision:
            raise ValueError("HARDFAIL: entity, master_node och decision är obligatoriska")
        
        if decision == 'REJECTED' and not reason:
            raise ValueError("HARDFAIL: reason är obligatorisk för REJECTED-beslut")
        
        rule_id = str(uuid.uuid4())
        created_at = datetime.datetime.now().isoformat()
        
        with self._lock:
            try:
                self.conn.execute("""
                    INSERT INTO validation_rules 
                    (id, entity_name, master_node, decision, reason, adjusted_name, 
                     adjusted_master_node, created_at, similarity_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    rule_id, entity, master_node, decision, reason,
                    adjusted_name, adjusted_master_node, created_at, similarity_score
                ])
                self.conn.commit()
                LOGGER.debug(f"Validation rule tillagd: {entity} -> {master_node} ({decision})")
            except Exception as e:
                self.conn.rollback()
                LOGGER.error(f"HARDFAIL: Kunde inte lägga till validation rule: {e}")
                raise

    def get_validation_rule(self, entity: str) -> dict | None:
        """
        Hämta senaste validation rule för en entitet (case-insensitive).
        
        Args:
            entity: Entitetens namn
            
        Returns:
            Dict med regel-data eller None om ingen regel finns.
        """
        with self._lock:
            # Hämtar den senaste regeln (case-insensitive matchning)
            row = self.conn.execute("""
                SELECT id, entity_name, master_node, decision, reason, 
                       adjusted_name, adjusted_master_node, created_at, similarity_score
                FROM validation_rules
                WHERE entity_name ILIKE ?
                ORDER BY created_at DESC
                LIMIT 1
            """, [entity]).fetchone()
            
            if not row:
                return None
                
            return {
                "id": row[0],
                "entity_name": row[1],
                "master_node": row[2],
                "decision": row[3],
                "reason": row[4],
                "adjusted_name": row[5],
                "adjusted_master_node": row[6],
                "created_at": row[7],
                "similarity_score": row[8]
            }

    # --- PENDING REVIEWS (Shadowgraph Queue) ---

    def add_pending_review(self, entity: str, master_node: str, score: float, reason: str, context: dict = None):
        """Lägg till en entitet i granskningskön."""
        if self.read_only:
            raise RuntimeError("HARDFAIL: Försöker skriva i read_only mode")
            
        created_at = datetime.datetime.now().isoformat()
        context_json = json.dumps(context or {}, ensure_ascii=False)
        
        with self._lock:
            self.conn.execute("""
                INSERT INTO pending_reviews (entity_name, master_node, similarity_score, reason, context, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (entity_name) DO UPDATE SET
                    master_node = EXCLUDED.master_node,
                    similarity_score = EXCLUDED.similarity_score,
                    reason = EXCLUDED.reason,
                    context = EXCLUDED.context,
                    created_at = EXCLUDED.created_at
            """, [entity, master_node, score, reason, context_json, created_at])

    def get_pending_reviews(self, limit: int = 50) -> list[dict]:
        """Hämta entiteter som väntar på granskning."""
        with self._lock:
            rows = self.conn.execute("""
                SELECT entity_name, master_node, similarity_score, reason, context, created_at
                FROM pending_reviews
                ORDER BY similarity_score ASC, created_at DESC
                LIMIT ?
            """, [limit]).fetchall()
            
        return [{
            "entity_name": r[0],
            "master_node": r[1],
            "similarity_score": r[2],
            "reason": r[3],
            "context": json.loads(r[4]) if r[4] else {},
            "created_at": r[5]
        } for r in rows]

    def delete_pending_review(self, entity_name: str):
        """Ta bort från kön (när beslut är fattat)."""
        if self.read_only:
            raise RuntimeError("HARDFAIL: Försöker skriva i read_only mode")
            
        with self._lock:
            self.conn.execute("DELETE FROM pending_reviews WHERE entity_name = ?", [entity_name])

