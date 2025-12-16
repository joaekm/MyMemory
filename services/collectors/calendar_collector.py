"""
Calendar Collector - Google Calendar Integration

Hämtar kalenderhändelser och skapar Daily Digest-filer.

Output: Assets/Calendar/Calendar_YYYY-MM-DD_[UUID].md
Format: YAML header + markdown body

Princip: HARDFAIL > Silent Fallback
"""

import os
import re
import time
import yaml
import logging
import datetime
import uuid
import zoneinfo
from pathlib import Path

# --- CONFIG ---
def ladda_yaml(filnamn, strict=True):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, 'config', filnamn),
        os.path.join(script_dir, '..', 'config', filnamn)
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f:
                return yaml.safe_load(f)
    if strict:
        print(f"[CRITICAL] Kunde inte hitta: {filnamn}")
        exit(1)
    return {}


CONFIG = ladda_yaml('my_mem_config.yaml', strict=True)

TZ_NAME = CONFIG.get('system', {}).get('timezone', 'UTC')
try:
    SYSTEM_TZ = zoneinfo.ZoneInfo(TZ_NAME)
except Exception as e:
    print(f"[CRITICAL] HARDFAIL: Ogiltig timezone '{TZ_NAME}': {e}")
    exit(1)

CALENDAR_FOLDER = os.path.expanduser(CONFIG['paths'].get('asset_calendar', '~/MyMemory/Assets/Calendar'))
LOG_FILE = os.path.expanduser(CONFIG['logging']['log_file_path'])
GOOGLE_CONF = CONFIG.get('google', {})
CALENDAR_CONF = GOOGLE_CONF.get('calendar', {})

# Google credentials - MÅSTE finnas i config
if not GOOGLE_CONF.get('credentials_path'):
    print(f"[CRITICAL] HARDFAIL: google.credentials_path saknas i config")
    exit(1)
if not GOOGLE_CONF.get('token_path'):
    print(f"[CRITICAL] HARDFAIL: google.token_path saknas i config")
    exit(1)

CREDENTIALS_PATH = os.path.expanduser(GOOGLE_CONF['credentials_path'])
TOKEN_PATH = os.path.expanduser(GOOGLE_CONF['token_path'])
SCOPES = GOOGLE_CONF.get('scopes', ['https://www.googleapis.com/auth/calendar.readonly'])
CALENDAR_IDS = CALENDAR_CONF.get('calendar_ids', ['primary'])
HISTORY_DAYS = CALENDAR_CONF.get('history_days', 7)
FUTURE_DAYS = CALENDAR_CONF.get('future_days', 14)

log_dir = os.path.dirname(LOG_FILE)
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - CALENDAR - %(levelname)s - %(message)s'
)
LOGGER = logging.getLogger('MyMem_CalendarCollector')


def _ts():
    return datetime.datetime.now(SYSTEM_TZ).strftime("[%H:%M:%S]")


# --- GOOGLE AUTH ---

def get_calendar_service():
    """
    Autentisera mot Google Calendar API.
    Returnerar en service-instans eller None vid fel.
    """
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as e:
        print(f"{_ts()} ❌ CALENDAR: Google API bibliotek saknas")
        LOGGER.error(f"HARDFAIL: Google API bibliotek saknas: {e}")
        return None
    
    creds = None
    
    # Ladda befintlig token
    if os.path.exists(TOKEN_PATH):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        except Exception as e:
            LOGGER.warning(f"Kunde inte ladda token: {e}")
    
    # Refresh eller ny auth
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                LOGGER.warning(f"Kunde inte refresha token: {e}")
                creds = None
        
        if not creds:
            if not os.path.exists(CREDENTIALS_PATH):
                print(f"{_ts()} ❌ CALENDAR: Credentials saknas - kör OAuth-setup först")
                LOGGER.error(f"HARDFAIL: Credentials saknas: {CREDENTIALS_PATH}")
                return None
            
            try:
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
                creds = flow.run_local_server(port=0)
            except Exception as e:
                LOGGER.error(f"HARDFAIL: OAuth-flöde misslyckades: {e}")
                return None
        
        # Spara token
        try:
            os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
            with open(TOKEN_PATH, 'w') as token:
                token.write(creds.to_json())
        except Exception as e:
            LOGGER.warning(f"Kunde inte spara token: {e}")
    
    try:
        service = build('calendar', 'v3', credentials=creds)
        return service
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Kunde inte skapa Calendar service: {e}")
        return None


# --- EVENT EXTRACTION ---

def strip_html(text: str) -> str:
    """Ta bort HTML-taggar från text."""
    if not text:
        return ""
    return re.sub(r'<[^>]+>', '', text).strip()


def extract_event_info(event: dict) -> dict:
    """
    Extrahera relevant info från ett kalenderevent.
    """
    # Start/sluttid
    start = event.get('start', {})
    end = event.get('end', {})
    
    start_dt = start.get('dateTime') or start.get('date')
    end_dt = end.get('dateTime') or end.get('date')
    
    # Deltagare med RSVP-status
    attendees = []
    for attendee in event.get('attendees', []):
        name = attendee.get('displayName') or attendee.get('email', '').split('@')[0]
        status = attendee.get('responseStatus', 'needsAction')
        if name:
            status_text = {
                'accepted': '(accepterat)',
                'declined': '(tackat nej)',
                'tentative': '(kanske)',
                'needsAction': '(ej svarat)'
            }.get(status, '')
            attendees.append(f"{name} {status_text}".strip())
    
    # Beskrivning utan HTML
    description = event.get('description', '')
    clean_description = strip_html(description)
    
    return {
        'id': event.get('id', ''),
        'summary': event.get('summary', 'Ingen titel'),
        'start': start_dt,
        'end': end_dt,
        'location': event.get('location', ''),
        'attendees': attendees,
        'description': clean_description,
        'organizer': event.get('organizer', {}).get('displayName') or event.get('organizer', {}).get('email', ''),
        'status': event.get('status', 'confirmed'),
        'html_link': event.get('htmlLink', '')
    }


def fetch_events_for_date(service, calendar_id: str, target_date: datetime.date) -> list:
    """
    Hämta alla events för ett specifikt datum.
    """
    # Skapa tidsintervall för dagen
    start_of_day = datetime.datetime.combine(target_date, datetime.time.min).replace(tzinfo=SYSTEM_TZ)
    end_of_day = datetime.datetime.combine(target_date, datetime.time.max).replace(tzinfo=SYSTEM_TZ)
    
    time_min = start_of_day.isoformat()
    time_max = end_of_day.isoformat()
    
    events = []
    page_token = None
    
    while True:
        try:
            result = service.events().list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy='startTime',
                pageToken=page_token
            ).execute()
            
            for event in result.get('items', []):
                events.append(extract_event_info(event))
            
            page_token = result.get('nextPageToken')
            if not page_token:
                break
                
        except Exception as e:
            LOGGER.error(f"HARDFAIL: Kunde inte hämta events för {target_date}: {e}")
            raise RuntimeError(f"HARDFAIL: Calendar API fel") from e
    
    return events


# --- DAILY DIGEST ---

def get_existing_digest_path(target_date: datetime.date) -> str | None:
    """
    Hitta befintlig digest-fil för ett datum.
    Returnerar sökvägen eller None om ingen finns.
    """
    date_str = target_date.strftime('%Y-%m-%d')
    base_pattern = f"Calendar_{date_str}_"
    
    try:
        for f in os.listdir(CALENDAR_FOLDER):
            if f.startswith(base_pattern) and f.endswith('.md'):
                return os.path.join(CALENDAR_FOLDER, f)
    except FileNotFoundError:
        LOGGER.debug(f"Calendar folder finns inte: {CALENDAR_FOLDER}")
    
    return None


def create_daily_digest(target_date: datetime.date, events: list) -> bool:
    """
    Skapa eller uppdatera en Daily Digest för ett datum.
    
    Force overwrite: Alltid skriva om filen för att reflektera ändringar.
    
    Returns:
        True om fil skapades/uppdaterades, False om inga events
    """
    date_str = target_date.strftime('%Y-%m-%d')
    
    # Kolla om det finns en befintlig fil
    existing_path = get_existing_digest_path(target_date)
    
    # Om inga events, ta bort befintlig fil om den finns
    if not events:
        if existing_path and os.path.exists(existing_path):
            os.remove(existing_path)
            LOGGER.info(f"Raderade tom digest för {date_str}")
        return False
    
    # Använd befintligt UUID eller skapa nytt
    if existing_path:
        # Extrahera UUID från befintlig fil
        existing_name = os.path.basename(existing_path)
        uuid_match = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', existing_name)
        unit_id = uuid_match.group(1) if uuid_match else str(uuid.uuid4())
        output_path = existing_path
    else:
        unit_id = str(uuid.uuid4())
        filnamn = f"Calendar_{date_str}_{unit_id}.md"
        output_path = os.path.join(CALENDAR_FOLDER, filnamn)
    
    # Formatera events
    event_lines = []
    for event in events:
        # Tid
        start = event['start']
        end = event['end']
        
        # Hantera heldagsevent vs tidsevent
        if 'T' in str(start):
            try:
                start_dt = datetime.datetime.fromisoformat(start.replace('Z', '+00:00'))
                end_dt = datetime.datetime.fromisoformat(end.replace('Z', '+00:00'))
                time_str = f"{start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')}"
            except Exception as e:
                LOGGER.debug(f"Kunde inte parsa tid {start}-{end}: {e}")
                time_str = "Heldag"
        else:
            time_str = "Heldag"
        
        # Deltagare (inline)
        attendees_str = ", ".join(event['attendees']) if event['attendees'] else ""
        
        # Bygg event-block
        lines = [f"## {time_str}: {event['summary']}"]
        
        if event['location']:
            lines.append(f"**Plats:** {event['location']}")
        
        if attendees_str:
            lines.append(f"**Deltagare:** {attendees_str}")
        
        if event['organizer']:
            lines.append(f"**Organisatör:** {event['organizer']}")
        
        if event['description']:
            lines.append(f"\n{event['description']}")
        
        lines.append("")  # Tom rad efter varje event
        event_lines.append("\n".join(lines))
    
    # Metadata
    archived_at = datetime.datetime.now(SYSTEM_TZ).isoformat()
    day_iso = datetime.datetime.combine(target_date, datetime.time(12, 0)).replace(tzinfo=SYSTEM_TZ).isoformat()
    
    # Bygg innehåll
    content = f"""================================================================================
METADATA FRÅN KALENDER
================================================================================
DATUM_TID:     {day_iso}
ARKIVERAD:     {archived_at}
KÄLLA:         Google Calendar API
UNIT_ID:       {unit_id}
--------------------------------------------------------------------------------
SAMMANFATTNING (Auto):
Daglig kalenderöversikt för {date_str}.
Innehåller {len(events)} möten/händelser.
================================================================================

# Kalender {date_str}

{chr(10).join(event_lines)}
"""
    
    # Skriv fil
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    action = "uppdaterad" if existing_path else "ny"
    print(f"{_ts()} 🔄 CALENDAR: {date_str} → Calendar ({len(events)} events, {action})")
    LOGGER.info(f"Digest {'uppdaterad' if existing_path else 'skapad'}: {os.path.basename(output_path)}")
    
    return True


# --- MAIN ---

def run_collector():
    """
    Huvudloop för Calendar Collector.
    """
    os.makedirs(CALENDAR_FOLDER, exist_ok=True)
    
    service = get_calendar_service()
    if not service:
        return 0
    
    print(f"{_ts()} ✓ Calendar Collector startar ({len(CALENDAR_IDS)} kalendrar, {HISTORY_DAYS}d bakåt + {FUTURE_DAYS}d framåt)")
    LOGGER.info(f"Calendar Collector startar: {len(CALENDAR_IDS)} kalendrar")
    
    new_files = 0
    today = datetime.date.today()
    
    # Hämta för varje dag i intervallet
    for i in range(-HISTORY_DAYS, FUTURE_DAYS + 1):
        target_date = today + datetime.timedelta(days=i)
        
        # Samla events från alla kalendrar
        all_events = []
        for calendar_id in CALENDAR_IDS:
            try:
                events = fetch_events_for_date(service, calendar_id, target_date)
                all_events.extend(events)
            except Exception as e:
                LOGGER.error(f"HARDFAIL: Kunde inte hämta {calendar_id} för {target_date}: {e}")
                raise
        
        # Sortera på starttid
        all_events.sort(key=lambda x: x.get('start', ''))
        
        # Skapa digest
        if create_daily_digest(target_date, all_events):
            new_files += 1
    
    print(f"{_ts()} ✓ Calendar Collector klar ({new_files} nya filer)")
    LOGGER.info(f"Calendar Collector klar: {new_files} filer")
    
    return new_files


if __name__ == "__main__":
    try:
        run_collector()
    except KeyboardInterrupt:
        print(f"\n{_ts()} Calendar Collector avslutad.")
        LOGGER.info("Calendar Collector avslutad av användare")
    except Exception as e:
        print(f"{_ts()} ❌ CALENDAR: {e}")
        LOGGER.error(f"HARDFAIL: {e}")
        raise

