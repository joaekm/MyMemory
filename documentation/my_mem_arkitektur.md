
# Systemarkitektur (v6.0 - Strukturerad Assets)

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

### MyMem Chat (v5.2 - Full Transparency)

**Pipeline:**
```
Planering (Flash Lite)
    ↓
Jägaren (Search Lake) + Vektorn (ChromaDB)
    ↓
Domaren (Flash Lite - Re-ranking)
    ↓
Syntes (Gemini Pro)
```

**Komponenter:**
- **Jägaren:** Python-baserad "brute force"-scanning av `/Lake` för exakta nyckelord. Löser "Vector Blindness".
- **Domaren:** LLM-baserad filtrering som prioriterar innehåll över format.
- **Syntesen:** Genererar svar med källhänvisning.

**Prestandaproblem (Simulering 2025-12-03):**
- Snitttid per runda: **50.6 sekunder**
- Max tid: **130 sekunder**
- `MAX_CHARS = 100000` → skickar upp till 100k tecken till Gemini Pro
- **Lösning:** OBJEKT-43 (Summary-First Search)

**Konfiguration:**
- `chat_prompts.yaml`: Alla system-instruktioner (Planering, Domare, Syntes).
- **Refresh:** Initierar nya DB-anslutningar vid varje sökning för att se nydata direkt.

### Pipeline v6.0 (Planerad - OBJEKT-46)

**Beslut 2025-12-03:** Ny arkitektur med tydligare separation of concerns.

```
Input → IntentRouter → ContextBuilder → Planner → Synthesizer → Output
             (AI)           (Kod)         (AI)        (AI)
         Klassificera     Hämta data   Bygg rapport   Svara
```

| Komponent | Typ | Ansvar | Fil |
|-----------|-----|--------|-----|
| **IntentRouter** | AI (Flash Lite) | Klassificera intent (FACT/INSPIRATION), parsa tid, upplös kontext | `intent_router.py` |
| **ContextBuilder** | **Kod** | Deterministisk sökning baserad på strategi | `context_builder.py` |
| **Planner** | AI (Flash Lite) | Skapa kurerad rapport från kandidater | `planner.py` |
| **Synthesizer** | AI (Pro) | Generera svar från rapport | (befintlig) |

**Nyckelskillnader mot v5.2:**
- Synthesizer får en **rapport**, inte råa dokument
- ContextBuilder är **deterministisk kod**, inte AI
- Tydlig SOC: Varje komponent har ETT ansvar
- Förberedd för agentic loop (v7.0)

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
  kuzu_db: "~/MyMemory/Index/KuzuDB"
  taxonomy_file: "~/MyMemory/Index/my_mem_taxonomy.json"
```

## 6. Tech Stack & Beroenden

| Kategori | Teknologi |
|----------|-----------|
| **Språk** | Python 3.12 |
| **Vektordatabas** | ChromaDB |
| **Grafdatabas** | KùzuDB (inbäddad) |
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

## 7. Kända Begränsningar (2025-12-03)

Identifierade under första stresstestet:

| Problem | Objekt | Prio |
|---------|--------|------|
| Ingen aggregerad insikt | OBJEKT-41 | 0 (KRITISK) |
| Förstår inte "igår"/"förra veckan" | OBJEKT-42 | 0 (KRITISK) |
| Långsam syntes (50-130s) | OBJEKT-43 | 0.5 |
| Felstavade namn i transkribering | OBJEKT-44 | 1 |
| Insamlingsagenter jobbar i mörkret | OBJEKT-45 | 1 |
