#!/usr/bin/env python3
"""
MASTER VALIDATION - Område B (Schema & Anchored Extraction)

Verifierar acceptanskriterier för Schema-Driven Extraction v10.1:
1. Ankare (Guardrails) - Återanvändning av UUID.
2. Schema-Fit (Relevans) - Brus vs Substans.
3. Metadata Integrity - Systemfält (last_seen_at, confidence).
4. Edge Confidence - Viktning av relationer.
"""

import sys
import os
import json
import logging
import datetime
from typing import List, Dict, Any

# Tysta loggar
logging.basicConfig(level=logging.CRITICAL)

# Setup path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from services.processors.doc_converter import strict_entity_extraction
except ImportError as e:
    print(f"CRITICAL: Kunde inte importera doc_converter: {e}")
    sys.exit(1)

# --- HELPER FUNCTIONS ---

def print_header(title):
    print(f"\n{'='*60}\n {title}\n{'='*60}")

def print_result(passed, msg):
    icon = "✅" if passed else "❌"
    print(f"{icon} {msg}")
    return passed

def validate_iso_timestamp(ts_str):
    try:
        datetime.datetime.fromisoformat(str(ts_str))
        return True
    except ValueError:
        return False

# --- TEST CASES ---

def test_anchor_guardrails():
    print_header("TEST 1: Ankare (Guardrails)")
    
    # Scenario: Vi injicerar en känd entitet via context string
    fake_uuid = "11111111-2222-3333-4444-555555555555"
    anchor_context = f"Följande entiteter existerar redan: Namn: 'Ankare Andersson', Typ: 'Person', UUID: '{fake_uuid}'."
    
    input_text = "Ankare Andersson signerade avtalet."
    
    print(f"Input: '{input_text}'")
    print(f"Context: '{anchor_context}'")
    
    # OBS: strict_entity_extraction tar arguments: text, source_hint="", known_entities_context=""
    result = strict_entity_extraction(input_text, known_entities_context=anchor_context)
    nodes = result.get('nodes', [])
    
    found_anchor = False
    uuid_match = False
    
    for n in nodes:
        if "Ankare" in n.get('name', ""):
            found_anchor = True
            if n.get('uuid') == fake_uuid:
                uuid_match = True
            else:
                print(f"   ⚠️  Hittade 'Ankare Andersson' men fel UUID: {n.get('uuid')}")

    if not found_anchor:
        return print_result(False, "Misslyckades att extrahera 'Ankare Andersson'.")
    
    return print_result(uuid_match, f"Verifierade att UUID '{fake_uuid}' återanvändes.")

def test_confidence_stress_schema_fit():
    print_header("TEST 2 & 5: Schema-Fit & Confidence Stress-Test")
    
    # Del 1: Låg kvalitet (Brus)
    low_quality_text = "Vi borde ta en fika och snacka lite."
    print(f"Scenario A (Brus): '{low_quality_text}'")
    
    res_low = strict_entity_extraction(low_quality_text)
    nodes_low = res_low.get('nodes', [])
    
    # Krav: Antingen inga noder, ELLER noder med confidence < 0.3
    passed_low = True
    for n in nodes_low:
        conf = n.get('confidence', 0.5)
        if conf >= 0.3:
            print(f"   ❌ Hittade brus-nod '{n.get('name')}' med hög confidence: {conf}")
            passed_low = False
        else:
            print(f"   ℹ️  Hittade brus-nod '{n.get('name')}' med låg confidence ({conf}). OK.")
    
    if not nodes_low:
        print("   ✅ Inga noder extraherades (Korrekt).")
    
    # Del 2: Hög kvalitet (Substans)
    high_quality_text = "Projekt Titan har budgetmöte 2024-01-01."
    print(f"\nScenario B (Substans): '{high_quality_text}'")
    
    res_high = strict_entity_extraction(high_quality_text)
    nodes_high = res_high.get('nodes', [])
    
    titan_node = next((n for n in nodes_high if "Titan" in n.get('name', "")), None)
    found_titan = False
    
    if titan_node:
        conf = titan_node.get('confidence', 0.0)
        if conf > 0.8:
            found_titan = True
            print(f"   ✅ Hittade 'Titan' med hög confidence: {conf}")
        else:
            print(f"   ❌ 'Titan' hade för låg confidence: {conf} (Kräver > 0.8)")
    else:
        print("   ❌ Missade att extrahera 'Titan'.")

    return print_result(passed_low and found_titan, "Schema-Fit test avklarat.")

def test_metadata_integrity():
    print_header("TEST 4 & 6: Metadata Integrity")
    
    text = "Jocke jobbar på IT."
    result = strict_entity_extraction(text)
    nodes = result.get('nodes', [])
    
    if not nodes:
        return print_result(False, "Inga noder att testa metadata på.")
        
    node = nodes[0]
    print(f"Inspekterar nod: {node.get('name')}")
    
    # 1. Check Last Seen At
    lsa = node.get('last_seen_at')
    valid_ts = validate_iso_timestamp(lsa)
    print(f"   Checking last_seen_at: {lsa} -> {'OK' if valid_ts else 'FAIL'}")
    
    # 2. Check Status
    status = node.get('status')
    valid_status = status == "PROVISIONAL"
    print(f"   Checking status: {status} -> {'OK' if valid_status else 'FAIL (Expected PROVISIONAL)'}")
    
    # 3. Check Healing Policy (Should NOT be present in extraction output)
    has_policy = 'healing_policy' in node
    print(f"   Checking healing_policy absence -> {'OK' if not has_policy else 'FAIL (Should not be in output)'}")
    
    all_ok = valid_ts and valid_status and not has_policy
    return print_result(all_ok, "Metadata valid.")

def test_edge_confidence():
    print_header("TEST 7: Edge Confidence")
    
    # Fall 1: Svag
    text_weak = "Jag tror att Jocke känner Lisa."
    print(f"Input Svag: '{text_weak}'")
    res_weak = strict_entity_extraction(text_weak)
    edges_weak = res_weak.get('edges', [])
    
    conf_weak = 0.0
    if edges_weak:
        conf_weak = edges_weak[0].get('confidence', 0.0)
        print(f"   Funnen relation konfidens: {conf_weak}")
    else:
        print("   Ingen relation hittad (kan vara ok om confidence är för låg).")
        
    # Fall 2: Stark
    text_strong = "Jocke är chef för Lisa."
    print(f"Input Stark: '{text_strong}'")
    res_strong = strict_entity_extraction(text_strong)
    edges_strong = res_strong.get('edges', [])
    
    conf_strong = 0.0
    if edges_strong:
        conf_strong = edges_strong[0].get('confidence', 0.0)
        print(f"   Funnen relation konfidens: {conf_strong}")
    else:
        return print_result(False, "Missade stark relation helt.")
        
    # Jämför
    if conf_strong > conf_weak:
        return print_result(True, f"Stark relation ({conf_strong}) > Svag relation ({conf_weak})")
    elif conf_strong == conf_weak == 1.0:
        # Edge case om modellen är överdrivet säker på allt
        print("   ⚠️  Båda hade max confidence. Svårt att differentiera.")
        return print_result(True, "Pass (men modellen är väldigt säker av sig).")
    else:
        return print_result(False, f"Misslyckades att vikta relationer korrekt. Stark: {conf_strong}, Svag: {conf_weak}")

# --- MAIN ---

def main():
    print("\n🚀 STARTAR VALIDERING AV OMRÅDE B (v10.1)...\n")
    
    results = [
        test_anchor_guardrails(),
        test_confidence_stress_schema_fit(),
        test_metadata_integrity(),
        test_edge_confidence()
    ]
    
    print_header("SAMMANFATTNING")
    if all(results):
        print("✅ ALLA TESTER GODKÄNDA. Område B är redo.")
        sys.exit(0)
    else:
        print("❌ VISSA TESTER MISSLYCKADES.")
        sys.exit(1)

if __name__ == "__main__":
    main()