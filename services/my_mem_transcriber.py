import os
import time
import yaml
import logging
import datetime
import json
import shutil
import threading
import re
import zoneinfo
from concurrent.futures import ThreadPoolExecutor
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

try:
    from google import genai
    from google.genai import types
except ImportError:
    print("[CRITICAL] Saknar bibliotek 'google-genai'.")
    exit(1)

# --- CONFIG LOADER ---
def ladda_yaml(filnamn, strict=True):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, 'config', filnamn),
        os.path.join(script_dir, '..', 'config', filnamn)
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f: return yaml.safe_load(f)
    if strict:
        print(f"[CRITICAL] Kunde inte hitta: {filnamn}")
        exit(1)
    return {}

CONFIG = ladda_yaml('my_mem_config.yaml', strict=True)
PROMPTS = ladda_yaml('services_prompts.yaml', strict=True)

# --- TIDSZON ---
TZ_NAME = CONFIG.get('system', {}).get('timezone', 'UTC')
try:
    SYSTEM_TZ = zoneinfo.ZoneInfo(TZ_NAME)
except Exception as e:
    print(f"[CRITICAL] HARDFAIL: Ogiltig timezone '{TZ_NAME}': {e}")
    exit(1)

try:
    ASSET_STORE = os.path.expanduser(CONFIG['paths']['asset_store'])
    LOG_FILE = os.path.expanduser(CONFIG['logging']['log_file_path'])
except KeyError as e:
    print(f"[CRITICAL] Konfigurationsfel: {e}")
    exit(1)

API_KEY = CONFIG.get('ai_engine', {}).get('api_key', '')
MEDIA_EXTENSIONS = CONFIG.get('processing', {}).get('audio_extensions', []) 
MODELS = CONFIG.get('ai_engine', {}).get('models', {})
TARGET_MODEL = MODELS.get(CONFIG.get('ai_engine', {}).get('tasks', {}).get('transcription'))

if not TARGET_MODEL:
    print(f"[CRITICAL] Saknar modellkonfiguration.")
    exit(1)

MAX_WORKERS = 5 
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS)
PROCESSED_FILES = set()
PROCESS_LOCK = threading.Lock() 
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$')

log_dir = os.path.dirname(LOG_FILE)
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - TRANS - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('MyMem_Transcriber')
# Konsol-handler utan dubblering (fil-loggning hanteras av basicConfig)
_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
LOGGER.addHandler(_console)

# --- TIGHT LOGGING HELPERS ---
def _ts():
    """Returnerar kompakt tidsstämpel [HH:MM:SS]"""
    return datetime.datetime.now(SYSTEM_TZ).strftime("[%H:%M:%S]")

def _kort(filnamn, max_len=25):
    """Trunkerar filnamn för tight logging"""
    if len(filnamn) <= max_len:
        return filnamn
    return "..." + filnamn[-(max_len-3):]

def _log(emoji, msg):
    """Tight konsol-loggning + detaljerad fil-loggning"""
    print(f"{_ts()} {emoji} TRANS: {msg}")
    LOGGER.info(msg)

if not API_KEY:
    print("[CRITICAL] Ingen API-nyckel hittad.")
    exit(1)
AI_CLIENT = genai.Client(api_key=API_KEY)

def clean_ghost_artifacts():
    """Städar bort gamla artefakter från tidigare versioner."""
    # Läs base path från config
    base_mem = os.path.dirname(ASSET_STORE)  # ~/MyMemory/Assets -> ~/MyMemory
    ghost_drop = os.path.join(base_mem, "DropZone")
    ghost_log = os.path.join(base_mem, "Logs", "dfm_system.log")
    if os.path.exists(ghost_drop):
        try: 
            shutil.rmtree(ghost_drop)
            LOGGER.info(f"Städade bort gammal DropZone: {ghost_drop}")
        except Exception as e:
            LOGGER.warning(f"Kunde inte ta bort {ghost_drop}: {e}")
    if os.path.exists(ghost_log) and ghost_log != LOG_FILE:
        try: 
            os.remove(ghost_log)
            LOGGER.info(f"Städade bort gammal loggfil: {ghost_log}")
        except Exception as e:
            LOGGER.warning(f"Kunde inte ta bort {ghost_log}: {e}")

def fa_fil_skapad_datum(filväg):
    try:
        stat = os.stat(filväg)
        timestamp = stat.st_birthtime if hasattr(stat, 'st_birthtime') else stat.st_mtime
        dt = datetime.datetime.fromtimestamp(timestamp, SYSTEM_TZ)
        return dt.isoformat()
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Kunde inte läsa tidsstämpel för {filväg}: {e}")
        raise RuntimeError(f"HARDFAIL: Kunde inte läsa tidsstämpel för {filväg}") from e

def safe_upload(filväg, original_namn):
    safe_path = None
    try:
        safe_name = f"temp_upload_{int(time.time())}_{threading.get_ident()}{os.path.splitext(filväg)[1]}"
        safe_path = os.path.join(os.path.dirname(filväg), safe_name)
        try: os.symlink(filväg, safe_path)
        except OSError: shutil.copy2(filväg, safe_path)
        upload_file = AI_CLIENT.files.upload(file=safe_path, config={'display_name': original_namn})
        if os.path.exists(safe_path): os.remove(safe_path)
        return upload_file
    except Exception as e:
        LOGGER.error(f"Upload-fel för {original_namn}: {e}")
        if safe_path and os.path.exists(safe_path): os.remove(safe_path)
        return None

def stada_och_parsa_json(text_response):
    try:
        # Försök hitta ett JSON-objekt (startar med { och slutar med })
        match = re.search(r'\{.*\}', text_response, re.DOTALL)
        if match:
            text = match.group(0)
            return json.loads(text)
        # Försök parsa hela texten om regex missar
        return json.loads(text_response)
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Kunde inte parsa JSON: {e}")
        raise ValueError(f"HARDFAIL: Kunde inte parsa JSON-svar") from e

def skapa_rich_header(filnamn, skapelsedatum, model, data):
    now = datetime.datetime.now(SYSTEM_TZ).isoformat()
    speakers = "\n- ".join(data.get('speakers', [])) or "- Inga identifierade"
    entities = "\n- ".join(data.get('entities', [])) or "- Inga identifierade"
    summary = data.get('summary', 'Ingen sammanfattning tillgänglig.')
    
    # HÄR ÄR ÄNDRINGEN: DATUM_TID istället för INSPELAT
    header = f"""================================================================================
METADATA FRÅN TRANSKRIBERING (MyMem)
================================================================================
FILNAMN:       {filnamn}
DATUM_TID:     {skapelsedatum}
TRANSKRIBERAT: {now}
MODELL:        {model}
--------------------------------------------------------------------------------
IDENTIFIERADE TALARE:
- {speakers}

IDENTIFIERADE PLATSER/ENTITETER:
- {entities}

SAMMANFATTNING (Preliminär):
{summary}
================================================================================

"""
    return header

def processa_mediafil(filväg, filnamn):
    # MODELLER FRÅN CONFIG (inga hårdkodade versionsnummer)
    MODEL_FAST = MODELS.get('model_fast')   # -> "models/gemini-flash-latest"
    MODEL_SMART = MODELS.get('model_pro')   # -> "models/gemini-pro-latest"

    # SÄKERHETSINSTÄLLNINGAR: Tillåt allt (vi vill ha rå transkribering)
    SAFETY_SETTINGS = [
        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
    ]

    with PROCESS_LOCK:
        if filnamn in PROCESSED_FILES: return
        if filnamn.startswith("temp_upload_"): return
        PROCESSED_FILES.add(filnamn)

    base_name = os.path.splitext(filnamn)[0]
    kort_namn = _kort(filnamn)
    
    if not UUID_SUFFIX_PATTERN.search(base_name):
        LOGGER.warning(f"Skippar fil utan UUID: {filnamn}")
        with PROCESS_LOCK:
            PROCESSED_FILES.discard(filnamn)
        return

    txt_fil = os.path.join(ASSET_STORE, f"{base_name}.txt")
    if os.path.exists(txt_fil): return

    _log("📥", f"{kort_namn} → Upload")
    upload_file = None
    start_time = time.time()

    try:
        # --- UPLOAD ---
        upload_file = safe_upload(filväg, filnamn)
        if not upload_file:
            raise Exception("Upload misslyckades.")

        waits = 0
        while upload_file.state.name == "PROCESSING":
            time.sleep(2)
            upload_file = AI_CLIENT.files.get(name=upload_file.name)
            waits += 1
            if waits > 30:
                raise Exception("Timeout processing.")

        if upload_file.state.name == "FAILED":
            raise Exception("File state FAILED.")

        # --- STEG 1: TRANSKRIBERING (FLASH) ---
        flash_start = time.time()
        transcript_text = ""
        transcribe_prompt = "Transkribera ljudfilen ordagrant på svenska. Markera talare (Talare 1 osv) om du kan urskilja dem. Svara ENBART med transkriberad text."

        # Retry-loop för Flash
        for attempt in range(5):
            try:
                response = AI_CLIENT.models.generate_content(
                    model=MODEL_FAST,
                    contents=[types.Content(role="user", parts=[
                        types.Part.from_uri(file_uri=upload_file.uri, mime_type=upload_file.mime_type),
                        types.Part.from_text(text=transcribe_prompt)])],
                    config=types.GenerateContentConfig(
                        response_mime_type="text/plain",
                        safety_settings=SAFETY_SETTINGS
                    )
                )
                # Robust kontroll av svaret
                try:
                    transcript_text = response.text
                except Exception:
                    finish_reason = "Unknown"
                    if response.candidates:
                        finish_reason = response.candidates[0].finish_reason
                    raise Exception(f"Inget text-svar. Finish Reason: {finish_reason}")
                break
            except Exception as e:
                error_str = str(e)
                if "503" in error_str or "429" in error_str or "overloaded" in error_str.lower():
                    wait_time = 5 * (2 ** attempt)
                    print(f"{_ts()} ⚠️ TRANS: {kort_namn} → Retry {attempt+1}/5 ({wait_time}s)")
                    LOGGER.warning(f"API överbelastad för {filnamn}. Försök {attempt+1}/5. Väntar {wait_time}s")
                    time.sleep(wait_time)
                else:
                    raise e

        if not transcript_text:
            raise Exception("Flash genererade ingen text.")
        
        flash_time = int(time.time() - flash_start)
        _log("⚡", f"{kort_namn} → Flash OK ({flash_time}s)")

        # --- STEG 2: ANALYS (PRO) ---
        metadata = {}
        analysis_context = transcript_text[:100000]
        analysis_prompt = PROMPTS['transcriber']['analysis_prompt']

        try:
            response_analysis = AI_CLIENT.models.generate_content(
                model=MODEL_SMART,
                contents=[types.Content(role="user", parts=[
                    types.Part.from_text(text=f"{analysis_prompt}\n\nTRANSCRIPT TO ANALYZE:\n{analysis_context}")])],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    safety_settings=SAFETY_SETTINGS
                )
            )
            metadata = stada_och_parsa_json(response_analysis.text)
            _log("🧠", f"{kort_namn} → Pro OK")
        except ValueError as e:
            LOGGER.error(f"HARDFAIL: JSON-parsing misslyckades för {filnamn}: {e}")
            raise
        except Exception as e:
            LOGGER.error(f"HARDFAIL: Metadata-analys misslyckades för {filnamn}: {e}")
            raise RuntimeError(f"HARDFAIL: Metadata-analys misslyckades") from e
        
        # Validera att metadata har nödvändiga fält
        if not metadata or not isinstance(metadata, dict):
            LOGGER.error(f"HARDFAIL: Metadata är ogiltigt för {filnamn}")
            raise ValueError(f"HARDFAIL: Metadata är ogiltigt för {filnamn}")

        # --- STEG 3: SPARA ---
        skapelsedatum = fa_fil_skapad_datum(filväg)
        model_info = f"{MODEL_FAST} (Audio) + {MODEL_SMART} (Analysis)"
        header = skapa_rich_header(filnamn, skapelsedatum, model_info, metadata)

        with open(txt_fil, 'w', encoding='utf-8') as f:
            f.write(header + transcript_text)

        total_time = int(time.time() - start_time)
        _log("✅", f"{kort_namn} → Klar ({total_time}s)")

    except Exception as e:
        LOGGER.error(f"FEL vid transkribering av {filnamn}: {e}")
        print(f"{_ts()} ❌ TRANS: {kort_namn} → FAILED (se logg)")
        with PROCESS_LOCK:
            PROCESSED_FILES.discard(filnamn)

class AudioHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory: return
        filnamn = os.path.basename(event.src_path)
        if filnamn.startswith("temp_upload_"): return
        if os.path.splitext(event.src_path)[1].lower() in MEDIA_EXTENSIONS:
            if UUID_SUFFIX_PATTERN.search(os.path.splitext(filnamn)[0]):
                EXECUTOR.submit(processa_mediafil, event.src_path, filnamn)

if __name__ == "__main__":
    clean_ghost_artifacts()
    
    # Räkna väntande jobb
    pending = 0
    if os.path.exists(ASSET_STORE):
        for f in os.listdir(ASSET_STORE):
            if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                if not f.startswith("temp_upload_") and UUID_SUFFIX_PATTERN.search(os.path.splitext(f)[0]):
                    base = os.path.splitext(f)[0]
                    if not os.path.exists(os.path.join(ASSET_STORE, f"{base}.txt")):
                        pending += 1
                        EXECUTOR.submit(processa_mediafil, os.path.join(ASSET_STORE, f), f)
    
    if pending > 0:
        print(f"{_ts()} ✓ Transcriber online ({pending} väntande)")
    else:
        print(f"{_ts()} ✓ Transcriber online")

    observer = Observer()
    observer.schedule(AudioHandler(), ASSET_STORE, recursive=False)
    observer.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: 
        EXECUTOR.shutdown(wait=False)
        observer.stop()
    observer.join()