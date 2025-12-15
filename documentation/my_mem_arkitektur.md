
# Systemarkitektur (v8.2 - Pivot or Persevere)

Detta dokument beskriver den tekniska sanningen om systemets implementation, uppdaterad December 2025.

## 1. Huvudprinciper

1. **HARDFAIL > Silent Fallback:** Systemet ska misslyckas tydligt istället för att tyst falla tillbaka. Alla fel rapporteras explicit till användaren. (Se `.cursorrules`)

2. **Split Indexing:** Vi skiljer strikt på Realtid (Vektorsökning) och Batch (Graf/Taxonomi) för att undvika process-låsningar. Konsolidering (Graf) körs i intervaller, Vektorn körs direkt.

3. **OTS-Modellen:** All kunskap struktureras i grafen enligt taxonomin Operativt - Taktiskt - Strategiskt.

4. **Rich Raw Data:** All insamlad data (Ljud, Slack) försedda med en "Rich Header" (tidsstämplar, talare, käll-ID). Garanterar spårbarhet.

5. **Agentic Reasoning:** Chatten är en process som planerar, väljer källa (Graf vs Vektor) och syntetiserar.

6. **Idempotens & Självläkning:** Alla agenter hoppar över filer som redan är klara, men fyller automatiskt i "hål" om filer saknas i nästa led.

7. **Validering & Underhåll:** Vid varje uppstart körs systemvalidering och loggrensning (>24h) automatiskt.

## 2. Datamodell: Trippel Lagring

Systemet använder tre lagringsnivåer för att balansera integritet, prestanda och spårbarhet.

### "Asset Store" (Lagring 1 - Källan)

Strukturerad mappstruktur under `~/MyMemory/Assets/`:

```
Assets/
├── Recordings/     # Ljudfiler från MemoryDrop (m4a, mp3, wav)
├── Transcripts/    # Transkriberade .txt-filer från Transcriber
├── Documents/      # Dokument från MemoryDrop (pdf, docx, txt)
├── Slack/          # Daily digests från Slack Collector
├── Sessions/       # Chat-sessioner med learnings
└── Failed/         # Misslyckade transkriptioner
```

- **Namnstandard:** `[Originalnamn]_[UUID].[ext]`
- **Syfte:** "Sanningen". Här finns rådatan. **Aldrig röra.**
- **Config:** Alla sökvägar i `my_mem_config.yaml` under `paths.asset_*`

### "Lake" (Lagring 2 - Mellanlager)
- **Innehåll:** `.md`-filer med standardiserad YAML-frontmatter (innehållande UUID).
- **Ansvarig:** Skapas uteslutande av DocConverter.
- **Syfte:** Normalisering. Allt är text här. Stabil över tid.

### "Index" (Lagring 3 - Hjärnan)
- **ChromaDB:** Vektorer för semantisk sökning (Textlikhet).
- **KùzuDB:** Graf för entitets-relationer och tidslinjer (Exakthet).
- **taxonomy.json:** Sanningens källa för Masternoder (OTS).
- **Framtid:** Grafen ska lära sig aliases över tid (OBJEKT-44).

## 3. Agent-sviten (Tjänsterna)

Hela systemet orkestreras av `start_services.py` (för realtidstjänster) och manuella/schemalagda anrop (för batch).

### 3.1 Insamling & Logistik

| Agent | Input | Funktion | Output |
|-------|-------|----------|--------|
| **File Retriever** | DropZone | Flyttar filer till Assets, tilldelar UUID | `Recordings/` eller `Documents/` |
| **Slack Collector** | Slack API | "Daily Digest" - en .txt per kanal/dag | `Slack/` |

### 3.2 Bearbetning (The Core)

| Agent | Bevakar | Modell | Output |
|-------|---------|--------|--------|
| **Transcriber** | `Recordings/` | Flash (transkribering) + Pro (analys & QC) | `.txt` i `Transcripts/` |
| **Doc Converter** | `Transcripts/`, `Documents/`, `Slack/`, `Sessions/` | Gemini Flash | `.md` i Lake |

**Transcriber-flöde (v6.0):**
1. Flash transkriberar ljudfil ordagrant
2. Pro gör sanity check (kvalitetskontroll)
3. Pro identifierar talare och formaterar
4. Misslyckade filer flyttas till `Failed/`

### 3.3 Indexering (Delad Arkitektur)

| Agent | Status | Funktion | Syfte |
|-------|--------|----------|-------|
| **Vector Indexer** | Realtid (Watchdog) | Uppdaterar ChromaDB | Snabb sökbarhet |
| **Graph Builder** | Batch (Manuell) | Konsoliderar mot OTS | Struktur & relationer |

## 4. Konsumtion & Gränssnitt

### MyMem Chat - Pipeline v8.2 "Pivot or Persevere"

**Arkitektur:**
```
Input → IntentRouter → ContextBuilder → Planner (ReAct) → Synthesizer → Output
             (AI)           (Kod)           (AI)              (AI)
         Mission Goal    Hämta data     Bygg Tornet         Svara
                         + Reranking    + Bevis
```

**Komponenter:**

| Komponent | Version | Typ | Ansvar | Fil |
|-----------|---------|-----|--------|-----|
| **SessionEngine** | v8.2 | Kod | Orchestrator, session state, Pivot or Persevere | `session_engine.py` |
| **IntentRouter** | v7.0 | AI (Flash Lite) | Skapa Mission Goal, parsa tid, extrahera keywords/entities | `intent_router.py` |
| **ContextBuilder** | v7.5 | Kod | Time-Aware Reranking, parallel Lake+Vektor-sökning | `context_builder.py` |
| **Planner** | v8.2 | AI (Flash Lite) | ReAct-loop, bygger "Tornet" iterativt | `planner.py` |
| **Synthesizer** | - | AI (Pro) | Generera svar från Planner-rapport | `synthesizer.py` |

**Nyckelkoncept:**

- **Tornet (current_synthesis):** Iterativt byggd arbetshypotes. Uppdateras varje ReAct-loop.
- **Bevisen (facts):** Append-only lista med extraherade fakta. Växer monotont.
- **Pivot or Persevere:** SessionEngine skickar befintligt Torn + Facts till ny fråga. Planner avgör om det är relevant.
- **Librarian Loop:** Two-stage retrieval med scan (summary) + deep read (fulltext).
- **Time-Aware Reranking:** `hybrid_score = original_score * (1 + time_boost)` boostar nyare dokument.

**ReAct-loopen (Planner):**
```
1. Evaluate: Läs kandidater, uppdatera Tornet, extrahera Bevis
2. Decide: COMPLETE | SEARCH | ABORT
3. If SEARCH: Kör ny sökning → Librarian Scan → Loop
4. If COMPLETE: Returnera Tornet som rapport
```

**Konfiguration:**
- `chat_prompts.yaml`: Alla system-instruktioner
- `my_mem_config.yaml`: Reranking-parametrar (boost_strength, top_n_fulltext)
- **Refresh:** Nya DB-anslutningar vid varje sökning för realtidsdata

### Launcher (macOS)
- **Fil:** `MyMemory.app/Contents/Resources/Scripts/main.scpt`
- **Funktion:** Orkestrerar start av backend och frontend.
- **Debug Mode:** Argument `--debug` visar tankeprocess.

### Simuleringsverktyg (Nytt)
- **Fil:** `tools/simulate_session.py`
- **Funktion:** Stresstestning med AI-persona (Interrogator + Evaluator).
- **Output:** Utvärderingsrapport + teknisk logg i `logs/`.

## 5. Konfiguration

All styrning sker via konfigurationsfiler:

| Fil | Syfte |
|-----|-------|
| `my_mem_config.yaml` | Sökvägar, API-nycklar, Slack-kanaler, AI-modeller |
| `my_mem_taxonomy.json` | Masternoder (OTS) - 26 huvudnoder |
| `chat_prompts.yaml` | System-prompter för chatten |
| `services_prompts.yaml` | Prompter för insamlingsagenter |
| `.cursorrules` | **Utvecklingsregler** (HARDFAIL, Ingen AI-cringe) |

### Sökvägar i my_mem_config.yaml

```yaml
paths:
  drop_folder: "~/Desktop/MemoryDrop"
  lake_store: "~/MyMemory/Lake"
  asset_store: "~/MyMemory/Assets"
  # Asset sub-folders
  asset_recordings: "~/MyMemory/Assets/Recordings"
  asset_transcripts: "~/MyMemory/Assets/Transcripts"
  asset_documents: "~/MyMemory/Assets/Documents"
  asset_slack: "~/MyMemory/Assets/Slack"
  asset_sessions: "~/MyMemory/Assets/Sessions"
  asset_failed: "~/MyMemory/Assets/Failed"
  # Index
  chroma_db: "~/MyMemory/Index/ChromaDB"
  kuzu_db: "~/MyMemory/Index/KuzuDB"  # Pekar på DuckDB-fil (LÖST-54)
  taxonomy_file: "~/MyMemory/Index/my_mem_taxonomy.json"
```

## 6. Tech Stack & Beroenden

| Kategori | Teknologi |
|----------|-----------|
| **Språk** | Python 3.12 |
| **Vektordatabas** | ChromaDB |
| **Grafdatabas** | DuckDB (relationell graf via nodes/edges) |
| **Parsing** | pandas, pypdf, python-docx |
| **UI** | Rich (CLI) |
| **AI-klient** | google-genai (v1.0+ syntax) |

**AI-Modeller:**
| Uppgift | Modell | Notering |
|---------|--------|----------|
| Planering | Gemini Flash Lite | Låg latens |
| Re-ranking | Gemini Flash Lite | Låg latens |
| Transkribering | Gemini Pro | Hög kvalitet |
| **Syntes** | Gemini Pro | **~70% av svarstiden** |
| Embeddings | all-MiniLM-L6-v2 | Lokal CPU |

## 7. Kända Begränsningar (2025-12-15)

Aktuell status efter Pipeline v8.2:

| Problem | Objekt | Status |
|---------|--------|--------|
| Aggregerad insikt | OBJEKT-41 | ⚠️ Utvärdera - Tornet kan lösa detta |
| Temporal filtering | OBJEKT-42 | ⚠️ Delvis löst - IntentRouter parsar tid, filtering saknas |
| Långsam syntes | LÖST-57 | ✅ Time-Aware Reranking + TOP_N_FULLTEXT |
| Entity Resolution | OBJEKT-44 | ⚠️ Delvis löst - Infrastruktur finns, inlärning saknas |
| Context Injection vid insamling | OBJEKT-45 | ⚠️ Delvis löst - Graf-kontext finns, ej i prompts |
| Entiteter i taxonomin | OBJEKT-51 | ⚠️ Delvis löst - Graf har Entity-noder, taxonomi ej städad |
| Embedding-migration | OBJEKT-47 | ⚠️ DEADLINE 2026-01-14 |
