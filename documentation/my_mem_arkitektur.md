
# Systemarkitektur (v7.0 - Tre-fas pipeline)

Detta dokument beskriver den tekniska sanningen om systemets implementation, uppdaterad efter OBJEKT-68 refaktorering (Januari 2026).

## 1. Huvudprinciper

1. **HARDFAIL > Silent Fallback:** Systemet ska misslyckas tydligt istället för att tyst falla tillbaka. Alla fel rapporteras explicit.

2. **Datakvalitet först:** Bättre data ger bättre svar oavsett reasoning-logik. Fokus på ingestion, validering och förädling.

3. **MCP som exponering:** MyMemory är händerna (kunskapsbas), inte hjärnan (reasoning). Externa AI-verktyg hanterar reasoning.

4. **Schema som SSOT:** `graph_schema_template.json` definierar tillåtna nodtyper, relationer och properties.

5. **Idempotens & Självläkning:** Alla agenter hoppar över redan klara filer och fyller automatiskt i hål.

6. **Central LLM-hantering:** Alla LLM-anrop går via `LLMService` singleton med throttling och retry.

## 2. Datamodell: Trippel Lagring

### "Assets" (Lagring 1 - Källan)
- **Innehåll:** Originalfiler (PDF, Docx, ljud, etc.)
- **Namnstandard:** `[beskrivning]_[UUID].[ext]`
- **Syfte:** Sanningen. Aldrig röra.
- **Sökväg:** `~/MyMemory/Assets/`

### "Lake" (Lagring 2 - Mellanlager)
- **Innehåll:** `.md`-filer med standardiserad YAML-frontmatter
- **Ansvarig:** Skapas av DocConverter och Transcriber
- **Syfte:** Normaliserad text med metadata
- **Sökväg:** `~/MyMemory/Lake/`

### "Index" (Lagring 3 - Hjärnan)
- **ChromaDB:** Vektorer för semantisk sökning
- **DuckDB:** Relationell graf (nodes + edges tabeller)
- **Sökväg:** `~/MyMemory/Index/`

### 3-Timestamp-systemet
Alla Lake-filer har tre tidsstämplar i frontmatter:
- `timestamp_ingestion`: När filen indexerades i Lake
- `timestamp_content`: När innehållet faktiskt hände (eller "UNKNOWN")
- `timestamp_updated`: Sätts av Dreamer vid förädling

## 3. Tre-fas Pipeline

### Översikt

```
┌─────────────────────────────────────────────────────────────────┐
│ FAS 1: COLLECT & NORMALIZE                                      │
│ DropZone → File Retriever → Assets (UUID-normaliserade)         │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ FAS 2: INGESTION (nya data, per dokument)                       │
│     ┌──────────────────┴──────────────────┐                     │
│     │                                     │                     │
│ Transcriber                      Ingestion Engine               │
│ (ljud → text)                    (text + metadata + graf)       │
│     ↓                                     ↓                     │
│ Assets/Transcripts               Lake (.md + frontmatter)       │
│     └─────────────────────────────────────┘                     │
│                       ↓                                         │
│               Vector Indexer → ChromaDB                         │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ FAS 3: DREAMING (hela kunskapsbasen, batch)                     │
│ Dreamer → Entity Resolution, merge/split/rename                 │
│         → Graf-förädling (DuckDB)                               │
│         → Lake-uppdatering (node_context)                       │
└─────────────────────────────────────────────────────────────────┘
```

### Fasernas roller

| Fas | Trigger | Scope | Fråga |
|-----|---------|-------|-------|
| **1. Collect** | Ny fil i DropZone | En fil | "Var ska denna fil lagras?" |
| **2. Ingestion** | Ny fil i Assets | Ett dokument | "Vad finns i DETTA dokument?" |
| **3. Dreaming** | Schemalagt/manuellt | Hela kunskapsbasen | "Hur passar allt ihop?" |

### Komponenter

| Komponent | Plats | Bevakar | Output |
|-----------|-------|---------|--------|
| **File Retriever** | `collectors/` | DropZone | Assets (UUID-normaliserade) |
| **Transcriber** | `processors/` | Assets/Recordings | Assets/Transcripts |
| **Ingestion Engine** | `engines/` | Assets/* | Lake + Graf + Vektor |
| **Vector Indexer** | `indexers/` | Lake | ChromaDB |
| **Dreamer** | `engines/` | (batch) | Graf + Lake |

### Distinktion: Ingestion vs Dreamer

| Aspekt | Ingestion Engine | Dreamer |
|--------|------------------|---------|
| **Trigger** | Ny fil | Schemalagt/manuellt |
| **Scope** | Ett dokument | Hela kunskapsbasen |
| **LLM TaskType** | `ENRICHMENT` | `ENTITY_RESOLUTION` |
| **Graf-operationer** | Skapa noder | Merge, split, rename |
| **Konfidens** | Initial (0.5) | Uppdateras vid bekräftelse |

**Nyckelprincip:** Ingestion är **snabb och självständig**. Dreamer är **reflekterande**.

### EntityGatekeeper (Dubblettkontroll)
Vid ingestion kontrollerar Ingestion Engine varje entitet:
1. **LINK:** Exakt eller fuzzy-match i grafen → återanvänd befintlig UUID
2. **CREATE:** Ingen match → skapa ny provisional nod

## 4. Index-struktur

### ChromaDB (Vektor)
- **Collection:** `knowledge_base`
- **Embedding:** `KBLab/sentence-bert-swedish-cased` (lokal, 768 dim, svenska + engelska)
- **Dokument:** Sammanfattning + nyckelord + innehåll (max 8000 tecken)

### DuckDB (Graf)
Relationell modell med två tabeller:
```sql
nodes(id TEXT PRIMARY KEY, type TEXT, aliases TEXT, properties TEXT)
edges(source TEXT, target TEXT, edge_type TEXT, properties TEXT)
```

## 5. MCP-exponering

### index_search_mcp.py (9 verktyg)
| Verktyg | Funktion |
|---------|----------|
| `search_graph_nodes` | Sök noder i grafen |
| `query_vector_memory` | Vektorsökning i ChromaDB |
| `search_by_date_range` | Tidsfilterad sökning |
| `search_lake_metadata` | Sök i Lake metadata |
| `get_neighbor_network` | Hämta relaterade noder |
| `get_entity_summary` | Sammanfattning av entitet |
| `get_graph_statistics` | Grafstatistik |
| `parse_relative_date` | Parsa "igår", "förra veckan" |
| `read_document_content` | Läs dokumentinnehåll |

### validator_mcp.py (2 verktyg)
| Verktyg | Funktion |
|---------|----------|
| `validate_extraction` | Validera extraherad data mot schema |
| `extract_and_validate_doc` | Extrahera och validera dokument |

## 6. Dreamer - Förädling

`services/engines/dreamer.py` förädlar på tre platser:

1. **Vektor (ChromaDB):** Säkerställer att noder är indexerade för semantisk sökning
2. **Graf (DuckDB):** Merge, split, rename av dubbletter via LLM-bedömning
3. **Lake:** Uppdatering av node_context och metadata

### Urvalsstrategi (80/20)
- **80% Relevans:** Noder som används ofta och nyligen
- **20% Underhåll:** Noder som inte städats på länge

### LLM-användning
- `TaskType.ENTITY_RESOLUTION` - för merge/split-beslut
- `TaskType.STRUCTURAL_ANALYSIS` - för strukturell optimering

### Trigger (OBJEKT-76)
Dreamer triggas automatiskt av `dreamer_daemon.py` baserat på:
1. **Threshold:** När ~15 nya graf-noder skapats (konfigurerbart)
2. **Fallback:** Max 24h sedan senaste körning

Daemon pollar en JSON-räknarfil (`~/MyMemory/Index/.dreamer_state.json`) som uppdateras av Ingestion Engine vid varje graf-skrivning. Körs via launchd på macOS.

```bash
# Kolla status
python services/engines/dreamer_daemon.py --status

# Kör manuellt
python services/engines/dreamer_daemon.py --once
```

## 7. LLMService - Central LLM-hantering

`services/utils/llm_service.py` är en singleton som hanterar alla LLM-anrop.

### Arkitektur

```python
class LLMService:
    """Singleton för centraliserade LLM-anrop."""

    # Modeller (från config)
    models = {'pro': ..., 'fast': ..., 'lite': ...}

    # Task → Modell mappning
    task_model_map = {
        TaskType.TRANSCRIPTION: 'pro',
        TaskType.ENRICHMENT: 'fast',
        TaskType.VALIDATION: 'lite',
        TaskType.ENTITY_RESOLUTION: 'lite',
        TaskType.STRUCTURAL_ANALYSIS: 'lite',
    }

    # Centraliserad throttling och retry
    throttler = AdaptiveThrottler(...)
```

### Användning per komponent

| Fil | Metod | TaskType | Anledning |
|-----|-------|----------|-----------|
| `ingestion_engine.py` | `.generate()` | `ENRICHMENT` | Metadata-generering |
| `dreamer.py` | `.generate()` | `ENTITY_RESOLUTION` | Merge/split-beslut |
| `transcriber.py` | `.client` | `TRANSCRIPTION` | Multimodal (file upload) |
| `validator_mcp.py` | `.client` | `VALIDATION` | Multi-turn konversation |

### Varför `.client` ibland?
- **Multimodal:** Transcriber behöver `client.files.upload()` för ljudfiler
- **Multi-turn:** Validator MCP behöver `contents`-lista för konversation

## 8. Konfiguration

| Fil | Syfte |
|-----|-------|
| `config/my_mem_config.yaml` | Sökvägar, API-nycklar, modeller |
| `config/graph_schema_template.json` | SSOT: nodtyper, relationer, properties |
| `config/lake_metadata_template.json` | SSOT: Lake frontmatter-schema |
| `config/services_prompts.yaml` | Promptar för tjänster |

## 9. Tech Stack

| Kategori | Teknologi |
|----------|-----------|
| **Språk** | Python 3.12 |
| **Vektordatabas** | ChromaDB |
| **Grafdatabas** | DuckDB (relationell graf) |
| **AI-modeller** | Google Gemini (Pro/Flash/Lite) |
| **Embeddings** | KBLab/sentence-bert-swedish-cased (768 dim) |
| **MCP** | FastMCP |

## 10. Filstruktur

```
services/
├── agents/                    # MCP-servrar
│   ├── index_search_mcp.py      # Sök-verktyg (9 st)
│   └── validator_mcp.py         # Validerings-verktyg (2 st)
├── collectors/                # Fas 1: Insamling
│   └── file_retriever.py        # DropZone → Assets
├── engines/                   # Centrala motorer
│   ├── dreamer.py               # Fas 3: Batch-förädling
│   ├── dreamer_daemon.py        # Threshold-trigger för Dreamer (OBJEKT-76)
│   └── ingestion_engine.py      # Fas 2: Dokument-bearbetning
├── indexers/                  # Indexering
│   └── vector_indexer.py        # Lake → ChromaDB
├── processors/                # Specialbearbetning
│   └── transcriber.py           # Ljud → Text
└── utils/                     # Hjälpfunktioner
    ├── graph_service.py         # DuckDB-wrapper
    ├── lake_service.py          # Lake-operationer
    ├── llm_service.py           # Central LLM-hantering
    ├── schema_validator.py      # Schema-validering
    └── vector_service.py        # ChromaDB-wrapper
```

## 11. Status (Januari 2026)

- **Datakvalitet:** ~75% klar. Property chain validerad.
- **MCP-server:** 11 verktyg. Alfa-status. Används med Claude Desktop.
- **OBJEKT-68:** ✅ Komplett. Tre-fas pipeline, LLM-konsolidering.
- **OBJEKT-76:** ✅ Komplett. Dreamer-daemon med threshold-trigger.
- **Kvarstående:** Extractor+Critic POC.

---
*Senast uppdaterad: 2026-01-17*
*Se `my_mem_koncept_logg.md` för resonemang bakom beslut.*
