---
unit_id: f3b9d8a1-b8f2-4e1a-8b0a-9d6f3c7e0002
owner_id: "joakim.ekman"
access_level: "Nivå_3_Delad_Organisation"
context_id: "PROJEKT_DFM_V1"
source_type: "System_Dokument"
source_ref: "dfm_summary.md"
data_format: "text/markdown"
timestamp_created: "2025-12-03T18:00:00Z"
policy_tags: []
original_binary_ref: null
---

# Projektets Konceptuella Sammanfattning (v11.0 - Tre-fas pipeline)

Detta dokument är en "torr" sammanfattning av slutsatserna. För fullständigt resonemang, se `my_mem_koncept_logg.md`.

## 1. Mål & Användarnytta

* **Mål:** Bygga ett "Företagsminne" med hög datakvalitet, exponerat via MCP för integration med AI-verktyg.
* **Användarnytta:** Kunskapsbasen är tillgänglig i Claude Desktop, Cursor, eller valfritt AI-verktyg. Ingen egen chatt att underhålla.
* **Nyckelinsikt (2026-01):** MyMemory är **händerna** (kunskapsbas + context assembly), inte hjärnan (reasoning).

## 2. Kärnprinciper

### HARDFAIL > Silent Fallback
Systemet ska misslyckas tydligt istället för att gissa. Inga tysta fallbacks.

### Datakvalitet först
Garbage in, garbage out. Bättre data ger bättre svar oavsett reasoning-logik.

### Schema som SSOT
`graph_schema_template.json` definierar tillåtna nodtyper, relationer och properties.

### Trippel Lagring
* **Assets:** Originalfiler. Heligt – aldrig röra.
* **Lake:** Normaliserad Markdown med YAML-frontmatter.
* **Index:** Vektor (ChromaDB) + Graf (DuckDB).

## 3. Tre-fas Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│ FAS 1: COLLECT & NORMALIZE                                      │
│ DropZone → File Retriever → Assets (UUID-normaliserade)         │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ FAS 2: INGESTION (nya data, per dokument)                       │
│ Transcriber + Ingestion Engine → Lake + Graf + Vektor           │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ FAS 3: DREAMING (hela kunskapsbasen, batch)                     │
│ Dreamer → Entity Resolution, merge/split/rename                 │
└─────────────────────────────────────────────────────────────────┘
```

### Rollfördelning

| Fas | Motor | Fråga den besvarar |
|-----|-------|---------------------|
| **2. Ingestion** | `engines/ingestion_engine.py` | "Vad finns i DETTA dokument?" |
| **3. Dreaming** | `engines/dreamer.py` | "Hur passar allt ihop?" |

**Nyckelprincip:** Ingestion är snabb och självständig. Dreamer är reflekterande.

## 4. MCP-exponering

### 11 verktyg exponerade
**Sökning (9 st):** search_graph_nodes, query_vector_memory, search_by_date_range, search_lake_metadata, get_neighbor_network, get_entity_summary, get_graph_statistics, parse_relative_date, read_document_content

**Validering (2 st):** validate_extraction, extract_and_validate_doc

### Användning
* Alfa-status
* Används dagligen med Claude Desktop
* Kan pluggas in i Cursor, eller valfritt MCP-kompatibelt verktyg

## 5. Dreamer - Förädling

Förädlar på tre platser:
1. **Vektor (ChromaDB):** Semantiska kopplingar
2. **Graf (DuckDB):** Merge, split, rename av noder
3. **Lake:** Uppdatering av node_context och metadata

**Trigger:** Just nu endast vid rebuild. Designfråga att lösa.

## 6. 3-Timestamp-systemet

* `timestamp_ingestion`: När filen indexerades i Lake
* `timestamp_content`: När innehållet faktiskt hände (eller "UNKNOWN")
* `timestamp_updated`: Sätts av Dreamer vid förädling

## 7. Status (Januari 2026)

| Område | Status |
|--------|--------|
| **Datakvalitet** | ~75% klar. Property chain validerad. |
| **MCP-server** | 11 verktyg. Alfa. Fungerande. Testat. |
| **Ingestion** | Fungerar. Metadata-modell definierad i SSOT. |
| **Dreamer** | Fungerar. Trigger-mekanism saknas. |
| **OBJEKT-68** | ✅ Komplett. Tre-fas pipeline, LLM-konsolidering. |

### Senaste framsteg (2026-01-17)
* **OBJEKT-68 (LÖST):** Arkitekturrefaktorering
  - Tre-fas pipeline: Collect → Ingestion → Dreaming
  - `services/engines/` ny katalog för centrala motorer
  - LLM-konsolidering via `LLMService` singleton
  - Svenska funktionsnamn → engelska
* **OBJEKT-63 (LÖST):** E2E property chain test (`tools/test_property_chain.py`)
* Nytt schema: `lake_metadata_template.json` (SSOT för Lake frontmatter)
* MCP-testsvit: `tools/test_mcp_search.py` (27 tester, alla pass)

### Kvarstående
* Dreamer-trigger i produktion (OBJEKT-61)
* Extractor + Critic POC (validera hypotes)

## 8. Teknisk Stack

| Komponent | Teknologi |
|-----------|-----------|
| Språk | Python 3.12 |
| Vektordatabas | ChromaDB |
| Grafdatabas | DuckDB (relationell graf) |
| AI-modeller | Google Gemini (Pro/Flash/Lite) |
| Embeddings | KBLab/sentence-bert-swedish-cased (lokal, 768 dim) |
| MCP | FastMCP |

## 9. Utvecklingsregler

Definierade i `.cursorrules` och `CLAUDE.md`:

1. **HARDFAIL > Silent Fallback** – Inga tysta gissningar
2. **Schema som SSOT** – `graph_schema_template.json` styr ontologin
3. **Inga hårdkodade värden** – Sökvägar, promptar i config
4. **Ingen AI-cringe** – Professionella namn (ej "Trädgårdsmästaren")

---
*Senast uppdaterad: 2026-01-17*
*Se `my_mem_arkitektur.md` för teknisk implementation.*
*Se `my_mem_koncept_logg.md` för resonemang bakom beslut.*
