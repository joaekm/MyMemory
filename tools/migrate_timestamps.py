#!/usr/bin/env python3
"""
migrate_timestamps.py - Migrerar befintliga Lake-filer till nytt tidsstämpelformat

Lägger till:
- timestamp_ingestion: Filens mtime (när den skapades i Lake)
- timestamp_content: Extraheras från DATUM_TID/DATUM+START headers, eller "UNKNOWN"
- timestamp_updated: null (sätts av Dreamer vid framtida uppdateringar)

Tar bort (om den finns):
- timestamp_created: Ersätts av timestamp_ingestion

Användning:
    python tools/migrate_timestamps.py              # Dry-run (visa vad som skulle ändras)
    python tools/migrate_timestamps.py --apply      # Verkställ ändringar
    python tools/migrate_timestamps.py --verbose    # Visa detaljer
"""

import os
import sys
import re
import yaml
import datetime
import argparse
from pathlib import Path

# Lägg till projektroten
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# --- CONFIG ---
def load_config():
    config_path = project_root / "config" / "my_mem_config.yaml"
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

CONFIG = load_config()
LAKE_PATH = os.path.expanduser(CONFIG['paths']['lake_store'])

# --- PATTERNS (samma som i doc_converter.py) ---
STANDARD_TIMESTAMP_PATTERN = re.compile(r'^DATUM_TID:\s+(.+)$', re.MULTILINE)
TRANSCRIBER_DATE_PATTERN = re.compile(r'^DATUM:\s+(\d{4}-\d{2}-\d{2})$', re.MULTILINE)
TRANSCRIBER_START_PATTERN = re.compile(r'^START:\s+(\d{2}:\d{2})$', re.MULTILINE)


def extract_content_date(text: str) -> str:
    """
    Extraherar timestamp_content från filinnehåll.
    Samma logik som doc_converter.py.
    """
    header_section = text[:3000]

    # 1. Försök DATUM_TID (collectors: Slack, Calendar, Gmail)
    match = STANDARD_TIMESTAMP_PATTERN.search(header_section)
    if match:
        ts_str = match.group(1).strip()
        try:
            dt = datetime.datetime.fromisoformat(ts_str)
            return dt.isoformat()
        except ValueError:
            pass

    # 2. Försök Transcriber-format (DATUM + START)
    date_match = TRANSCRIBER_DATE_PATTERN.search(header_section)
    start_match = TRANSCRIBER_START_PATTERN.search(header_section)

    if date_match and start_match:
        date_str = date_match.group(1)
        time_str = start_match.group(1)
        try:
            dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            return dt.isoformat()
        except ValueError:
            pass
    elif date_match:
        date_str = date_match.group(1)
        try:
            dt = datetime.datetime.strptime(f"{date_str} 12:00", "%Y-%m-%d %H:%M")
            return dt.isoformat()
        except ValueError:
            pass

    return "UNKNOWN"


def parse_frontmatter(filepath: str) -> tuple:
    """
    Läser en Lake-fil och returnerar (frontmatter_dict, body_text, raw_content).
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    if not content.startswith('---'):
        return {}, content, content

    parts = content.split('---', 2)
    if len(parts) < 3:
        return {}, content, content

    try:
        frontmatter = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        frontmatter = {}

    body = parts[2]
    return frontmatter, body, content


def write_file(filepath: str, frontmatter: dict, body: str):
    """Skriver tillbaka filen med uppdaterad frontmatter."""
    fm_str = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(f"---\n{fm_str}---{body}")


def migrate_file(filepath: str, dry_run: bool, verbose: bool) -> dict:
    """
    Migrerar en enskild fil.

    Returns:
        dict med status: {"action": "migrated|skipped|error", "details": str}
    """
    filename = os.path.basename(filepath)

    try:
        frontmatter, body, raw_content = parse_frontmatter(filepath)

        if not frontmatter:
            return {"action": "skipped", "details": "Ingen frontmatter"}

        changes = []

        # --- TIMESTAMP_INGESTION ---
        if 'timestamp_ingestion' not in frontmatter:
            # Använd timestamp_created om den finns, annars filens mtime
            if 'timestamp_created' in frontmatter:
                frontmatter['timestamp_ingestion'] = frontmatter['timestamp_created']
                changes.append(f"timestamp_ingestion = {frontmatter['timestamp_ingestion']} (från timestamp_created)")
            else:
                mtime = os.stat(filepath).st_mtime
                frontmatter['timestamp_ingestion'] = datetime.datetime.fromtimestamp(mtime).isoformat()
                changes.append(f"timestamp_ingestion = {frontmatter['timestamp_ingestion']} (från mtime)")

        # --- TIMESTAMP_CONTENT ---
        if 'timestamp_content' not in frontmatter:
            # Extrahera från innehållet
            timestamp_content = extract_content_date(raw_content)
            frontmatter['timestamp_content'] = timestamp_content
            changes.append(f"timestamp_content = {timestamp_content}")

        # --- TIMESTAMP_UPDATED ---
        if 'timestamp_updated' not in frontmatter:
            frontmatter['timestamp_updated'] = None
            changes.append("timestamp_updated = null")

        # --- TA BORT timestamp_created ---
        if 'timestamp_created' in frontmatter:
            del frontmatter['timestamp_created']
            changes.append("timestamp_created borttagen")

        # --- INGEN ÄNDRING BEHÖVS ---
        if not changes:
            return {"action": "skipped", "details": "Redan migrerad"}

        # --- SKRIV ÄNDRINGAR ---
        if not dry_run:
            # Ordna frontmatter i önskad ordning
            ordered_fm = {}
            key_order = [
                'unit_id', 'source_ref', 'original_filename',
                'timestamp_ingestion', 'timestamp_content', 'timestamp_updated',
                'context_summary', 'relations_summary', 'document_keywords',
                'source_type', 'ai_model'
            ]
            for key in key_order:
                if key in frontmatter:
                    ordered_fm[key] = frontmatter[key]
            # Lägg till övriga nycklar
            for key, value in frontmatter.items():
                if key not in ordered_fm:
                    ordered_fm[key] = value

            write_file(filepath, ordered_fm, body)

        return {"action": "migrated", "details": "; ".join(changes)}

    except Exception as e:
        return {"action": "error", "details": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Migrera Lake-filer till nytt tidsstämpelformat")
    parser.add_argument('--apply', action='store_true', help="Verkställ ändringar (annars dry-run)")
    parser.add_argument('--verbose', '-v', action='store_true', help="Visa detaljer för varje fil")
    args = parser.parse_args()

    dry_run = not args.apply

    if dry_run:
        print("=" * 60)
        print("DRY-RUN MODE - Inga ändringar görs")
        print("Kör med --apply för att verkställa")
        print("=" * 60)
    else:
        print("=" * 60)
        print("APPLY MODE - Ändringar kommer att skrivas")
        print("=" * 60)

    print(f"\nLake-sökväg: {LAKE_PATH}\n")

    if not os.path.exists(LAKE_PATH):
        print(f"FEL: Lake-mappen finns inte: {LAKE_PATH}")
        sys.exit(1)

    # Hitta alla .md filer
    files = [f for f in os.listdir(LAKE_PATH) if f.endswith('.md')]
    print(f"Hittade {len(files)} Lake-filer\n")

    stats = {"migrated": 0, "skipped": 0, "error": 0, "unknown_content": 0}

    for filename in sorted(files):
        filepath = os.path.join(LAKE_PATH, filename)
        result = migrate_file(filepath, dry_run, args.verbose)

        stats[result["action"]] += 1

        # Räkna UNKNOWN
        if "timestamp_content = UNKNOWN" in result.get("details", ""):
            stats["unknown_content"] += 1

        if args.verbose or result["action"] == "error":
            status_icon = {
                "migrated": "✅",
                "skipped": "⏭️",
                "error": "❌"
            }.get(result["action"], "?")
            print(f"{status_icon} {filename}")
            if args.verbose:
                print(f"   {result['details']}")
        elif result["action"] == "migrated":
            print(f"✅ {filename}")

    # Sammanfattning
    print("\n" + "=" * 60)
    print("SAMMANFATTNING")
    print("=" * 60)
    print(f"Migrerade:  {stats['migrated']}")
    print(f"Överhoppade: {stats['skipped']}")
    print(f"Fel:        {stats['error']}")
    print(f"\nFiler med timestamp_content=UNKNOWN: {stats['unknown_content']}")

    if dry_run and stats['migrated'] > 0:
        print("\n⚠️  Kör med --apply för att verkställa ändringarna")


if __name__ == "__main__":
    main()
