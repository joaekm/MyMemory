import subprocess
import sys
import time
import os
import signal
import datetime
import re
import yaml

# Tjänsterna som ska startas
SERVICES = [
    {"path": "services/my_mem_file_retriever.py", "name": "File Retriever"},
    {"path": "services/my_mem_slack_collector.py", "name": "Slack Collector"},
    {"path": "services/my_mem_doc_converter.py", "name": "Doc Converter"},
    {"path": "services/my_mem_transcriber.py", "name": "Transcriber"},
    {"path": "services/my_mem_vector_indexer.py", "name": "Vector Indexer"},
]

processes = []

# Regex för UUID i filnamn
UUID_MD_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.md$')

def _ts():
    return datetime.datetime.now().strftime("[%H:%M:%S]")

def _load_config():
    """Ladda config för sökvägar"""
    config_path = os.path.join(os.path.dirname(__file__), 'config', 'my_mem_config.yaml')
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    return None

def _get_lake_files(lake_store):
    """Returnerar dict med {uuid: (filnamn, filepath)} för alla filer i Lake"""
    lake_files = {}
    if os.path.exists(lake_store):
        for f in os.listdir(lake_store):
            if f.endswith('.md') and not f.startswith('.'):
                match = UUID_MD_PATTERN.search(f)
                if match:
                    lake_files[match.group(1)] = (f, os.path.join(lake_store, f))
    return lake_files

def quick_health_check():
    """Snabb hälsokontroll av systemets dataflöde"""
    config = _load_config()
    if not config:
        return
    
    asset_store = os.path.expanduser(config['paths']['asset_store'])
    lake_store = os.path.expanduser(config['paths']['lake_store'])
    chroma_path = os.path.expanduser(config['paths']['chroma_db'])
    kuzu_path = os.path.expanduser(config['paths']['kuzu_db'])
    
    uuid_pattern = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.[a-zA-Z0-9]+$')
    
    # Räkna Assets
    assets_total = 0
    assets_invalid = 0
    if os.path.exists(asset_store):
        for f in os.listdir(asset_store):
            if not f.startswith('.'):
                assets_total += 1
                if not uuid_pattern.search(f):
                    assets_invalid += 1
    
    # Räkna Lake
    lake_count = 0
    if os.path.exists(lake_store):
        lake_count = len([f for f in os.listdir(lake_store) if f.endswith('.md') and not f.startswith('.')])
    
    # Räkna ChromaDB
    vector_count = 0
    vector_status = "?"
    try:
        import chromadb
        client = chromadb.PersistentClient(path=chroma_path)
        coll = client.get_collection(name="dfm_knowledge_base")
        vector_count = coll.count()
        vector_status = "✓" if vector_count == lake_count else f"⚠️ diff {abs(vector_count - lake_count)}"
    except:
        vector_status = "offline"
    
    # Räkna KuzuDB
    graph_count = 0
    graph_status = "?"
    try:
        import kuzu
        db = kuzu.Database(kuzu_path)
        conn = kuzu.Connection(db)
        res = conn.execute("MATCH (u:Unit) RETURN count(u)").get_next()
        graph_count = res[0]
        graph_status = "✓" if graph_count == lake_count else f"⚠️ diff {abs(graph_count - lake_count)}"
        del conn
        del db
    except:
        graph_status = "offline"
    
    # Bygg statusrad
    assets_status = "✓" if assets_invalid == 0 else f"⚠️ {assets_invalid} ogiltiga"
    
    print(f"📊 HEALTH: Assets {assets_total} ({assets_status}) | Lake {lake_count} | Vector {vector_count} ({vector_status}) | Graf {graph_count} ({graph_status})")
    print()
    
    # Returnera info för repair
    return {
        'lake_count': lake_count,
        'vector_count': vector_count,
        'graph_count': graph_count,
        'lake_store': lake_store,
        'chroma_path': chroma_path,
        'kuzu_path': kuzu_path
    }

def auto_repair(health_info):
    """Reparerar saknade filer i Vector och Graf"""
    if not health_info:
        return
    
    lake_count = health_info['lake_count']
    vector_count = health_info['vector_count']
    graph_count = health_info['graph_count']
    lake_store = health_info['lake_store']
    chroma_path = health_info['chroma_path']
    
    repaired = False
    
    # --- VECTOR REPAIR ---
    if vector_count < lake_count:
        try:
            import chromadb
            from chromadb.utils import embedding_functions
            
            lake_files = _get_lake_files(lake_store)
            lake_ids = set(lake_files.keys())
            
            client = chromadb.PersistentClient(path=chroma_path)
            emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
            coll = client.get_or_create_collection(name="dfm_knowledge_base", embedding_function=emb_fn)
            vector_ids = set(coll.get()['ids'])
            
            missing = lake_ids - vector_ids
            if missing:
                print(f"{_ts()} 🔧 REPAIR: Indexerar {len(missing)} saknade filer i Vector...")
                
                for uid in missing:
                    filename, filepath = lake_files[uid]
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            content = f.read()
                        
                        if not content.startswith("---"):
                            continue
                        parts = content.split("---", 2)
                        if len(parts) < 3:
                            continue
                        
                        metadata = yaml.safe_load(parts[1])
                        text = parts[2].strip()
                        
                        ai_summary = metadata.get('ai_summary') or ""
                        context_id = metadata.get('context_id') or "unknown"
                        timestamp = metadata.get('timestamp_created') or ""
                        
                        full_doc = f"FILENAME: {filename}\nCONTEXT: {context_id}\nSUMMARY: {ai_summary}\n\nCONTENT:\n{text[:8000]}"
                        
                        coll.upsert(
                            ids=[uid],
                            documents=[full_doc],
                            metadatas=[{"context": context_id, "timestamp": timestamp, "filename": filename}]
                        )
                    except Exception as e:
                        print(f"{_ts()} ⚠️ Kunde inte indexera {filename}: {e}")
                
                print(f"{_ts()} ✅ REPAIR: Vector klar")
                repaired = True
                
        except Exception as e:
            print(f"{_ts()} ❌ Vector repair misslyckades: {e}")
    
    # --- GRAPH REPAIR ---
    if graph_count < lake_count:
        print(f"{_ts()} 🔧 REPAIR: Kör Graph Builder för {lake_count - graph_count} saknade noder...")
        try:
            result = subprocess.run(
                [sys.executable, "services/my_mem_graph_builder.py"],
                capture_output=True,
                text=True,
                timeout=120
            )
            if result.returncode == 0:
                print(f"{_ts()} ✅ REPAIR: Graf klar")
                repaired = True
            else:
                print(f"{_ts()} ⚠️ Graph builder avslutade med fel")
        except subprocess.TimeoutExpired:
            print(f"{_ts()} ⚠️ Graph builder timeout (120s)")
        except Exception as e:
            print(f"{_ts()} ❌ Graph repair misslyckades: {e}")
    
    if repaired:
        print()

def start_all():
    print(f"\n--- MyMem Services (v5.0) ---\n")
    
    # Kör hälsokontroll och auto-repair
    health_info = quick_health_check()
    auto_repair(health_info)
    
    python_exec = sys.executable

    for service in SERVICES:
        script_path = service["path"]
        if not os.path.exists(script_path):
            print(f"{_ts()} ❌ {service['name']}: fil saknas")
            continue
            
        try:
            p = subprocess.Popen([python_exec, script_path])
            processes.append(p)
            time.sleep(0.8)  # Låt tjänsten starta och skriva sin egen output
        except Exception as e:
            print(f"{_ts()} ❌ {service['name']}: {e}")

    print(f"\n--- Ready ---\n")

def stop_all(signum, frame):
    print(f"\n{_ts()} Stänger ner...")
    for p in processes:
        try:
            p.terminate()
        except: pass
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, stop_all)
    start_all()
    while True:
        time.sleep(1)