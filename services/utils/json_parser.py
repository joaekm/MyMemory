"""
JSON Parser - Robust parsing av LLM-svar som ska vara JSON.

Hanterar vanliga LLM-quirks:
- Markdown code fences (```json ... ```)
- Prefix-text ("SVAR:", "Här kommer svaret:")
- Trailing commas
- Whitespace-problem
"""

import json
import logging
import re

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
    
    # Steg 2: Fixa vanliga JSON-fel (trailing commas)
    text = re.sub(r',\s*}', '}', text)
    text = re.sub(r',\s*]', ']', text)
    
    # Steg 3: Hitta sista giltiga JSON-objekt med raw_decode
    # (Undviker greedy regex som fångar fel {..} vid blandad text)
    # Prioriterar objekt ({}) över arrayer ([]) - tar sista av varje typ
    decoder = json.JSONDecoder()
    last_object = None
    last_object_pos = -1
    last_array = None
    last_array_pos = -1
    
    for i, char in enumerate(text):
        if char == '{':
            try:
                obj, end = decoder.raw_decode(text, i)
                if isinstance(obj, dict):
                    last_object = obj
                    last_object_pos = i
            except json.JSONDecodeError:
                continue
        elif char == '[':
            try:
                obj, end = decoder.raw_decode(text, i)
                if isinstance(obj, list):
                    last_array = obj
                    last_array_pos = i
            except json.JSONDecodeError:
                continue
    
    # Prioritera objekt över arrayer (LLM-svar är oftast objekt)
    if last_object is not None:
        LOGGER.debug(f"[{context}] Hittade JSON-objekt vid position {last_object_pos}")
        return last_object
    if last_array is not None:
        LOGGER.debug(f"[{context}] Hittade JSON-array vid position {last_array_pos}")
        return last_array
    
    LOGGER.error(f"[{context}] Kunde inte hitta JSON-block i respons: {text[:200]}...")
    raise ValueError(f"HARDFAIL: [{context}] Inget JSON-block hittades i LLM-svar")


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
    
    # Test 6: Flera JSON-liknande strukturer - ska ta SISTA giltiga
    test6 = '''DOK 1 ([ID: abc-123]): {namn: "test"} - Kasta
DOK 2 ([ID: def-456]): Innehåller data - Kasta

Slutsats:

{"keep_ids": ["xyz-789"], "discard_ids": ["abc-123", "def-456"]}'''
    result6 = parse_llm_json(test6, 'test6')
    assert "keep_ids" in result6, "Test 6 borde hitta sista JSON med keep_ids"
    print(f"Test 6: {result6}")
    
    print("Alla tester passerade!")






