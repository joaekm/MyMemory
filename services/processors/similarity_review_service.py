"""
Similarity Review Service - Semantisk likhetsanalys och review-förslag.

Hanterar semantisk likhetsanalys med LLM och genererar review-förslag för användargranskning.
"""

import os
import sys
import json
import logging
from typing import Optional
from google import genai
from google.genai import types

# Lägg till projektroten i sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.utils.graph_service import GraphStore
from services.utils.json_parser import parse_llm_json

# --- CONFIG & PROMPTS LOADER ---
def _load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths_to_check = [
        os.path.join(script_dir, '..', '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, 'config', 'my_mem_config.yaml'),
    ]
    import yaml
    for p in paths_to_check:
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            for k, v in config['paths'].items():
                config['paths'][k] = os.path.expanduser(v)
            return config
    raise RuntimeError("HARDFAIL: Config not found")

def _load_prompts():
    """Ladda prompts från services_prompts.yaml"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths_to_check = [
        os.path.join(script_dir, '..', '..', 'config', 'services_prompts.yaml'),
        os.path.join(script_dir, '..', 'config', 'services_prompts.yaml'),
        os.path.join(script_dir, 'config', 'services_prompts.yaml'),
    ]
    import yaml
    for p in paths_to_check:
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
    raise RuntimeError("HARDFAIL: services_prompts.yaml not found")

CONFIG = _load_config()
PROMPTS = _load_prompts()
API_KEY = CONFIG.get('ai_engine', {}).get('api_key', '')
MODEL_FAST = CONFIG.get('ai_engine', {}).get('models', {}).get('model_fast', 'models/gemini-flash-latest')
GRAPH_PATH = os.path.expanduser(CONFIG['paths']['graph_db'])

# --- AI SETUP ---
AI_CLIENT = genai.Client(api_key=API_KEY) if API_KEY else None

# --- LOGGING ---
LOGGER = logging.getLogger('SIMILARITY_REVIEW')


def _calculate_similarity(entity_name: str, master_node: str, graph: GraphStore) -> dict:
    """
    Beräkna semantisk likhetsgrad mot approved_references med LLM.
    
    Använder LLM för att upptäcka semantiska avvikelser (katter bland hermeliner)
    som enkel string-similarity inte kan fånga.
    
    Args:
        entity_name: Entitetens namn att jämföra
        master_node: Masternodens namn
        graph: GraphStore-instans
        
    Returns:
        Dict med similarity_score, closest_match, matches_rejected, suggested_action
    """
    if not AI_CLIENT:
        LOGGER.error("HARDFAIL: AI_CLIENT inte initierad")
        return {
            "similarity_score": 0.0,
            "closest_match": None,
            "matches_rejected": False,
            "suggested_action": "REVIEW"
        }
    
    try:
        context = graph.get_extraction_context(master_node)
        approved_references = context.get("approved_references", [])
        rejected_examples = context.get("rejected_examples", [])
        
        # Ta de 15-20 bästa referenserna
        top_references = approved_references[:20]
        
        if not top_references:
            # Inga referenser - alltid REVIEW
            return {
                "similarity_score": 0.0,
                "closest_match": None,
                "matches_rejected": False,
                "suggested_action": "REVIEW"
            }
        
        # Bygg prompt för semantisk jämförelse
        reference_names = [ref.get("entity_name", "") for ref in top_references if ref.get("entity_name")]
        rejected_names = [rej.get("entity_name", "") for rej in rejected_examples if rej.get("entity_name")]
        
        # Ladda prompt från config
        prompt_template = PROMPTS.get('similarity_review', {}).get('similarity_analysis_prompt', '')
        if not prompt_template:
            LOGGER.error("HARDFAIL: similarity_review.similarity_analysis_prompt saknas i config")
            raise RuntimeError("HARDFAIL: similarity_review.similarity_analysis_prompt saknas i config")
        
        # Formatera prompt med dynamiska värden
        prompt = prompt_template.format(
            master_node=master_node,
            entity_name=entity_name,
            approved_references=json.dumps(reference_names, ensure_ascii=False, indent=2),
            rejected_examples=json.dumps(rejected_names, ensure_ascii=False, indent=2)
        )

        # Anropa LLM
        response = AI_CLIENT.models.generate_content(
            model=MODEL_FAST,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,  # Låg temperatur för konsistent bedömning
                max_output_tokens=500
            )
        )
        
        result_text = response.text.strip()
        result = parse_llm_json(result_text)
        
        if not result:
            LOGGER.warning(f"Kunde inte parsa LLM-svar för {entity_name}")
            return {
                "similarity_score": 0.0,
                "closest_match": None,
                "matches_rejected": False,
                "suggested_action": "REVIEW"
            }
        
        # Validera och normalisera resultat
        similarity_score = float(result.get("similarity_score", 0.0))
        similarity_score = max(0.0, min(1.0, similarity_score))  # Clamp till 0.0-1.0
        
        closest_match = result.get("closest_match")
        if closest_match and isinstance(closest_match, dict):
            closest_match["similarity"] = float(closest_match.get("similarity", similarity_score))
        elif similarity_score >= 0.5 and reference_names:
            # Fallback: använd första referensen om LLM inte gav closest_match
            closest_match = {
                "entity_name": reference_names[0],
                "similarity": similarity_score
            }
        
        matches_rejected = bool(result.get("matches_rejected", False))
        suggested_action = result.get("suggested_action", "REVIEW")
        if suggested_action not in ["APPROVE", "REVIEW", "REJECT"]:
            suggested_action = "REVIEW"
        
        reason = result.get("reason", "")
        
        return {
            "similarity_score": similarity_score,
            "closest_match": closest_match,
            "matches_rejected": matches_rejected,
            "suggested_action": suggested_action,
            "reason": reason
        }
        
    except Exception as e:
        LOGGER.error(f"HARDFAIL: Kunde inte beräkna semantisk similarity för {entity_name} i {master_node}: {e}")
        return {
            "similarity_score": 0.0,
            "closest_match": None,
            "matches_rejected": False,
            "suggested_action": "REVIEW"
        }

