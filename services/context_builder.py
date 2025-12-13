"""
ContextBuilder - Pipeline v7.0

Ansvar:
- Deterministisk informationshämtning (INGEN AI)
- Kör Lake + Vektor parallellt
- Returnera topp 50 kandidater med metadata+summary
- Ingen intent-logik (Planner hanterar urval)

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
TAXONOMY_FILE = os.path.expanduser(CONFIG['paths'].get('taxonomy_file', '~/MyMemory/Index/my_mem_taxonomy.json'))

# Max kandidater att returnera
MAX_CANDIDATES = 50


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


def _search_vector(query: str, n_results: int = 30) -> dict:
    """
    Sök i vektordatabasen (ChromaDB).
    Returnerar dict med {doc_id: doc_data}
    """
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
    Deduplicera och ranka kandidater.
    Ingen intent-baserad viktning - neutral kombination.
    
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
        else:
            all_candidates[doc_id] = doc
    
    # Sortera på score (högst först)
    ranked = sorted(all_candidates.values(), key=lambda x: x.get('score', 0), reverse=True)
    
    return ranked[:MAX_CANDIDATES]


def format_candidates_for_planner(candidates: list, top_n_fulltext: int = 5) -> str:
    """
    Formatera kandidater för Planner-prompten.
    Topp N dokument får FULLTEXT, övriga får SUMMARY.
    
    Args:
        candidates: Lista med kandidat-dokument
        top_n_fulltext: Antal dokument som får fulltext (default 5)
    
    Returns:
        Formaterad sträng för Planner-prompten
    """
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
            "score": round(c.get('score', 0), 3)
        })
    
    # Formatera för Planner: Topp 5 får FULLTEXT, övriga får SUMMARY
    formatted_output = format_candidates_for_planner(candidates)
    
    return {
        "status": "OK",
        "candidates": slim_candidates,
        "candidates_full": candidates,
        "candidates_formatted": formatted_output,
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
