#!/usr/bin/env python3
"""
validate_prompts.py - Deterministisk validering av promptar med LLM-fix

╔══════════════════════════════════════════════════════════════════════════════╗
║  DENNA FIL FÅR ALDRIG ÄNDRAS UTAN EXPLICIT TILLÅTELSE FRÅN ANVÄNDAREN!       ║
║  Validatorn är "lagen" - den som skriver promptarna får inte ändra reglerna. ║
╚══════════════════════════════════════════════════════════════════════════════╝

Validerar prompt-filer mot MyMemory-projektets regler:
- P4: Ingen AI-cringe (töntiga metafornamn)
- P7: Inga hårdkodade kategorier (taxonomi-noder i listor)
- HE: Inga hårdkodade entiteter (specifika namn på personer/projekt/org)
- OH: Overhead (upprepade instruktioner)
- FB: Fallback (hänvisningar till gammal arkitektur)
- RD: Redundant (promptar som inte används i kod)

Användning:
    python tools/validate_prompts.py                          # Validera alla
    python tools/validate_prompts.py config/chat_prompts.yaml # Validera specifik fil
    python tools/validate_prompts.py --fix                    # Validera och fixa med LLM
    python tools/validate_prompts.py --fix --dry-run          # Visa förslag utan att spara
"""

import os
import re
import sys
import glob
import yaml
import argparse
from pathlib import Path
from collections import Counter


# === CONFIG LOADER ===

def _load_config():
    """Ladda projektconfig för API-nyckel."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(script_dir, '..', 'config', 'my_mem_config.yaml'),
        os.path.join(script_dir, 'config', 'my_mem_config.yaml'),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f:
                return yaml.safe_load(f)
    return None


# === KONFIGURATION ===

# Princip 4: AI-cringe termer (töntiga metafornamn)
CRINGE_TERMS = [
    "Trädgårdsmästaren", "Trädgårdsmästare",
    "Bibliotekarien", "Bibliotekarie", 
    "Portvakten", "Portvakt",
    "Väktaren", "Väktare",
    "Budbäraren", "Budbärare",
    "Skrivaren", "Skrivare",
    "Läsaren", "Läsare",
    "Vägvisaren", "Vägvisare",
    "Arkivarien", "Arkivarie",
    "Trollkarlen", "Trollkarl",
    "Magikern", "Magiker",
]

# Princip 7: Taxonomins huvudnoder (för att detektera hårdkodade listor)
TAXONOMY_NODES = [
    "Händelser", "Projekt", "Administration", 
    "Person", "Aktör", "Teknologier", "Metodik", "Erbjudande",
    "Vision", "Affär", "Kultur", "Organisation", "Arbetsverktyg",
    "Process", "Marknad", "Juridik", "Förändring"
]

# Princip HE: Hårdkodade entiteter (specifika namn som borde vara generiska i exempel)
# Undantag: interrogator-promptar (simulerar specifik användare)
HARDCODED_ENTITIES = [
    # Personer
    "Cenk", "Joakim Ekman", "Susanne", "Tommy",
    # Organisationer  
    "Digitalist", "Läkarförbundet",
    # Projekt/Enheter
    "Adda", "Inköpslänken", "Almedalsveckan", "Drive",
]

# Promptar som får ha hårdkodade entiteter (t.ex. simulering av specifik användare)
HE_EXEMPT_PROMPTS = [
    "interrogator",
    "interrogator_check",
]

# Fallback: Legacy-termer som inte längre ska användas
LEGACY_TERMS = [
    # Gammal intent-klassificering (före v7.0)
    ("STRICT", "Gammal intent-klassificering, ersatt av mission_goal"),
    ("RELAXED", "Gammal intent-klassificering, ersatt av mission_goal"),
    ("intent_type", "Gammal intent-klassificering, ersatt av mission_goal"),
    
    # Före reranker (v7.5)
    ("Recency Mode", "Ersatt av automatisk reranking"),
    ("recency_mode", "Ersatt av automatisk reranking"),
    
    # Gammal entity-hantering
    ("entity_register", "Ersatt av graph_builder"),
    
    # Före facts list (efter v7.5 implementation)
    # ("working_findings", "Ersatt av facts list"),  # Aktivera efter implementation
]

# Overhead: Mönster som indikerar upprepning
OVERHEAD_MIN_LENGTH = 20  # Minsta längd för att räknas som "upprepad instruktion"
OVERHEAD_THRESHOLD = 2    # Antal gånger samma mening måste förekomma


# === VALIDERINGSFUNKTIONER ===

def check_ai_cringe(prompt_key: str, prompt_content: str, filepath: str) -> list:
    """
    P4: Leta efter töntiga AI-metafornamn.
    """
    violations = []
    
    for term in CRINGE_TERMS:
        if term.lower() in prompt_content.lower():
            # Hitta radnummer (ungefärligt)
            lines = prompt_content.split('\n')
            for i, line in enumerate(lines, 1):
                if term.lower() in line.lower():
                    violations.append({
                        "file": filepath,
                        "prompt": prompt_key,
                        "line": i,
                        "rule": "P4",
                        "message": f"AI-cringe term: '{term}'",
                        "code": line.strip()[:80]
                    })
                    break
    
    return violations


def check_hardcoded_taxonomy(prompt_key: str, prompt_content: str, filepath: str) -> list:
    """
    P7: Leta efter hårdkodade listor med taxonomi-noder.
    """
    violations = []
    lines = prompt_content.split('\n')
    
    for i, line in enumerate(lines, 1):
        # Räkna taxonomi-noder på raden
        found_nodes = [node for node in TAXONOMY_NODES if f'"{node}"' in line or f"'{node}'" in line]
        
        # Om 3+ noder på samma rad = troligen hårdkodad lista
        # UNDANTAG: validate_prompts.py själv (validatorn behöver känna till taxonomi-noder)
        if len(found_nodes) >= 3 and 'validate_prompts.py' not in filepath:
            violations.append({
                "file": filepath,
                "prompt": prompt_key,
                "line": i,
                "rule": "P7",
                "message": f"Hårdkodade kategorier: {found_nodes}",
                "code": line.strip()[:80]
            })
    
    return violations


def check_overhead(prompt_key: str, prompt_content: str, filepath: str) -> list:
    """
    OH: Leta efter upprepade instruktioner i samma prompt.
    """
    violations = []
    lines = prompt_content.split('\n')
    
    # Normalisera och räkna meningar
    sentences = []
    for line in lines:
        # Ta bort whitespace och gör lowercase för jämförelse
        normalized = ' '.join(line.strip().lower().split())
        if len(normalized) >= OVERHEAD_MIN_LENGTH:
            sentences.append((normalized, line.strip()))
    
    # Räkna förekomster
    counter = Counter([s[0] for s in sentences])
    
    for normalized, original in sentences:
        if counter[normalized] >= OVERHEAD_THRESHOLD:
            # Rapportera bara första förekomsten
            if normalized not in [v.get('_normalized') for v in violations]:
                violations.append({
                    "file": filepath,
                    "prompt": prompt_key,
                    "line": 0,  # Svårt att ange exakt rad
                    "rule": "OH",
                    "message": f"Upprepad instruktion ({counter[normalized]}x)",
                    "code": original[:60] + "...",
                    "_normalized": normalized  # Intern, för dedup
                })
    
    # Ta bort intern nyckel
    for v in violations:
        v.pop('_normalized', None)
    
    return violations


def check_hardcoded_entities(prompt_key: str, prompt_content: str, filepath: str) -> list:
    """
    HE: Leta efter hårdkodade entitetsnamn i promptar.
    Exempel bör använda generiska placeholders som <projekt>, <person>, X, Y.
    
    Undantag: Promptar i HE_EXEMPT_PROMPTS (t.ex. interrogator som simulerar användare)
    """
    # Undantag för simulerings-promptar
    if prompt_key in HE_EXEMPT_PROMPTS:
        return []
    
    violations = []
    lines = prompt_content.split('\n')
    
    for term in HARDCODED_ENTITIES:
        for i, line in enumerate(lines, 1):
            if term in line:
                violations.append({
                    "file": filepath,
                    "prompt": prompt_key,
                    "line": i,
                    "rule": "HE",
                    "message": f"Hårdkodad entitet: '{term}' - använd generisk placeholder",
                    "code": line.strip()[:80]
                })
                break  # En violation per term räcker
    
    return violations


def check_legacy_fallback(prompt_key: str, prompt_content: str, filepath: str) -> list:
    """
    FB: Leta efter hänvisningar till gammal arkitektur.
    """
    violations = []
    lines = prompt_content.split('\n')
    
    for term, reason in LEGACY_TERMS:
        for i, line in enumerate(lines, 1):
            # Case-insensitive för vissa termer
            if term in line or (term.lower() in line.lower() and term[0].isupper()):
                violations.append({
                    "file": filepath,
                    "prompt": prompt_key,
                    "line": i,
                    "rule": "FB",
                    "message": f"Legacy-term '{term}': {reason}",
                    "code": line.strip()[:80]
                })
                break  # En violation per term räcker
    
    return violations


def check_redundant_prompts(prompts: dict, filepath: str, project_root: str) -> list:
    """
    RD: Leta efter promptar som definieras men aldrig används i kod.
    Söker i både services/ och tools/
    """
    violations = []
    
    # Läs alla Python-filer i services/ och tools/
    all_code = ""
    search_dirs = [
        os.path.join(project_root, 'services'),
        os.path.join(project_root, 'tools'),
    ]
    
    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        for py_file in glob.glob(os.path.join(search_dir, "*.py")):
            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    all_code += f.read()
            except Exception as e:
                # HARDFAIL: Logga men fortsätt med nästa fil (validator ska inte krascha)
                import sys
                sys.stderr.write(f"HARDFAIL: Kunde inte läsa {py_file}: {e}\n")
                continue
    
    # Kolla varje prompt-nyckel
    for prompt_key in prompts.keys():
        # Mönster för att hitta användning: PROMPTS.get('key') eller PROMPTS['key']
        patterns = [
            f"PROMPTS.get('{prompt_key}'",
            f"PROMPTS.get(\"{prompt_key}\"",
            f"PROMPTS['{prompt_key}']",
            f"PROMPTS[\"{prompt_key}\"]",
            f"'{prompt_key}'",  # Mer generöst - nyckelnamnet nämns
            f"\"{prompt_key}\"",
        ]
        
        found = any(pattern in all_code for pattern in patterns)
        
        if not found:
            violations.append({
                "file": filepath,
                "prompt": prompt_key,
                "line": 0,
                "rule": "RD",
                "message": f"Prompt '{prompt_key}' verkar inte användas i kod",
                "code": "(Definerad men ej refererad i services/ eller tools/)"
            })
    
    return violations


# === LLM FIX ===

def fix_violations_with_llm(filepath: str, violations: list, dry_run: bool = False) -> bool:
    """
    Använd LLM för att fixa violations i en prompt-fil.
    
    Returns:
        True om filen uppdaterades, False annars
    """
    config = _load_config()
    if not config:
        print("❌ Kunde inte ladda config för LLM-fix")
        return False
    
    api_key = config.get('ai_engine', {}).get('api_key')
    if not api_key:
        print("❌ API-nyckel saknas i config")
        return False
    
    # Lazy import av google.genai
    try:
        from google import genai
    except ImportError as e:
        # HARDFAIL: Logga och returnera False (detta är intentional - saknad dependency)
        import sys
        sys.stderr.write(f"HARDFAIL: google-genai inte installerat: {e}\n")
        print("❌ google-genai inte installerat. Kör: pip install google-genai")
        return False
    
    # Läs original-filen
    with open(filepath, 'r', encoding='utf-8') as f:
        original_content = f.read()
    
    # Bygg fix-prompt
    violations_text = "\n".join([
        f"- [{v['rule']}] {v['prompt']}: {v['message']} (kod: {v['code']})"
        for v in violations
    ])
    
    fix_prompt = f"""Du är en expert på att förbättra AI-promptar.

UPPGIFT: Fixa följande violations i YAML-filen nedan.

VIOLATIONS ATT FIXA:
{violations_text}

REGLER:
- P4 (AI-cringe): Ersätt töntiga metafornamn med deskriptiva namn
- P7 (Hårdkodade kategorier): Ta bort hårdkodade listor av taxonomi-noder, använd {{taxonomy_context}} placeholder istället
- OH (Overhead): Ta bort upprepade instruktioner, behåll bara en instans
- FB (Legacy): Ta bort eller uppdatera hänvisningar till gammal arkitektur
- RD (Redundant): Markera med kommentar att prompten kan tas bort (ändra inte om osäker)

VIKTIGT:
- Returnera ENDAST den fixade YAML-filen, inget annat
- Behåll all övrig struktur och innehåll intakt
- Ändra så lite som möjligt för att fixa problemet

ORIGINAL FIL:
```yaml
{original_content}
```

FIXAD FIL (endast YAML, inga förklaringar):"""

    try:
        client = genai.Client(api_key=api_key)
        model = config.get('ai_engine', {}).get('models', {}).get('model_lite', 'gemini-flash')
        
        response = client.models.generate_content(
            model=model,
            contents=fix_prompt
        )
        
        fixed_content = response.text.strip()
        
        # Ta bort eventuella markdown code fences
        if fixed_content.startswith("```yaml"):
            fixed_content = fixed_content[7:]
        if fixed_content.startswith("```"):
            fixed_content = fixed_content[3:]
        if fixed_content.endswith("```"):
            fixed_content = fixed_content[:-3]
        fixed_content = fixed_content.strip()
        
        # Validera att det fortfarande är giltig YAML
        try:
            yaml.safe_load(fixed_content)
        except yaml.YAMLError as e:
            print(f"❌ LLM genererade ogiltig YAML: {e}")
            return False
        
        if dry_run:
            print("\n" + "=" * 60)
            print("DRY RUN - Föreslagna ändringar (sparas ej):")
            print("=" * 60)
            
            # Visa diff (enkel version)
            original_lines = original_content.split('\n')
            fixed_lines = fixed_content.split('\n')
            
            import difflib
            diff = difflib.unified_diff(
                original_lines, 
                fixed_lines, 
                fromfile='original', 
                tofile='fixed',
                lineterm=''
            )
            print('\n'.join(diff))
            return False
        
        # Spara fixad fil
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(fixed_content)
        
        print(f"✅ Fixade {len(violations)} violation(s) i {filepath}")
        return True
        
    except Exception as e:
        # HARDFAIL: Logga och returnera False (detta är intentional - LLM-fix är optional)
        import sys
        sys.stderr.write(f"HARDFAIL: LLM-fix misslyckades: {e}\n")
        print(f"❌ LLM-fix misslyckades: {e}")
        return False


# === HUVUDVALIDERING ===

def validate_prompt_file(filepath: str, project_root: str = None) -> list:
    """Validera en prompt-fil (YAML)."""
    violations = []
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            prompts = yaml.safe_load(f)
    except Exception as e:
        # HARDFAIL: Returnera violation istället för att krascha validatorn (detta är intentional)
        import sys
        sys.stderr.write(f"HARDFAIL: Kunde inte läsa YAML-fil {filepath}: {e}\n")
        return [{
            "file": filepath,
            "line": 0,
            "rule": "ERROR",
            "prompt": "",
            "message": f"Kunde inte läsa YAML-fil: {e}",
            "code": ""
        }]
        return [{
            "file": filepath,
            "prompt": "",
            "line": 0,
            "rule": "ERROR",
            "message": f"Kunde inte läsa/parsa fil: {e}",
            "code": ""
        }]
    
    if not isinstance(prompts, dict):
        return [{
            "file": filepath,
            "prompt": "",
            "line": 0,
            "rule": "ERROR",
            "message": "Filen är inte ett giltigt YAML-dict",
            "code": ""
        }]
    
    # Validera varje prompt
    for prompt_key, prompt_data in prompts.items():
        if not isinstance(prompt_data, dict):
            continue
        
        # Hämta instruction-texten
        instruction = prompt_data.get('instruction', '')
        role = prompt_data.get('role', '')
        full_content = f"{role}\n{instruction}"
        
        # Kör alla valideringar
        violations.extend(check_ai_cringe(prompt_key, full_content, filepath))
        violations.extend(check_hardcoded_taxonomy(prompt_key, full_content, filepath))
        violations.extend(check_hardcoded_entities(prompt_key, full_content, filepath))
        violations.extend(check_overhead(prompt_key, full_content, filepath))
        violations.extend(check_legacy_fallback(prompt_key, full_content, filepath))
    
    # Kolla redundanta promptar (behöver project_root)
    if project_root:
        violations.extend(check_redundant_prompts(prompts, filepath, project_root))
    
    return violations


def find_prompt_files(project_root: str) -> list:
    """Hitta alla prompt-filer i projektet."""
    prompt_files = []
    config_dir = os.path.join(project_root, 'config')
    
    if os.path.isdir(config_dir):
        for filename in os.listdir(config_dir):
            if filename.endswith('_prompts.yaml') or filename.endswith('_prompts.yml'):
                prompt_files.append(os.path.join(config_dir, filename))
    
    return prompt_files


def format_violations(violations: list) -> str:
    """Formatera violations för output."""
    if not violations:
        return "✅ Inga prompt-violations hittades!"
    
    output = []
    output.append(f"❌ {len(violations)} prompt-violation(s) hittades:\n")
    
    # Gruppera per fil och prompt
    by_file = {}
    for v in violations:
        key = (v['file'], v['prompt'])
        if key not in by_file:
            by_file[key] = []
        by_file[key].append(v)
    
    for (filepath, prompt_key), file_violations in by_file.items():
        if prompt_key:
            output.append(f"\n📄 {filepath} → {prompt_key}")
        else:
            output.append(f"\n📄 {filepath}")
        
        for v in file_violations:
            line_info = f"Rad {v['line']:4d}" if v['line'] > 0 else "        "
            output.append(f"   {line_info} [{v['rule']}]: {v['message']}")
            if v['code']:
                output.append(f"            → {v['code']}")
    
    return '\n'.join(output)


def parse_args():
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Validera prompt-filer mot MyMemory-regler"
    )
    parser.add_argument(
        'file', 
        nargs='?', 
        help='Prompt-fil att validera (default: alla i config/)'
    )
    parser.add_argument(
        '--fix', 
        action='store_true', 
        help='Använd LLM för att automatiskt fixa violations'
    )
    parser.add_argument(
        '--dry-run', 
        action='store_true', 
        help='Visa föreslagna ändringar utan att spara (kräver --fix)'
    )
    return parser.parse_args()


def main():
    """CLI entrypoint."""
    args = parse_args()
    
    # Hitta projektrot
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    
    if args.file:
        if os.path.isfile(args.file):
            prompt_files = [args.file]
        else:
            print(f"❌ Hittade inte: {args.file}")
            sys.exit(1)
    else:
        # Default: validera alla prompt-filer
        prompt_files = find_prompt_files(project_root)
        if not prompt_files:
            print("❌ Inga prompt-filer hittades i config/")
            sys.exit(1)
    
    # Validera alla filer
    all_violations = []
    violations_by_file = {}
    
    for filepath in prompt_files:
        violations = validate_prompt_file(filepath, project_root)
        all_violations.extend(violations)
        if violations:
            violations_by_file[filepath] = violations
    
    print(format_violations(all_violations))
    
    # Om --fix och det finns violations, försök fixa med LLM
    if args.fix and violations_by_file:
        print("\n" + "=" * 60)
        print("🔧 FIXING VIOLATIONS WITH LLM...")
        print("=" * 60)
        
        fixed_count = 0
        for filepath, violations in violations_by_file.items():
            # Filtrera bort RD (redundant) - de är osäkra att autofixa
            fixable = [v for v in violations if v['rule'] != 'RD']
            if fixable:
                if fix_violations_with_llm(filepath, fixable, dry_run=args.dry_run):
                    fixed_count += 1
        
        if not args.dry_run and fixed_count > 0:
            # Validera igen efter fix
            print("\n" + "=" * 60)
            print("🔄 RE-VALIDATING AFTER FIX...")
            print("=" * 60)
            
            all_violations = []
            for filepath in prompt_files:
                violations = validate_prompt_file(filepath, services_dir)
                all_violations.extend(violations)
            
            print(format_violations(all_violations))
    
    # Exit code: 0 om inga violations, 1 annars
    sys.exit(0 if len(all_violations) == 0 else 1)


if __name__ == "__main__":
    main()

