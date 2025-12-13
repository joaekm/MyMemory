"""
JSON Parser - Robust parsing av LLM-svar som ska vara JSON.

Hanterar vanliga LLM-quirks:
- Markdown code fences (```json ... ```)
- Prefix-text ("SVAR:", "Här kommer svaret:")
- Trailing commas
- Whitespace-problem
"""

import re
import json
import logging

LOGGER = logging.getLogger('JsonParser')


def parse_llm_json(text: str, context: str = "unknown") -> dict:
    """
    Parsa JSON från LLM-svar, även om det finns text före/efter.
    
    Args:
        text: Rå LLM-respons
        context: Beskrivning för logging (t.ex. "planner_select")
    
    Returns:
        dict: Parsed JSON
    
    Raises:
        ValueError: Om JSON inte kan extraheras eller parsas
    """
    if not text or not text.strip():
        LOGGER.error(f"[{context}] Tom LLM-respons")
        raise ValueError(f"HARDFAIL: [{context}] LLM returnerade tom respons")
    
    original = text
    
    # Steg 1: Ta bort markdown code fences
    text = text.replace("```json", "").replace("```", "").strip()
    
    # Steg 2: Försök hitta JSON-block med regex
    # Matchar { ... } eller [ ... ] på yttersta nivån
    json_match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', text)
    
    if json_match:
        json_str = json_match.group(1)
    else:
        LOGGER.error(f"[{context}] Kunde inte hitta JSON-block i respons: {text[:200]}...")
        raise ValueError(f"HARDFAIL: [{context}] Inget JSON-block hittades i LLM-svar")
    
    # Steg 3: Fixa vanliga JSON-fel
    # Trailing commas
    json_str = re.sub(r',\s*}', '}', json_str)
    json_str = re.sub(r',\s*]', ']', json_str)
    
    # Steg 4: Parsa
    try:
        result = json.loads(json_str)
        LOGGER.debug(f"[{context}] JSON parsad OK")
        return result
    except json.JSONDecodeError as e:
        LOGGER.error(f"[{context}] JSON parse error: {e}")
        LOGGER.error(f"[{context}] Extraherad JSON: {json_str[:500]}...")
        LOGGER.error(f"[{context}] Original respons: {original[:500]}...")
        raise ValueError(f"HARDFAIL: [{context}] Kunde inte parsa JSON: {e}") from e


# --- TEST ---
if __name__ == "__main__":
    # Test 1: Ren JSON
    test1 = '{"status": "OK", "keywords": ["test"]}'
    print(f"Test 1: {parse_llm_json(test1, 'test1')}")
    
    # Test 2: JSON med markdown fences
    test2 = '```json\n{"status": "OK"}\n```'
    print(f"Test 2: {parse_llm_json(test2, 'test2')}")
    
    # Test 3: JSON med prefix-text
    test3 = 'SVAR:\n\n{"status": "OK", "data": "test"}'
    print(f"Test 3: {parse_llm_json(test3, 'test3')}")
    
    # Test 4: JSON med trailing comma
    test4 = '{"items": ["a", "b",], "count": 2,}'
    print(f"Test 4: {parse_llm_json(test4, 'test4')}")
    
    # Test 5: Lång analys före JSON
    test5 = '''Analys av frågan: Användaren söker information om möten.
    
    Baserat på dokumenten har jag identifierat följande:
    
    {"selected_ids": ["doc1", "doc2"], "reasoning": "Dessa dokument är relevanta"}'''
    print(f"Test 5: {parse_llm_json(test5, 'test5')}")
    
    print("Alla tester passerade!")

