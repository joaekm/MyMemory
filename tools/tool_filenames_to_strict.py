import os
import uuid
import shutil
import re
import yaml

# --- CONFIG ---
# Läs sökvägar från config (Princip 8)
script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml')
with open(config_path, 'r') as f:
    config = yaml.safe_load(f)
ASSET_STORE = os.path.expanduser(config['paths']['asset_store'])
LAKE_STORE = os.path.expanduser(config['paths']['lake_store'])

# Regex för att se om en fil redan är korrekt
# Matchar _[UUID].ext i slutet
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.[a-zA-Z0-9]+$')

def migrate_assets():
    print(f"--- 1. Migrerar Assets: {ASSET_STORE} ---")
    if not os.path.exists(ASSET_STORE):
        print("Assets-mappen hittades inte.")
        return

    count_fixed = 0
    count_ok = 0

    for f in os.listdir(ASSET_STORE):
        if f.startswith('.'): continue
        full_path = os.path.join(ASSET_STORE, f)
        if os.path.isdir(full_path): continue

        # Kolla om den redan är korrekt
        if UUID_SUFFIX_PATTERN.search(f):
            count_ok += 1
            continue

        # Om den inte matchar mönstret, måste vi fixa den.
        # Strategi: Behåll originalnamnet, lägg på UUID sist.
        base, ext = os.path.splitext(f)
        
        # Städa namnet från fula tecken
        clean_base = re.sub(r'[ _-]+', '_', base).strip('_')
        
        new_uuid = str(uuid.uuid4())
        new_name = f"{clean_base}_{new_uuid}{ext}"
        
        new_path = os.path.join(ASSET_STORE, new_name)
        
        try:
            os.rename(full_path, new_path)
            print(f"🔧 Fixad: {f} -> {new_name}")
            count_fixed += 1
        except Exception as e:
            # HARDFAIL: Logga men fortsätt med nästa fil (detta är intentional - fortsätt vid fel)
            import sys
            sys.stderr.write(f"HARDFAIL: Kunde inte byta namn på {f}: {e}\n")

    print(f"KLAR. {count_fixed} filer åtgärdade. {count_ok} var redan korrekta.\n")

def clean_lake():
    print(f"--- 2. Städar Lake: {LAKE_STORE} ---")
    print("Eftersom vi bytt ID-strategi (från Filnamn till UUID) är det säkrast att")
    print("rensa 'Sjön' på gamla metadata-filer och låta DocConverter bygga om dem")
    print("från de nyligen omdöpta filerna i Assets.")
    
    if not os.path.exists(LAKE_STORE):
        print("Lake-mappen hittades inte.")
        return

    svar = input("Vill du radera alla gamla .md-filer i Lake så systemet kan bygga om dem rent? (j/n): ")
    if svar.lower() != 'j':
        print("Avbryter städning av Lake.")
        return

    deleted = 0
    for f in os.listdir(LAKE_STORE):
        if f.endswith(".md"):
            try:
                os.remove(os.path.join(LAKE_STORE, f))
                deleted += 1
            except Exception as e:
                # HARDFAIL: Logga men fortsätt med nästa fil (detta är intentional - fortsätt vid fel)
                import sys
                sys.stderr.write(f"HARDFAIL: Kunde inte radera {f}: {e}\n")
                # Fortsätt med nästa fil istället för att krascha hela scriptet
    
    print(f"Raderade {deleted} filer i Lake. Starta systemet för att bygga om dem.\n")

if __name__ == "__main__":
    print("=== MIGRERING TILL STRICT SUFFIX MODE ===")
    print("Detta script kommer att:")
    print("1. Döpa om filer i Assets som saknar UUID-suffix.")
    print("2. (Valfritt) Rensa Lake för att tvinga fram en ren om-indexering.")
    print("=========================================")
    
    confirm = input("Är du säker på att du vill köra detta? (skriv 'KÖR'): ")
    if confirm == "KÖR":
        migrate_assets()
        clean_lake()
        print("=== MIGRERING KLAR ===")
        print("Nästa steg: Starta systemet (start_services.py).")
        print("DocConverter kommer nu att upptäcka 'nya' filer i Assets och skapa korrekt metadata i Lake.")
    else:
        print("Avbröt.")