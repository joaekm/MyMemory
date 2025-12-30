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
import glob # NYTT: För att hitta kalenderfiler

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
from services.indexers.graph_builder import (
    get_all_entities as get_known_entities,
    get_canonical_from_graph as get_canonical,
    add_entity_alias,
)

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
    # NYTT: Behöver veta var kalendern ligger
    CALENDAR_FOLDER = os.path.expanduser(CONFIG['paths'].get('asset_calendar', '~/MyMemory/Assets/Calendar'))
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
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

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

# --- HELPER: KALENDER-UPPSLAG (NY FUNKTION) ---
def _get_calendar_context(file_timestamp):
    """
    Försöker hitta ett kalendermöte som matchar filens tidpunkt.
    Returnerar en kontext-sträng att injicera i prompten.
    """
    try:
        if not os.path.exists(CALENDAR_FOLDER):
            return ""

        file_date_str = file_timestamp.strftime('%Y-%m-%d')
        # Sök efter digest-fil för rätt datum
        pattern = os.path.join(CALENDAR_FOLDER, f"Calendar_{file_date_str}_*.md")
        files = glob.glob(pattern)
        
        if not files:
            LOGGER.debug(f"Ingen kalenderfil hittades för {file_date_str}")
            return ""
        
        # Använd den första matchande filen (borde bara finnas en per dag)
        calendar_file = files[0]
        
        with open(calendar_file, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # Enkel parsing av Markdown-eventen från CalendarCollector
        # Format: ## HH:MM-HH:MM: Titel
        # Eller: ## HH:MM: Titel
        events = []
        
        # Regex för att hitta event-rubriker
        event_matches = re.finditer(r'^##\s+(\d{2}:\d{2})(?:-(\d{2}:\d{2}))?:\s+(.+)$', content, re.MULTILINE)
        
        for match in event_matches:
            start_str = match.group(1)
            end_str = match.group(2)
            title = match.group(3).strip()
            
            # Hämta beskrivning (texten fram till nästa ## eller slut)
            start_pos = match.end()
            next_match = re.search(r'^##\s', content[start_pos:], re.MULTILINE)
            end_pos = start_pos + next_match.start() if next_match else len(content)
            details = content[start_pos:end_pos].strip()
            
            # Konvertera tider till datetime för jämförelse
            try:
                event_start = datetime.datetime.strptime(f"{file_date_str} {start_str}", "%Y-%m-%d %H:%M").replace(tzinfo=SYSTEM_TZ)
                
                # Om inspelningen startade inom +/- 20 minuter från mötets start
                # ELLER om inspelningen startade under mötets gång (om sluttid finns)
                is_match = False
                
                # Check 1: Start proximity (+/- 20 min)
                diff = abs((file_timestamp - event_start).total_seconds())
                if diff <= 1200: # 20 min
                    is_match = True
                
                # Check 2: Inside meeting
                if not is_match and end_str:
                    event_end = datetime.datetime.strptime(f"{file_date_str} {end_str}", "%Y-%m-%d %H:%M").replace(tzinfo=SYSTEM_TZ)
                    if event_start <= file_timestamp <= event_end:
                        is_match = True
                
                if is_match:
                    _log("📅", f"Hittade kalendermatch: '{title}'")
                    return f"""
MÖTESKONTEXT (Hittad i kalendern):
Titel: {title}
Tid: {start_str} - {end_str if end_str else '?'}
Detaljer:
{details}

INSTRUKTION: Använd ovanstående information för att:
1. Identifiera talare (se deltagare i Detaljer).
2. Förstå syftet med mötet.
3. Rätta eventuella felhörda namn eller termer.
"""
            except Exception as e:
                LOGGER.debug(f"Kunde inte parsa event-tid: {e}")
                continue
                
    except Exception as e:
        LOGGER.warning(f"Fel vid kalenderuppslag: {e}")
    
    return ""


def _build_speaker_context(max_people: int = 40, max_aliases: int = 40) -> str:
    """
    Hämtar kända talare (Person-entities) + alias från grafen för prompt injection.
    """
    try:
        entities = get_known_entities()
    except Exception as exc:
        LOGGER.debug(f"Kunde inte hämta kända entiteter: {exc}")
        return ""

    persons = [e for e in entities if e.get("type") == "Person"]
    if not persons:
        return ""

    canonical_names = sorted({p["id"] for p in persons if p.get("id")})[:max_people]
    alias_pairs = []
    for person in persons:
        canonical = person.get("id")
        for alias in person.get("aliases") or []:
            alias_pairs.append(f"{alias} = {canonical}")
    alias_pairs = alias_pairs[:max_aliases]

    lines = []
    lines.append("KÄNDA TALARE (canonical namn):")
    for name in canonical_names:
        lines.append(f"- {name}")

    if alias_pairs:
        lines.append("")
        lines.append("ALIAS SOM SKA NORMALISERAS (alias = canonical):")
        for pair in alias_pairs:
            lines.append(f"- {pair}")

    lines.append("")
    lines.append("INSTRUKTIONER:")
    lines.append("- Använd canonical namnen ovan när du identifierar talare.")
    lines.append("- Om ett alias förekommer i transkripten, skriv canonical namnet i svaret.")

    return "\n".join(lines)

def clean_ghost_artifacts():
    """Städar bort gamla artefakter från tidigare versioner."""
    base_mem = os.path.dirname(ASSET_STORE)
    ghost_drop = os.path.join(base_mem, "DropZone")
    ghost_log = os.path.join(base_mem, "Logs", "dfm_system.log")
    if os.path.exists(ghost_drop):
        try: 
            shutil.rmtree(ghost_drop)
        except Exception as e:
            LOGGER.debug(f"Kunde inte radera ghost drop-folder {ghost_drop}: {e}")
    if os.path.exists(ghost_log) and ghost_log != LOG_FILE:
        try: 
            os.remove(ghost_log)
        except Exception as e:
            LOGGER.debug(f"Kunde inte radera ghost log-fil {ghost_log}: {e}")

def fa_fil_skapad_datum(filväg):
    """Hämta filens skapelsedatum via central DateService."""
    try:
        ts = get_timestamp(filväg)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=SYSTEM_TZ)
        return ts
    except RuntimeError as e:
        LOGGER.error(f"HARDFAIL: {e}")
        raise

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
        if match:
            text = match.group(0)
            return json.loads(text)
        return json.loads(text_response)
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Kunde inte parsa JSON: {e}")
        raise ValueError(f"HARDFAIL: Kunde inte parsa JSON-svar") from e

def skapa_rich_header(filnamn, skapelsedatum, model, data, unit_id=None):
    now = datetime.datetime.now(SYSTEM_TZ).isoformat()
    speakers = "\n- ".join(data.get('speakers', [])) or "- Inga identifierade"
    entities = "\n- ".join(data.get('entities', [])) or "- Inga identifierade"
    summary = data.get('summary', 'Ingen sammanfattning tillgänglig.')
    unit_id_line = f"UNIT_ID:       {unit_id}\n" if unit_id else ""
    
    header = f"""================================================================================
METADATA FRÅN TRANSKRIBERING (MyMem)
================================================================================
FILNAMN:       {filnamn}
{unit_id_line}DATUM_TID:     {skapelsedatum.isoformat()}
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
    try:
        base, ext = os.path.splitext(filnamn)
        clean_name = UUID_SUFFIX_PATTERN.sub('', base) + ext
        dest = os.path.join(FAILED_FOLDER, clean_name)
        shutil.move(filväg, dest)
        LOGGER.warning(f"Flyttade till failed: {clean_name} - {reason}")
        return True
    except Exception as e:
        LOGGER.error(f"Kunde inte flytta till failed: {filnamn} - {e}")
        return False


def _normalize_speakers(result: dict):
    """
    Mappa identifierade talare till canonical namn och registrera alias i grafen.
    """
    speakers = result.get("speakers") or []
    if not speakers:
        return

    normalized = []
    alias_records = []

    for raw_name in speakers:
        if not raw_name:
            continue
        name = raw_name.strip()
        canonical = get_canonical(name)
        if canonical:
            normalized.append(canonical)
            if canonical != name:
                alias_records.append({"alias": name, "canonical": canonical})
                try:
                    add_entity_alias(canonical, name, "Person")
                except Exception as exc:
                    LOGGER.debug(f"Kunde inte lägga till alias {name}->{canonical}: {exc}")
        else:
            normalized.append(name)

    if normalized:
        # Behåll ordning men ta bort dubbletter
        seen = set()
        ordered = []
        for name in normalized:
            if name not in seen:
                ordered.append(name)
                seen.add(name)
        result["speakers"] = ordered

    if alias_records:
        result["speaker_aliases"] = alias_records

def _do_transcription(upload_file, model, kort_namn, filnamn, safety_settings):
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
                config=types.GenerateContentConfig(response_mime_type="text/plain", safety_settings=safety_settings)
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
                _log("⚠️", f"{kort_namn} → Retry {attempt+1}/5 ({wait_time}s)")
                time.sleep(wait_time)
            else:
                raise e
    
    if not transcript:
        raise Exception(f"Transkribering genererade ingen text efter 5 försök.")
    
    return transcript, int(time.time() - start)

def _do_analysis(transcript, model, kort_namn, filnamn, safety_settings, context_string=""):
    """
    Steg 2: Analys och Metadata.
    Nu med stöd för context_string (från kalender).
    """
    analysis_context = transcript[:2000000]
    raw_prompt = PROMPTS['transcriber']['analysis_prompt']
    
    context_payload = context_string.strip() or "Ingen extra kontext tillgänglig."
    analysis_prompt = raw_prompt.replace("{context_injection}", context_payload)
    
    result = None
    
    for attempt in range(5):
        try:
            response = AI_CLIENT.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=[
                    types.Part.from_text(text=f"{analysis_prompt}\n\nTRANSCRIPT TO ANALYZE:\n{analysis_context}")])],
                config=types.GenerateContentConfig(response_mime_type="application/json", safety_settings=safety_settings)
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
                _log("⚠️", f"{kort_namn} → Analysis Retry {attempt+1}/5 ({wait_time}s)")
                time.sleep(wait_time)
            else:
                LOGGER.error(f"HARDFAIL: Metadata-analys misslyckades för {filnamn}: {e}")
                raise RuntimeError(f"HARDFAIL: Metadata-analys misslyckades") from e
    
    if not result:
        raise Exception("Analys genererade inget resultat efter 5 försök.")
    
    return result

def processa_mediafil(filväg, filnamn):
    MODEL_FAST = MODELS.get('model_fast')
    MODEL_SMART = MODELS.get('model_pro')

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
    
    uuid_match = UUID_SUFFIX_PATTERN.search(base_name)
    if not uuid_match:
        LOGGER.warning(f"Skippar fil utan UUID: {filnamn}")
        with PROCESS_LOCK: PROCESSED_FILES.discard(filnamn)
        return
    
    unit_id = uuid_match.group(1)
    txt_fil = os.path.join(TRANSCRIPTS_FOLDER, f"{base_name}.txt")
    if os.path.exists(txt_fil): return

    _log("📥", f"{kort_namn} → Upload")
    upload_file = None
    start_time = time.time()

    try:
        # 1. Hämta skapelsedatum
        try:
            timestamp = fa_fil_skapad_datum(filväg)
        except RuntimeError as e:
            # HARDFAIL: Logga och använd fallback (detta är intentional - datum saknas)
            LOGGER.warning(f"Datum saknas för {kort_namn}, använder nuvarande tid: {e}")
            _log("⚠️", f"{kort_namn} → Datum saknas, hoppar över kalender: {e}")
            timestamp = datetime.datetime.now(SYSTEM_TZ)

        # 2. Hämta graf-/kalenderkontext
        speaker_context = _build_speaker_context()
        calendar_context = _get_calendar_context(timestamp)
        context_sections = [section for section in (speaker_context, calendar_context) if section]
        combined_context = "\n\n".join(context_sections)

        # 3. Ladda upp
        upload_file = safe_upload(filväg, filnamn)
        if not upload_file: raise Exception("Upload misslyckades.")

        waits = 0
        while upload_file.state.name == "PROCESSING":
            time.sleep(2)
            upload_file = AI_CLIENT.files.get(name=upload_file.name)
            waits += 1
            if waits > 30: raise Exception("Timeout processing.")

        if upload_file.state.name == "FAILED": raise Exception("File state FAILED.")

        # 4. Transkribering (Step 1 - Flash)
        raw_transcript, trans_time = _do_transcription(upload_file, MODEL_FAST, kort_namn, filnamn, SAFETY_SETTINGS)
        _log("⚡", f"{kort_namn} → Flash OK ({trans_time}s)")
        
        # 5. Analys med Context Injection (Step 2 - Pro)
        result = _do_analysis(raw_transcript, MODEL_SMART, kort_namn, filnamn, SAFETY_SETTINGS, context_string=combined_context)
        _log("🧠", f"{kort_namn} → Pro OK (Context Aware)")
        _normalize_speakers(result)
        
        # Kvalitetskontroll & Retry (samma som förut)
        quality_status = result.get('quality_status', 'OK')
        if quality_status == 'FAILED':
            failure_reason = result.get('failure_reason', 'Okänd anledning')
            _log("⚠️", f"{kort_namn} → Flash kvalitetsfel: {failure_reason}")
            _log("🔄", f"{kort_namn} → Retry med Pro+Pro...")
            
            raw_transcript, trans_time = _do_transcription(upload_file, MODEL_SMART, kort_namn, filnamn, SAFETY_SETTINGS)
            _log("🧠", f"{kort_namn} → Pro transkribering OK ({trans_time}s)")
            
            result = _do_analysis(raw_transcript, MODEL_SMART, kort_namn, filnamn, SAFETY_SETTINGS, context_string=combined_context)
            _log("🧠", f"{kort_namn} → Pro analys OK")
            _normalize_speakers(result)
            
            if result.get('quality_status', 'OK') == 'FAILED':
                failure_reason = result.get('failure_reason', 'Okänd anledning')
                _log("🚫", f"{kort_namn} → FAILED: {failure_reason}")
                _move_to_failed(filväg, filnamn, failure_reason)
                with PROCESS_LOCK: PROCESSED_FILES.discard(filnamn)
                return

        # 6. Spara
        final_transcript = result.get('transcript', raw_transcript)
        header = skapa_rich_header(filnamn, timestamp, MODEL_SMART, result, unit_id)

        with open(txt_fil, 'w', encoding='utf-8') as f:
            f.write(header + final_transcript)

        total_time = int(time.time() - start_time)
        _log("✅", f"{kort_namn} → Klar ({total_time}s)")

    except Exception as e:
        LOGGER.error(f"FEL vid transkribering av {filnamn}: {e}")
        print(f"{_ts()} ❌ TRANS: {kort_namn} → FAILED (se logg)")
        with PROCESS_LOCK: PROCESSED_FILES.discard(filnamn)

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
    pending = 0
    if os.path.exists(RECORDINGS_FOLDER):
        for f in os.listdir(RECORDINGS_FOLDER):
            if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                if not f.startswith("temp_upload_") and UUID_SUFFIX_PATTERN.search(os.path.splitext(f)[0]):
                    base = os.path.splitext(f)[0]
                    if not os.path.exists(os.path.join(TRANSCRIPTS_FOLDER, f"{base}.txt")):
                        pending += 1
                        EXECUTOR.submit(processa_mediafil, os.path.join(RECORDINGS_FOLDER, f), f)
    
    status_msg = f"({pending} väntande)" if pending > 0 else ""
    print(f"{_ts()} ✓ Transcriber online {status_msg}")

    observer = Observer()
    observer.schedule(AudioHandler(), RECORDINGS_FOLDER, recursive=False)
    observer.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: 
        EXECUTOR.shutdown(wait=False)
        observer.stop()
    observer.join()