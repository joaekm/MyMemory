"""
ContextBuilder - Pipeline v8.5

Ansvar:
- Deterministisk informationshämtning (INGEN AI)
- Kör ALLTID båda: search_lake + vector_db (kvalitet > hastighet)
- Filtrera på time_filter om angivet
- Deduplicera och returnera max ~50 kandidater med metadata+summary

ID-format: Alla IDs normaliseras till UUID för korrekt deduplicering
"""

import os
import re
import json
import yaml
import logging
import chromadb
from chromadb.utils import embedding_functions

# UUID-mönster för att extrahera från filnamn
UUID_PATTERN = re.compile(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', re.IGNORECASE)

# --- CONFIG LOADER ---
def _load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, 'config', 'my_mem_config.yaml'),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f:
                config = yaml.safe_load(f)
            return config
    raise FileNotFoundError("Config not found")

CONFIG = _load_config()
LOGGER = logging.getLogger('ContextBuilder')

LAKE_PATH = os.path.expanduser(CONFIG['paths'].get('lake_store', '~/MyMemory/Lake'))
CHROMA_PATH = os.path.expanduser(CONFIG['paths']['chroma_db'])
TAXONOMY_FILE = os.path.expanduser(CONFIG['paths'].get('taxonomy_file', '~/MyMemory/Index/my_mem_taxonomy.json'))
API_KEY = CONFIG['ai_engine']['api_key']

# Max kandidater att returnera
MAX_CANDIDATES = 50

# Token Economy: Hur många dokument som får fulltext i Planner-prompten
TOP_N_FULLTEXT = 3

# Viktningsfaktorer (v8.5: Alltid RELAXED)
LAKE_BOOST = 1.0


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


def _expand_keywords_via_graph(keywords: list, graph_paths: list) -> list:
    """
    Expandera keywords med subnoder från angivna graf-paths.
    
    Args:
        keywords: Ursprungliga sökord
        graph_paths: Lista med huvudnoder att expandera (från IntentRouter)
    
    Returns:
        Expanderad lista med sökord
    """
    expanded = set(keywords)  # Börja med ursprungliga
    
    for path in graph_paths:
        if path in TAXONOMY:
            subnodes = TAXONOMY[path]
            # Lägg till subnoder som extra söktermer
            for subnode in subnodes[:10]:  # Max 10 subnoder per huvudnod
                expanded.add(subnode)
            LOGGER.debug(f"Expanderade '{path}' med {len(subnodes[:10])} subnoder")
    
    return list(expanded)


def _search_lake(keywords: list) -> dict:
    """
    Sök i Lake efter exakta keyword-matchningar.
    Returnerar dict med {doc_id: doc_data}
    """
    if not keywords:
        return {}
    
    # Splitta eventuella sammansatta keywords (LLM kan returnera "Adda PoC" som ett keyword)
    expanded_keywords = []
    for kw in keywords:
        if ' ' in kw:
            expanded_keywords.extend(kw.split())
        else:
            expanded_keywords.append(kw)
    keywords = list(set(expanded_keywords))  # Dedup
    
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
            
            # Extrahera summary och timestamp från YAML-header
            summary = ""
            timestamp = ""
            if content.startswith("---"):
                try:
                    yaml_end = content.index("---", 3)
                    yaml_content = content[3:yaml_end]
                    metadata = yaml.safe_load(yaml_content)
                    summary = metadata.get('summary', '')[:200]
                    timestamp = metadata.get('timestamp', '')
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
                    "timestamp": timestamp,  # v8.5: För time_filter
                    "source": "LAKE",
                    "matched_keywords": matched_keywords,
                    "score": len(matched_keywords) / len(keywords),  # Hur många keywords matchade
                    "content": content  # Behålls för Planner
                }
                
        except Exception as e:
            LOGGER.debug(f"Kunde inte läsa {filename}: {e}")
            continue
    
    return hits


def _search_vector(query: str, n_results: int = 30) -> dict:
    """
    Sök i vektordatabasen (ChromaDB).
    Returnerar dict med {doc_id: doc_data}
    """
    hits = {}
    
    try:
        # Initiera ChromaDB (matchar my_mem_vector_indexer.py)
        emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        collection = client.get_collection(name="dfm_knowledge_base", embedding_function=emb_fn)
        
        results = collection.query(query_texts=[query], n_results=n_results)
        
        if results and results['ids'] and results['ids'][0]:
            for i, raw_id in enumerate(results['ids'][0]):
                # Normalisera ID till lowercase för konsekvent matchning med Lake
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
                    "timestamp": metadata.get('timestamp', ''),  # v8.5: För time_filter
                    "source": "VECTOR",
                    "score": score,
                    "distance": distance,
                    "content": content
                }
                
    except Exception as e:
        LOGGER.error(f"Vektorsökning misslyckades: {e}")
    
    return hits


def format_candidates_for_planner(candidates: list, top_n_fulltext: int = 3) -> str:
    """
    Formatera kandidater för Planner-prompten.
    
    Token Economy: Endast topp N kandidater får fulltext,
    resten får bara summary för att spara tokens.
    
    Args:
        candidates: Lista med kandidat-dicts (med 'content', 'summary', 'filename')
        top_n_fulltext: Antal som får fulltext (default 3)
    
    Returns:
        Formaterad sträng för prompt
    """
    if not candidates:
        return "Inga dokument hittades."
    
    lines = []
    for i, c in enumerate(candidates):
        filename = c.get('filename', 'unknown')
        score = c.get('score', 0)
        
        if i < top_n_fulltext:
            # Topp N: Inkludera fulltext
            content = c.get('content', c.get('summary', ''))
            lines.append(f"### [{i+1}] {filename} (score: {score:.2f})")
            lines.append(content[:5000] if content else '')  # Max 5000 chars per dokument
            lines.append("")
        else:
            # Resten: Bara summary
            summary = c.get('summary', '')[:200]
            lines.append(f"### [{i+1}] {filename} (score: {score:.2f}) [SUMMARY ONLY]")
            lines.append(summary)
            lines.append("")
    
    return "\n".join(lines)


def _filter_by_time(candidates: dict, time_filter: dict) -> dict:
    """
    Filtrera kandidater på tidsperiod.
    
    Args:
        candidates: Dict med {doc_id: doc_data}
        time_filter: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
    
    Returns:
        Filtrerad dict
    """
    if not time_filter:
        return candidates
    
    start = time_filter.get('start', '')
    end = time_filter.get('end', '')
    
    if not start and not end:
        return candidates
    
    filtered = {}
    excluded_count = 0
    
    for doc_id, doc in candidates.items():
        timestamp = doc.get('timestamp', '')
        
        # Om dokumentet saknar timestamp, behåll det (bättre recall)
        if not timestamp:
            filtered[doc_id] = doc
            continue
        
        # Extrahera datum-delen (YYYY-MM-DD)
        doc_date = timestamp[:10] if len(timestamp) >= 10 else timestamp
        
        # Kolla om dokumentet är inom intervallet
        in_range = True
        if start and doc_date < start:
            in_range = False
        if end and doc_date > end:
            in_range = False
        
        if in_range:
            filtered[doc_id] = doc
        else:
            excluded_count += 1
    
    if excluded_count > 0:
        LOGGER.info(f"time_filter: Exkluderade {excluded_count} dokument utanför {start} - {end}")
    
    return filtered


def _dedupe_and_rank(lake_hits: dict, vector_hits: dict, time_filter: dict = None) -> list:
    """
    Deduplicera, filtrera på tid, och ranka kandidater.
    
    Args:
        lake_hits: Träffar från Lake-sökning
        vector_hits: Träffar från vektorsökning  
        time_filter: Optional {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
    
    Returns:
        Sorterad lista med kandidater (max MAX_CANDIDATES)
    """
    # Slå ihop och deduplicera
    all_candidates = {}
    
    # LAKE-träffar
    for doc_id, doc in lake_hits.items():
        doc['score'] = doc.get('score', 0.5) * LAKE_BOOST
        all_candidates[doc_id] = doc
    
    # Vektor-träffar (lägg till om inte redan finns, annars kombinera score)
    for doc_id, doc in vector_hits.items():
        if doc_id in all_candidates:
            # Kombinera scores om dokumentet finns i båda
            existing = all_candidates[doc_id]
            existing['score'] = (existing['score'] + doc['score']) / 2
            existing['source'] = "LAKE+VECTOR"
        else:
            all_candidates[doc_id] = doc
    
    # v8.5: Filtrera på tidsperiod
    if time_filter:
        all_candidates = _filter_by_time(all_candidates, time_filter)
    
    # Sortera på score (högst först)
    ranked = sorted(all_candidates.values(), key=lambda x: x.get('score', 0), reverse=True)
    
    # Begränsa till MAX_CANDIDATES
    return ranked[:MAX_CANDIDATES]


def build_context(intent_data: dict, debug_trace: dict = None) -> dict:
    """
    Bygg kontext baserat på IntentRouter-output.
    
    Args:
        intent_data: Output från IntentRouter med:
            - keywords: Lista med sökord
            - mission_goal: Beskrivning av uppdraget (används som vector_query)
            - time_filter: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} eller None
        debug_trace: Dict för att samla debug-info (optional)
    
    Returns:
        dict med:
            - status: "OK" eller "NO_RESULTS"
            - candidates: Lista med kandidat-dokument
            - stats: Statistik över sökningen
    """
    keywords = intent_data.get('keywords', [])
    # v8.5: Härled vector_query från mission_goal
    vector_query = intent_data.get('mission_goal', '')
    time_filter = intent_data.get('time_filter')
    
    # Steg 1: Sök i Lake (alltid)
    lake_hits = _search_lake(keywords)
    LOGGER.info(f"Lake-sökning: {len(lake_hits)} träffar")
    
    # Steg 2: Sök i vektor (alltid - kvalitet > hastighet)
    vector_hits = _search_vector(vector_query) if vector_query else {}
    LOGGER.info(f"Vektor-sökning: {len(vector_hits)} träffar")
    
    # Steg 3: Deduplicera, filtrera på tid, och ranka
    candidates = _dedupe_and_rank(lake_hits, vector_hits, time_filter)
    
    # Bygg statistik
    stats = {
        "keywords": len(keywords),
        "lake_hits": len(lake_hits),
        "vector_hits": len(vector_hits),
        "after_dedup_and_filter": len(candidates),
        "time_filter": time_filter
    }
    
    # Spara till debug_trace
    if debug_trace is not None:
        debug_trace['context_builder'] = {
            "keywords": keywords,
            "vector_query": vector_query[:100] if vector_query else None,
            "time_filter": time_filter,
            "stats": stats
        }
        debug_trace['context_builder_candidates'] = [
            {"id": c['id'], "source": c['source'], "score": round(c.get('score', 0), 3)}
            for c in candidates[:10]  # Logga top 10
        ]
    
    # HARDFAIL om inga träffar
    if not candidates:
        LOGGER.warning(f"HARDFAIL: Inga träffar för keywords={keywords}, vector_query={vector_query}")
        return {
            "status": "NO_RESULTS",
            "reason": f"Sökning returnerade 0 träffar för keywords={keywords}",
            "suggestion": "Försök med bredare söktermer eller annan formulering",
            "candidates": [],
            "stats": stats
        }
    
    # Ta bort fulltext-content från kandidater (Planner får hämta det själv)
    # Behåll bara metadata + summary
    slim_candidates = []
    for c in candidates:
        slim_candidates.append({
            "id": c['id'],
            "filename": c['filename'],
            "summary": c['summary'],
            "timestamp": c.get('timestamp', ''),
            "source": c['source'],
            "score": round(c.get('score', 0), 3)
        })
    
    # Formatera för Planner (Token Economy)
    candidates_formatted = format_candidates_for_planner(candidates, TOP_N_FULLTEXT)
    
    return {
        "status": "OK",
        "candidates": slim_candidates,
        "candidates_full": candidates,  # Fullständig data för Planner
        "candidates_formatted": candidates_formatted,  # Formaterad för prompt
        "stats": stats
    }


def search(query: str, time_filter: dict = None) -> dict:
    """
    Enkel sökfunktion för Planner.
    
    Detta är en wrapper runt build_context() för enklare användning
    vid follow-up sökningar där Planner behöver göra extra sökningar.
    
    Args:
        query: Söksträng
        time_filter: Optional {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
    
    Returns:
        dict med candidates_full och status
    """
    # Enkel tokenisering för keywords
    keywords = [w for w in query.split() if len(w) >= 2]
    
    # Bygg intent_data för build_context
    intent_data = {
        "keywords": keywords,
        "mission_goal": query,
        "time_filter": time_filter
    }
    
    result = build_context(intent_data)
    
    return {
        "status": result.get("status", "NO_RESULTS"),
        "candidates_full": result.get("candidates_full", []),
        "stats": result.get("stats", {})
    }


# --- TEST ---
if __name__ == "__main__":
    # Enkel test
    test_intent = {
        "keywords": ["Strategi", "AI"],
        "mission_goal": "AI-strategi och framtidsplaner",
        "time_filter": {"start": "2025-12-01", "end": "2025-12-15"}
    }
    
    result = build_context(test_intent)
    print(f"Status: {result['status']}")
    print(f"Stats: {result['stats']}")
    print(f"Kandidater: {len(result['candidates'])}")
    for c in result['candidates'][:5]:
        print(f"  - {c['filename']} ({c['source']}, score={c['score']}, ts={c.get('timestamp', 'N/A')})")

