#!/usr/bin/env python3
"""
verify_area_a_v2.py - Verifiering av Schema & Logic Switch (Område A - Hardened)

Verifierar acceptanskriterier enligt implementeringsplan 2.0:
1. Status-spärren (UNKNOWN nekas).
2. Metadata-kravet (Confidence och Last_seen_at är obligatoriska).
3. Healing-policy (Ska finnas i schemat och kunna läsas).
4. Policy Loading (Specifikt värde 90 dagar för Person).
"""

import os
import sys
import logging
from datetime import datetime

# Setup path för att hitta modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Tysta loggningen för renare output
logging.basicConfig(level=logging.CRITICAL)

try:
    # KORRIGERAD SÖKVÄG: services.utils.schema_validator
    from services.utils.schema_validator import SchemaValidator
except ImportError as e:
    print(f"❌ CRITICAL: Kunde inte importera SchemaValidator. Kontrollera sökvägarna: {e}")
    sys.exit(1)

def get_valid_base_payload():
    """Skapar en payload som uppfyller alla nya strikta krav."""
    return {
        "type": "Person",
        "id": "dummy-uuid-123",
        "name": "Test Person",
        "email": "test@example.com",
        "created_at": datetime.now().isoformat(),
        # NYA OBLIGATORISKA FÄLT:
        "last_seen_at": datetime.now().isoformat(),
        "confidence": 0.5,
        "status": "PROVISIONAL",
        "source_system": "TestRunner"
    }

def run_test(name, func):
    print(f"TEST: {name}...", end=" ")
    try:
        success, msg = func()
        if success:
            print(f"✅ PASS")
            return True
        else:
            print(f"❌ FAIL - {msg}")
            return False
    except Exception as e:
        print(f"❌ CRITICAL FAIL - {e}")
        # Skriv ut stacktrace vid behov för debugging
        import traceback
        traceback.print_exc()
        return False

# --- TESTFALL ---

def test_policy_loading_and_healing():
    """
    AC 3 & 4: Policy Loading Test & Healing-policy.
    Verifierar att healing_policy finns och att max_days_as_provisional == 90 för Person.
    """
    validator = SchemaValidator()
    
    # Hämta policy för Person
    policy = validator.get_healing_policy("Person")
    
    if not policy:
        return False, "Ingen healing_policy hittades för 'Person'."
    
    ttl = policy.get("max_days_as_provisional")
    if ttl != 90:
        return False, f"Fel TTL i policy. Förväntade 90, fick: {ttl}"
        
    return True, "Policy laddad korrekt."

def test_status_barrier():
    """
    AC 1: Status-spärren.
    Försök spara en nod med status="UNKNOWN". Ska ge SchemaValidationError (returnera False).
    """
    validator = SchemaValidator()
    payload = get_valid_base_payload()
    payload["status"] = "UNKNOWN" # Ogiltig status
    
    is_valid, error_msg = validator.validate_node(payload)
    
    if is_valid:
        return False, "Systemet släppte igenom status='UNKNOWN'."
    
    if "Invalid status" in str(error_msg):
        return True, ""
    
    return False, f"Nekades, men fel felmeddelande: {error_msg}"

def test_strict_field_validation_confidence():
    """
    AC 2 & 5: Metadata-kravet & Strict Field Validation.
    Försök validera en nod som saknar 'confidence'. Ska nekas.
    """
    validator = SchemaValidator()
    payload = get_valid_base_payload()
    del payload["confidence"] # Ta bort obligatoriskt fält
    
    is_valid, error_msg = validator.validate_node(payload)
    
    if is_valid:
        return False, "Systemet släppte igenom nod utan 'confidence'."
    
    if "Missing system field" in str(error_msg) and "confidence" in str(error_msg):
        return True, ""
        
    return False, f"Nekades, men fel felmeddelande: {error_msg}"

def test_strict_field_validation_last_seen():
    """
    AC 2: Metadata-kravet (Last Seen At).
    Försök validera en nod som saknar 'last_seen_at'. Ska nekas.
    """
    validator = SchemaValidator()
    payload = get_valid_base_payload()
    del payload["last_seen_at"] # Ta bort obligatoriskt fält
    
    is_valid, error_msg = validator.validate_node(payload)
    
    if is_valid:
        return False, "Systemet släppte igenom nod utan 'last_seen_at'."
    
    if "Missing system field" in str(error_msg) and "last_seen_at" in str(error_msg):
        return True, ""
        
    return False, f"Nekades, men fel felmeddelande: {error_msg}"

def test_happy_path_provisional():
    """
    Verifierar att en korrekt formaterad PROVISIONAL nod släpps igenom.
    """
    validator = SchemaValidator()
    payload = get_valid_base_payload()
    
    is_valid, error_msg = validator.validate_node(payload)
    
    if is_valid:
        return True, ""
    return False, f"Korrekt payload nekades: {error_msg}"

if __name__ == "__main__":
    print("\n=== VERIFIERING OMRÅDE A: SCHEMA 2.1 (HARDENED) ===\n")
    
    tests = [
        run_test("AC 3 & 4: Policy Loading & Healing Check (Person TTL=90)", test_policy_loading_and_healing),
        run_test("AC 1: Status-spärren (Reject UNKNOWN)", test_status_barrier),
        run_test("AC 2 & 5: Strict Validation (Reject missing Confidence)", test_strict_field_validation_confidence),
        run_test("AC 2: Strict Validation (Reject missing Last_seen_at)", test_strict_field_validation_last_seen),
        run_test("Happy Path: Valid Provisional Node", test_happy_path_provisional)
    ]
    
    print("\n" + "="*50)
    if all(tests):
        print("✅ ALLA ACCEPTANSKRITERIER FÖR OMRÅDE A UPPFYLLDA.")
        sys.exit(0)
    else:
        print("❌ TESTER MISSLYCKADES. Se detaljer ovan.")
        sys.exit(1)