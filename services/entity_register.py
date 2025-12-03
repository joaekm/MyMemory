"""
Entity Consolidator - Alias-hantering för MyMemory

Paradigm: Alias = Insamlingsinstruktion (INTE sök-expansion)
- Vid INSAMLING: Normalisera varianter till canonical name
- Vid FEEDBACK: Lägg till nya alias-mappningar
- Vid SÖKNING: Ingen expansion behövs (metadata är redan normaliserad)

Lagring: ~/MyMemory/Index/entity_aliases.json
"""

import os
import json
import logging
from typing import Optional

LOGGER = logging.getLogger('EntityConsolidator')

# Default path för alias-fil
_ALIAS_FILE = os.path.expanduser("~/MyMemory/Index/entity_aliases.json")

# Cache för snabb lookup
_ALIASES_CACHE = None
_REVERSE_CACHE = None  # variant -> canonical


def _load_aliases() -> dict:
    """Ladda alias-mappningar från fil."""
    global _ALIASES_CACHE, _REVERSE_CACHE
    
    if _ALIASES_CACHE is not None:
        return _ALIASES_CACHE
    
    if os.path.exists(_ALIAS_FILE):
        try:
            with open(_ALIAS_FILE, 'r', encoding='utf-8') as f:
                _ALIASES_CACHE = json.load(f)
        except Exception as e:
            LOGGER.error(f"Kunde inte ladda aliases: {e}")
            _ALIASES_CACHE = {"persons": {}, "projects": {}, "concepts": {}}
    else:
        # Skapa tom struktur
        _ALIASES_CACHE = {"persons": {}, "projects": {}, "concepts": {}}
    
    # Bygg reverse cache (variant -> canonical)
    _REVERSE_CACHE = {}
    for entity_type, entities in _ALIASES_CACHE.items():
        for canonical, aliases in entities.items():
            _REVERSE_CACHE[canonical.lower()] = canonical
            for alias in aliases:
                _REVERSE_CACHE[alias.lower()] = canonical
    
    return _ALIASES_CACHE


def _save_aliases(data: dict):
    """Spara alias-mappningar till fil."""
    global _ALIASES_CACHE, _REVERSE_CACHE
    
    # Säkerställ att mappen finns
    os.makedirs(os.path.dirname(_ALIAS_FILE), exist_ok=True)
    
    try:
        with open(_ALIAS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        # Invalidera cache
        _ALIASES_CACHE = data
        
        # Bygg om reverse cache
        _REVERSE_CACHE = {}
        for entity_type, entities in data.items():
            for canonical, aliases in entities.items():
                _REVERSE_CACHE[canonical.lower()] = canonical
                for alias in aliases:
                    _REVERSE_CACHE[alias.lower()] = canonical
        
        LOGGER.info(f"Sparade aliases till {_ALIAS_FILE}")
    except Exception as e:
        LOGGER.error(f"Kunde inte spara aliases: {e}")
        raise


def get_canonical(variant: str) -> Optional[str]:
    """
    Slå upp canonical name för en variant.
    
    Används vid INSAMLING för att normalisera namn i metadata.
    
    Args:
        variant: En namnvariant (t.ex. "Sänk")
    
    Returns:
        Canonical name (t.ex. "Cenk Bisgen") eller None om okänd
    
    Exempel:
        get_canonical("Sänk") -> "Cenk Bisgen"
        get_canonical("Okänd Person") -> None
    """
    _load_aliases()  # Säkerställ att cache är laddad
    
    if _REVERSE_CACHE is None:
        return None
    
    return _REVERSE_CACHE.get(variant.lower())


def add_alias(canonical: str, alias: str, source: str = "user", entity_type: str = "persons"):
    """
    Lägg till en alias-mappning.
    
    Används vid EXPLICIT FEEDBACK ("/learn Cenk = Sänk").
    
    Args:
        canonical: Kanoniskt namn (t.ex. "Cenk Bisgen")
        alias: Variant att mappa (t.ex. "Sänk")
        source: Varifrån aliaset kommer ("user", "consolidator", "transcriber")
        entity_type: Typ av entitet ("persons", "projects", "concepts")
    
    Raises:
        ValueError: Om entity_type är ogiltigt
    """
    if entity_type not in ["persons", "projects", "concepts"]:
        raise ValueError(f"Ogiltig entity_type: {entity_type}")
    
    data = _load_aliases()
    
    # Säkerställ att entity_type finns
    if entity_type not in data:
        data[entity_type] = {}
    
    # Säkerställ att canonical finns
    if canonical not in data[entity_type]:
        data[entity_type][canonical] = []
    
    # Lägg till alias om det inte redan finns
    alias_lower = alias.lower()
    existing_lower = [a.lower() for a in data[entity_type][canonical]]
    
    if alias_lower not in existing_lower and alias_lower != canonical.lower():
        data[entity_type][canonical].append(alias)
        _save_aliases(data)
        LOGGER.info(f"Lade till alias: '{alias}' -> '{canonical}' (source={source})")
    else:
        LOGGER.debug(f"Alias '{alias}' finns redan för '{canonical}'")


def remove_alias(canonical: str, alias: str, entity_type: str = "persons"):
    """
    Ta bort en alias-mappning.
    
    Används för att korrigera felaktiga alias.
    
    Args:
        canonical: Kanoniskt namn
        alias: Alias att ta bort
        entity_type: Typ av entitet
    """
    data = _load_aliases()
    
    if entity_type not in data:
        return
    
    if canonical not in data[entity_type]:
        return
    
    # Ta bort (case-insensitive)
    alias_lower = alias.lower()
    data[entity_type][canonical] = [
        a for a in data[entity_type][canonical]
        if a.lower() != alias_lower
    ]
    
    _save_aliases(data)
    LOGGER.info(f"Tog bort alias: '{alias}' från '{canonical}'")


def get_known_entities() -> dict:
    """
    Hämta alla kända entiteter och deras aliases.
    
    Används vid INSAMLING för context injection i prompts.
    
    Returns:
        dict med:
            - persons: Lista med kanoniska personnamn
            - projects: Lista med kanoniska projektnamn
            - concepts: Lista med kanoniska konceptnamn
            - aliases: Dict med alla alias-mappningar {variant: canonical}
    
    Exempel:
        {
            "persons": ["Cenk Bisgen", "Joakim Ekman"],
            "projects": ["Adda PoC", "MyMemory"],
            "concepts": ["Strategi", "AI"],
            "aliases": {"Sänk": "Cenk Bisgen", "Jocke": "Joakim Ekman"}
        }
    """
    data = _load_aliases()
    
    result = {
        "persons": list(data.get("persons", {}).keys()),
        "projects": list(data.get("projects", {}).keys()),
        "concepts": list(data.get("concepts", {}).keys()),
        "aliases": {}
    }
    
    # Bygg alias-mapping (variant -> canonical)
    for entity_type, entities in data.items():
        for canonical, aliases in entities.items():
            for alias in aliases:
                result["aliases"][alias] = canonical
    
    return result


def get_all_aliases() -> dict:
    """
    Hämta hela alias-strukturen.
    
    Returns:
        dict med hela alias-strukturen (persons, projects, concepts)
    """
    return _load_aliases().copy()


def clear_cache():
    """
    Rensa cache. Används vid testning eller efter extern uppdatering.
    """
    global _ALIASES_CACHE, _REVERSE_CACHE
    _ALIASES_CACHE = None
    _REVERSE_CACHE = None


# --- TEST ---
if __name__ == "__main__":
    # Testa grundläggande funktioner
    print("=== Entity Consolidator Test ===\n")
    
    # Lägg till testdata
    add_alias("Cenk Bisgen", "Sänk", source="test")
    add_alias("Cenk Bisgen", "Cenk", source="test")
    add_alias("Joakim Ekman", "Jocke", source="test")
    add_alias("Adda PoC", "Adda-grejen", source="test", entity_type="projects")
    
    # Testa lookup
    print(f"get_canonical('Sänk') = {get_canonical('Sänk')}")
    print(f"get_canonical('Cenk') = {get_canonical('Cenk')}")
    print(f"get_canonical('Jocke') = {get_canonical('Jocke')}")
    print(f"get_canonical('Okänd') = {get_canonical('Okänd')}")
    
    # Testa get_known_entities
    print(f"\nKända entiteter: {json.dumps(get_known_entities(), indent=2, ensure_ascii=False)}")

