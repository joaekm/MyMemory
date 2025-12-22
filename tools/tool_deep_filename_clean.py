import os
import re
import shutil
import uuid

# --- CONFIG ---
# Läs sökvägar från config (Princip 8)
import yaml
script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml')
with open(config_path, 'r') as f:
    config = yaml.safe_load(f)
ASSET_STORE = os.path.expanduser(config['paths']['asset_store'])

# NY REGEX: Hanterar både bindestreck (-) och understreck (_) i UUIDt
# Matchar 8-4-4-4-12 tecken i början av filnamnet.
# Separator kan vara [-_]
START_UUID_DIRTY_PATTERN = re.compile(r'^([0-9a-fA-F]{8}[-_][0-9a-fA-F]{4}[-_][0-9a-fA-F]{4}[-_][0-9a-fA-F]{4}[-_][0-9a-fA-F]{12})[_\- ]+(.*)$')

# Regex för korrekt UUID i slutet (alltid med bindestreck, för det satte vi nyss)
END_UUID_PATTERN = re.compile(r'^(.*)[_\- ]([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$')

def normalize_uuid_string(dirty_uuid):
    """Gör om 'abc_def' till 'abc-def' så vi kan jämföra."""
    return dirty_uuid.replace('_', '-')

def process_filename(filename):
    base, ext = os.path.splitext(filename)
    
    # 1. Sök efter "Dirty UUID" i början (med understreck)
    match_start = START_UUID_DIRTY_PATTERN.match(base)
    start_uuid_raw = None
    start_uuid_clean = None
    clean_base_from_start = base 
    
    if match_start:
        start_uuid_raw = match_start.group(1)
        start_uuid_clean = normalize_uuid_string(start_uuid_raw) # Konvertera till standardformat
        clean_base_from_start = match_start.group(2) # Resten av namnet
    
    # 3. Sök efter korrekt UUID i slutet
    match_end = END_UUID_PATTERN.match(clean_base_from_start)
    end_uuid = None
    final_content_name = clean_base_from_start 
    
    if match_end:
        final_content_name = match_end.group(1)
        end_uuid = match_end.group(2)

    # --- LOGIKTRÄDET ---

    if start_uuid_clean:
        # Vi hittade ett ID i början!
        if end_uuid:
            # Jämför dem (nu när båda är normaliserade till bindestreck)
            if start_uuid_clean == end_uuid:
                 # De är samma. Ta bort det fula i början.
                return f"{final_content_name}_{start_uuid_clean}{ext}", "MATCH (Städat bort start-prefix)"
            else:
                # Olika. Start-ID vinner.
                return f"{final_content_name}_{start_uuid_clean}{ext}", "REPLACE (Start-ID ersatte slut-ID)"
        else:
            # Fanns bara i början. Flytta till slut.
            return f"{final_content_name}_{start_uuid_clean}{ext}", "MOVED (Från start till slut)"

    else:
        # Inget ID i början.
        if end_uuid:
            return filename, "SKIP (Redan OK)"
        else:
            # Inget ID alls.
            new_id = str(uuid.uuid4())
            return f"{base}_{new_id}{ext}", "NEW (Nytt ID skapat)"

def run_reorder():
    print(f"--- REORDER UUIDs (Deep Clean): {ASSET_STORE} ---")
    
    if not os.path.exists(ASSET_STORE):
        print("Assets hittades inte.")
        return

    count = 0
    changes = 0

    files = [f for f in os.listdir(ASSET_STORE) if not f.startswith('.')]
    
    for f in files:
        new_name, action = process_filename(f)
        
        if action.startswith("SKIP"):
            continue

        if new_name != f:
            old_path = os.path.join(ASSET_STORE, f)
            new_path = os.path.join(ASSET_STORE, new_name)
            
            try:
                os.rename(old_path, new_path)
                print(f"🔧 {f}\n   -> {new_name} [{action}]")
                changes += 1
            except Exception as e:
                # HARDFAIL: Logga men fortsätt med nästa fil (detta är intentional - fortsätt vid fel)
                import sys
                sys.stderr.write(f"HARDFAIL: Kunde inte byta namn på {f}: {e}\n")
        
        count += 1

    print(f"\nKLAR. {changes} filer åtgärdade.")

if __name__ == "__main__":
    run_reorder()