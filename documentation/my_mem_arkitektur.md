
# Systemarkitektur (v6.0 - MCP-pivot)

Detta dokument beskriver den tekniska sanningen om systemets implementation, uppdaterad efter pivoten från egen chatt till MCP-exponering (Januari 2026).

## 1. Huvudprinciper

1. **HARDFAIL > Silent Fallback:** Systemet ska misslyckas tydligt istället för att tyst falla tillbaka. Alla fel rapporteras explicit.

2. **Datakvalitet först:** Bättre data ger bättre svar oavsett reasoning-logik. Fokus på ingestion, validering och förädling.

3. **MCP som exponering:** MyMemory är händerna (kunskapsbas), inte hjärnan (reasoning). Externa AI-verktyg hanterar reasoning.

4. **Schema som SSOT:** `graph_schema_template.json` definierar tillåtna nodtyper, relationer och properties.

5. **Idempotens & Självläkning:** Alla agenter hoppar över redan klara filer och fyller automatiskt i hål.

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

## 3. Ingestion-flöde

```
DropZone → File Retriever → Assets (UUID-normaliserade original)
                ↓
    ┌──────────┴──────────┐
    │                     │
Transcriber          DocConverter
(ljud → text)        (text + metadata + graf-extraktion)
    ↓                     ↓
Assets/Transcripts   Lake (.md + frontmatter)
    └─────────────────────┘
              ↓
      Vector Indexer (realtid) → ChromaDB

      Dreamer (batch) → Graf-förädling
```

### Komponenter

| Komponent | Bevakar | Output | Funktion |
|-----------|---------|--------|----------|
| **File Retriever** | DropZone | Assets | UUID-normalisering, sortering |
| **Transcriber** | Assets/Recordings | Assets/Transcripts | Ljud → Text via Gemini |
| **DocConverter** | Assets/* | Lake | Text-extraktion + AI-metadata + EntityGatekeeper |
| **Vector Indexer** | Lake | ChromaDB | Delta-scan + Watchdog |
| **Dreamer** | (batch) | Graf + Lake | Entity Resolution, förädling |

### EntityGatekeeper (Dubblettkontroll)
Vid ingestion kontrollerar DocConverter varje entitet:
1. **LINK:** Exakt eller fuzzy-match i grafen → återanvänd befintlig UUID
2. **CREATE:** Ingen match → skapa ny provisional nod

## 4. Index-struktur

### ChromaDB (Vektor)
- **Collection:** `dfm_knowledge_base`
- **Embedding:** `all-MiniLM-L6-v2` (lokal)
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

EntityResolver i `dreamer.py` förädlar på tre platser:

1. **Vektor (ChromaDB):** Säkerställer att noder är indexerade för semantisk sökning
2. **Graf (DuckDB):** Merge, split, rename av dubbletter via LLM-bedömning
3. **Lake:** Uppdatering av node_context och metadata

### Urvalsstrategi (80/20)
- **80% Relevans:** Noder som används ofta och nyligen
- **20% Underhåll:** Noder som inte städats på länge

### Trigger
Just nu endast vid rebuild. Designfråga att lösa.

## 7. Konfiguration

| Fil | Syfte |
|-----|-------|
| `config/my_mem_config.yaml` | Sökvägar, API-nycklar, modeller |
| `config/graph_schema_template.json` | SSOT: nodtyper, relationer, properties |
| `config/services_prompts.yaml` | Promptar för tjänster |

## 8. Tech Stack

| Kategori | Teknologi |
|----------|-----------|
| **Språk** | Python 3.12 |
| **Vektordatabas** | ChromaDB |
| **Grafdatabas** | DuckDB (relationell graf) |
| **AI-modeller** | Google Gemini (Pro/Flash/Lite) |
| **Embeddings** | all-MiniLM-L6-v2 (lokal) |
| **MCP** | FastMCP |

## 9. Status (Januari 2026)

- **Datakvalitet:** ~70% klar. Principer etablerade.
- **MCP-server:** 11 verktyg. Alfa-status. Används med Claude Desktop.
- **Kvarstående:** Metadata-modell, Dreamer-trigger, ingestion-validering.

---
*Senast uppdaterad: 2026-01-14*
*Se `my_mem_koncept_logg.md` för resonemang bakom beslut.*
