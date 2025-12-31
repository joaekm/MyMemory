#!/usr/bin/env python3
"""
verify_area_a.py - Verifiering av Schema & Logic Switch (Område A)

Detta skript testar:
1. Att det nya schemat laddas med fälten 'status' och 'distinguishing_context'.
2. Att Trusted Sources (Slack) får skapa VERIFIED noder.
3. Att Untrusted Sources (DocConverter) NEKAS skapa VERIFIED noder.
4. Att Untrusted Sources (DocConverter) TILLÅTS skapa PROVISIONAL noder.
5. Att ogiltiga statusar avvisas.
"""

import os
import sys
import logging
import datetime

# Setup path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Tysta loggningen för renare output
logging.basicConfig(level=logging.CRITICAL)

try:
    from services.utils.schema_validator import SchemaValidator
except ImportError as e:
    print(f"❌ CRITICAL: Kunde inte importera SchemaValidator: {e}")
    sys.exit(1)

# Hjälpfunktion för att skapa minimal giltig payload
def get_base_props():
    return {
        "id": "dummy-uuid",
        "created_at": datetime.datetime.now().isoformat(),
        "last_synced_at": datetime.datetime.now().isoformat()
    }

def run_test(name, func):
    print(f"TEST: {name}...", end=" ")
    try:
        success, msg = func()
        if success:
            print("✅ PASS")
            return True
        else:
            print(f"❌ FAIL - {msg}")
            return False
    except Exception as e:
        print(f"❌ CRITICAL FAIL - {e}")
        return False

def test_schema_loading():
    validator = SchemaValidator()
    # Kolla om status finns i base_properties
    base_props = validator.schema.get('base_properties', {}).get('properties', {})
    if 'status' not in base_props:
        return False, "Fältet 'status' saknas i base_properties i schemat."
    if 'distinguishing_context' not in base_props:
        return False, "Fältet 'distinguishing_context' saknas i schemat."
    return True, ""

def test_trusted_source_verified():
    """Slack ska få skapa VERIFIED."""
    validator = SchemaValidator()
    props = get_base_props()
    props.update({
        "name": "Trusted User",
        "email": "trusted@example.com",
        "status": "VERIFIED",
        "type": "INTERNAL" # Required for Person
    })
    is_valid, _, error = validator.validate_node("Person", props, "Slack")
    if is_valid:
        return True, ""
    return False, f"Slack borde fått skapa VERIFIED, men fick fel: {error}"

def test_untrusted_source_verified_block():
    """DocConverter ska NEKAS att skapa VERIFIED."""
    validator = SchemaValidator()
    props = get_base_props()
    props.update({
        "name": "Untrusted User",
        "email": "untrusted@example.com",
        "status": "VERIFIED",
        "type": "INTERNAL"
    })
    is_valid, _, error = validator.validate_node("Person", props, "DocConverter")
    
    # Vi förväntar oss False här!
    if is_valid:
        return False, "Säkerhetshål! DocConverter tilläts skapa VERIFIED nod."
    
    if "NOT allowed" in str(error) and "Must be PROVISIONAL" in str(error):
        return True, ""
    return False, f"Fick fel, men inte rätt felmeddelande: {error}"

def test_untrusted_source_provisional_allow():
    """DocConverter ska FÅ skapa PROVISIONAL."""
    validator = SchemaValidator()
    props = get_base_props()
    props.update({
        "name": "Provisional User",
        "email": "prov@example.com",
        "status": "PROVISIONAL",
        "type": "INTERNAL",
        "distinguishing_context": ["Dokument", "Analys"]
    })
    is_valid, _, error = validator.validate_node("Person", props, "DocConverter")
    if is_valid:
        return True, ""
    return False, f"DocConverter borde fått skapa PROVISIONAL, men fick fel: {error}"

def test_invalid_status_enum():
    """Status måste vara VERIFIED eller PROVISIONAL."""
    validator = SchemaValidator()
    props = get_base_props()
    props.update({
        "name": "Bad Status",
        "email": "bad@example.com",
        "status": "MAYBE", # Ogiltig
        "type": "INTERNAL"
    })
    is_valid, _, error = validator.validate_node("Person", props, "Slack")
    
    if is_valid:
        return False, "Validatorn släppte igenom ogiltig status 'MAYBE'."
    
    if "not in enum" in str(error):
        return True, ""
    return False, f"Fick fel, men inte för enum: {error}"

if __name__ == "__main__":
    print("=== VERIFIERING OMRÅDE A: SCHEMA & LOGIC SWITCH (FIXED) ===\n")
    
    results = [
        run_test("Ladda uppdaterat schema", test_schema_loading),
        run_test("Trusted Source (Slack) -> VERIFIED", test_trusted_source_verified),
        run_test("Untrusted Source (DocConverter) -> VERIFIED (Block)", test_untrusted_source_verified_block),
        run_test("Untrusted Source (DocConverter) -> PROVISIONAL (Allow)", test_untrusted_source_provisional_allow),
        run_test("Validera Status Enum", test_invalid_status_enum)
    ]
    
    print("\n" + "="*50)
    if all(results):
        print("✅ ALLA TESTER GODKÄNDA. Redo för Område B.")
        sys.exit(0)
    else:
        print("❌ VISSA TESTER MISSLYCKADES. Backa och fixa.")
        sys.exit(1)