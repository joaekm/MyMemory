"""
Kronologen - Temporal Domain Agent (v8.3 Bygglaget)

Extraherar tidsrelaterade KnowledgeFragments från dokument.
Fokuserar på händelser, datum, deadlines, tidslinjer.

Princip: HARDFAIL > Silent Fallback
"""

import os
import yaml
import logging
from datetime import datetime
from typing import List, Dict
from google import genai

LOGGER = logging.getLogger('Chronologist')


# --- CONFIG LOADER ---
def _load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml'),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f:
                return yaml.safe_load(f)
    raise FileNotFoundError("HARDFAIL: Config not found")


def _load_agent_prompts():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', '..', 'config', 'agent_prompts.yaml'),
        os.path.join(script_dir, '..', 'config', 'agent_prompts.yaml'),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
    raise FileNotFoundError("HARDFAIL: agent_prompts.yaml not found")


CONFIG = _load_config()
API_KEY = CONFIG['ai_engine']['api_key']
MODEL_LITE = CONFIG['ai_engine']['models']['model_lite']


# Import KnowledgeFragment from planner
try:
    from services.pipeline.planner import KnowledgeFragment
except ImportError as e:
    LOGGER.debug(f"Fallback import KnowledgeFragment: {e}")
    from planner import KnowledgeFragment

# Import JSON parser
try:
    from services.utils.json_parser import parse_llm_json
except ImportError as e:
    LOGGER.debug(f"Fallback import json_parser: {e}")
    from utils.json_parser import parse_llm_json


def _format_docs_for_prompt(docs: List[Dict]) -> str:
    """Formatera dokument för LLM-prompt."""
    lines = []
    for i, doc in enumerate(docs[:10]):  # Max 10 dokument
        doc_id = doc.get('id', f'doc_{i}')
        filename = doc.get('filename', 'unknown')
        timestamp = doc.get('timestamp', '')
        summary = doc.get('summary', doc.get('content', '')[:500])
        
        lines.append(f"[{doc_id}] {filename}")
        if timestamp:
            lines.append(f"  Datum: {timestamp}")
        lines.append(f"  Sammanfattning: {summary[:300]}...")
        lines.append("")
    
    return "\n".join(lines)


def extract_temporal(
    docs: List[Dict],
    anchor_date: str = None,
    time_filter: Dict = None
) -> List['KnowledgeFragment']:
    """
    Extrahera tidsrelaterade bitar från dokument.
    
    Args:
        docs: Lista med dokument (id, filename, timestamp, summary/content)
        anchor_date: Referensdatum för relativa uttryck (default: idag)
        time_filter: Optional filter {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
    
    Returns:
        Lista med KnowledgeFragment av typ "temporal"
    """
    if not docs:
        LOGGER.warning("Chronologist: Inga dokument att analysera")
        return []
    
    if anchor_date is None:
        anchor_date = datetime.now().strftime("%Y-%m-%d")
    
    # Ladda prompt
    try:
        PROMPTS = _load_agent_prompts()
    except FileNotFoundError:
        LOGGER.error("HARDFAIL: agent_prompts.yaml saknas")
        return []
    
    chrono_prompt = PROMPTS.get('chronologist', {}).get('instruction', '')
    if not chrono_prompt:
        LOGGER.error("HARDFAIL: chronologist.instruction saknas i agent_prompts.yaml")
        return []
    
    # Formatera dokument
    docs_formatted = _format_docs_for_prompt(docs)
    
    # Bygg prompt
    full_prompt = chrono_prompt.format(
        documents=docs_formatted,
        anchor_date=anchor_date,
        time_filter=str(time_filter) if time_filter else "Inget filter"
    )
    
    LOGGER.debug(f"Chronologist: Analyserar {len(docs)} dokument med ankare {anchor_date}")
    
    # Anropa LLM
    try:
        client = genai.Client(api_key=API_KEY)
        response = client.models.generate_content(
            model=MODEL_LITE,
            contents=full_prompt
        )
        
        if not response.text:
            LOGGER.warning("Chronologist: Tom respons från LLM")
            return []
        
        # Parsa JSON-svar
        result = parse_llm_json(response.text, context="chronologist")
        
        # Extrahera events och konvertera till KnowledgeFragments
        events = result.get('events', [])
        fragments = []
        
        for event in events:
            fragment = KnowledgeFragment(
                content=f"{event.get('date', 'Okänt datum')}: {event.get('event', 'Okänd händelse')}",
                source_doc_id=event.get('source', 'unknown'),
                fragment_type="temporal",
                confidence=event.get('confidence', 0.8),
                metadata={
                    "date": event.get('date'),
                    "event_type": event.get('type', 'event'),
                    "extracted_by": "chronologist"
                }
            )
            fragments.append(fragment)
        
        LOGGER.info(f"Chronologist: Extraherade {len(fragments)} temporal fragments")
        return fragments
        
    except Exception as e:
        LOGGER.error(f"Chronologist: Fel vid LLM-anrop: {e}")
        return []


# --- TEST ---
if __name__ == "__main__":
    print("=== Test Chronologist ===")
    
    test_docs = [
        {
            "id": "test_1",
            "filename": "mote_2025-12-10.md",
            "timestamp": "2025-12-10",
            "summary": "Möte med Adda om AI PoC. Deadline satt till 15 december."
        },
        {
            "id": "test_2",
            "filename": "slack_2025-12-12.md",
            "timestamp": "2025-12-12",
            "summary": "Diskussion om budgeten. Beslut att öka till 600k."
        }
    ]
    
    fragments = extract_temporal(test_docs, anchor_date="2025-12-16")
    
    print(f"\nExtraherade {len(fragments)} fragments:")
    for f in fragments:
        print(f"  - {f.content} (source: {f.source_doc_id}, conf: {f.confidence})")
    
    print("\n✓ Chronologist test OK")

