"""
ContextBuilder - Pipeline v7.5 "Time-Aware Reranking"

Ansvar:
- Deterministisk informationshämtning (INGEN AI)
- Kör Lake + Vektor parallellt
- Returnera topp 50 kandidater med metadata+summary
- Ingen intent-logik (Planner hanterar urval)
- TIME-AWARE RERANKING: Boostar nyare dokument automatiskt

ID-format: Alla IDs normaliseras till UUID för korrekt deduplicering
"""

import os
import re
import json
import yaml
import logging
from datetime import datetime

import chromadb
from chromadb.utils import embedding_functions
import dateutil.parser
import kuzu

# UUID-mönster för att extrahera från filnamn
UUID_PATTERN = re.compile(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', re.IGNORECASE)

# --- CONFIG LOADER ---
def _load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, 'config', 'my_mem_config.yaml'),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f:
                config = yaml.safe_load(f)
            return config
    raise FileNotFoundError("HARDFAIL: Config not found")

CONFIG = _load_config()
LOGGER = logging.getLogger('ContextBuilder')

LAKE_PATH = os.path.expanduser(CONFIG['paths'].get('lake_store', '~/MyMemory/Lake'))
CHROMA_PATH = os.path.expanduser(CONFIG['paths']['chroma_db'])
KUZU_PATH = os.path.expanduser(CONFIG['paths']['kuzu_db'])
TAXONOMY_FILE = os.path.expanduser(CONFIG['paths'].get('taxonomy_file', '~/MyMemory/Index/my_mem_taxonomy.json'))

# Max kandidater att returnera
MAX_CANDIDATES = 50

# --- RERANKING CONFIG (Pipeline v7.5) ---
RERANKING_CONFIG = CONFIG.get('reranking', {})
BOOST_STRENGTH = RERANKING_CONFIG.get('boost_strength', 0.3)
LEGACY_FACTOR = RERANKING_CONFIG.get('legacy_factor_days', 7.0)
RELEVANCE_THRESHOLD = RERANKING_CONFIG.get('relevance_threshold', 0.0)
TOP_N_FULLTEXT = RERANKING_CONFIG.get('top_n_fulltext', 3)
RECALL_LIMIT = RERANKING_CONFIG.get('recall_limit', 50)


def _load_taxonomy() -> dict:
    """Ladda hela taxonomin (huvudnoder + subnoder)."""
    try:
        if os.path.exists(TAXONOMY_FILE):
            with open(TAXONOMY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        LOGGER.warning(f"Kunde inte ladda taxonomi: {e}")
    return {}


TAXONOMY = _load_taxonomy()


# --- GRAF-LÄSNING (KuzuDB) ---

# Singleton för read-only connection
_KUZU_DB = None
_KUZU_CONN = None

def _get_kuzu_connection():
    """Readonly connection till KuzuDB för grafsökningar."""
    global _KUZU_DB, _KUZU_CONN
    
    if _KUZU_CONN is not None:
        return _KUZU_CONN
    
    if not os.path.exists(KUZU_PATH):
        LOGGER.warning(f"KuzuDB finns inte: {KUZU_PATH}")
        return None
    
    try:
        _KUZU_DB = kuzu.Database(KUZU_PATH)
        _KUZU_CONN = kuzu.Connection(_KUZU_DB)
        return _KUZU_CONN
    except Exception as e:
        LOGGER.error(f"Kunde inte ansluta till KuzuDB: {e}")
        return None


def get_graph_context_for_search(keywords: list, entities: list) -> str:
    """
    Hämta graf-kontext för söktermerna.
    Hjälper Planner att hitta kreativa spår att utforska.
    
    Args:
        keywords: Sökord från IntentRouter
        entities: Entiteter från IntentRouter
    
    Returns:
        Formaterad sträng med grafkopplingar
    """
    conn = _get_kuzu_connection()
    if conn is None:
        return "(Graf ej tillgänglig)"
    
    try:
        lines = []
        search_terms = keywords + [e.split(':')[-1] if ':' in e else e for e in entities]
        
        for term in search_terms[:5]:  # Max 5 termer
            # Fuzzy match mot Entity-namn och aliases
            result = conn.execute("""
                MATCH (e:Entity)
                WHERE e.id CONTAINS $term OR list_any(e.aliases, x -> x CONTAINS $term)
                RETURN e.id, e.type, e.aliases
                LIMIT 3
            """, {"term": term})
            
            matches = []
            while result.has_next():
                row = result.get_next()
                entity_id = row[0]
                entity_type = row[1]
                aliases = row[2] or []
                
                # Hitta relaterade dokument (Units)
                rel_result = conn.execute("""
                    MATCH (u:Unit)-[:UNIT_MENTIONS]->(e:Entity {id: $id})
                    RETURN u.title
                    LIMIT 3
                """, {"id": entity_id})
                
                related_docs = []
                while rel_result.has_next():
                    doc_row = rel_result.get_next()
                    if doc_row[0]:
                        related_docs.append(doc_row[0][:30])
                
                match_info = f"  - {entity_id} ({entity_type})"
                if aliases:
                    match_info += f" [alias: {', '.join(aliases[:3])}]"
                if related_docs:
                    match_info += f"\n    → Nämns i: {', '.join(related_docs)}"
                matches.append(match_info)
            
            if matches:
                lines.append(f'"{term}":')
                lines.extend(matches)
        
        if not lines:
            return "(Inga grafkopplingar hittades för söktermerna)"
        
        return "\n".join(lines)
    
    except Exception as e:
        LOGGER.error(f"get_graph_context_for_search error: {e}")
        return f"(Kunde inte hämta grafkontext: {e})"


# --- RERANKING FUNCTIONS (Pipeline v7.5) ---

def _parse_date(meta: dict) -> datetime | None:
    """
    Robust datumextraktion från metadata.
    Returnerar None vid fel (ingen boost, inget straff).
    
    Söker i: timestamp_created (Lake), timestamp (Vector), date
    """
    # Prova olika nycklar
    date_str = meta.get('timestamp_created') or meta.get('timestamp') or meta.get('date')
    if not date_str:
        return None
    
    try:
        # dateutil hanterar ISO8601 med timezone
        return dateutil.parser.parse(str(date_str)).replace(tzinfo=None)
    except Exception as e:
        LOGGER.warning(f"Kunde inte parsa datum '{date_str}': {e}")
        return None


def _calculate_hybrid_score(doc: dict, original_score: float) -> float:
    """
    Beräknar hybrid-score med Relevance Gate + Inverse Age Boost.
    
    Relevance Gate: Endast dokument med score >= RELEVANCE_THRESHOLD får boost.
    Detta förhindrar att irrelevant spam från igår vinner över relevant content.
    
    Formula: final_score = original_score * (1 + time_boost)
    where time_boost = BOOST_STRENGTH / ((days_old / LEGACY_FACTOR) + 1)
    """
    # RELEVANCE GATE: Skräp förblir skräp
    if original_score < RELEVANCE_THRESHOLD:
        doc['debug_gate'] = 'BLOCKED'
        doc['debug_score_final'] = original_score
        return original_score
    
    doc_date = _parse_date(doc)
    
    # Inget datum = ingen boost (men inget straff heller)
    if not doc_date:
        doc['debug_gate'] = 'NO_DATE'
        doc['debug_score_final'] = original_score
        return original_score
    
    # Beräkna ålder i dagar
    now = datetime.now()
    days_old = (now - doc_date).days
    
    # Skydd mot framtida datum (bugg i data)
    if days_old < 0:
        LOGGER.warning(f"Framtida datum i dokument: {doc.get('filename')}")
        days_old = 0
    
    # Inverse Age Boost
    time_boost = BOOST_STRENGTH / ((days_old / LEGACY_FACTOR) + 1)
    final_score = original_score * (1 + time_boost)
    
    # Debug-info
    doc['debug_gate'] = 'BOOSTED'
    doc['debug_score_orig'] = round(original_score, 4)
    doc['debug_score_final'] = round(final_score, 4)
    doc['debug_age_days'] = days_old
    doc['debug_boost'] = round(time_boost, 4)
    
    # KALIBRERINGSLOGG: Skriv ut scores för att hitta rätt threshold
    LOGGER.info(f"RERANK: {doc.get('filename', 'unknown')[:30]} | "
                f"orig={original_score:.3f} | final={final_score:.3f} | "
                f"age={days_old}d | gate={doc['debug_gate']}")
    
    return final_score


def _search_lake(keywords: list) -> dict:
    """
    Sök i Lake efter exakta keyword-matchningar.
    Returnerar dict med {doc_id: doc_data}
    """
    if not keywords:
        return {}
    
    hits = {}
    if not os.path.exists(LAKE_PATH):
        LOGGER.warning(f"Lake path finns inte: {LAKE_PATH}")
        return hits
    
    try:
        files = [f for f in os.listdir(LAKE_PATH) if f.endswith('.md')]
    except Exception as e:
        LOGGER.error(f"Kunde inte lista Lake-filer: {e}")
        return hits
    
    for filename in files:
        filepath = os.path.join(LAKE_PATH, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            content_lower = content.lower()
            
            # Extrahera summary från YAML-header
            summary = ""
            if content.startswith("---"):
                try:
                    yaml_end = content.index("---", 3)
                    yaml_content = content[3:yaml_end]
                    metadata = yaml.safe_load(yaml_content)
                    summary = metadata.get('summary', '')[:200]
                except Exception as e:
                    LOGGER.debug(f"Kunde inte parsa YAML-header i {filename}: {e}")
            
            if not summary:
                summary = content[:200].replace("\n", " ")
            
            # Kolla om något keyword matchar
            matched_keywords = []
            for kw in keywords:
                if kw.lower() in content_lower:
                    matched_keywords.append(kw)
            
            if matched_keywords:
                # Extrahera UUID från filnamnet för korrekt deduplicering
                uuid_match = UUID_PATTERN.search(filename)
                if uuid_match:
                    doc_id = uuid_match.group(1).lower()
                else:
                    # Fallback till filnamn om UUID inte hittas
                    doc_id = filename.replace('.md', '')
                    LOGGER.warning(f"Kunde inte extrahera UUID från {filename}")
                
                hits[doc_id] = {
                    "id": doc_id,
                    "filename": filename,
                    "summary": summary,
                    "source": "LAKE",
                    "matched_keywords": matched_keywords,
                    "score": len(matched_keywords) / len(keywords),
                    "content": content
                }
                
        except Exception as e:
            LOGGER.debug(f"Kunde inte läsa {filename}: {e}")
            continue
    
    return hits


def _search_vector(query: str, n_results: int = None) -> dict:
    """
    Sök i vektordatabasen (ChromaDB).
    Returnerar dict med {doc_id: doc_data}
    
    RECALL HORIZON FIX: Hämtar RECALL_LIMIT (50) för att ge Rerankern tillräckligt material.
    """
    if n_results is None:
        n_results = RECALL_LIMIT  # Default 50 från config
    hits = {}
    
    if not query:
        return hits
    
    try:
        # Initiera ChromaDB
        emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        collection = client.get_collection(name="dfm_knowledge_base", embedding_function=emb_fn)
        
        results = collection.query(query_texts=[query], n_results=n_results)
        
        if results and results['ids'] and results['ids'][0]:
            for i, raw_id in enumerate(results['ids'][0]):
                doc_id = raw_id.lower()
                distance = results['distances'][0][i] if results['distances'] else 1.0
                metadata = results['metadatas'][0][i] if results['metadatas'] else {}
                content = results['documents'][0][i] if results['documents'] else ""
                
                # Konvertera distance till score (lägre distance = högre score)
                score = max(0, 1 - distance)
                
                hits[doc_id] = {
                    "id": doc_id,
                    "filename": metadata.get('filename', f"{doc_id}.md"),
                    "summary": metadata.get('summary', content[:200] if content else ''),
                    "source": "VECTOR",
                    "score": score,
                    "distance": distance,
                    "content": content
                }
                
    except Exception as e:
        LOGGER.error(f"Vektorsökning misslyckades: {e}")
    
    return hits


def _dedupe_and_rank(lake_hits: dict, vector_hits: dict) -> list:
    """
    Deduplicera och ranka kandidater med TIME-AWARE RERANKING.
    
    Pipeline v7.5: Applicerar hybrid scoring baserat på relevans + färskhet.
    
    Returns:
        Sorterad lista med kandidater (max MAX_CANDIDATES)
    """
    all_candidates = {}
    
    # Lake-träffar
    for doc_id, doc in lake_hits.items():
        all_candidates[doc_id] = doc
    
    # Vektor-träffar (lägg till om inte redan finns, annars kombinera score)
    for doc_id, doc in vector_hits.items():
        if doc_id in all_candidates:
            existing = all_candidates[doc_id]
            existing['score'] = (existing['score'] + doc['score']) / 2
            existing['source'] = "LAKE+VECTOR"
            # Behåll timestamp från vektor-metadata om den finns
            if 'timestamp' in doc and 'timestamp' not in existing:
                existing['timestamp'] = doc['timestamp']
        else:
            all_candidates[doc_id] = doc
    
    # NYTT: Applicera hybrid scoring (Time-Aware Reranking)
    for doc_id, doc in all_candidates.items():
        original_score = doc.get('score', 0.5)
        doc['hybrid_score'] = _calculate_hybrid_score(doc, original_score)
    
    # Sortera på hybrid_score (inte score)
    ranked = sorted(all_candidates.values(), key=lambda x: x.get('hybrid_score', 0), reverse=True)
    
    return ranked[:MAX_CANDIDATES]


def format_candidates_for_planner(candidates: list, top_n_fulltext: int = None) -> str:
    """
    Formatera kandidater för Planner-prompten.
    Topp N dokument får FULLTEXT, övriga får SUMMARY.
    
    Pipeline v7.5: Default är nu TOP_N_FULLTEXT (3) från config.
    "Lost in the Middle"-fix: Färre fulltext = bättre LLM-attention.
    
    Args:
        candidates: Lista med kandidat-dokument
        top_n_fulltext: Antal dokument som får fulltext (default från config)
    
    Returns:
        Formaterad sträng för Planner-prompten
    """
    if top_n_fulltext is None:
        top_n_fulltext = TOP_N_FULLTEXT  # Default 3 från config
    formatted_output = []
    
    for i, doc in enumerate(candidates):
        is_top_tier = i < top_n_fulltext
        
        # Välj innehåll baserat på position
        if is_top_tier:
            content = doc.get('content', doc.get('summary', ''))[:8000]  # Max 8000 tecken per doc
            type_label = "FULL_TEXT"
        else:
            content = doc.get('summary', '')[:300]
            type_label = "SUMMARY"
        
        entry = (
            f"=== DOKUMENT {i+1} [{type_label}] ===\n"
            f"ID: {doc.get('id', 'unknown')[:12]}...\n"
            f"Fil: {doc.get('filename', 'Unknown')}\n"
            f"Källa: {doc.get('source', 'Unknown')}\n"
            f"Score: {doc.get('score', 0):.2f}\n"
            f"INNEHÅLL:\n{content}\n"
            f"{'=' * 40}\n"
        )
        formatted_output.append(entry)
    
    return "\n".join(formatted_output)


def build_context(keywords: list, entities: list = None, time_filter: dict = None, 
                  vector_query: str = None, debug_trace: dict = None) -> dict:
    """
    Bygg kontext genom att söka i Lake och Vektor parallellt.
    
    Pipeline v7.0: Förenklad signatur utan intent-logik.
    
    Args:
        keywords: Lista med sökord
        entities: Lista med entiteter (t.ex. ["Person: Cenk"]) - används för utökad sökning
        time_filter: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} (ej implementerat än)
        vector_query: Söksträng för vektorsökning (om None, bygg från keywords)
        debug_trace: Dict för att samla debug-info (optional)
    
    Returns:
        dict med:
            - status: "OK" eller "NO_RESULTS"
            - candidates: Lista med kandidat-dokument (slim)
            - candidates_full: Lista med fullständiga dokument
            - stats: Statistik över sökningen
    """
    # Expandera keywords med entity-namn
    search_keywords = list(keywords) if keywords else []
    if entities:
        for entity in entities:
            # Extrahera namn från "Person: Cenk" -> "Cenk"
            if ':' in entity:
                name = entity.split(':', 1)[1].strip()
                if name and name not in search_keywords:
                    search_keywords.append(name)
            elif entity not in search_keywords:
                search_keywords.append(entity)
    
    # Bygg vector_query om inte angiven
    if not vector_query and search_keywords:
        vector_query = " ".join(search_keywords)
    
    # Sök i Lake
    lake_hits = _search_lake(search_keywords)
    LOGGER.info(f"Lake-sökning: {len(lake_hits)} träffar")
    
    # Sök i vektor
    vector_hits = _search_vector(vector_query) if vector_query else {}
    LOGGER.info(f"Vektor-sökning: {len(vector_hits)} träffar")
    
    # Deduplicera och ranka
    candidates = _dedupe_and_rank(lake_hits, vector_hits)
    
    # Bygg statistik
    stats = {
        "keywords": search_keywords,
        "lake_hits": len(lake_hits),
        "vector_hits": len(vector_hits),
        "after_dedup": len(candidates)
    }
    
    # Spara till debug_trace
    if debug_trace is not None:
        debug_trace['context_builder'] = {
            "keywords": search_keywords,
            "entities": entities,
            "vector_query": vector_query,
            "stats": stats
        }
        debug_trace['context_builder_candidates'] = [
            {"id": c['id'], "source": c['source'], "score": round(c.get('score', 0), 3)}
            for c in candidates[:10]
        ]
    
    # HARDFAIL om inga träffar
    if not candidates:
        LOGGER.warning(f"Inga träffar för keywords={search_keywords}")
        return {
            "status": "NO_RESULTS",
            "reason": f"Sökning returnerade 0 träffar för keywords={search_keywords}",
            "suggestion": "Försök med bredare söktermer",
            "candidates": [],
            "candidates_full": [],
            "candidates_formatted": "",
            "stats": stats
        }
    
    # Skapa slim version för debug/logging
    slim_candidates = []
    for c in candidates:
        slim_candidates.append({
            "id": c['id'],
            "filename": c['filename'],
            "summary": c['summary'],
            "source": c['source'],
            "score": round(c.get('score', 0), 3),
            "hybrid_score": round(c.get('hybrid_score', 0), 3),
            "debug_gate": c.get('debug_gate', 'N/A'),
            "debug_age_days": c.get('debug_age_days', 'N/A')
        })
    
    # Formatera för Planner: Topp 5 får FULLTEXT, övriga får SUMMARY
    formatted_output = format_candidates_for_planner(candidates)
    
    # Hämta graf-kontext för att hjälpa Planner hitta kreativa spår
    graph_context = get_graph_context_for_search(search_keywords, entities or [])
    
    return {
        "status": "OK",
        "candidates": slim_candidates,
        "candidates_full": candidates,
        "candidates_formatted": formatted_output,
        "graph_context": graph_context,
        "stats": stats
    }


def search(query: str, n_results: int = 30) -> dict:
    """
    Enkel sökfunktion för Planner-loopens extra sökningar.
    
    Args:
        query: Söksträng (används för både Lake och Vektor)
        n_results: Max antal resultat
    
    Returns:
        dict med candidates
    """
    keywords = query.split()
    return build_context(
        keywords=keywords,
        vector_query=query
    )


# --- TEST ---
if __name__ == "__main__":
    # Enkel test
    result = build_context(
        keywords=["Strategi", "AI"],
        entities=["Person: Cenk Bisgen"],
        vector_query="AI-strategi och framtidsplaner"
    )
    print(f"Status: {result['status']}")
    print(f"Stats: {result['stats']}")
    print(f"Kandidater: {len(result['candidates'])}")
    for c in result['candidates'][:5]:
        print(f"  - {c['filename']} ({c['source']}, score={c['score']})")
