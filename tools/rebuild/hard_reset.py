#!/usr/bin/env python3
"""
HARD DATA RESET - MyMemory v6

⚠️  VARNING: Detta script raderar ALL data!
    - Lake (alla .md filer)
    - Transcripts (transkriberade filer)
    - ChromaDB (vektorer)
    - DuckDB Graf (noder och kanter)
    - Taxonomi (återställs från config/taxonomy_template.json)
    - Rebuild Manifest (återställs)

Användning:
    python tools/rebuild/hard_reset.py --confirm
    python tools/rebuild/hard_reset.py --confirm --no-backup  # Skippa backup
"""

import os
import sys
import json
import shutil
import yaml
from datetime import datetime

# Lägg till project root i path för imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# --- CONFIG ---
def load_yaml(filnamn):
    """Ladda YAML-config från config-mappen."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(script_dir))
    config_path = os.path.join(project_root, 'config', filnamn)
    
    if not os.path.exists(config_path):
        print(f"[FEL] Saknar {filnamn}")
        sys.exit(1)
    
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

CONFIG = load_yaml('my_mem_config.yaml')

LAKE_STORE = os.path.expanduser(CONFIG['paths']['lake_store'])
TRANSCRIPTS_FOLDER = os.path.expanduser(CONFIG['paths']['asset_transcripts'])
CHROMA_PATH = os.path.expanduser(CONFIG['paths']['chroma_db'])
GRAPH_PATH = os.path.expanduser(CONFIG['paths']['graph_db'])
MANIFEST_FILE = os.path.join(os.path.expanduser(CONFIG['paths']['asset_store']), '.rebuild_manifest.json')

# MyMemory root (parent of Lake, Index, Assets) - deriverat från lake_store
MYMEMORY_ROOT = os.path.dirname(LAKE_STORE)


def clear_folder(path, name):
    """Raderar alla filer i en mapp (behåller mappen)."""
    if not os.path.exists(path):
        print(f"  ⏭️  {name}: Finns inte")
        return 0
    
    count = 0
    for f in os.listdir(path):
        fp = os.path.join(path, f)
        if os.path.isfile(fp):
            os.remove(fp)
            count += 1
    print(f"  🗑️  {name}: {count} filer raderade")
    return count


def clear_index(path, name, recreate_dir=True):
    """Raderar hela index-katalogen eller filen.
    
    Args:
        path: Sökväg att radera
        name: Namn för loggning
        recreate_dir: Om True, skapa tom katalog efter radering (för ChromaDB).
                      Om False, lämna sökvägen tom.
    """
    if not os.path.exists(path):
        print(f"  ⏭️  {name}: Finns inte")
        return
    
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)
    
    if recreate_dir:
        os.makedirs(path, exist_ok=True)
        print(f"  🗑️  {name}: Raderad och återskapad")
    else:
        print(f"  🗑️  {name}: Raderad")


def clear_duckdb(path, name):
    """Radera DuckDB-filer (huvudfil + WAL).
    
    DuckDB skapar två filer:
    - path (huvudfilen)
    - path.wal (Write-Ahead Log)
    """
    deleted = []
    for ext in ['', '.wal']:
        fpath = path + ext
        if os.path.exists(fpath):
            os.remove(fpath)
            deleted.append(os.path.basename(fpath))
    
    if deleted:
        print(f"  🗑️  {name}: Raderade {', '.join(deleted)}")
    else:
        print(f"  ⏭️  {name}: Finns inte")


def create_backup():
    """Skapar backup av hela MyMemory-mappen."""
    if not os.path.exists(MYMEMORY_ROOT):
        print(f"  ⏭️  Backup: MyMemory-mapp finns inte")
        return None
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.expanduser(f"~/MyMemory_bku_{timestamp}")
    
    print(f"  📦 Skapar backup: {backup_path}")
    print(f"     Detta kan ta en stund...")
    
    try:
        shutil.copytree(MYMEMORY_ROOT, backup_path)
        
        # Räkna storlek
        total_size = 0
        file_count = 0
        for dirpath, dirnames, filenames in os.walk(backup_path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                total_size += os.path.getsize(fp)
                file_count += 1
        
        size_mb = total_size / (1024 * 1024)
        print(f"  ✅ Backup klar: {file_count} filer, {size_mb:.1f} MB")
        return backup_path
    except Exception as e:
        print(f"  ❌ Backup misslyckades: {e}")
        raise RuntimeError(f"HARDFAIL: Kunde inte skapa backup: {e}") from e


def reset_manifest():
    """Raderar rebuild manifest filen."""
    if os.path.exists(MANIFEST_FILE):
        try:
            os.remove(MANIFEST_FILE)
            print(f"  🗑️  Manifest: Raderad ({os.path.basename(MANIFEST_FILE)})")
        except Exception as e:
            # HARDFAIL: Logga men fortsätt (cleanup-fel ska inte krascha reset)
            import sys
            sys.stderr.write(f"HARDFAIL: Kunde inte radera manifest: {e}\n")
            print(f"  ⚠️  HARDFAIL: Kunde inte radera manifest: {e}")
    else:
        print(f"  ⏭️  Manifest: Finns inte")

def main():
    if "--confirm" not in sys.argv:
        print("""
╔══════════════════════════════════════════════════════════════╗
║                    ⚠️  HARD DATA RESET ⚠️                     ║
╠══════════════════════════════════════════════════════════════╣
║  Detta kommer att PERMANENT radera:                          ║
║                                                              ║
║  • Alla filer i Lake/                                        ║
║  • Alla filer i Assets/Transcripts/                          ║
║  • Hela ChromaDB (vektorer)                                  ║
║  • Hela DuckDB (graf)                                        ║
║  • Rebuild Manifest                                          ║
║                                                              ║
║  Recordings, Documents, Slack behålls!                       ║
║                                                              ║
║  En backup skapas automatiskt innan reset.                   ║
╚══════════════════════════════════════════════════════════════╝

För att köra: python tools/tool_hard_reset.py --confirm
Skippa backup: python tools/tool_hard_reset.py --confirm --no-backup
""")
        sys.exit(0)
    
    print("\n🔥 HARD DATA RESET - MyMemory v6\n")
    print("=" * 50)
    
    # 0. Backup (om inte --no-backup)
    if "--no-backup" not in sys.argv:
        backup_path = create_backup()
        print()
    else:
        print("  ⏭️  Backup: Skippas (--no-backup)")
        backup_path = None
    
    # 1. Lake
    clear_folder(LAKE_STORE, "Lake")
    
    # 2. Transcripts
    clear_folder(TRANSCRIPTS_FOLDER, "Transcripts")
    
    # 3. ChromaDB (återskapas som tom katalog)
    clear_index(CHROMA_PATH, "ChromaDB", recreate_dir=True)
    
    # 4. DuckDB Graf (fil + WAL)
    clear_duckdb(GRAPH_PATH, "DuckDB Graf")
    
    # 5. Manifest
    reset_manifest()
    
    print("=" * 50)
    print("\n✅ RESET KOMPLETT!")
    print("\nNästa steg:")
    print("  1. python tools/tool_staged_rebuild.py --confirm --phase foundation (Bygg grunden)")
    print("  2. python tools/tool_staged_rebuild.py --confirm --phase enrichment (Berika med ljud)")


if __name__ == "__main__":
    main()

