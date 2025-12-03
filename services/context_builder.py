"""
ContextBuilder - Pipeline v6.0 Fas 2

Ansvar:
- Deterministisk informationshämtning (INGEN AI)
- Kör ALLTID båda: search_lake + vector_db (kvalitet > hastighet)
- Graf-expansion baserat på graph_paths från IntentRouter
- Viktning baserat på intent (STRICT prioriterar LAKE)
- Deduplicera och returnera max ~50 kandidater med metadata+summary
"""

import os
import json
import yaml
import logging
import chromadb
from chromadb.utils import embedding_functions

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
    raise FileNotFoundError("Config not found")

CONFIG = _load_config()
LOGGER = logging.getLogger('ContextBuilder')

LAKE_PATH = os.path.expanduser(CONFIG['paths'].get('lake_store', '~/MyMemory/Lake'))
CHROMA_PATH = os.path.expanduser(CONFIG['paths']['chroma_db'])
TAXONOMY_FILE = os.path.expanduser(CONFIG['paths'].get('taxonomy_file', '~/MyMemory/Index/my_mem_taxonomy.json'))
API_KEY = CONFIG['ai_engine']['api_key']

# Max kandidater att returnera
MAX_CANDIDATES = 50

# Viktningsfaktorer
LAKE_BOOST_STRICT = 1.3   # STRICT: LAKE-träffar får 30% boost
LAKE_BOOST_RELAXED = 1.0  # RELAXED: Ingen boost


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
                except:
                    pass
            
            if not summary:
                summary = content[:200].replace("\n", " ")
            
            # Kolla om något keyword matchar
            matched_keywords = []
            for kw in keywords:
                if kw.lower() in content_lower:
                    matched_keywords.append(kw)
            
            if matched_keywords:
                doc_id = filename.replace('.md', '')
                hits[doc_id] = {
                    "id": doc_id,
                    "filename": filename,
                    "summary": summary,
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
            for i, doc_id in enumerate(results['ids'][0]):
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


def _dedupe_and_rank(lake_hits: dict, vector_hits: dict, intent: str) -> list:
    """
    Deduplicera och ranka kandidater.
    
    Args:
        lake_hits: Träffar från Lake-sökning
        vector_hits: Träffar från vektorsökning  
        intent: "STRICT" eller "RELAXED"
    
    Returns:
        Sorterad lista med kandidater (max MAX_CANDIDATES)
    """
    # Slå ihop och deduplicera
    all_candidates = {}
    
    # LAKE-träffar (med boost för STRICT)
    boost = LAKE_BOOST_STRICT if intent == "STRICT" else LAKE_BOOST_RELAXED
    for doc_id, doc in lake_hits.items():
        doc['score'] = doc.get('score', 0.5) * boost
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
    
    # Sortera på score (högst först)
    ranked = sorted(all_candidates.values(), key=lambda x: x.get('score', 0), reverse=True)
    
    # Begränsa till MAX_CANDIDATES
    return ranked[:MAX_CANDIDATES]


def build_context(intent_data: dict, debug_trace: dict = None) -> dict:
    """
    Bygg kontext baserat på IntentRouter-output.
    
    Args:
        intent_data: Output från IntentRouter med:
            - intent: "STRICT" eller "RELAXED"
            - keywords: Lista med sökord
            - vector_query: Söksträng för vektorsökning
            - graph_paths: Lista med huvudnoder att expandera
            - time_filter: Tidsfilter (ej implementerat än)
        debug_trace: Dict för att samla debug-info (optional)
    
    Returns:
        dict med:
            - status: "OK" eller "NO_RESULTS"
            - candidates: Lista med kandidat-dokument
            - stats: Statistik över sökningen
    """
    intent = intent_data.get('intent', 'RELAXED')
    keywords = intent_data.get('keywords', [])
    vector_query = intent_data.get('vector_query', '')
    graph_paths = intent_data.get('graph_paths', [])
    
    # Steg 1: Expandera keywords via graf (om RELAXED och graph_paths angivna)
    if intent == "RELAXED" and graph_paths:
        expanded_keywords = _expand_keywords_via_graph(keywords, graph_paths)
        LOGGER.info(f"Graf-expansion: {len(keywords)} → {len(expanded_keywords)} keywords")
    else:
        expanded_keywords = keywords
    
    # Steg 2: Sök i Lake (alltid)
    lake_hits = _search_lake(expanded_keywords)
    LOGGER.info(f"Lake-sökning: {len(lake_hits)} träffar")
    
    # Steg 3: Sök i vektor (alltid - kvalitet > hastighet)
    vector_hits = _search_vector(vector_query) if vector_query else {}
    LOGGER.info(f"Vektor-sökning: {len(vector_hits)} träffar")
    
    # Steg 4: Deduplicera och ranka
    candidates = _dedupe_and_rank(lake_hits, vector_hits, intent)
    
    # Bygg statistik
    stats = {
        "original_keywords": len(keywords),
        "expanded_keywords": len(expanded_keywords),
        "lake_hits": len(lake_hits),
        "vector_hits": len(vector_hits),
        "after_dedup": len(candidates),
        "graph_paths_used": graph_paths
    }
    
    # Spara till debug_trace
    if debug_trace is not None:
        debug_trace['context_builder'] = {
            "intent": intent,
            "keywords_original": keywords,
            "keywords_expanded": expanded_keywords if expanded_keywords != keywords else None,
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
            "source": c['source'],
            "score": round(c.get('score', 0), 3)
        })
    
    return {
        "status": "OK",
        "candidates": slim_candidates,
        "candidates_full": candidates,  # Fullständig data för Planner
        "stats": stats
    }


# --- TEST ---
if __name__ == "__main__":
    # Enkel test
    test_intent = {
        "intent": "RELAXED",
        "keywords": ["Strategi", "AI"],
        "vector_query": "AI-strategi och framtidsplaner",
        "graph_paths": ["Strategi", "Teknologier"]
    }
    
    result = build_context(test_intent)
    print(f"Status: {result['status']}")
    print(f"Stats: {result['stats']}")
    print(f"Kandidater: {len(result['candidates'])}")
    for c in result['candidates'][:5]:
        print(f"  - {c['filename']} ({c['source']}, score={c['score']})")

