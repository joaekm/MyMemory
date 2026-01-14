# MyMemory - Claude Code Guidelines

## Projektöversikt

MyMemory är ett personligt kunskapshanteringssystem ("Digitalist Företagsminne") som samlar in, bearbetar och indexerar användarens data från olika källor (dokument, ljud, Slack, mail, kalender). Exponeras via MCP-server för integration med AI-verktyg som Claude Desktop och Cursor.

**Tech Stack:** Python 3.12, ChromaDB (vektor), DuckDB (graf), Google Gemini AI, MCP (Model Context Protocol)

## Viktiga kommandon

```bash
# Starta alla tjänster
python start_services.py

# Validera kod efter ändringar
python tools/validate_rules.py services/<ändrad_fil>.py

# Validera promptar
python tools/validate_prompts.py config/services_prompts.yaml

# Inspektera graf/vektor
python tools/tool_inspect_graph.py
python tools/tool_inspect_vector.py

# Rebuild efter hard reset
python tools/tool_hard_reset.py
python tools/tool_staged_rebuild.py --confirm --phase foundation
```

## Projektstruktur

```
config/                     # Konfigurationsfiler
  my_mem_config.yaml          # Huvudconfig (sökvägar, API-nycklar)
  graph_schema_template.json  # SSOT: nodtyper, relationer, properties
  services_prompts.yaml       # Promptar för tjänster
services/                   # Huvudkod
  agents/                     # Dreamer, MCP-servrar
  collectors/                 # Datainsamling (Slack, File, Gmail, Calendar)
  indexers/                   # Vector Indexer
  processors/                 # DocConverter, Transcriber
  utils/                      # Hjälpfunktioner (graph_service, vector_service, etc.)
tools/                      # Verktyg och validatorer
  rebuild/                    # Staged rebuild system
documentation/              # Arkitekturdokumentation
```

## Arkitektur

### Tre lagringsnivåer
1. **Assets** (`~/MyMemory/Assets`) - Originalfiler, aldrig röra
2. **Lake** (`~/MyMemory/Lake`) - Normaliserade .md-filer med YAML-frontmatter
3. **Index** (`~/MyMemory/Index`) - ChromaDB (vektor) + DuckDB (graf)

### Ingestion-flöde
```
DropZone → File Retriever → Assets (UUID-normaliserade original)
                ↓
    ┌──────────┴──────────┐
    │                     │
Transcriber          DocConverter
(ljud → text)        (text + AI-metadata + graf-extraktion)
    ↓                     ↓
Assets/Transcripts   Lake (.md + frontmatter)
    └─────────────────────┘
              ↓
      Vector Indexer (realtid) → ChromaDB

      Dreamer (batch) → Graf-förädling
```

### Exponering
```
MCP-server (index_search_mcp.py) → Claude Desktop / Cursor / andra AI-verktyg
```

### Dreamer - förädling på tre platser
1. **Vektor** - semantiska kopplingar (ChromaDB)
2. **Graf** - noder och relationer: merge, split, rename (DuckDB)
3. **Lake** - uppdatering av node_context + metadata i frontmatter

### 3-timestamp-systemet
- `timestamp_ingestion` - när filen indexerades i Lake
- `timestamp_content` - när innehållet hände (extraherat eller UNKNOWN)
- `timestamp_updated` - sätts av Dreamer vid förädling

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
- **Graf-schema:** Läs från `config/graph_schema_template.json`
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
config/my_mem_config.yaml
config/graph_schema_template.json
services/utils/graph_service.py
services/processors/doc_converter.py
services/agents/dreamer.py
```

## Konfigurationsfiler

| Fil | Syfte |
|-----|-------|
| `config/my_mem_config.yaml` | Sökvägar, API-nycklar, modeller |
| `config/graph_schema_template.json` | SSOT: tillåtna noder, relationer, properties |
| `config/services_prompts.yaml` | Promptar för tjänster |
