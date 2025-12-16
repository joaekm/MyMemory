import os
import sys
import time
import yaml
import logging
import datetime
import json
import shutil
import threading
import re
import zoneinfo

# Lägg till projektroten i sys.path för att hitta services-paketet
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from concurrent.futures import ThreadPoolExecutor
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

try:
    from google import genai
    from google.genai import types
except ImportError:
    print("[CRITICAL] Saknar bibliotek 'google-genai'.")
    exit(1)

from services.utils.date_service import get_timestamp

# --- CONFIG LOADER ---
def ladda_yaml(filnamn, strict=True):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', '..', 'config', filnamn),
        os.path.join(script_dir, '..', 'config', filnamn),
        os.path.join(script_dir, 'config', filnamn),
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
    RECORDINGS_FOLDER = os.path.expanduser(CONFIG['paths']['asset_recordings'])
    TRANSCRIPTS_FOLDER = os.path.expanduser(CONFIG['paths']['asset_transcripts'])
    FAILED_FOLDER = os.path.expanduser(CONFIG['paths']['asset_failed'])
    LOG_FILE = os.path.expanduser(CONFIG['logging']['log_file_path'])
except KeyError as e:
    print(f"[CRITICAL] Konfigurationsfel: {e}")
    exit(1)

# Säkerställ att mapparna finns
os.makedirs(RECORDINGS_FOLDER, exist_ok=True)
os.makedirs(TRANSCRIPTS_FOLDER, exist_ok=True)
os.makedirs(FAILED_FOLDER, exist_ok=True)

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
UPLOAD_LOCK = threading.Lock()  # Förhindrar samtidiga uploads som hänger API:et
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
    """Hämta filens skapelsedatum via central DateService."""
    try:
        ts = get_timestamp(filväg)
        # Konvertera till ISO-format med timezone
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=SYSTEM_TZ)
        return ts.isoformat()
    except RuntimeError as e:
        LOGGER.error(f"HARDFAIL: {e}")
        raise

def safe_upload(filväg, original_namn):
    """Upload med sekventiell begränsning för att undvika API-blockering."""
    safe_path = None
    with UPLOAD_LOCK:  # Endast en upload åt gången
        time.sleep(1)  # Andrum mellan uploads
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

def skapa_rich_header(filnamn, skapelsedatum, model, data, unit_id=None):
    now = datetime.datetime.now(SYSTEM_TZ).isoformat()
    speakers = "\n- ".join(data.get('speakers', [])) or "- Inga identifierade"
    entities = "\n- ".join(data.get('entities', [])) or "- Inga identifierade"
    summary = data.get('summary', 'Ingen sammanfattning tillgänglig.')
    
    # UUID sparas i header för spårbarhet (tas bort från filnamn)
    unit_id_line = f"UNIT_ID:       {unit_id}\n" if unit_id else ""
    
    header = f"""================================================================================
METADATA FRÅN TRANSKRIBERING (MyMem)
================================================================================
FILNAMN:       {filnamn}
{unit_id_line}DATUM_TID:     {skapelsedatum}
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

def _move_to_failed(filväg, filnamn, reason):
    """Flytta ljudfil till failed-mappen (utan UUID i filnamnet)."""
    try:
        # Ta bort UUID från filnamnet för failed-filer
        base, ext = os.path.splitext(filnamn)
        clean_name = UUID_SUFFIX_PATTERN.sub('', base) + ext
        dest = os.path.join(FAILED_FOLDER, clean_name)
        shutil.move(filväg, dest)
        LOGGER.warning(f"Flyttade till failed: {clean_name} - {reason}")
        return True
    except Exception as e:
        LOGGER.error(f"Kunde inte flytta till failed: {filnamn} - {e}")
        return False


def _do_transcription(upload_file, model, kort_namn, filnamn, safety_settings):
    """Transkribera ljudfil med angiven modell. Returnerar (transcript, tid_i_sek)."""
    transcribe_prompt = "Transkribera ljudfilen ordagrant på svenska. Markera talare (Talare 1 osv) om du kan urskilja dem. Svara ENBART med transkriberad text."
    start = time.time()
    transcript = ""
    
    for attempt in range(5):
        try:
            response = AI_CLIENT.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=[
                    types.Part.from_uri(file_uri=upload_file.uri, mime_type=upload_file.mime_type),
                    types.Part.from_text(text=transcribe_prompt)])],
                config=types.GenerateContentConfig(
                    response_mime_type="text/plain",
                    safety_settings=safety_settings
                )
            )
            try:
                transcript = response.text
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
    
    if not transcript:
        raise Exception(f"Transkribering genererade ingen text efter 5 försök.")
    
    return transcript, int(time.time() - start)


def _do_analysis(transcript, model, kort_namn, filnamn, safety_settings):
    """Analysera transkript med angiven modell. Returnerar result dict."""
    analysis_context = transcript[:2000000]  # 2M tecken
    analysis_prompt = PROMPTS['transcriber']['analysis_prompt']
    result = None
    
    for attempt in range(5):
        try:
            response = AI_CLIENT.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=[
                    types.Part.from_text(text=f"{analysis_prompt}\n\nTRANSCRIPT TO ANALYZE:\n{analysis_context}")])],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    safety_settings=safety_settings
                )
            )
            result = stada_och_parsa_json(response.text)
            break
        except ValueError as e:
            LOGGER.error(f"HARDFAIL: JSON-parsing misslyckades för {filnamn}: {e}")
            raise
        except Exception as e:
            error_str = str(e)
            if "503" in error_str or "429" in error_str or "overloaded" in error_str.lower():
                wait_time = 5 * (2 ** attempt)
                print(f"{_ts()} ⚠️ TRANS: {kort_namn} → Analysis Retry {attempt+1}/5 ({wait_time}s)")
                LOGGER.warning(f"Analysis API överbelastad för {filnamn}. Försök {attempt+1}/5. Väntar {wait_time}s")
                time.sleep(wait_time)
            else:
                LOGGER.error(f"HARDFAIL: Metadata-analys misslyckades för {filnamn}: {e}")
                raise RuntimeError(f"HARDFAIL: Metadata-analys misslyckades") from e
    
    if not result:
        raise Exception("Analys genererade inget resultat efter 5 försök.")
    
    if not isinstance(result, dict):
        raise ValueError(f"HARDFAIL: Resultat är ogiltigt för {filnamn}")
    
    return result


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
    
    # Extrahera UUID från filnamnet
    uuid_match = UUID_SUFFIX_PATTERN.search(base_name)
    if not uuid_match:
        LOGGER.warning(f"Skippar fil utan UUID: {filnamn}")
        with PROCESS_LOCK:
            PROCESSED_FILES.discard(filnamn)
        return
    
    unit_id = uuid_match.group(1)
    
    # Output till Transcripts-mappen (DocConverter övervakar den)
    txt_fil = os.path.join(TRANSCRIPTS_FOLDER, f"{base_name}.txt")
    if os.path.exists(txt_fil): 
        return

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

        # --- PIPELINE: Flash + Pro (med retry via Pro + Pro vid kvalitetsfel) ---
        raw_transcript = None
        result = None
        used_pro_retry = False
        
        # Försök 1: Flash transkribering + Pro analys
        raw_transcript, trans_time = _do_transcription(upload_file, MODEL_FAST, kort_namn, filnamn, SAFETY_SETTINGS)
        _log("⚡", f"{kort_namn} → Flash OK ({trans_time}s)")
        
        result = _do_analysis(raw_transcript, MODEL_SMART, kort_namn, filnamn, SAFETY_SETTINGS)
        _log("🧠", f"{kort_namn} → Pro OK")
        
        # Kvalitetskontroll
        quality_status = result.get('quality_status', 'OK')
        if quality_status == 'FAILED':
            failure_reason = result.get('failure_reason', 'Okänd anledning')
            _log("⚠️", f"{kort_namn} → Flash kvalitetsfel: {failure_reason}")
            _log("🔄", f"{kort_namn} → Retry med Pro+Pro...")
            
            # Försök 2: Pro transkribering + Pro analys
            used_pro_retry = True
            raw_transcript, trans_time = _do_transcription(upload_file, MODEL_SMART, kort_namn, filnamn, SAFETY_SETTINGS)
            _log("🧠", f"{kort_namn} → Pro transkribering OK ({trans_time}s)")
            
            result = _do_analysis(raw_transcript, MODEL_SMART, kort_namn, filnamn, SAFETY_SETTINGS)
            _log("🧠", f"{kort_namn} → Pro analys OK")
            
            # Andra kvalitetskontrollen
            quality_status = result.get('quality_status', 'OK')
            if quality_status == 'FAILED':
                failure_reason = result.get('failure_reason', 'Okänd anledning')
                _log("🚫", f"{kort_namn} → FAILED (även med Pro): {failure_reason}")
                _move_to_failed(filväg, filnamn, failure_reason)
                with PROCESS_LOCK:
                    PROCESSED_FILES.discard(filnamn)
                return

        # --- SPARA ---
        # Använd PRO:s korrigerade transkription (med riktiga talarnamn)
        final_transcript = result.get('transcript', raw_transcript)
        
        skapelsedatum = fa_fil_skapad_datum(filväg)
        if used_pro_retry:
            model_info = f"{MODEL_SMART} (Audio+Analysis, retry efter Flash-kvalitetsfel)"
        else:
            model_info = f"{MODEL_FAST} (Audio) + {MODEL_SMART} (Analysis)"
        header = skapa_rich_header(filnamn, skapelsedatum, model_info, result, unit_id)

        with open(txt_fil, 'w', encoding='utf-8') as f:
            f.write(header + final_transcript)

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
    
    # Räkna väntande jobb i Recordings-mappen
    pending = 0
    if os.path.exists(RECORDINGS_FOLDER):
        for f in os.listdir(RECORDINGS_FOLDER):
            if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                if not f.startswith("temp_upload_") and UUID_SUFFIX_PATTERN.search(os.path.splitext(f)[0]):
                    base = os.path.splitext(f)[0]
                    # Kolla om transkription redan finns i Transcripts-mappen
                    if not os.path.exists(os.path.join(TRANSCRIPTS_FOLDER, f"{base}.txt")):
                        pending += 1
                        EXECUTOR.submit(processa_mediafil, os.path.join(RECORDINGS_FOLDER, f), f)
    
    if pending > 0:
        print(f"{_ts()} ✓ Transcriber online ({pending} väntande)")
    else:
        print(f"{_ts()} ✓ Transcriber online")

    # Övervaka Recordings-mappen
    observer = Observer()
    observer.schedule(AudioHandler(), RECORDINGS_FOLDER, recursive=False)
    observer.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: 
        EXECUTOR.shutdown(wait=False)
        observer.stop()
    observer.join()