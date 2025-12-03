"""
Consolidator - Drömprocessen för MyMemory (OBJEKT-48)

Ansvar:
- Läsa signaler från session_logger
- Identifiera mönster (zero-hit searches, frekventa termer)
- Generera "synapser" (alias-förslag, taxonomi-förslag)
- Applicera godkända synapser

Körs via: `python -m services.entity_consolidator` eller `--consolidate` flag
"""

import os
import json
import yaml
import logging
from datetime import datetime
from typing import Optional
from collections import Counter

# Loaders
try:
    from services.session_logger import get_unprocessed_sessions, mark_sessions_processed
    from services.entity_register import add_alias, get_known_entities
    from google import genai
except ImportError:
    from session_logger import get_unprocessed_sessions, mark_sessions_processed
    from entity_register import add_alias, get_known_entities
    from google import genai

LOGGER = logging.getLogger('Consolidator')

# --- CONFIG LOADER ---
def _load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, 'config', 'my_mem_config.yaml'),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f:
                return yaml.safe_load(f)
    return {}

def _load_prompts():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', 'config', 'chat_prompts.yaml'),
        os.path.join(script_dir, 'config', 'chat_prompts.yaml'),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
    return {}

CONFIG = _load_config()
PROMPTS = _load_prompts()

API_KEY = CONFIG.get('ai_engine', {}).get('api_key', '')
MODEL_LITE = CONFIG.get('ai_engine', {}).get('models', {}).get('model_lite', '')

# AI Client (lazy init)
_AI_CLIENT = None

def _get_ai_client():
    global _AI_CLIENT
    if _AI_CLIENT is None and API_KEY:
        _AI_CLIENT = genai.Client(api_key=API_KEY)
    return _AI_CLIENT


def analyze_signals(sessions: list) -> dict:
    """
    Analysera signaler och identifiera mönster.
    
    Args:
        sessions: Lista med sessioner från session_logger
    
    Returns:
        dict med identifierade mönster
    """
    zero_hit_keywords = Counter()
    frequent_keywords = Counter()
    aborts = []
    
    for session in sessions:
        for signal in session.get('signals', []):
            if signal.get('type') == 'search':
                keywords = signal.get('keywords', [])
                hits = signal.get('hits', 0)
                
                if hits == 0:
                    for kw in keywords:
                        zero_hit_keywords[kw] += 1
                else:
                    for kw in keywords:
                        frequent_keywords[kw] += 1
            
            elif signal.get('type') == 'abort':
                aborts.append({
                    'session_id': session.get('session_id'),
                    'rounds': signal.get('rounds'),
                    'reason': signal.get('reason')
                })
    
    return {
        'zero_hit_keywords': zero_hit_keywords.most_common(20),
        'frequent_keywords': frequent_keywords.most_common(20),
        'aborts': aborts,
        'total_sessions': len(sessions)
    }


def generate_synapse_suggestions(patterns: dict) -> list:
    """
    Generera synaps-förslag baserat på mönster.
    
    Args:
        patterns: Mönster från analyze_signals
    
    Returns:
        Lista med synaps-förslag
    """
    suggestions = []
    known = get_known_entities()
    known_names = set(known.get('persons', []) + known.get('projects', []))
    known_aliases = set(known.get('aliases', {}).keys())
    
    # Analysera zero-hit keywords
    for keyword, count in patterns.get('zero_hit_keywords', []):
        if count < 2:
            continue  # Behöver minst 2 träffar för att vara intressant
        
        # Kolla om det liknar ett känt namn (fuzzy match)
        for known_name in known_names:
            if _is_similar(keyword, known_name):
                # Föreslå alias
                suggestions.append({
                    'type': 'alias',
                    'canonical': known_name,
                    'alias': keyword,
                    'confidence': 0.7,
                    'reason': f"'{keyword}' söktes {count} gånger utan träff, liknar '{known_name}'"
                })
                break
    
    return suggestions


def _is_similar(a: str, b: str, threshold: float = 0.7) -> bool:
    """Enkel likhetskontroll (case-insensitive substring eller prefix)."""
    a_lower = a.lower()
    b_lower = b.lower()
    
    # Exakt match (case-insensitive)
    if a_lower == b_lower:
        return False  # Redan samma, inget alias behövs
    
    # Substring match
    if a_lower in b_lower or b_lower in a_lower:
        return True
    
    # Prefix match (minst 3 tecken)
    if len(a_lower) >= 3 and len(b_lower) >= 3:
        if a_lower[:3] == b_lower[:3]:
            return True
    
    return False


def apply_synapse(synapse: dict, auto_apply: bool = False) -> bool:
    """
    Applicera en synaps (alias, taxonomi-förslag, etc.)
    
    Args:
        synapse: Synaps-objekt
        auto_apply: Om True, applicera automatiskt utan bekräftelse
    
    Returns:
        True om applicerad, False annars
    """
    synapse_type = synapse.get('type')
    
    if synapse_type == 'alias':
        canonical = synapse.get('canonical')
        alias = synapse.get('alias')
        confidence = synapse.get('confidence', 0)
        
        # Kräv hög confidence för auto-apply
        if auto_apply and confidence < 0.8:
            LOGGER.info(f"Skippar låg-confidence alias: {alias} -> {canonical}")
            return False
        
        try:
            add_alias(canonical, alias, source="consolidator")
            LOGGER.info(f"Applicerade alias: {alias} -> {canonical}")
            return True
        except Exception as e:
            LOGGER.error(f"Kunde inte applicera alias: {e}")
            return False
    
    return False


def run_consolidation(auto_apply: bool = False, verbose: bool = True) -> dict:
    """
    Kör hela konsolideringsprocessen.
    
    Args:
        auto_apply: Om True, applicera synapser automatiskt
        verbose: Om True, skriv ut progress
    
    Returns:
        dict med resultat
    """
    if verbose:
        print("=== Consolidator (Dreaming) ===\n")
    
    # Steg 1: Hämta oprocessade sessioner
    sessions = get_unprocessed_sessions()
    
    if not sessions:
        if verbose:
            print("Inga oprocessade sessioner att analysera.")
        return {"status": "NO_SESSIONS", "processed": 0}
    
    if verbose:
        print(f"Hittade {len(sessions)} oprocessade sessioner.")
    
    # Steg 2: Analysera signaler
    patterns = analyze_signals(sessions)
    
    if verbose:
        print(f"\nMönster identifierade:")
        print(f"  - Zero-hit keywords: {len(patterns['zero_hit_keywords'])}")
        print(f"  - Frequent keywords: {len(patterns['frequent_keywords'])}")
        print(f"  - Avbrott: {len(patterns['aborts'])}")
    
    # Steg 3: Generera synaps-förslag
    suggestions = generate_synapse_suggestions(patterns)
    
    if verbose:
        print(f"\nSynaps-förslag: {len(suggestions)}")
        for s in suggestions:
            print(f"  - [{s['type']}] {s.get('alias', '')} -> {s.get('canonical', '')} (conf: {s.get('confidence', 0):.2f})")
    
    # Steg 4: Applicera synapser
    applied = 0
    if auto_apply:
        for synapse in suggestions:
            if apply_synapse(synapse, auto_apply=True):
                applied += 1
    
    # Steg 5: Markera sessioner som processade
    session_ids = [s.get('session_id') for s in sessions]
    mark_sessions_processed(session_ids)
    
    if verbose:
        print(f"\n✓ Processade {len(sessions)} sessioner.")
        if auto_apply:
            print(f"✓ Applicerade {applied} synapser.")
        else:
            print(f"  (Använd --apply för att applicera synapser automatiskt)")
    
    return {
        "status": "OK",
        "processed": len(sessions),
        "patterns": patterns,
        "suggestions": suggestions,
        "applied": applied
    }


# --- CLI ---
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Consolidator - Dreaming process")
    parser.add_argument("--apply", action="store_true", help="Applicera synapser automatiskt")
    parser.add_argument("--quiet", action="store_true", help="Tyst körning")
    
    args = parser.parse_args()
    
    result = run_consolidation(
        auto_apply=args.apply,
        verbose=not args.quiet
    )
    
    if result.get('status') != 'OK':
        print(f"\nStatus: {result.get('status')}")

