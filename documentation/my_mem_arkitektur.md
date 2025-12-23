
# Systemarkitektur (v9.5 - The Graph Awakens)

Detta dokument beskriver den tekniska sanningen om systemets implementation, uppdaterad December 2025.

## 1. Huvudprinciper

1. **HARDFAIL > Silent Fallback:** Systemet ska misslyckas tydligt istället för att tyst falla tillbaka. Alla fel rapporteras explicit till användaren. (Se `.cursorrules`)

2. **Split Indexing:** Vi skiljer strikt på Realtid (Vektorsökning) och Batch (Graf/Taxonomi) för att undvika process-låsningar. Konsolidering (Graf) körs i intervaller, Vektorn körs direkt.

3. **OTS-Modellen:** All kunskap struktureras i grafen enligt taxonomin Operativt - Taktiskt - Strategiskt.

4. **Rich Raw Data:** All insamlad data (Ljud, Slack) försedda med en "Rich Header" (tidsstämplar, talare, käll-ID). Garanterar spårbarhet.

5. **Agentic Reasoning:** Chatten är en process som planerar, väljer källa (Graf vs Vektor) och syntetiserar.

6. **Idempotens & Självläkning:** Alla agenter hoppar över filer som redan är klara, men fyller automatiskt i "hål" om filer saknas i nästa led.

7. **Validering & Underhåll:** Vid varje uppstart körs systemvalidering och loggrensning (>24h) automatiskt.

8. **Graph Truth:** Grafen är sanningen för entiteter och relationer. Vektorn är sökvägen till innehållet. Taxonomin är kartan.

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
├── Calendar/       # Daily digests från Calendar Collector (Google)
├── Mail/           # E-post från Gmail Collector
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
- **GraphDB (DuckDB):** 
    - `nodes`: Entiteter och Koncept (Canonical + Aliases).
    - `edges`: Relationer (DEALS_WITH, UNIT_MENTIONS).
    - `evidence`: LLM-genererade observationer från Multipass-analys.
- **taxonomy.json:** Sanningens källa för Masternoder (OTS).

## 3. Agent-sviten (Tjänsterna)

Hela systemet orkestreras av `start_services.py` (för realtidstjänster) och manuella/schemalagda anrop (för batch).

### 3.1 Insamling & Logistik

| Agent | Input | Funktion | Output |
|-------|-------|----------|--------|
| **File Retriever** | DropZone | Flyttar filer till Assets, tilldelar UUID | `Recordings/` eller `Documents/` |
| **Slack Collector** | Slack API | "Daily Digest" - en .txt per kanal/dag | `Slack/` |
| **Calendar Collector** | Google Calendar API | Daily Digest per dag med möten | `Calendar/` |
| **Gmail Collector** | Gmail API (label) | E-post med specifik label som .txt | `Mail/` |

### 3.2 Bearbetning (The Core)

| Agent | Bevakar | Modell | Output |
|-------|---------|--------|--------|
| **Transcriber** | `Recordings/` | Flash (transkribering) + Pro (analys & QC) | `.txt` i `Transcripts/` |
| **Doc Converter** | `Transcripts/`, `Documents/`, `Slack/`, `Sessions/`, `Calendar/`, `Mail/` | Gemini Flash/Lite | `.md` i Lake |

### 3.2.1 Graph-Boosted Transcriber (v8.3)
Transcriber använder nu grafens kunskap för att förbättra kvaliteten:
1. **Context Injection:** Hämtar kända Personer och Alias från GraphDB + dagens Kalender-events.
2. **Canonical Normalization:** Mappar automatiskt namnvarianter ("Jocke") till grafens canonical ("Joakim Ekman").
3. **Alias Learning:** Nya namnvarianter registreras direkt som alias i grafen.

### 3.2.2 Multipass Extraction (v8.3)
DocConverter använder nu en parallelliserad "Multipass"-strategi för djupare analys:
1. **Per-Masternode:** Kör en separat, strikt LLM-pass (Model Lite) för varje relevant masternod.
2. **Evidence Generation:** Varje träff sparas som ett "Evidence Item" i GraphDB (`evidence`-tabellen) med confidence score.
3. **Parallellism:** Använder ThreadPoolExecutor för att köra alla masternoder samtidigt.

### 3.2.3 DocConverter v9.3: Clustered Multipass & Idempotens
DocConverter har genomgått en omfattande refaktorering för prestanda och stabilitet:
1. **Clustered Multipass:** Istället för ett anrop per masternod, grupperas noder i semantiska kluster (`Entities`, `Business`, `Strategy`, `Ops`). Detta minskar API-anropen med ~80% samtidigt som precisionen bibehålls.
2. **Idempotens:** Processen kontrollerar nu explicit om filen redan finns i Lake *innan* bearbetning påbörjas. Detta gör återstart av processer (Rebuild) snabb och säker.
3. **Robust Config:** Config-hanteringen har härdats för att hantera sökvägar och promptar dynamiskt från `services_prompts.yaml`.

### 3.3 Indexering (Delad Arkitektur)

| Agent | Status | Funktion | Syfte |
|-------|--------|----------|-------|
| **Vector Indexer** | Realtid (Watchdog) | Uppdaterar ChromaDB | Snabb sökbarhet |
| **Graph Builder** | Batch (Manuell) | Konsoliderar mot OTS | Struktur & relationer |
| **Dreamer** | Batch (Schemalagd) | Evidence-baserad konsolidering | Taxonomi-vård |

### 3.3.1 Dreamer v9.5: Batch Processing & Human-in-the-Loop
Dreamer har uppgraderats från en enkel loop till en batch-orienterad motor:
1. **Batch Processing:** Använder `consolidate_batch`-prompten för att bearbeta listor av kandidater i ett enda anrop. Detta ökar genomströmningen dramatiskt.
2. **ReviewObject:** Osäkra noder (confidence < 0.9) skickas inte längre automatiskt till grafen. Istället skapas `ReviewObject` som samlas i en kö för manuell granskning via `interactive_review`.
3. **Aggregerad Konfidens:** En ny algoritm beräknar sannolikheten för en entitet baserat på bevis från flera oberoende källor.
4. **Sync:** Synkroniserar taxonomin mot grafens "Canonical Truth" (rensar alias/stale noder).
5. **Backpropagation:** Skriver "Graph Context Summary" tillbaka till Lake-filen för att göra vektorn smartare.

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

## 5. Konfiguration

All styrning sker via konfigurationsfiler:

| Fil | Syfte |
|-----|-------|
| `my_mem_config.yaml` | Sökvägar, API-nycklar, Slack-kanaler, Google OAuth, AI-modeller |
| `my_mem_taxonomy.json` | Masternoder (OTS) + **Multipass Definitions** |
| `chat_prompts.yaml` | System-prompter för chatten |
| `services_prompts.yaml` | Prompter för insamlingsagenter |
| `agent_prompts.yaml` | Prompter för agenter (Chronologist, etc.) |
| `.cursorrules` | **Utvecklingsregler** (HARDFAIL, Ingen AI-cringe) |

### Prompt Management (Princip 7)
Systemet följer strikt principen att **inga prompter får finnas i kod**.
- Alla prompter laddas dynamiskt från YAML-filer vid uppstart.
- Koden validerar att nödvändiga prompter finns (HARDFAIL annars).
- `doc_converter` och `dreamer` använder nu config-drivna prompter för full flexibilitet.

## 6. Tech Stack & Beroenden

| Kategori | Teknologi |
|----------|-----------|
| **Språk** | Python 3.12 |
| **Vektordatabas** | ChromaDB |
| **Grafdatabas** | DuckDB (relationell graf via nodes/edges/evidence) |
| **Parsing** | pandas, pymupdf, python-docx |
| **UI** | Rich (CLI) |
| **AI-klient** | google-genai (v1.0+ syntax) |

**AI-Modeller:**
| Uppgift | Modell | Notering |
|---------|--------|----------|
| Planering | Gemini Flash Lite | Låg latens |
| Re-ranking | Gemini Flash Lite | Låg latens |
| Multipass | Gemini Flash Lite | Massiv parallellism |
| Transkribering | Gemini Pro | Hög kvalitet |
| **Syntes** | Gemini Pro | **~70% av svarstiden** |
| Embeddings | all-MiniLM-L6-v2 | Lokal CPU |

## 7. Rebuild & Underhåll

### tool_staged_rebuild.py (v2.1 - Phase-Aware)
Huvudverktyg för systemåterställning och migrering. Stöder **fas-baserad rebuild** för optimal datakvalitet:

**Två faser:**
1. **Foundation Phase (`--phase foundation`):** Bygger grunden från textkällor (Slack, Documents, Mail, Calendar). Transcriber är **exkluderad** för att säkerställa att grafen byggs på ren textdata först.
2. **Enrichment Phase (`--phase enrichment`):** Bearbetar ljud/transkript med kontext från grunden. Transcriber är **aktiverad** och använder grafens etablerade entiteter och alias för bättre kvalitet.

**Manifest-baserad spårning:**
- `.rebuild_manifest.json` spårar progress per UUID och fas.
- Systemet hoppar automatiskt över redan processerade filer.
- Stödjer återupptagning efter avbrott.

**Användning:**
```bash
python tools/tool_staged_rebuild.py --confirm --phase foundation
python tools/tool_staged_rebuild.py --confirm --phase enrichment --multipass
```

**Legacy-lägen:**
- **Taxonomy Only:** Snabbare ombyggnad av enbart taxonomin från "Trusted Sources" (exkluderar transcripts). Använder Multipass för att bygga en ren struktur.

### tool_hard_reset.py
Nollställer systemet men **bevarar** `taxonomy.json` via en template-strategi (`config/taxonomy_template.json`) för att inte tappa definitioner. Raderar även `.rebuild_manifest.json`.

### Interactive Review (`services/review/interactive_review.py`)
Ett interaktivt CLI-verktyg för Human-in-the-Loop validering av entiteter.
- Låter användaren godkänna, justera eller avvisa nya entiteter.
- Stödjer namnbyten, omflyttning till annan masternod, alias-koppling och relationsskapande.
- Sparar beslut som "Validation Rules" i grafen för att systemet ska minnas och inte fråga igen.
- Används både fristående och integrerat i Rebuild-processen.

### Process Manager (`tools/rebuild/process_manager.py`)
Orkestrerar uppstart och övervakning av bakgrundstjänster under rebuild.
- Startar tjänster (File Retriever, Transcriber, Doc Converter, Vector Indexer).
- Övervakar progress via Lake och Failed-mappar.
- Hanterar timeouts och process-städning.
