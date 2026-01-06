import os
import sys
import yaml
import shutil

# Lägg till projektroten i sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# OBS: Importerar från ditt nya filnamn 'lake_service'
try:
    from services.utils.lake_service import LakeEditor
except ImportError:
    print("❌ CRITICAL: Kunde inte hitta services/utils/lake_service.py")
    sys.exit(1)

# Konfiguration
LAKE_DIR = os.path.expanduser("~/MyMemory/Lake")
TEST_FILE = os.path.join(LAKE_DIR, "hand_test_åäö.md")

def setup_test_file():
    """Skapar en dummy-fil att leka med."""
    os.makedirs(LAKE_DIR, exist_ok=True)
    
    # Notera: Vi skriver med 'ä' direkt för att simulera en korrekt fil
    frontmatter = {
        "unit_id": "test-123",
        "original_filename": "hand_test_åäö.txt",
        "status": "pending",
        "summary": "En fil om räksmörgåsar."
    }
    
    with open(TEST_FILE, 'w', encoding='utf-8') as f:
        f.write("---\n")
        yaml.dump(frontmatter, f, sort_keys=False, allow_unicode=True)
        f.write("---\n\n")
        f.write("# Rubrik\nDetta är brödtexten som INTE får röras.")
    
    print(f"📁 Skapade testfil: {TEST_FILE}")

def run_test():
    print("--- 🧪 STARTAR LAKE HANDS TEST ---")
    
    # 1. Setup
    setup_test_file()
    editor = LakeEditor()
    
    # 2. Läs-test
    print("\n[TEST 1] Läsa Metadata...")
    meta = editor.read_metadata(TEST_FILE)
    if meta.get("summary") == "En fil om räksmörgåsar.":
        print("✅ Läsning lyckades (Svenska tecken OK).")
    else:
        print(f"❌ Läsning misslyckades. Fick: {meta}")
        return

    # 3. Skriv-test (Uppdatering)
    print("\n[TEST 2] Uppdatera Metadata (Kirurgi)...")
    success = editor.update_metadata(TEST_FILE, {
        "status": "validated",
        "checked_by": "Dreamer"
    })
    
    if success:
        print("✅ Update-funktionen returnerade True.")
    else:
        print("❌ Update-funktionen misslyckades.")
        return

    # 4. Append-test
    print("\n[TEST 3] Lägga till nyckelord...")
    editor.append_keyword(TEST_FILE, "Testnyckelord")
    
    # 5. Verifiering av resultatet (Raw read)
    print("\n[VERIFIERING] Läser filen från disk...")
    with open(TEST_FILE, 'r', encoding='utf-8') as f:
        content = f.read()
        
    print("-" * 40)
    print(content)
    print("-" * 40)
    
    # Kontroller
    checks = []
    checks.append("status: validated" in content)
    checks.append("Testnyckelord" in content)
    checks.append("räksmörgåsar" in content) # Kollar att unicoden överlevde skrivningen
    checks.append("# Rubrik" in content)       # Kollar att vi inte raderade brödtexten
    
    if all(checks):
        print("\n🎉 SUCCÉ! Händerna fungerar perfekt.")
        print("   - Metadata uppdaterad.")
        print("   - Unicode (åäö) bevarad.")
        print("   - Brödtext orörd.")
    else:
        print("\n⚠️ Något gick fel. Kolla utskriften ovan.")

    # Cleanup (Valfritt, kommentera bort om du vill se filen)
    # os.remove(TEST_FILE)

if __name__ == "__main__":
    run_test()