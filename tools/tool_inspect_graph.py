import os
import sys

# Lägg till services i path för import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.my_mem_graph_builder import KuzuSession

# Sökväg till din databas
db_path = os.path.expanduser("~/MyMemory/Index/KuzuDB")

# ID på filen om "Industritorgets Arkitektur" (Cenk/Jocke-mötet)
target_id = "39b32268-1bb8-4fb0-9ca6-d0bf7d2af784"

def inspektera_nod():
    try:
        with KuzuSession(db_path, timeout=30, caller="tool_inspect_graph") as conn:
            print(f"🔍 Inspekterar Unit ID: {target_id}")
            
            # 1. Finns noden alls?
            check = conn.execute(f'MATCH (u:Unit {{id: "{target_id}"}}) RETURN u.summary').get_next()
            if not check:
                print("❌ Noden finns inte ens i databasen!")
                return
            else:
                print("✅ Noden existerar i databasen.")
                print(f"   Summary preview: {check[0][:50]}...")

            # 2. Vilka relationer har den?
            # Vi letar efter (Unit)-[:DEALS_WITH]->(Concept)
            relations = conn.execute(f'''
                MATCH (u:Unit {{id: "{target_id}"}})-[r:DEALS_WITH]->(c:Concept)
                RETURN c.id
            ''')
            
            results = []
            while relations.has_next():
                results.append(relations.get_next()[0])
                
            print(f"\n🔗 Antal kopplingar hittade: {len(results)}")
            
            if len(results) == 0:
                print("😱 BEVISAT: Noden är helt isolerad! Den saknar kopplingar.")
                print("   (Detta bekräftar att Graph Builder ignorerade 'graph_master_node')")
            else:
                print(f"   Kopplad till: {results}")
                if "Arkitektur" in results:
                    print("   🤔 Hmmm. Den verkar faktiskt vara kopplad till Arkitektur. Hypotesen var fel.")

    except TimeoutError as e:
        print(f"❌ TIMEOUT: Kuzu låst för länge: {e}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    inspektera_nod()
