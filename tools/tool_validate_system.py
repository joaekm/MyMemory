import os
import sys
import yaml
import chromadb
import re
from chromadb.utils import embedding_functions

# Lägg till services i path för import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.my_mem_graph_builder import KuzuSession

# --- CONFIG LOADER ---
def ladda_yaml(filnamn):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, 'config', filnamn),
        os.path.join(script_dir, '..', 'config', filnamn)
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f: return yaml.safe_load(f)
    print(f"[FEL] Saknar {filnamn}")
    exit(1)

CONFIG = ladda_yaml('my_mem_config.yaml')

LAKE_STORE = os.path.expanduser(CONFIG['paths']['lake_store'])
ASSET_STORE = os.path.expanduser(CONFIG['paths']['asset_store'])
CHROMA_PATH = os.path.expanduser(CONFIG['paths']['chroma_db'])
KUZU_PATH = os.path.expanduser(CONFIG['paths']['kuzu_db'])

# Hämta extensions
DOC_EXTS = CONFIG.get('processing', {}).get('document_extensions', [])
AUDIO_EXTS = CONFIG.get('processing', {}).get('audio_extensions', [])

# Regex för Strict Mode
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.[a-zA-Z0-9]+$')
UUID_MD_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.md$')

def print_header(title):
    print(f"\n{'='*60}")
    print(f" {title}")
    print(f"{'='*60}")

def get_lake_ids():
    """Returnerar dict med {uuid: filnamn} för alla filer i Lake"""
    lake_ids = {}
    lake_files = [f for f in os.listdir(LAKE_STORE) if f.endswith('.md') and not f.startswith('.')]
    for f in lake_files:
        match = UUID_MD_PATTERN.search(f)
        if match:
            lake_ids[match.group(1)] = f
    return lake_ids

def validera_filer():
    print_header("1. FILSYSTEMS-AUDIT (Strict Mode)")
    
    all_assets = [f for f in os.listdir(ASSET_STORE) if not f.startswith('.')]
    doc_files = [f for f in all_assets if os.path.splitext(f)[1].lower() in DOC_EXTS]
    lake_files = [f for f in os.listdir(LAKE_STORE) if f.endswith('.md') and not f.startswith('.')]
    
    # 1.1 KONTROLLERA UUID-NAMNSTANDARD I ASSETS
    invalid_names = []
    for f in all_assets:
        if not UUID_SUFFIX_PATTERN.search(f):
            invalid_names.append(f)

    print(f"📦 Assets Totalt:     {len(all_assets)} st")
    
    if invalid_names:
        print(f"❌ [VARNING] Hittade {len(invalid_names)} filer i Assets som bryter mot namnstandarden!")
        for bad in invalid_names[:3]: print(f"   - {bad} (Saknar _[UUID])")
        if len(invalid_names) > 3: print("   ... (och fler)")
    else:
        print("✅ Alla filer i Assets följer standarden [Namn]_[UUID].")

    print(f"   - Dokument/.txt:  {len(doc_files)} st (Målvärde för Sjön)")
    print(f"🌊 Lake (Markdown):  {len(lake_files)} st")
    
    # 1.2 INTEGRITETS-CHECK (Lake vs Assets)
    # Filerna i Lake ska ha EXAKT samma basnamn som dokumenten i Assets.
    # Ex: Assets: "Rapport_123.pdf" -> Lake: "Rapport_123.md"
    
    asset_bases = {os.path.splitext(f)[0] for f in doc_files}
    lake_bases = {os.path.splitext(f)[0] for f in lake_files}
    
    missing_in_lake = asset_bases - lake_bases
    zombies_in_lake = lake_bases - asset_bases # Filer i sjön som inte har en källa

    if len(lake_files) == len(doc_files) and not missing_in_lake:
        print(f"\n✅ BALANS: {len(lake_files)} filer i Sjön matchar antalet källdokument.")
    else:
        if missing_in_lake:
            print(f"\n❌ SAKNAS: {len(missing_in_lake)} dokument har inte konverterats!")
            for m in list(missing_in_lake)[:3]: print(f"   - {m} (Finns ej i Sjön)")
        
        if zombies_in_lake:
             print(f"\n⚠️ ZOMBIES: {len(zombies_in_lake)} filer i Sjön saknar källfil i Assets (gammalt skräp?).")

    return len(lake_files)

def validera_chroma(expected_count, lake_ids):
    print_header("2. VEKTOR-AUDIT (CHROMA)")
    try:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        coll = client.get_collection(name="dfm_knowledge_base")
        count = coll.count()
        print(f"🧠 Vektorer i minnet: {count} st")
        
        if count == expected_count:
            print("✅ SYNKAD: Vektordatabasen matchar Sjön.")
        else:
            print(f"❌ OSYNKAD: Diff på {abs(count - expected_count)} dokument.")
            
            # Visa vilka som saknas
            vector_ids = set(coll.get()['ids'])
            lake_id_set = set(lake_ids.keys())
            
            missing_in_vector = lake_id_set - vector_ids
            if missing_in_vector:
                print(f"\n   Saknas i Vector ({len(missing_in_vector)} st):")
                for uid in missing_in_vector:
                    filename = lake_ids.get(uid, uid)
                    # Visa namn utan UUID för läsbarhet
                    display_name = filename.rsplit('_', 1)[0] if '_' in filename else filename
                    print(f"   - {display_name}")
            
            orphans_in_vector = vector_ids - lake_id_set
            if orphans_in_vector:
                print(f"\n   ⚠️ Föräldralösa i Vector ({len(orphans_in_vector)} st) - finns ej i Lake")

    except Exception as e:
        print(f"❌ KRITISKT FEL: Kunde inte läsa ChromaDB: {e}")

def validera_kuzu(expected_count, lake_ids):
    print_header("3. GRAF-AUDIT (KUZU)")
    try:
        with KuzuSession(KUZU_PATH, timeout=30, caller="tool_validate_system") as conn:
            res = conn.execute("MATCH (u:Unit) RETURN count(u)").get_next()
            unit_count = res[0]
            print(f"🕸️  Graf-noder (Units): {unit_count} st")
            
            if unit_count == expected_count:
                print("✅ SYNKAD: Grafen matchar Sjön.")
            else:
                print(f"❌ OSYNKAD: Grafen diffar med {abs(expected_count - unit_count)} noder.")
                
                # Hämta alla ID från grafen
                graph_ids = set()
                result = conn.execute("MATCH (u:Unit) RETURN u.id")
                while result.has_next():
                    graph_ids.add(result.get_next()[0])
                
                lake_id_set = set(lake_ids.keys())
                
                missing_in_graph = lake_id_set - graph_ids
                if missing_in_graph:
                    print(f"\n   Saknas i Graf ({len(missing_in_graph)} st):")
                    for uid in missing_in_graph:
                        filename = lake_ids.get(uid, uid)
                        # Visa namn utan UUID för läsbarhet
                        display_name = filename.rsplit('_', 1)[0] if '_' in filename else filename
                        print(f"   - {display_name}")
                    print(f"\n   Tips: Kör 'python services/my_mem_graph_builder.py' för att synka")

    except TimeoutError as e:
        print(f"❌ TIMEOUT: Kuzu låst för länge: {e}")
    except Exception as e:
        print(f"❌ KRITISKT FEL: Kunde inte läsa KuzuDB: {e}")

if __name__ == "__main__":
    print("=== MyMem System Validator (v3.0 - Diff Details) ===")
    lake_c = validera_filer()
    if lake_c > 0:
        lake_ids = get_lake_ids()
        validera_chroma(lake_c, lake_ids)
        validera_kuzu(lake_c, lake_ids)
    else:
        print("\nIngen data att validera i databaserna.")