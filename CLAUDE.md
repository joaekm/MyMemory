# MyMemory - Claude Code Guidelines

## Projektöversikt

MyMemory är ett personligt kunskapshanteringssystem ("Digitalist Företagsminne") som samlar in, bearbetar, indexerar och söker i användarens data från olika källor (dokument, ljud, Slack).

**Tech Stack:** Python 3.12, ChromaDB (vektor), DuckDB (graf), Google Gemini AI, Rich (CLI)

## Viktiga kommandon

```bash
# Starta alla tjänster
python start_services.py

# Validera kod efter ändringar
python tools/validate_rules.py services/<ändrad_fil>.py

# Validera promptar
python tools/validate_prompts.py config/chat_prompts.yaml
python tools/validate_prompts.py --fix  # Autofixa med LLM

# Simulering/stresstest
python tools/simulate_session.py
```

## Projektstruktur

```
config/              # Konfigurationsfiler (YAML, JSON)
  my_mem_config.yaml   # Huvudconfig (sökvägar, API-nycklar)
  chat_prompts.yaml    # Promptar för chat
  services_prompts.yaml # Promptar för tjänster
services/            # Huvudkod
  agents/              # AI-agenter (Dreamer, etc.)
  collectors/          # Datainsamling (Slack, File)
  engine/              # Kärnmotor
  indexers/            # ChromaDB, Graf
  interface/           # CLI/UI
  pipeline/            # Bearbetningspipeline
  processors/          # Dokumentbearbetning
  utils/               # Hjälpfunktioner
tools/               # Verktyg och validatorer
documentation/       # Arkitekturdokumentation
```

## Utvecklingsregler

### 1. Kör validatorer efter varje ändring
- **0 violations** krävs innan nästa fil får ändras
- Validatorerna får ALDRIG ändras utan explicit tillåtelse

### 2. HARDFAIL > Silent Fallback
- Inga tysta fallbacks - rapportera fel explicit
- Logga orsaken med full kontext
- Avbryt operationen istället för att gissa

### 3. Inga hårdkodade värden
- **Sökvägar:** Läs från `config/my_mem_config.yaml`
- **Kategorier:** Läs från `~/MyMemory/Index/my_mem_taxonomy.json`
- **Promptar:** Lägg i `config/*.yaml`, aldrig i Python-kod

### 4. Ingen AI-cringe
- Undvik töntiga metafornamn ("Trädgårdsmästaren", "Bibliotekarien")
- Använd deskriptiva namn som beskriver funktionen

### 5. Stanna vid vägval
Fråga användaren vid:
- Namngivning (funktioner, variabler, fält)
- Prompt-formuleringar
- Output-format (JSON-strukturer, API-kontrakt)
- Trade-offs och oklarheter

### 6. Generella lösningar på specifika problem
- Sök den generella orsaken, inte det specifika symptomet
- Undvik specifika fixar som skapar teknisk skuld

### 7. Skyddade filer
Fråga innan radering/omskrivning av:
```
services/utils/document_dna.py
services/my_mem_*_collector.py
services/graph_service.py
services/session_engine.py
config/my_mem_config.yaml
```

## Arkitektur (kortfattat)

**Tre lagringsnivåer:**
1. **Asset Store** (`~/MyMemory/Assets`) - Originalfiler, aldrig röra
2. **Lake** (`~/MyMemory/Lake`) - Normaliserade .md-filer med YAML-frontmatter
3. **Index** (`~/MyMemory/Index`) - ChromaDB + Graf + Taxonomi

**Agent-pipeline:**
```
DropZone → File Retriever → Assets
Assets → Transcriber/DocConverter → Lake
Lake → Vector Indexer (realtid) + Graph Builder (batch) → Index
Index → Chat Pipeline → Svar
```

## Konfigurationsfiler

| Fil | Syfte |
|-----|-------|
| `config/my_mem_config.yaml` | Sökvägar, API-nycklar, modeller |
| `config/chat_prompts.yaml` | System-prompter för chatten |
| `config/services_prompts.yaml` | Prompter för insamlingsagenter |
| `~/MyMemory/Index/my_mem_taxonomy.json` | Masternoder (kategorier) |
