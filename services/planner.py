"""
Planner - Pipeline v6.0 Fas 3

Ansvar:
- Ta emot kandidater från ContextBuilder (50 st med metadata+summary)
- Steg 1: Välja 10-15 mest relevanta baserat på summaries
- Steg 2: Läsa fulltext för valda dokument
- Steg 3: Skapa kurerad rapport för Synthesizer
"""

import os
import json
import yaml
import logging
from google import genai

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
                config = yaml.safe_load(f)
            return config
    raise FileNotFoundError("Config not found")

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
    raise FileNotFoundError("Prompts not found")

CONFIG = _load_config()
PROMPTS = _load_prompts()
LOGGER = logging.getLogger('Planner')

API_KEY = CONFIG['ai_engine']['api_key']
MODEL_LITE = CONFIG['ai_engine']['models']['model_lite']
MODEL_PRO = CONFIG['ai_engine']['models']['model_pro']  # För rapportgenerering (större kontext)

# AI Client (lazy init)
_AI_CLIENT = None

def _get_ai_client():
    global _AI_CLIENT
    if _AI_CLIENT is None:
        _AI_CLIENT = genai.Client(api_key=API_KEY)
    return _AI_CLIENT


def _select_documents(query: str, intent: str, candidates: list) -> list:
    """
    Steg 1: Välj de mest relevanta dokumenten baserat på summaries.
    
    Args:
        query: Användarens fråga
        intent: STRICT eller RELAXED
        candidates: Lista med kandidater (id, filename, summary, source, score)
    
    Returns:
        Lista med valda doc_ids
    """
    prompt_template = PROMPTS.get('planner_select', {}).get('instruction', '')
    
    if not prompt_template:
        LOGGER.error("HARDFAIL: planner_select prompt saknas i chat_prompts.yaml")
        raise ValueError("HARDFAIL: planner_select prompt saknas i chat_prompts.yaml")
    
    # Formatera kandidater för prompten
    candidates_text = ""
    for i, c in enumerate(candidates):
        candidates_text += f"{i+1}. [{c['id']}] {c['filename']} ({c['source']}, score={c['score']})\n"
        candidates_text += f"   Sammanfattning: {c['summary'][:150]}...\n\n"
    
    full_prompt = prompt_template.format(
        query=query,
        intent=intent,
        candidates=candidates_text
    )
    
    try:
        client = _get_ai_client()
        response = client.models.generate_content(
            model=MODEL_LITE,
            contents=full_prompt
        )
        
        text = response.text.replace("```json", "").replace("```", "").strip()
        LOGGER.debug(f"Planner selection LLM-svar: {text[:500]}...")
        result = json.loads(text)
        
        selected_ids = result.get('selected_ids', [])
        
        # Validera att alla IDs finns i kandidaterna
        valid_ids = {c['id'] for c in candidates}
        selected_ids = [id for id in selected_ids if id in valid_ids]
        
        LOGGER.info(f"Planner valde {len(selected_ids)} dokument")
        return selected_ids, text  # Returnera även rå LLM-text
        
    except json.JSONDecodeError as e:
        LOGGER.error(f"HARDFAIL: Planner selection JSON parse error: {e}")
        LOGGER.error(f"Rå LLM-respons: {text}")
        raise ValueError(f"HARDFAIL: Planner selection kunde inte parsa AI-svar: {e}") from e
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Planner selection misslyckades: {e}")
        raise RuntimeError(f"HARDFAIL: Planner selection misslyckades: {e}") from e


def _create_report(query: str, intent: str, documents: list) -> dict:
    """
    Steg 3: Skapa kurerad rapport från valda dokument.
    
    Args:
        query: Användarens fråga
        intent: STRICT eller RELAXED
        documents: Lista med fullständiga dokument (id, filename, content)
    
    Returns:
        dict med report, sources_used, gaps, confidence
    """
    prompt_template = PROMPTS.get('planner_report', {}).get('instruction', '')
    
    if not prompt_template:
        LOGGER.error("HARDFAIL: planner_report prompt saknas")
        return {
            "status": "ERROR",
            "reason": "planner_report prompt saknas",
            "report": "",
            "sources_used": [],
            "gaps": ["Kunde inte skapa rapport pga saknad prompt"],
            "confidence": 0.0
        }
    
    # Formatera dokument för prompten
    # MODEL_PRO har ~2M kontext - använd 15000 tecken per doc för detaljerad extraktion
    docs_text = ""
    for doc in documents:
        docs_text += f"=== DOKUMENT: {doc['filename']} ===\n"
        docs_text += f"{doc['content'][:15000]}\n\n"
    
    full_prompt = prompt_template.format(
        query=query,
        intent=intent,
        documents=docs_text
    )
    
    try:
        client = _get_ai_client()
        # Använd MODEL_PRO för bättre extraktion av specifika fakta
        response = client.models.generate_content(
            model=MODEL_PRO,
            contents=full_prompt
        )
        
        text = response.text.replace("```json", "").replace("```", "").strip()
        LOGGER.debug(f"Planner report LLM-svar: {text[:500]}...")
        result = json.loads(text)
        
        return {
            "status": "OK",
            "report": result.get('report', ''),
            "sources_used": result.get('sources_used', []),
            "gaps": result.get('gaps', []),
            "confidence": result.get('confidence', 0.5),
            "llm_raw": text  # Rå LLM-text för debug
        }
        
    except json.JSONDecodeError as e:
        LOGGER.error(f"HARDFAIL: Planner report JSON parse error: {e}")
        LOGGER.error(f"Rå LLM-respons: {text}")
        return {
            "status": "ERROR",
            "reason": f"JSON parse error: {e}",
            "report": "",
            "sources_used": [],
            "gaps": ["Kunde inte parsa AI-svar"],
            "confidence": 0.0
        }
        
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Planner report error: {e}")
        return {
            "status": "ERROR",
            "reason": str(e),
            "report": "",
            "sources_used": [],
            "gaps": ["Fel vid rapportgenerering"],
            "confidence": 0.0
        }


def create_report(context_result: dict, intent_data: dict, debug_trace: dict = None) -> dict:
    """
    Huvudfunktion: Skapa rapport från ContextBuilder-resultat.
    
    Args:
        context_result: Output från ContextBuilder med candidates och candidates_full
        intent_data: Output från IntentRouter
        debug_trace: Dict för att samla debug-info (optional)
    
    Returns:
        dict med:
            - status: "OK" eller "ERROR"
            - report: Markdown-rapport för Synthesizer
            - sources_used: Lista med använda filnamn
            - gaps: Lista med identifierade luckor
            - confidence: 0.0-1.0
    """
    query = intent_data.get('vector_query', '')  # Använd vector_query som fråga
    intent = intent_data.get('intent', 'RELAXED')
    candidates = context_result.get('candidates', [])
    candidates_full = context_result.get('candidates_full', [])
    
    if not candidates:
        return {
            "status": "NO_CANDIDATES",
            "reason": "Inga kandidater från ContextBuilder",
            "report": "",
            "sources_used": [],
            "gaps": ["Inga dokument hittades"],
            "confidence": 0.0
        }
    
    # Steg 1: Välj dokument baserat på summaries
    selected_ids, selection_llm_raw = _select_documents(query, intent, candidates)
    
    if debug_trace is not None:
        debug_trace['planner_selected_ids'] = selected_ids
        debug_trace['planner_selection_llm_raw'] = selection_llm_raw
    
    # Steg 2: Hämta fulltext för valda dokument
    # Skapa lookup från candidates_full
    full_lookup = {c['id']: c for c in candidates_full}
    selected_docs = []
    for doc_id in selected_ids:
        if doc_id in full_lookup:
            selected_docs.append(full_lookup[doc_id])
    
    if not selected_docs:
        LOGGER.warning("Inga dokument kunde hämtas för valda IDs")
        return {
            "status": "ERROR",
            "reason": "Kunde inte hämta fulltext för valda dokument",
            "report": "",
            "sources_used": [],
            "gaps": ["Dokument saknas"],
            "confidence": 0.0
        }
    
    LOGGER.info(f"Hämtade fulltext för {len(selected_docs)} dokument")
    
    # Steg 3: Skapa rapport
    result = _create_report(query, intent, selected_docs)
    
    if debug_trace is not None:
        debug_trace['planner_report'] = {
            "status": result.get('status'),
            "sources_used": result.get('sources_used', []),
            "gaps": result.get('gaps', []),
            "confidence": result.get('confidence', 0),
            "report_length": len(result.get('report', ''))
        }
        debug_trace['planner_report_llm_raw'] = result.get('llm_raw')
    
    return result


# --- TEST ---
if __name__ == "__main__":
    print("Planner modul laddad.")
    print("Kräver ContextBuilder-output för att testa.")

