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

# Validera property chain (kör efter ändringar i schema/processors/utils/agents)
python tools/test_property_chain.py

# Validera MCP-verktyg (kör efter ändringar i index_search_mcp.py)
python tools/test_mcp_search.py

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
- **Kör property chain test** efter ändringar i schema-filer eller processors:
  ```bash
  python tools/test_property_chain.py
  ```
  Testet verifierar att properties propagerar korrekt: Schema → DocConverter → Lake → Vector → Graf

### 2. HARDFAIL > Silent Fallback
- Inga tysta fallbacks - rapportera fel explicit
- Logga orsaken med full kontext
- Avbryt operationen istället för att gissa

**Undantag:** Fallbacks är tillåtna endast om de är explicit dokumenterade i svaret, loggas med WARNING, och användaren kan se att en fallback användes.

```python
# ❌ DÅLIGT: Tyst fallback
if not results:
    results = relaxed_search(query)  # Användaren vet inte

# ✅ BRA: Explicit hardfail
if not results:
    return {"status": "NO_RESULTS", "reason": f"Strict search for '{query}' returned 0 hits"}
```

### 3. Configuration Driven
Inga inställningar, modellnamn (t.ex. `gemini-pro`), API-nycklar eller tröskelvärden (t.ex. `0.95`) får hårdkodas i Python-filer. De måste hämtas dynamiskt från konfigurationsfiler (yaml) eller miljövariabler.

- **Sökvägar:** Läs från `config/my_mem_config.yaml`
- **Graf-schema:** Läs från `config/graph_schema_template.json` (SSOT för ontologin)
- **Promptar:** Lägg i `config/services_prompts.yaml`, aldrig i Python-kod
- **Undantag:** Default-värden i `.get()`-anrop är okej

```python
# ❌ DÅLIGT
VALID_NODE_TYPES = ["Person", "Organization", "Project"]
GRAPH_PATH = os.path.expanduser("~/MyMemory/Index/my_mem_graph")
MODEL = "gemini-pro"

# ✅ BRA
from services.utils.schema_validator import get_allowed_node_types
CONFIG = load_config('my_mem_config.yaml')
GRAPH_PATH = os.path.expanduser(CONFIG['paths']['graph_db'])
MODEL = CONFIG.get('models', {}).get('model_pro')
```

### 4. External Prompts
Promptar till LLM får **ALDRIG** definieras i koden. Variabler som heter `prompt`, `instruction` eller `template` får inte tilldelas långa strängar eller f-strings. All prompt-text ska laddas från `config/services_prompts.yaml`.

### 5. Schema Consistency
Koden måste strikt följa namngivningen i `graph_schema_template.json`. Det är förbjudet att uppfinna egna nycklar för noder eller properties (t.ex. använda `evidence` om schemat säger `distinguishing_context`). Om koden refererar till en property, MÅSTE den finnas i schemat.

### 6. Traceability
Dataflödet måste vara spårbart. Det är förbjudet att skriva över dynamisk data med hårdkodad dummy-data (t.ex. "LLM sammanfattad fakta") eller återställa tidsstämplar manuellt utan logik. Data som extraheras i ett steg måste bevaras till lagring.

### 7. Ingen AI-cringe
- Undvik töntiga metafornamn ("Trädgårdsmästaren", "Bibliotekarien")
- Använd deskriptiva namn som beskriver funktionen

### 8. Stanna vid vägval
Fråga användaren vid:
- Namngivning (funktioner, variabler, fält)
- Prompt-formuleringar
- Output-format (JSON-strukturer, API-kontrakt)
- Trade-offs och oklarheter

### 9. Generella lösningar på specifika problem
- Sök den generella orsaken, inte det specifika symptomet
- Undvik specifika fixar som skapar teknisk skuld

```
Problem: "Systemet hittar inte 'Cenk' när jag söker"
❌ DÅLIGT: "Lägg till 'Cenk' som alias i konfigurationen"
✅ BRA: "Varför hittar vi inte varianter av namn generellt?" → Entity Resolution
```

### 10. Skyddade filer
Fråga innan radering/omskrivning av:
```
config/my_mem_config.yaml
config/graph_schema_template.json
services/utils/graph_service.py
services/processors/doc_converter.py
services/agents/dreamer.py
```

### 11. Arbeta i Main - Aldrig i Worktrees
- **ALDRIG** arbeta i git worktree-branches
- Alla ändringar sker direkt i main-repot (`/Users/jekman/Projects/MyMemory`)
- Worktrees skapar förvirring, synkproblem, och commits kan gå förlorade

### 12. Dokumentation och Commits
- **Committa efter varje logisk ändring** - inte bara vid sessionsslut
- **Uppdatera dokumentation:**
  - `documentation/my_mem_backlogg.md` - när objekt löses eller läggs till
  - `documentation/my_mem_koncept_logg.md` - vid arkitekturbeslut
- Historik går förlorad om commits skjuts upp

## Felsökning & Loggar

**Loggfil:** `~/MyMemory/Logs/my_mem_system.log`

**Vid felsökning - kolla loggen först!** Innan du frågar om fel eller oväntade beteenden, läs loggen:
```bash
# Senaste raderna
tail -100 ~/MyMemory/Logs/my_mem_system.log

# Sök efter fel
grep -i "error\|exception\|fail" ~/MyMemory/Logs/my_mem_system.log | tail -50

# Specifik tjänst (TRANS, VECTOR, SLACK, RETRIEVER, etc.)
grep "TRANS" ~/MyMemory/Logs/my_mem_system.log | tail -50
```

Alla tjänster loggar till samma fil med prefix: `TRANS`, `VECTOR`, `SLACK`, `RETRIEVER`, `GMAIL`, `CALENDAR`.

## Konfigurationsfiler

| Fil | Syfte |
|-----|-------|
| `config/my_mem_config.yaml` | Sökvägar, API-nycklar, modeller |
| `config/graph_schema_template.json` | SSOT: graf-noder, relationer, properties |
| `config/lake_metadata_template.json` | SSOT: Lake frontmatter-schema |
| `config/services_prompts.yaml` | Promptar för tjänster |
