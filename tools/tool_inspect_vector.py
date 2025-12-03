import chromadb
import os
import yaml
from chromadb.utils import embedding_functions

# Config-hack för snabbhet
base_path = os.path.expanduser("~/MyMemory")
chroma_path = os.path.join(base_path, "Index/ChromaDB")

print(f"--- RÅDATA-ANALYS: {chroma_path} ---")

try:
    client = chromadb.PersistentClient(path=chroma_path)
    emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    coll = client.get_collection(name="dfm_knowledge_base", embedding_function=emb_fn)
    
    query = "Industritorget"
    print(f"🔎 Söker efter: '{query}'")
    
    results = coll.query(
        query_texts=[query],
        n_results=5
    )
    
    if not results['ids'][0]:
        print("❌ Inga träffar alls i databasen.")
    else:
        print(f"✅ Hittade {len(results['ids'][0])} träffar. Visar innehåll:\n")
        
        for i, uid in enumerate(results['ids'][0]):
            filename = results['metadatas'][0][i].get('filename', 'Okänd fil')
            content = results['documents'][0][i]
            dist = results['distances'][0][i]
            
            print(f"--- TRÄFF {i+1} (Avstånd: {dist:.4f}) ---")
            print(f"📂 Fil: {filename}")
            print(f"🆔 ID:  {uid}")
            print(f"📝 INNEHÅLL (Första 200 tecken):")
            print(f"'{content[:200]}...'") # <-- HÄR SER VI OM DATAN ÄR TOM
            print("-" * 40)

except Exception as e:
    print(f"Krasch: {e}")