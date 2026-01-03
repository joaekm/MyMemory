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
import glob

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
from services.utils.graph_service import GraphStore

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
    CALENDAR_FOLDER = os.path.expanduser(CONFIG['paths'].get('asset_calendar', '~/MyMemory/Assets/Calendar'))
    GRAPH_PATH = os.path.expanduser(CONFIG['paths']['graph_db'])
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

MAX_WORKERS = 5 
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS)
PROCESSED_FILES = set()
PROCESS_LOCK = threading.Lock()
UPLOAD_LOCK = threading.Lock()
UUID_SUFFIX_PATTERN = re.compile(r'_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$')

log_dir = os.path.dirname(LOG_FILE)
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - TRANS - %(levelname)s - %(message)s')
LOGGER = logging.getLogger('MyMem_Transcriber')
_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
LOGGER.addHandler(_console)

# Silence external loggers
logging.getLogger("google").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# --- TIGHT LOGGING HELPERS ---
def _ts():
    return datetime.datetime.now(SYSTEM_TZ).strftime("[%H:%M:%S]")

def _kort(filnamn, max_len=25):
    if len(filnamn) <= max_len:
        return filnamn
    return "..." + filnamn[-(max_len-3):]

def _log(emoji, msg):
    print(f"{_ts()} {emoji} TRANS: {msg}")
    LOGGER.info(msg)

if not API_KEY:
    print("[CRITICAL] Ingen API-nyckel hittad.")
    exit(1)
AI_CLIENT = genai.Client(api_key=API_KEY)

# --- CONTEXT BUILDERS ---

def _build_graph_context() -> str:
    """
    Hämtar kända talare och alias direkt från DuckDB.
    """
    if not os.path.exists(GRAPH_PATH):
        return ""

    try:
        gs = GraphStore(GRAPH_PATH, read_only=True)
        persons = gs.find_nodes_by_type("Person")
        
        lines = ["KÄNDA TALARE (från Grafen):"]
        aliases = []
        
        for p in persons:
            name = p.get('properties', {}).get('name', p['id'])
            lines.append(f"- {name}")
            
            node_aliases = p.get('aliases', [])
            for alias in node_aliases:
                aliases.append(f"{alias} = {name}")
        
        if aliases:
            lines.append("\nKÄNDA ALIAS (Normalisera till namnet efter likamed-tecknet):")
            for a in aliases:
                lines.append(f"- {a}")
                
        gs.close()
        return "\n".join(lines)
        
    except Exception as e:
        LOGGER.warning(f"Kunde inte läsa grafen: {e}")
        return ""

def _get_calendar_context(file_timestamp: datetime.datetime) -> str:
    """
    Hämtar möteskontext från kalender-digests.
    Matchar tidsstämpel +/- 20 minuter.
    """
    try:
        if not os.path.exists(CALENDAR_FOLDER):
            return ""

        file_date_str = file_timestamp.strftime('%Y-%m-%d')
        pattern = os.path.join(CALENDAR_FOLDER, f"Calendar_{file_date_str}_*.md")
        files = glob.glob(pattern)
        
        if not files:
            return ""
        
        with open(files[0], 'r', encoding='utf-8') as f:
            content = f.read()
            
        event_matches = re.finditer(r'^##\s+(\d{2}:\d{2})(?:-(\d{2}:\d{2}))?:\s+(.+)$', content, re.MULTILINE)
        
        for match in event_matches:
            start_str = match.group(1)
            end_str = match.group(2)
            title = match.group(3).strip()
            
            start_pos = match.end()
            next_match = re.search(r'^##\s', content[start_pos:], re.MULTILINE)
            end_pos = start_pos + next_match.start() if next_match else len(content)
            details = content[start_pos:end_pos].strip()
            
            try:
                event_start = datetime.datetime.strptime(f"{file_date_str} {start_str}", "%Y-%m-%d %H:%M").replace(tzinfo=SYSTEM_TZ)
                
                diff = abs((file_timestamp - event_start).total_seconds())
                is_match = False
                
                if diff <= 1200:
                    is_match = True
                
                if not is_match and end_str:
                    event_end = datetime.datetime.strptime(f"{file_date_str} {end_str}", "%Y-%m-%d %H:%M").replace(tzinfo=SYSTEM_TZ)
                    if event_start <= file_timestamp <= event_end:
                        is_match = True
                
                if is_match:
                    _log("📅", f"Kalendermatch: '{title}'")
                    return f"""
MÖTESKONTEXT (Från Kalender):
Titel: {title}
Tid: {start_str} - {end_str if end_str else '?'}
Deltagare/Info:
{details}

INSTRUKTION: Använd deltagarlistan ovan för att identifiera "Talare X" och förstå syftningar.
"""
            except Exception:
                continue 
                
    except Exception as e:
        LOGGER.warning(f"Kalenderfel: {e}")
    
    return ""

# --- PROCESSING HELPERS ---

def clean_ghost_artifacts():
    base_mem = os.path.dirname(ASSET_STORE)
    ghost_drop = os.path.join(base_mem, "DropZone")
    if os.path.exists(ghost_drop):
        try: shutil.rmtree(ghost_drop)
        except: pass

def safe_upload(filväg, original_namn):
    safe_path = None
    with UPLOAD_LOCK:
        time.sleep(1)
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
        match = re.search(r'\{.*\}', text_response, re.DOTALL)
        if match: return json.loads(match.group(0))
        return json.loads(text_response)
    except Exception as e:
        LOGGER.error(f"JSON Parse Fail: {text_response[:100]}...")
        raise ValueError(f"Kunde inte parsa JSON: {e}")

def skapa_rich_header(filnamn, skapelsedatum, audio_duration_sec, model, data, unit_id=None):
    """
    Skapar den nya, utökade headern för transkriberingsfiler.
    """
    date_str = skapelsedatum.strftime('%Y-%m-%d')
    start_str = skapelsedatum.strftime('%H:%M')
    
    # Beräkna sluttid
    if audio_duration_sec:
        end_dt = skapelsedatum + datetime.timedelta(seconds=audio_duration_sec)
        end_str = end_dt.strftime('%H:%M')
    else:
        end_str = "??"

    # Data från LLM-analysen
    title = data.get('title', 'Okänt Möte')
    location = data.get('location', 'Okänd')
    summary = data.get('summary', 'Ingen sammanfattning tillgänglig.')
    
    # Formatera detaljerad deltagarlista
    participants_list = []
    raw_speakers = data.get('speakers_detailed', [])
    
    # Fallback om detailed saknas men simple finns
    if not raw_speakers and data.get('speakers'):
        for s in data.get('speakers'):
            participants_list.append(str(s))
    else:
        for p in raw_speakers:
            if isinstance(p, dict):
                p_str = p.get('name', 'Okänd')
                extras = []
                if p.get('role'): extras.append(p.get('role'))
                if p.get('org'): extras.append(p.get('org'))
                
                if extras:
                    p_str += f" ({', '.join(extras)})"
                participants_list.append(p_str)
            else:
                participants_list.append(str(p))
    
    participants_str = "\n- ".join(participants_list) if participants_list else "- Inga identifierade"
    unit_id_line = f"UNIT_ID:       {unit_id}\n" if unit_id else ""
    
    header = f"""================================================================================
METADATA FRÅN TRANSKRIBERING (MyMem)
================================================================================
TITEL:         {title}
DATUM:         {date_str}
START:         {start_str}
SLUT:          {end_str}
PLATS:         {location}
FILNAMN:       {filnamn}
{unit_id_line}MODELL:        {model} (Berikad)
--------------------------------------------------------------------------------
SAMMANFATTNING:
{summary}
--------------------------------------------------------------------------------
DELTAGARE:
- {participants_str}
================================================================================

"""
    return header

def _do_transcription(upload_file, model, kort_namn, safety_settings):
    """Pass 1: Rå transkribering (Flash)."""
    # Hämta prompt från config
    prompt = PROMPTS.get('transcriber', {}).get('pass1_raw', '')
    if not prompt:
        raise ValueError("HARDFAIL: 'pass1_raw' prompt saknas i services_prompts.yaml")
        
    start = time.time()
    for attempt in range(5):
        try:
            response = AI_CLIENT.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=[
                    types.Part.from_uri(file_uri=upload_file.uri, mime_type=upload_file.mime_type),
                    types.Part.from_text(text=prompt)])],
                config=types.GenerateContentConfig(response_mime_type="text/plain", safety_settings=safety_settings)
            )
            return response.text, int(time.time() - start)
        except Exception as e:
            if "503" in str(e) or "429" in str(e):
                time.sleep(5 * (2 ** attempt))
            else:
                raise e
    raise Exception("Transkribering misslyckades efter retries.")

def _do_analysis(transcript, model, kort_namn, safety_settings, context_string=""):
    """
    Pass 2: Analys & Berikning med RAG-kontext (Pro).
    """
    raw_prompt = PROMPTS.get('transcriber', {}).get('pass2_enriched', '')
    if not raw_prompt:
        raise ValueError("HARDFAIL: 'pass2_enriched' prompt saknas i services_prompts.yaml")
    
    context_payload = context_string.strip() or "Ingen extra kontext tillgänglig."
    final_prompt = raw_prompt.replace("{context_injection}", context_payload)
    
    # Klipp transkript om det är gigantiskt
    analysis_context = transcript[:1500000] 

    for attempt in range(5):
        try:
            response = AI_CLIENT.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=[
                    types.Part.from_text(text=f"{final_prompt}\n\nTRANSCRIPT TO ENRICH:\n{analysis_context}")])],
                config=types.GenerateContentConfig(response_mime_type="application/json", safety_settings=safety_settings)
            )
            return stada_och_parsa_json(response.text)
        except Exception as e:
            if "503" in str(e) or "429" in str(e):
                time.sleep(5 * (2 ** attempt))
            else:
                LOGGER.error(f"Analys misslyckades: {e}")
                raise RuntimeError(f"Analys misslyckades: {e}")
    raise Exception("Analys misslyckades efter retries.")

# --- MAIN WORKER ---

def processa_mediafil(filväg, filnamn):
    MODEL_FAST = MODELS.get('model_fast')
    MODEL_SMART = MODELS.get('model_pro')
    
    SAFETY = [types.SafetySetting(category=cat, threshold="BLOCK_NONE") for cat in 
              ["HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_DANGEROUS_CONTENT", 
               "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_HARASSMENT"]]

    with PROCESS_LOCK:
        if filnamn in PROCESSED_FILES or filnamn.startswith("temp_upload_"): return
        PROCESSED_FILES.add(filnamn)

    base_name = os.path.splitext(filnamn)[0]
    kort_namn = _kort(filnamn)
    
    match = UUID_SUFFIX_PATTERN.search(base_name)
    if not match:
        LOGGER.warning(f"Skippar fil utan UUID: {filnamn}")
        with PROCESS_LOCK: PROCESSED_FILES.discard(filnamn)
        return
    unit_id = match.group(1)
    
    txt_fil = os.path.join(TRANSCRIPTS_FOLDER, f"{base_name}.txt")
    if os.path.exists(txt_fil): 
        with PROCESS_LOCK: PROCESSED_FILES.discard(filnamn)
        return

    _log("📥", f"{kort_namn} → Startar")
    upload_file = None
    start_time = time.time()

    try:
        # 1. Tidsbestämning
        try: timestamp = get_timestamp(filväg)
        except: timestamp = datetime.datetime.now(SYSTEM_TZ)

        # 2. Bygg Kontext
        graph_ctx = _build_graph_context()
        cal_ctx = _get_calendar_context(timestamp)
        combined_context = f"{graph_ctx}\n\n{cal_ctx}"

        # 3. Ladda upp fil
        upload_file = safe_upload(filväg, filnamn)
        if not upload_file: raise Exception("Upload failed")
        
        for _ in range(30):
            if upload_file.state.name == "ACTIVE": break
            if upload_file.state.name == "FAILED": raise Exception("File processing FAILED")
            time.sleep(2)
            upload_file = AI_CLIENT.files.get(name=upload_file.name)
        
        # 4. PASS 1: Rå Transkribering
        raw_transcript, dur = _do_transcription(upload_file, MODEL_FAST, kort_namn, SAFETY)
        _log("⚡", f"{kort_namn} → Flash OK ({dur}s)")

        # 5. PASS 2: Analys & Berikning
        result = _do_analysis(raw_transcript, MODEL_SMART, kort_namn, SAFETY, context_string=combined_context)
        _log("🧠", f"{kort_namn} → Pro OK (Berikad)")
        
        # Kvalitetskontroll
        if result.get('quality_status') == 'FAILED':
            _log("🚫", f"{kort_namn} → Kvalitet underkänd: {result.get('failure_reason')}")
            dest = os.path.join(FAILED_FOLDER, filnamn)
            shutil.move(filväg, dest)
            return

        # 6. Spara Resultat
        final_text = result.get('transcript', raw_transcript)
        header = skapa_rich_header(filnamn, timestamp, dur, MODEL_SMART, result, unit_id)
        
        with open(txt_fil, 'w', encoding='utf-8') as f:
            f.write(header + final_text)
            
        total_time = int(time.time() - start_time)
        _log("✅", f"{kort_namn} → Klar ({total_time}s)")

    except Exception as e:
        LOGGER.error(f"FEL {filnamn}: {e}")
        print(f"{_ts()} ❌ TRANS: {kort_namn} → FAILED")
    finally:
        with PROCESS_LOCK: PROCESSED_FILES.discard(filnamn)

class AudioHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory: return
        fname = os.path.basename(event.src_path)
        if os.path.splitext(fname)[1].lower() in MEDIA_EXTENSIONS:
            if UUID_SUFFIX_PATTERN.search(os.path.splitext(fname)[0]):
                EXECUTOR.submit(processa_mediafil, event.src_path, fname)

if __name__ == "__main__":
    clean_ghost_artifacts()
    
    pending = 0
    if os.path.exists(RECORDINGS_FOLDER):
        for f in os.listdir(RECORDINGS_FOLDER):
            if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                if not f.startswith("temp_") and UUID_SUFFIX_PATTERN.search(os.path.splitext(f)[0]):
                    base = os.path.splitext(f)[0]
                    if not os.path.exists(os.path.join(TRANSCRIPTS_FOLDER, f"{base}.txt")):
                        pending += 1
                        EXECUTOR.submit(processa_mediafil, os.path.join(RECORDINGS_FOLDER, f), f)
    
    print(f"{_ts()} ✓ Transcriber v10 (Berikad+Header) online ({pending} pending)")
    
    observer = Observer()
    observer.schedule(AudioHandler(), RECORDINGS_FOLDER, recursive=False)
    observer.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        EXECUTOR.shutdown(wait=False)
        observer.stop()
    observer.join()