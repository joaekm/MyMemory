import os
import yaml
import chromadb
import kuzu
import re
import datetime
import logging
from chromadb.utils import embedding_functions

# Enkel loggning för CLI-verktyg
logging.basicConfig(level=logging.WARNING, format='%(levelname)s - %(message)s')
LOGGER = logging.getLogger('SystemValidator')

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
# Asset sub-folders
RECORDINGS_FOLDER = os.path.expanduser(CONFIG['paths']['asset_recordings'])
TRANSCRIPTS_FOLDER = os.path.expanduser(CONFIG['paths']['asset_transcripts'])
DOCUMENTS_FOLDER = os.path.expanduser(CONFIG['paths']['asset_documents'])
SLACK_FOLDER = os.path.expanduser(CONFIG['paths']['asset_slack'])
SESSIONS_FOLDER = os.path.expanduser(CONFIG['paths']['asset_sessions'])
ASSET_SUBFOLDERS = [RECORDINGS_FOLDER, TRANSCRIPTS_FOLDER, DOCUMENTS_FOLDER, SLACK_FOLDER, SESSIONS_FOLDER]

CHROMA_PATH = os.path.expanduser(CONFIG['paths']['chroma_db'])
KUZU_PATH = os.path.expanduser(CONFIG['paths']['kuzu_db'])
LOG_FILE = os.path.expanduser(CONFIG['logging']['log_file_path'])

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
    
    # Samla alla filer från undermapparna
    all_assets = []
    folder_counts = {}
    
    for folder in ASSET_SUBFOLDERS:
        if os.path.exists(folder):
            folder_name = os.path.basename(folder)
            files = [f for f in os.listdir(folder) if not f.startswith('.') and os.path.isfile(os.path.join(folder, f))]
            folder_counts[folder_name] = len(files)
            all_assets.extend(files)
    
    doc_files = [f for f in all_assets if os.path.splitext(f)[1].lower() in DOC_EXTS]
    lake_files = [f for f in os.listdir(LAKE_STORE) if f.endswith('.md') and not f.startswith('.')]
    
    # 1.1 KONTROLLERA UUID-NAMNSTANDARD I ASSETS
    invalid_names = []
    for f in all_assets:
        if not UUID_SUFFIX_PATTERN.search(f):
            invalid_names.append(f)

    print(f"📦 Assets Totalt:     {len(all_assets)} st")
    for folder_name, count in folder_counts.items():
        print(f"   - {folder_name}: {count} st")
    
    if invalid_names:
        print(f"❌ [VARNING] Hittade {len(invalid_names)} filer som bryter mot namnstandarden!")
        for bad in invalid_names[:10]:  # Visa max 10
            print(f"   - {bad}")
        if len(invalid_names) > 10:
            print(f"   ... och {len(invalid_names) - 10} till")
    else:
        print("✅ Alla filer i Assets följer standarden [Namn]_[UUID].")

    print(f"   - Dokument/.txt:  {len(doc_files)} st (Målvärde för Sjön)")
    print(f"🌊 Lake (Markdown):  {len(lake_files)} st")
    
    # 1.2 INTEGRITETS-CHECK (Lake vs Assets)
    # Filerna i Lake ska ha EXAKT samma basnamn som dokumenten i Assets.
    
    asset_bases = {os.path.splitext(f)[0] for f in doc_files}
    lake_bases = {os.path.splitext(f)[0] for f in lake_files}
    
    missing_in_lake = asset_bases - lake_bases
    zombies_in_lake = lake_bases - asset_bases

    if len(lake_files) == len(doc_files) and not missing_in_lake:
        print(f"\n✅ BALANS: {len(lake_files)} filer i Sjön matchar antalet källdokument.")
    else:
        if missing_in_lake:
            print(f"\n❌ SAKNAS I LAKE: {len(missing_in_lake)} dokument har inte konverterats!")
            for m in sorted(list(missing_in_lake)[:10]):
                print(f"   - {m}")
            if len(missing_in_lake) > 10:
                print(f"   ... och {len(missing_in_lake) - 10} till")
        
        if zombies_in_lake:
            print(f"\n⚠️ ZOMBIES I LAKE: {len(zombies_in_lake)} filer i Sjön saknar källfil i Assets:")
            for z in sorted(list(zombies_in_lake)[:10]):
                print(f"   - {z}")

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
        LOGGER.error(f"Kunde inte läsa ChromaDB: {e}")
        print(f"❌ KRITISKT FEL: Kunde inte läsa ChromaDB: {e}")

def validera_kuzu(expected_count, lake_ids):
    print_header("3. GRAF-AUDIT (KUZU)")
    try:
        db = kuzu.Database(KUZU_PATH)
        conn = kuzu.Connection(db)
        
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
        
        del conn
        del db

    except Exception as e:
        LOGGER.error(f"Kunde inte läsa KuzuDB: {e}")
        print(f"❌ KRITISKT FEL: Kunde inte läsa KuzuDB: {e}")

def rensa_gammal_logg():
    """Rensar loggfilen på rader äldre än 24 timmar."""
    print_header("4. LOGG-RENSNING")
    
    if not os.path.exists(LOG_FILE):
        print(f"⚠️ Loggfil finns inte: {LOG_FILE}")
        return
    
    try:
        now = datetime.datetime.now()
        cutoff = now - datetime.timedelta(hours=24)
        
        # Läs alla rader
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        original_count = len(lines)
        kept_lines = []
        
        # Logg-format: "2025-12-11 14:06:33,526 - TRANS - INFO - ..."
        timestamp_pattern = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
        
        for line in lines:
            match = timestamp_pattern.match(line)
            if match:
                try:
                    line_time = datetime.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                    if line_time >= cutoff:
                        kept_lines.append(line)
                except ValueError as e:
                    # Kunde inte parsa tidsstämpel, behåll raden
                    LOGGER.debug(f"Kunde inte parsa tidsstämpel: {e}")
                    kept_lines.append(line)
            else:
                # Rad utan tidsstämpel (t.ex. fortsättning av felmeddelande), behåll
                kept_lines.append(line)
        
        removed_count = original_count - len(kept_lines)
        
        if removed_count > 0:
            # Skriv tillbaka de kvarvarande raderna
            with open(LOG_FILE, 'w', encoding='utf-8') as f:
                f.writelines(kept_lines)
            print(f"🧹 Rensade {removed_count} rader äldre än 24h")
            print(f"   Innan: {original_count} rader → Efter: {len(kept_lines)} rader")
        else:
            print(f"✅ Ingen rensning behövdes ({original_count} rader, alla inom 24h)")
            
    except Exception as e:
        LOGGER.error(f"Fel vid loggrensning: {e}")
        print(f"❌ Fel vid loggrensning: {e}")

def run_startup_checks():
    """
    Kör alla valideringar och returnerar health_info för auto_repair.
    Används av start_services.py vid uppstart.
    """
    print("=== MyMem System Validator ===")
    
    lake_c = validera_filer()
    lake_ids = get_lake_ids() if lake_c > 0 else {}
    
    # Hämta counts för health_info
    vector_count = 0
    graph_count = 0
    
    if lake_c > 0:
        # Chroma
        try:
            client = chromadb.PersistentClient(path=CHROMA_PATH)
            coll = client.get_collection(name="dfm_knowledge_base")
            vector_count = coll.count()
            validera_chroma(lake_c, lake_ids)
        except Exception as e:
            LOGGER.error(f"Kunde inte läsa ChromaDB: {e}")
            print(f"❌ KRITISKT FEL: Kunde inte läsa ChromaDB: {e}")
        
        # Kuzu
        try:
            db = kuzu.Database(KUZU_PATH)
            conn = kuzu.Connection(db)
            res = conn.execute("MATCH (u:Unit) RETURN count(u)").get_next()
            graph_count = res[0]
            del conn
            del db
            validera_kuzu(lake_c, lake_ids)
        except Exception as e:
            LOGGER.error(f"Kunde inte läsa KuzuDB: {e}")
            print(f"❌ KRITISKT FEL: Kunde inte läsa KuzuDB: {e}")
    else:
        print("\nIngen data att validera i databaserna.")
    
    # Rensa gammal logg
    rensa_gammal_logg()
    
    # Returnera health_info för auto_repair
    return {
        'lake_count': lake_c,
        'vector_count': vector_count,
        'graph_count': graph_count,
        'lake_store': LAKE_STORE,
        'chroma_path': CHROMA_PATH,
        'kuzu_path': KUZU_PATH,
        'lake_ids': lake_ids
    }

if __name__ == "__main__":
    run_startup_checks()