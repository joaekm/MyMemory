import os
import sys

# Path setup för att hitta services
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.join(script_dir, '..')
sys.path.insert(0, project_root)

# Använd VectorService (SSOT för embedding-modell)
from services.utils.vector_service import get_vector_service

vector_service = get_vector_service("knowledge_base")
coll = vector_service.collection

print(f"--- RÅDATA-ANALYS: {vector_service.db_path} ---")
print(f"--- Embedding-modell: {vector_service.model_name} ---")

try:
    
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
    print(f"HARDFAIL: Krasch vid inspektion av vektordatabas: {e}")
    raise