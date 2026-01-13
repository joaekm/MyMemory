import os
import sys
import re
import yaml
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

# 1. Setup Logging (Stderr för MCP)
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format='%(levelname)s:%(name)s:%(message)s'
)

# Path setup
project_root = str(Path(__file__).parent.parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from mcp.server.fastmcp import FastMCP
from services.utils.graph_service import GraphStore
# NY IMPORT: Använd VectorService (Single Source of Truth)
from services.utils.vector_service import get_vector_service

# --- CONFIG LOADING ---
def _load_config():
    config_path = os.path.join(project_root, "config", "my_mem_config.yaml")
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logging.error(f"Config load failed: {e}")
        return {}

CONFIG = _load_config()
PATHS = CONFIG.get('paths', {})

GRAPH_PATH = os.path.expanduser(PATHS.get('graph_db', '~/MyMemory/Index/GraphDB'))
LAKE_PATH = os.path.expanduser(PATHS.get('lake_dir', '~/MyMemory/Lake'))

mcp = FastMCP("MyMemoryTrinityConsole")

# --- HELPERS ---

def _parse_frontmatter(file_path: str) -> Dict:
    """Läser YAML-frontmatter från en markdown-fil."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    return yaml.safe_load(parts[1])
        return {}
    except Exception:
        return {}

# --- TOOL 1: GRAPH (Structure) ---

@mcp.tool()
def search_graph_nodes(query: str, node_type: str = None) -> str:
    """
    Söker efter STRUKTUR i Grafdatabasen.
    Hittar entiteter baserat på namn, ID eller alias.
    
    Används för att svara på: "Finns noden X?" eller "Hur ser relationerna ut?"
    """
    try:
        # GraphStore använder DuckDB internt och är robust
        graph = GraphStore(GRAPH_PATH, read_only=True)
        limit = 15
        
        # Direkt SQL för prestanda och filtrering
        sql = "SELECT id, type, aliases, properties FROM nodes WHERE (id ILIKE ? OR aliases ILIKE ?)"
        params = [f"%{query}%", f"%{query}%"]
        
        if node_type:
            sql += " AND type = ?"
            params.append(node_type)
            
        sql += " LIMIT ?"
        params.append(limit)
        
        rows = graph.conn.execute(sql, params).fetchall()
        graph.close()
        
        if not rows:
            return f"GRAF: Inga träffar för '{query}'" + (f" (Typ: {node_type})" if node_type else "")

        output = [f"=== GRAF RESULTAT ({len(rows)}) ==="]
        for r in rows:
            node_id, n_type, aliases_raw, props_raw = r
            props = json.loads(props_raw) if props_raw else {}
            aliases = json.loads(aliases_raw) if aliases_raw else []
            
            # Formatera output för läsbarhet
            name = props.get('name', node_id)
            ctx = props.get('context_keywords', [])
            ctx_str = f"Context: {ctx}" if ctx else "No context"
            alias_str = f"Aliases: {len(aliases)}" if aliases else ""
            
            output.append(f"• [{n_type}] {name}")
            output.append(f"  ID: {node_id}")
            if alias_str: output.append(f"  {alias_str}")
            output.append(f"  {ctx_str}")
            
        return "\n".join(output)
    except Exception as e:
        return f"Grafsökning misslyckades: {e}"

# --- TOOL 2: VECTOR (Semantics) ---

@mcp.tool()
def query_vector_memory(query_text: str, n_results: int = 5) -> str:
    """
    Söker i VEKTOR-minnet (Semantisk sökning).
    Använder VectorService för att garantera rätt modell och collection.
    """
    try:
        # 1. Hämta Singleton för Knowledge Base (samma som indexeraren använder)
        # Vi ber explicit om "knowledge_base" enligt din instruktion
        vs = get_vector_service("knowledge_base")
        
        # 2. Sök (VectorService returnerar en ren lista med dicts)
        results = vs.search(query_text=query_text, limit=n_results)
        
        if not results:
            return f"VEKTOR: Inga semantiska matchningar för '{query_text}'."

        output = [f"=== VEKTOR RESULTAT ('{query_text}') ==="]
        output.append(f"Modell: {vs.model_name}") # Bekräfta modellen för transparens
        output.append("-" * 30)
        
        for i, item in enumerate(results):
            # VectorService har redan packat upp Chroma-strukturen åt oss
            dist = item['distance']
            meta = item['metadata']
            content = item['document']
            uid = item['id']
            
            content_preview = content.replace('\n', ' ')[:150] + "..."
            
            # Bedöm kvalitet (lägre distans = bättre)
            quality = "🔥 Stark" if dist < 0.8 else "❄️ Svag" if dist > 1.2 else "☁️ Medel"
            
            output.append(f"{i+1}. [{quality} Match] (Dist: {dist:.3f})")
            output.append(f"   Fil: {meta.get('filename', 'Unknown')}")
            output.append(f"   Content: \"{content_preview}\"")
            output.append(f"   ID: {uid}")
            output.append("---")
            
        return "\n".join(output)

    except Exception as e:
        # Returnera felet till chatten för transparens
        return f"⚠️ VEKTOR-FEL: {str(e)}"

# --- TOOL 3: LAKE (Metadata) ---

@mcp.tool()
def search_by_date_range(
    start_date: str,
    end_date: str,
    date_field: str = "content"
) -> str:
    """
    Söker efter dokument inom ett datumintervall.

    Args:
        start_date: Startdatum (YYYY-MM-DD)
        end_date: Slutdatum (YYYY-MM-DD)
        date_field: Vilket datumfält som ska användas:
            - "content": timestamp_content (när innehållet hände)
            - "ingestion": timestamp_ingestion (när filen skapades i Lake)
            - "updated": timestamp_updated (senaste semantiska uppdatering)

    Returns:
        Lista med matchande dokument sorterade efter datum
    """
    from datetime import datetime

    # Mappa date_field till frontmatter-nyckel
    field_map = {
        "content": "timestamp_content",
        "ingestion": "timestamp_ingestion",
        "updated": "timestamp_updated"
    }

    if date_field not in field_map:
        return f"⚠️ Ogiltigt date_field: '{date_field}'. Använd: content, ingestion, updated"

    timestamp_key = field_map[date_field]

    try:
        # Parsa datum
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError as e:
        return f"⚠️ Ogiltigt datumformat: {e}. Använd YYYY-MM-DD"

    if not os.path.exists(LAKE_PATH):
        return f"⚠️ LAKE-FEL: Mappen {LAKE_PATH} finns inte."

    matches = []
    skipped_unknown = 0

    try:
        files = [f for f in os.listdir(LAKE_PATH) if f.endswith('.md')]

        for filename in files:
            full_path = os.path.join(LAKE_PATH, filename)
            frontmatter = _parse_frontmatter(full_path)

            timestamp_str = frontmatter.get(timestamp_key)

            # Hantera UNKNOWN och None
            if not timestamp_str or timestamp_str == "UNKNOWN":
                skipped_unknown += 1
                continue

            try:
                # Parsa ISO-format (med eller utan tid)
                if 'T' in timestamp_str:
                    file_dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    # Ta bort timezone för jämförelse
                    file_dt = file_dt.replace(tzinfo=None)
                else:
                    file_dt = datetime.strptime(timestamp_str[:10], "%Y-%m-%d")

                # Kolla om inom intervall
                if start_dt <= file_dt <= end_dt:
                    source_type = frontmatter.get('source_type', 'Unknown')
                    summary = frontmatter.get('context_summary', '')[:80]
                    matches.append({
                        'filename': filename,
                        'date': file_dt,
                        'source_type': source_type,
                        'summary': summary
                    })

            except (ValueError, TypeError) as e:
                logging.debug(f"Kunde inte parsa datum i {filename}: {e}")
                continue

        # Sortera efter datum
        matches.sort(key=lambda x: x['date'])

        if not matches:
            msg = f"DATUM: Inga träffar för {start_date} - {end_date} (fält: {date_field})"
            if skipped_unknown > 0:
                msg += f"\n⚠️ {skipped_unknown} filer har {timestamp_key}=UNKNOWN och exkluderades"
            return msg

        output = [f"=== DATUM RESULTAT ({len(matches)} träffar) ==="]
        output.append(f"Intervall: {start_date} → {end_date}")
        output.append(f"Fält: {timestamp_key}")
        if skipped_unknown > 0:
            output.append(f"⚠️ {skipped_unknown} filer med UNKNOWN exkluderade")
        output.append("-" * 30)

        for m in matches:
            date_str = m['date'].strftime("%Y-%m-%d %H:%M")
            output.append(f"📄 [{date_str}] {m['filename']}")
            output.append(f"   Typ: {m['source_type']}")
            if m['summary']:
                output.append(f"   {m['summary']}...")

        return "\n".join(output)

    except Exception as e:
        return f"Datumsökning misslyckades: {e}"


@mcp.tool()
def search_lake_metadata(keyword: str, field: str = None) -> str:
    """
    Söker i KÄLLFILERNAS metadata (Lake Header).
    Skannar markdown-filer för att se hur de är taggade.
    """
    matches = []
    scanned_count = 0
    
    try:
        if not os.path.exists(LAKE_PATH):
             return f"⚠️ LAKE-FEL: Mappen {LAKE_PATH} finns inte."

        # Hämta alla .md filer
        files = [f for f in os.listdir(LAKE_PATH) if f.endswith('.md')]
        
        for filename in files:
            scanned_count += 1
            full_path = os.path.join(LAKE_PATH, filename)
            frontmatter = _parse_frontmatter(full_path)
            
            found = False
            hit_details = []
            
            # Söklogik
            for k, v in frontmatter.items():
                # Om användaren specificerat fält, hoppa över andra
                if field and k != field:
                    continue
                
                # Sök i listor (t.ex. mentions, keywords)
                if isinstance(v, list):
                    for item in v:
                        if keyword.lower() in str(item).lower():
                            found = True
                            hit_details.append(f"{k}: ...{item}...")
                # Sök i strängar (t.ex. summary, title)
                elif isinstance(v, str):
                    if keyword.lower() in v.lower():
                        found = True
                        hit_details.append(f"{k}: {v[:50]}...")
            
            if found:
                matches.append(f"📄 {filename} -> [{', '.join(hit_details)}]")
                if len(matches) >= 10: # Cap results
                    break
        
        if not matches:
            return f"LAKE: Inga metadata-träffar för '{keyword}' (Skannade {scanned_count} filer)."
            
        output = [f"=== LAKE METADATA ({len(matches)} träffar) ==="]
        output.extend(matches)
        return "\n".join(output)

    except Exception as e:
        return f"Lake-sökning misslyckades: {e}"


# --- TOOL 5: RELATIONSHIP EXPLORER ---

@mcp.tool()
def get_neighbor_network(node_id: str) -> str:
    """
    Utforskar relationsnätverket kring en entitet.
    Visar både utgående och inkommande kopplingar.

    Användning: Efter att ha hittat en entitet med search_graph_nodes,
    använd detta för att se vem/vad den är kopplad till.
    """
    try:
        graph = GraphStore(GRAPH_PATH, read_only=True)

        # Hämta huvudnoden
        center_node = graph.get_node(node_id)
        if not center_node:
            graph.close()
            return f"Noden '{node_id}' hittades inte."

        # Hämta kanter
        out_edges = graph.get_edges_from(node_id)
        in_edges = graph.get_edges_to(node_id)

        # Samla grann-IDn för namnuppslag
        neighbor_ids = set()
        for e in out_edges:
            neighbor_ids.add(e['target'])
        for e in in_edges:
            neighbor_ids.add(e['source'])

        # Hämta namn på grannar
        neighbor_map = {}
        for nid in neighbor_ids:
            n = graph.get_node(nid)
            if n:
                props = n.get('properties', {})
                neighbor_map[nid] = props.get('name', nid)
            else:
                neighbor_map[nid] = nid

        graph.close()

        # Formatera output
        c_props = center_node.get('properties', {})
        c_name = c_props.get('name', node_id)

        output = [f"=== NÄTVERK: {c_name} ({center_node['type']}) ==="]

        if out_edges:
            output.append("\n--> UTGÅENDE:")
            for e in out_edges:
                target_name = neighbor_map.get(e['target'], e['target'])
                output.append(f"   [{e['type']}] -> {target_name}")

        if in_edges:
            output.append("\n<-- INKOMMANDE:")
            for e in in_edges:
                source_name = neighbor_map.get(e['source'], e['source'])
                output.append(f"   {source_name} -> [{e['type']}]")

        if not out_edges and not in_edges:
            output.append("   (Inga kopplingar - Isolerad nod)")

        return "\n".join(output)

    except Exception as e:
        return f"Nätverksutforskning misslyckades: {e}"


# --- TOOL 6: ENTITY SUMMARY ---

@mcp.tool()
def get_entity_summary(node_id: str) -> str:
    """
    Hämtar en komplett sammanfattning av allt vi vet om en entitet.
    Inkluderar metadata, konfidens, användningsstatistik och bevis/kontext.
    """
    try:
        graph = GraphStore(GRAPH_PATH, read_only=True)
        node = graph.get_node(node_id)

        if not node:
            graph.close()
            return f"Noden '{node_id}' hittades inte."

        props = node.get('properties', {})
        name = props.get('name', node_id)

        output = [f"=== SUMMERING: {name} ==="]
        output.append(f"Typ: {node['type']}")
        output.append(f"ID: {node_id}")
        output.append(f"Konfidens: {props.get('confidence', 'N/A')}")
        output.append(f"Status: {props.get('status', 'N/A')}")

        # Statistik
        retrieved = props.get('retrieved_times', 0)
        last_seen = props.get('last_retrieved_at', 'Aldrig')
        output.append(f"Popularitet: {retrieved} visningar | Sist sedd: {last_seen}")

        # Aliases
        aliases = node.get('aliases', [])
        if aliases:
            output.append(f"Aliases: {', '.join(aliases[:5])}" + (" ..." if len(aliases) > 5 else ""))

        # Kontext/bevis
        output.append("\n--- KONTEXT & BEVIS ---")
        ctx = props.get('node_context', [])
        if ctx:
            seen_txt = set()
            count = 0
            for item in ctx:
                if isinstance(item, dict):
                    txt = item.get('text', '').replace('\n', ' ')
                    origin = item.get('origin', 'Okänd')
                    if txt and txt not in seen_txt:
                        output.append(f"• \"{txt[:120]}...\" [Källa: {origin}]")
                        seen_txt.add(txt)
                        count += 1
                if count >= 10:
                    break
        else:
            output.append("(Ingen kontext lagrad)")

        graph.close()
        return "\n".join(output)

    except Exception as e:
        return f"Summering misslyckades: {e}"


# --- TOOL 7: GRAPH STATISTICS ---

@mcp.tool()
def get_graph_statistics() -> str:
    """
    Hämtar övergripande statistik om kunskapsgrafen.
    Visar antal noder och kanter per typ.
    """
    try:
        graph = GraphStore(GRAPH_PATH, read_only=True)
        stats = graph.get_stats()
        graph.close()

        output = ["=== GRAF STATISTIK ==="]
        output.append(f"Totalt antal noder: {stats['total_nodes']}")
        output.append(f"Totalt antal kanter: {stats['total_edges']}")

        output.append("\n--- Noder per Typ ---")
        for k, v in stats.get('nodes', {}).items():
            output.append(f"  {k}: {v}")

        output.append("\n--- Kanter per Typ ---")
        for k, v in stats.get('edges', {}).items():
            output.append(f"  {k}: {v}")

        return "\n".join(output)

    except Exception as e:
        return f"Kunde inte hämta statistik: {e}"


# --- TOOL 8: RELATIVE DATE PARSER ---

@mcp.tool()
def parse_relative_date(expression: str) -> str:
    """
    Konverterar naturliga tidsuttryck till absoluta datum.

    Exempel: "förra veckan", "igår", "3 dagar sedan"
    Returnerar start_date och end_date i YYYY-MM-DD format.
    Använd sedan search_by_date_range() med dessa datum.
    """
    today = datetime.now().date()
    result = {
        "today": today.isoformat(),
        "expression": expression,
        "start_date": None,
        "end_date": None,
        "description": ""
    }

    expr_lower = expression.lower().strip()

    # TODAY
    if expr_lower in ["today", "idag"]:
        result["start_date"] = today.isoformat()
        result["end_date"] = today.isoformat()
        result["description"] = f"Idag ({today.isoformat()})"

    # YESTERDAY
    elif expr_lower in ["yesterday", "igår"]:
        yesterday = today - timedelta(days=1)
        result["start_date"] = yesterday.isoformat()
        result["end_date"] = yesterday.isoformat()
        result["description"] = f"Igår ({yesterday.isoformat()})"

    # THIS WEEK
    elif expr_lower in ["this week", "denna veckan", "den här veckan"]:
        monday = today - timedelta(days=today.weekday())
        result["start_date"] = monday.isoformat()
        result["end_date"] = today.isoformat()
        result["description"] = f"Denna vecka ({monday.isoformat()} - {today.isoformat()})"

    # LAST WEEK
    elif expr_lower in ["last week", "förra veckan"]:
        prev_monday = today - timedelta(days=today.weekday() + 7)
        prev_sunday = prev_monday + timedelta(days=6)
        result["start_date"] = prev_monday.isoformat()
        result["end_date"] = prev_sunday.isoformat()
        result["description"] = f"Förra veckan ({prev_monday.isoformat()} - {prev_sunday.isoformat()})"

    # THIS MONTH
    elif expr_lower in ["this month", "denna månaden", "den här månaden"]:
        first_of_month = today.replace(day=1)
        result["start_date"] = first_of_month.isoformat()
        result["end_date"] = today.isoformat()
        result["description"] = f"Denna månad ({first_of_month.isoformat()} - {today.isoformat()})"

    # LAST MONTH
    elif expr_lower in ["last month", "förra månaden"]:
        first_of_this_month = today.replace(day=1)
        last_day_prev_month = first_of_this_month - timedelta(days=1)
        first_of_prev_month = last_day_prev_month.replace(day=1)
        result["start_date"] = first_of_prev_month.isoformat()
        result["end_date"] = last_day_prev_month.isoformat()
        result["description"] = f"Förra månaden ({first_of_prev_month.isoformat()} - {last_day_prev_month.isoformat()})"

    # X DAYS AGO
    elif match := re.search(r'(\d+)\s*(?:days?|dagar?)\s*(?:ago|sedan)', expr_lower):
        days = int(match.group(1))
        target_date = today - timedelta(days=days)
        result["start_date"] = target_date.isoformat()
        result["end_date"] = target_date.isoformat()
        result["description"] = f"{days} dagar sedan ({target_date.isoformat()})"

    # X WEEKS AGO
    elif match := re.search(r'(\d+)\s*(?:weeks?|veckor?)\s*(?:ago|sedan)', expr_lower):
        weeks = int(match.group(1))
        target_monday = today - timedelta(days=today.weekday() + (weeks * 7))
        target_sunday = target_monday + timedelta(days=6)
        result["start_date"] = target_monday.isoformat()
        result["end_date"] = target_sunday.isoformat()
        result["description"] = f"{weeks} vecka/or sedan ({target_monday.isoformat()} - {target_sunday.isoformat()})"

    # RECENT
    elif expr_lower in ["recent", "recently", "nyligen"]:
        start = today - timedelta(days=7)
        result["start_date"] = start.isoformat()
        result["end_date"] = today.isoformat()
        result["description"] = f"Senaste 7 dagarna ({start.isoformat()} - {today.isoformat()})"

    # FALLBACK
    else:
        result["description"] = f"Okänt uttryck: '{expression}'. Prova 'förra veckan', 'igår', '3 dagar sedan'."

    return json.dumps(result, indent=2, ensure_ascii=False)


# --- TOOL 9: READ DOCUMENT CONTENT (Smart Truncation) ---

def _smart_truncate(content: str, max_length: int, tail_ratio: float = 0.2) -> tuple:
    """
    Intelligent trunkering som bevarar frontmatter + head + tail.
    """
    if len(content) <= max_length:
        return content, False

    frontmatter = ""
    body = content

    if content.startswith('---'):
        end_idx = content.find('\n---', 3)
        if end_idx != -1:
            frontmatter = content[:end_idx + 4]
            body = content[end_idx + 4:].lstrip('\n')

    available = max_length - len(frontmatter) - 100

    if available <= 0:
        return content[:max_length] + "\n\n... [TRUNKERAD]", True

    tail_size = int(available * tail_ratio)
    head_size = available - tail_size

    body_head = body[:head_size].rstrip()
    body_tail = body[-tail_size:].lstrip() if tail_size > 0 else ""

    # Klipp vid radbrytning för renare output
    if len(body_head) > 200:
        last_break = body_head.rfind('\n', len(body_head) - 200)
        if last_break > len(body_head) * 0.7:
            body_head = body_head[:last_break]

    if len(body_tail) > 200:
        first_break = body_tail.find('\n', 0, 200)
        if first_break > 0:
            body_tail = body_tail[first_break + 1:]

    omitted = len(body) - len(body_head) - len(body_tail)
    truncated = f"{frontmatter}\n{body_head}\n\n[... {omitted:,} tecken utelämnade ...]\n\n{body_tail}"

    return truncated, True


@mcp.tool()
def read_document_content(doc_id: str, max_length: int = 8000, section: str = "smart") -> str:
    """
    Läser dokumentinnehåll från Lake med intelligent trunkering.

    Bevarar:
    - YAML frontmatter (alltid komplett)
    - Dokumentets början (~80%)
    - Dokumentets slut (~20%) - fångar org.nr, signaturer, fotnoter

    Args:
        doc_id: Dokumentets UUID eller filnamn
        max_length: Max antal tecken (default 8000)
        section: Läsläge:
            - "smart": Head + tail med bevarad frontmatter (rekommenderat)
            - "head": Endast första max_length tecken
            - "tail": Endast sista max_length tecken
            - "full": Ingen trunkering (varning: kan bli stort)
    """
    try:
        # Hitta filen
        filepath = None

        # Försök hitta via UUID i filnamn
        if os.path.exists(LAKE_PATH):
            for f in os.listdir(LAKE_PATH):
                if f.endswith('.md') and doc_id in f:
                    filepath = os.path.join(LAKE_PATH, f)
                    break

        # Fallback: sök i frontmatter
        if not filepath and os.path.exists(LAKE_PATH):
            for f in os.listdir(LAKE_PATH):
                if f.endswith('.md'):
                    full_path = os.path.join(LAKE_PATH, f)
                    fm = _parse_frontmatter(full_path)
                    if fm.get('uuid') == doc_id:
                        filepath = full_path
                        break

        if not filepath:
            return f"DOKUMENT EJ HITTAT: Kunde inte hitta fil för ID '{doc_id}' i Lake."

        # Läs filen
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            return f"LÄSFEL: Kunde inte läsa {filepath}: {e}"

        # Applicera trunkering
        filename = os.path.basename(filepath)
        full_length = len(content)
        header = f"=== DOKUMENT: {filename} ({full_length:,} tecken) ==="

        if section == "full" or full_length <= max_length:
            return f"{header}\n\n{content}"

        elif section == "head":
            truncated = content[:max_length].rstrip()
            return f"{header}\n[LÄGE: head - visar första {max_length:,} tecken]\n\n{truncated}\n\n... [TRUNKERAD - {full_length - max_length:,} tecken kvar]"

        elif section == "tail":
            truncated = content[-max_length:].lstrip()
            return f"{header}\n[LÄGE: tail - visar sista {max_length:,} tecken]\n\n[TRUNKERAD - {full_length - max_length:,} tecken före detta ...]\n\n{truncated}"

        else:  # "smart"
            truncated, was_truncated = _smart_truncate(content, max_length)
            if was_truncated:
                return f"{header}\n[LÄGE: smart - frontmatter + head + tail bevarade]\n\n{truncated}"
            return f"{header}\n\n{truncated}"

    except Exception as e:
        logging.error(f"read_document_content: Fel för {doc_id}: {e}")
        return f"Dokumentläsning misslyckades: {e}"


if __name__ == "__main__":
    try:
        mcp.run()
    except Exception as e:
        logging.critical(f"Server Crash: {e}")
        sys.exit(1)