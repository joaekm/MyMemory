#!/usr/bin/env python3
"""
validate_rules.py - Deterministisk validering av projektregler

╔══════════════════════════════════════════════════════════════════════════════╗
║  DENNA FIL FÅR ALDRIG ÄNDRAS UTAN EXPLICIT TILLÅTELSE FRÅN ANVÄNDAREN!       ║
║  Validatorn är "lagen" - den som skriver koden får inte ändra reglerna.      ║
╚══════════════════════════════════════════════════════════════════════════════╝

Validerar Python-kod mot MyMemory-projektets regler:
- Princip 2: HARDFAIL > Silent Fallback
- Princip 6: Inga hårdkodade promptar
- Princip 7: Taxonomin är Master (inga hårdkodade kategorier)
- Princip 8: Config är Sanning för Sökvägar
- Princip 9: Config-värden ska läsas från config (AI-modeller, API-nycklar, etc.)

Användning:
    python tools/validate_rules.py services/my_mem_chat.py
    python tools/validate_rules.py services/           # Alla .py i mappen
    python tools/validate_rules.py                     # Alla services/*.py
"""

import os
import re
import sys
from pathlib import Path


# === KÄNDA VÄRDEN ATT LETA EFTER ===

# Princip 7: Taxonomins huvudnoder (hårdkodade här för att validatorn ska vara självständig)
TAXONOMY_NODES = [
    "Händelser", "Projekt", "Administration", 
    "Person", "Aktör", "Teknologier", "Metodik", "Erbjudande",
    "Vision", "Affär", "Kultur", "Organisation", "Arbetsverktyg"
]

# Princip 8: Mönster för hårdkodade sökvägar
HARDCODED_PATH_PATTERNS = [
    r'["\']~/MyMemory/',           # ~/MyMemory/...
    r'["\']/Users/\w+/',           # /Users/username/...
    r'["\']/home/\w+/',            # /home/username/...
    r'expanduser\(["\']~/',        # expanduser("~/...) utan config
]

# Princip 9: Mönster för hårdkodade config-värden (ska läsas från config)
HARDCODED_CONFIG_PATTERNS = [
    # AI-modeller
    (r'["\']models/gemini-', "AI-modell (ska vara CONFIG['ai_engine']['models'])"),
    (r'["\']gemini-pro', "AI-modell (ska vara CONFIG['ai_engine']['models'])"),
    (r'["\']gemini-flash', "AI-modell (ska vara CONFIG['ai_engine']['models'])"),
    (r'["\']gpt-4', "AI-modell (ska vara CONFIG['ai_engine']['models'])"),
    (r'["\']gpt-3', "AI-modell (ska vara CONFIG['ai_engine']['models'])"),
    (r'["\']claude-', "AI-modell (ska vara CONFIG['ai_engine']['models'])"),
    # API-nycklar (Gemini börjar med AIza)
    (r'["\']AIza[A-Za-z0-9_-]{30,}', "API-nyckel (ska vara CONFIG['ai_engine']['api_key'])"),
    # Slack tokens
    (r'["\']xox[pbar]-', "Slack-token (ska vara CONFIG['slack'])"),
]


# === PRINCIP 1: HARDFAIL > Silent Fallback ===

def check_silent_fallbacks(filepath: str, content: str) -> list:
    """
    Leta efter tysta fallbacks som sväljer fel.
    
    Violations:
    - except: pass
    - except: continue
    - except SomeException: pass/continue
    - except: return [] / {} / None / ""
    - except: <tilldelning utan LOGGER>
    - except Exception: <kod utan loggning eller raise>
    """
    violations = []
    lines = content.split('\n')
    
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        
        # Mönster 1: "except:" eller "except SomeException:" följt av pass/continue på samma rad
        if re.match(r'^except(\s+\w+)?(\s+as\s+\w+)?:\s*(pass|continue)\s*(#.*)?$', stripped):
            violations.append({
                "file": filepath,
                "line": i,
                "rule": "P2",
                "message": "Tyst fallback: except med pass/continue",
                "code": stripped
            })
            continue
        
        # Mönster 2: except: return tomma värden (på samma rad)
        if re.match(r'^except(\s+\w+)?(\s+as\s+\w+)?:\s*return\s*(\[\]|\{\}|None|""|\'\')\s*(#.*)?$', stripped):
            violations.append({
                "file": filepath,
                "line": i,
                "rule": "P2",
                "message": "Tyst fallback: except med return tomvärde",
                "code": stripped
            })
            continue
        
        # Mönster 3: "except:" eller "except SomeException:" på egen rad
        if re.match(r'^except(\s+\w+)?(\s+as\s+\w+)?:\s*$', stripped):
            # Kolla nästa rad
            if i < len(lines):
                next_line = lines[i].strip()
                
                # 3a: Nästa rad är pass/continue
                if next_line in ['pass', 'continue']:
                    violations.append({
                        "file": filepath,
                        "line": i,
                        "rule": "P2",
                        "message": "Tyst fallback: except följt av pass/continue",
                        "code": f"{stripped} → {next_line}"
                    })
                    continue
                
                # 3b: Nästa rad är return tomvärde
                if re.match(r'^return\s*(\[\]|\{\}|None|""|\'\')', next_line):
                    violations.append({
                        "file": filepath,
                        "line": i,
                        "rule": "P2",
                        "message": "Tyst fallback: except följt av return tomvärde",
                        "code": f"{stripped} → {next_line}"
                    })
                    continue
            
            # 3c: Kolla om except-blocket saknar LOGGER eller raise
            has_logging = False
            except_indent = len(line) - len(line.lstrip())
            
            for j in range(i, min(i + 15, len(lines))):
                check_line = lines[j] if j < len(lines) else ""
                check_indent = len(check_line) - len(check_line.lstrip())
                
                # Sluta om vi lämnar except-blocket (mindre eller lika indentering på icke-tom rad)
                if check_line.strip() and check_indent <= except_indent and j > i:
                    break
                
                if 'LOGGER.' in check_line or 'logging.' in check_line or 'raise' in check_line:
                    has_logging = True
                    break
                # exit() är också en giltig hardfail
                if 'exit(' in check_line or 'sys.exit(' in check_line:
                    has_logging = True
                    break
            
            # KeyboardInterrupt är OK (används för graceful shutdown)
            if 'KeyboardInterrupt' in stripped:
                continue
            
            if not has_logging:
                violations.append({
                    "file": filepath,
                    "line": i,
                    "rule": "P2",
                    "message": "except-block utan loggning eller raise",
                    "code": stripped
                })
    
    return violations


# === PRINCIP 6: Inga Hårdkodade Promptar ===

def check_hardcoded_prompts(filepath: str, content: str) -> list:
    """
    Leta efter långa strängar som ser ut som AI-promptar.
    
    Heuristik:
    - Strängar > 200 tecken med instruktionsspråk
    - f-strängar med {context}, {query}, etc.
    """
    violations = []
    lines = content.split('\n')
    
    # Mönster: variabel = "lång sträng med prompt-ord"
    prompt_indicators = [
        'Du är', 'Du ska', 'Analysera', 'Returnera JSON', 'Svara på',
        'SYSTEM:', 'USER:', 'ASSISTANT:', 'Instruktion:', 'Uppgift:'
    ]
    
    for i, line in enumerate(lines, 1):
        # Hoppa över kommentarer och imports
        stripped = line.strip()
        if stripped.startswith('#') or stripped.startswith('import') or stripped.startswith('from'):
            continue
        
        # Leta efter långa strängar (>200 tecken)
        string_matches = re.findall(r'["\'](.{200,}?)["\']', line)
        for match in string_matches:
            # Kolla om det innehåller prompt-indikatorer
            for indicator in prompt_indicators:
                if indicator.lower() in match.lower():
                    violations.append({
                        "file": filepath,
                        "line": i,
                        "rule": "P6",
                        "message": f"Möjlig hårdkodad prompt (innehåller '{indicator}')",
                        "code": match[:80] + "..."
                    })
                    break
    
    return violations


# === PRINCIP 7: Taxonomin är Master ===

def check_hardcoded_taxonomy(filepath: str, content: str) -> list:
    """
    Leta efter hårdkodade listor med taxonomi-noder.
    
    Violations:
    - ["Person", "Projekt", ...] i kod
    - CATEGORIES = ["..."]
    """
    violations = []
    lines = content.split('\n')
    
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        
        # Hoppa över kommentarer
        if stripped.startswith('#'):
            continue
        
        # Räkna hur många taxonomi-noder som finns på raden
        found_nodes = [node for node in TAXONOMY_NODES if f'"{node}"' in line or f"'{node}'" in line]
        
        # Om 3+ taxonomi-noder på samma rad → troligen hårdkodad lista
        if len(found_nodes) >= 3:
            # Undantag: om det är i validate_rules.py själv
            if 'validate_rules.py' in filepath:
                continue
            
            violations.append({
                "file": filepath,
                "line": i,
                "rule": "P7",
                "message": f"Hårdkodade kategorier: {found_nodes}",
                "code": stripped[:100]
            })
    
    return violations


# === PRINCIP 8: Config är Sanning för Sökvägar ===

def check_hardcoded_paths(filepath: str, content: str) -> list:
    """
    Leta efter hårdkodade sökvägar som borde läsas från config.
    
    Violations:
    - "~/MyMemory/..."
    - "/Users/username/..."
    - os.path.expanduser("~/MyMemory/...") utan config-lookup
    """
    violations = []
    lines = content.split('\n')
    
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        
        # Hoppa över kommentarer
        if stripped.startswith('#'):
            continue
        
        # Kolla varje mönster
        for pattern in HARDCODED_PATH_PATTERNS:
            if re.search(pattern, line):
                # Undantag: om CONFIG finns på samma rad (läser från config)
                if 'CONFIG' in line or 'config[' in line.lower():
                    continue
                
                # Undantag: validate_rules.py själv
                if 'validate_rules.py' in filepath:
                    continue
                
                violations.append({
                    "file": filepath,
                    "line": i,
                    "rule": "P8",
                    "message": "Hårdkodad sökväg (ska läsas från config)",
                    "code": stripped[:100]
                })
                break  # En violation per rad räcker
    
    return violations


# === PRINCIP 9: Config-värden ska läsas från config ===

def check_hardcoded_config_values(filepath: str, content: str) -> list:
    """
    Leta efter hårdkodade config-värden som AI-modeller och API-nycklar.
    
    Violations:
    - "gemini-pro", "gemini-flash", etc.
    - API-nycklar (AIza...)
    - Slack-tokens (xoxp-, xoxb-)
    """
    violations = []
    lines = content.split('\n')
    
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        
        # Hoppa över kommentarer
        if stripped.startswith('#'):
            continue
        
        # Separera kod från inline-kommentar
        code_part = line.split('#')[0] if '#' in line else line
        
        # Kolla varje mönster - men ENDAST i kod-delen (inte kommentarer)
        for pattern, message in HARDCODED_CONFIG_PATTERNS:
            if re.search(pattern, code_part):
                # Undantag: om det är i config-filer
                if 'config' in filepath.lower() and filepath.endswith('.yaml'):
                    continue
                
                # Undantag: validate_rules.py själv
                if 'validate_rules.py' in filepath:
                    continue
                
                # Undantag: om CONFIG finns på samma rad (läser från config)
                if 'CONFIG' in code_part or "config[" in code_part.lower() or "config.get" in code_part.lower():
                    continue
                
                # Undantag: om MODELS eller PROMPTS läses (redan från config)
                if 'MODELS' in code_part or 'PROMPTS' in code_part:
                    continue
                
                violations.append({
                    "file": filepath,
                    "line": i,
                    "rule": "P9",
                    "message": f"Hårdkodat config-värde: {message}",
                    "code": stripped[:100]
                })
                break  # En violation per rad räcker
    
    return violations


# === MAIN VALIDATOR ===

def validate_file(filepath: str) -> list:
    """Kör alla valideringar på en fil."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return [{
            "file": filepath,
            "line": 0,
            "rule": "ERROR",
            "message": f"Kunde inte läsa fil: {e}",
            "code": ""
        }]
    
    violations = []
    violations.extend(check_silent_fallbacks(filepath, content))
    violations.extend(check_hardcoded_prompts(filepath, content))
    violations.extend(check_hardcoded_taxonomy(filepath, content))
    violations.extend(check_hardcoded_paths(filepath, content))
    violations.extend(check_hardcoded_config_values(filepath, content))
    
    return violations


def validate_directory(dirpath: str) -> list:
    """Validera alla .py-filer i en mapp."""
    violations = []
    for filepath in Path(dirpath).glob('*.py'):
        violations.extend(validate_file(str(filepath)))
    return violations


def format_violations(violations: list) -> str:
    """Formatera violations för output."""
    if not violations:
        return "✅ Inga violations hittades!"
    
    output = []
    output.append(f"❌ {len(violations)} violation(s) hittades:\n")
    
    # Gruppera per fil
    by_file = {}
    for v in violations:
        if v['file'] not in by_file:
            by_file[v['file']] = []
        by_file[v['file']].append(v)
    
    for filepath, file_violations in by_file.items():
        output.append(f"\n📄 {filepath}")
        for v in file_violations:
            output.append(f"   Rad {v['line']:4d} [{v['rule']}]: {v['message']}")
            if v['code']:
                output.append(f"            → {v['code']}")
    
    return '\n'.join(output)


def main():
    """CLI entrypoint."""
    if len(sys.argv) < 2:
        # Default: validera services/
        target = "services/"
    else:
        target = sys.argv[1]
    
    if os.path.isfile(target):
        violations = validate_file(target)
    elif os.path.isdir(target):
        violations = validate_directory(target)
    else:
        print(f"❌ Hittade inte: {target}")
        sys.exit(1)
    
    print(format_violations(violations))
    
    # Exit code: 0 om inga violations, 1 annars
    sys.exit(0 if len(violations) == 0 else 1)


if __name__ == "__main__":
    main()

