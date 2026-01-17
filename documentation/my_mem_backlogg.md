---
unit_id: c1a011a0-c0b7-4a8a-9b4a-5f6a9c7d0003
owner_id: "joakim.ekman"
access_level: "Nivå_3_Delad_Organisation"
context_id: "PROJEKT_DFM_V1"
source_type: "System_Dokument"
source_ref: "dfm_backlog.md"
data_format: "text/markdown"
timestamp_created: "2025-11-23T17:45:00Z"
policy_tags: []
original_binary_ref: null
---

# Projekt-Backlog

Detta dokument spårar vårt aktiva arbete. Uppdaterad 2026-01-17 (arkitekturanalys OBJEKT-68).

## Statusförklaring

| Status | Betydelse |
|--------|-----------|
| **LÖST** | Implementerat och verifierat |
| **OBSOLET** | Inte längre relevant pga arkitekturändring |
| **AKTIV** | Fortfarande relevant, ej påbörjad |
| **PÅGÅENDE** | Under arbete |

---

## Lösta Objekt (Historik)

* **LÖST-1 till LÖST-10:** (Se tidigare koncept-dokumentation).
* **LÖST-11:** Implementera **Centraliserad Loggning**.
* **LÖST-12:** Implementera **Robust Transformator** (Pivot till `pypdf`/`python-docx`).
* **LÖST-13:** Funktionellt verifiera **Desktop-agentens** Insamlar-flöde (fil-dump).
* **LÖST-14:** Implementera **Konfigurationsdrivna Sökvägar**.
* **LÖST-15:** Implementera **Metadata-berikning ("Enricher")** i Desktop-agenten med Google GenAI.
* **LÖST-16:** Implementera **Transformations-agenten** (Transcriber v2.8) med loop-fix och datum-fix.
* **LÖST-17:** Implementera **Indexeraren** (v2.0) med Kùzu-schema och robust felhantering.
* **LÖST-18:** Etablera **"Traffic Control"** (Portvakts-logik) för filhantering.
* **LÖST-19:** Implementera **"The Functional Trinity"** (Retriever, DocConverter, Transcriber).
* **LÖST-20:** Implementera **Slack Daily Archiver** (Daily Digest).
* **LÖST-21:** Implementera **"Dual Model Architecture"** i chatten.
* **LÖST-22:** Implementera **Context Injection** (Tid & Bio).
* **LÖST-23:** Implementera **"Rich Headers"** för text-mellanlagring.
* **LÖST-24:** Implementera **Rich UI** och Mac Launcher (v2.9 "The Overwriter").
* **LÖST-25:** Genomföra **Systemvalidering** (verify_system.py).
* **LÖST-26:** Fixa **Timestamp-diskrepans** (Timezone Awareness).

## Lösta Objekt (Hjärnan 3.0 / OTS-Fasen)

* **LÖST-28:** Implementera **Chat Refresh** (Realtime Memory).
* **LÖST-31:** Integrera **Kùzu (Graf)** i Chatten (Hybrid Search).
* **LÖST-4:** Implementera **Konsoliderings-modulen** (Taxonomi).
* **LÖST-34:** Implementera **Split Indexing**.
* **LÖST-35:** Implementera **Taxonomi-definitioner**.
* **LÖST-37:** Implementera **Hybrid Search v2 ("The Hunter")**.
* **LÖST-38:** Implementera **Re-ranking ("The Judge")**.
* **LÖST-39:** Implementera **YAML-baserade Prompter**.

## Lösta Objekt (MCP-pivot)

* **LÖST-50:** Implementera **DateService** (Central Datumhantering).
    * *Lösning:* `services/utils/date_service.py` med prioritet: Frontmatter → Filnamn → PDF-metadata → Filesystem.
* **LÖST-54:** Migrera från **KùzuDB till DuckDB**.
    * *Lösning:* `GraphStore`-klass med relationell graf-modell (nodes/edges tabeller).
* **LÖST-36:** Implementera **Kalender-ingestion**.
    * *Lösning:* Gmail/Calendar collectors implementerade.

---

## Obsoleta Objekt (pga MCP-pivot)

Dessa objekt är inte längre relevanta efter pivoten från egen chatt till MCP-exponering (Januari 2026). Se Konflikt 57 i `my_mem_koncept_logg.md`.

* **OBSOLET-46:** ~~Implementera **Pipeline v6.0**~~ (Refaktorering).
    * *Orsak:* Chat-pipeline övergiven. MCP-server ersätter egen reasoning.
    * *Ursprunglig plan:* IntentRouter → ContextBuilder → Planner → Synthesizer

* **OBSOLET-41:** ~~Implementera **"Aggregerad Insikt"**~~ ("The Inverted T").
    * *Orsak:* Chatt-fokuserad. AI-verktyget (Claude Desktop) hanterar nu insikt-generering.

* **OBSOLET-42:** ~~Implementera **"Temporal Intelligence"**~~.
    * *Orsak:* Delvis löst via `parse_relative_date` i MCP. Resten är AI-verktygets ansvar.

* **OBSOLET-43:** ~~Implementera **"Summary-First Search"**~~.
    * *Orsak:* Chatt-prestanda-optimering. Irrelevant utan egen chatt.

* **OBSOLET-49:** ~~Implementera **"MyMemory Engine"**~~ (API-separation).
    * *Orsak:* Övergiven. MCP-server är nu API-lagret.

* **OBSOLET-48:** ~~Implementera **"Sessioner som Lärdomar"**~~.
    * *Orsak:* SessionEngine borttagen. AI-verktyget äger sessionen. Se Konflikt 60.

* **OBSOLET-58:** ~~Implementera **Usage Tracking i Planner**~~.
    * *Orsak:* Planner borttagen med chat-pipeline.

* **OBSOLET-32:** ~~Implementera **"Quick Save"**~~ (Read/Write) i Chatten.
    * *Orsak:* Ingen egen chatt. Kan göras via MCP om behov uppstår.

* **OBSOLET-47:** ~~Migrera till **gemini-embedding-001**~~.
    * *Orsak:* Baserat på felaktig premiss. Systemet använder `paraphrase-multilingual-MiniLM-L12-v2` (lokal SentenceTransformer), INTE Googles `text-embedding-004`. Ingen Google-embedding används. Identifierad och städad 2026-01-15.

---

## EPIC-01: Dreamer - Intelligent Grafförädling (KLAR 2026-01-17)

**Status:** KLAR
**Startdatum:** 2026-01-15
**Slutdatum:** 2026-01-17
**Syfte:** Implementera grundsystemet för kontinuerlig förädling av kunskapsgrafen.

### Vision
Dreamer är den "sovande" intelligensen som analyserar hela kunskapsbasen och optimerar för användaren. Till skillnad från Ingestion (som hanterar ny data) arbetar Dreamer med helheten - hittar dubbletter, löser upp entiteter, städar brus, och förbättrar datakvaliteten över tid.

### Arkitektur (beslutad 2026-01-17)
```
┌─────────────────────────────────────────────────────────────────┐
│ FAS 3: DREAMING                                                 │
│ Ansvar: Förädla ALL data som helhet                            │
│ Perspektiv: Helheten (optimera för användaren)                 │
├─────────────────────────────────────────────────────────────────┤
│ engines/                                                        │
│   └── dreamer.py             # Batch-förädling                 │
│       ├── scan_candidates()                                     │
│       ├── structural_analysis()  # SPLIT/RENAME/DELETE         │
│       ├── entity_resolution()    # MERGE                       │
│       └── propagate_changes()    # Uppdatera Lake/Vektor       │
└─────────────────────────────────────────────────────────────────┘
```

### Operationer (implementerade)
| Operation | Beskrivning | Trigger |
|-----------|-------------|---------|
| **MERGE** | Slå ihop dubbletter | Semantisk likhet > 90% |
| **SPLIT** | Dela upp blandade entiteter | Motstridiga kontext-bevis |
| **RENAME** | Uppgradera till kanoniskt namn | Bättre namnform hittad |
| **DELETE** | Ta bort isolerade brus-noder | Låg konfidens, inga relationer |
| **RE-CATEGORIZE** | Ändra nodtyp | Bevis tyder på felaktig typ |

### POC-resultat (2026-01-15)
**OBJEKT-65: Extractor + Critic Pattern**
- 72% färre nya noder (47 → 13) - undviker dubbletter
- 40% brusreduktion via Critic
- 0 missade normaliseringar (baseline hade 3)
- 20% rikare metadata i summaries
- *Testresultat:* `tools/test_results/poc_extractor_critic_2026-01-15.md`

### POC-verktyg
**`tools/tool_dreamer_dryrun.py`** - Teknisk ritning för produktionsimplementation
- Kör Dreamer-logiken utan att skriva till grafen
- Loggar alla beslut för analys
- Testar tröskelvärden och spärrar
- **Utökningar (2026-01-17):**
  - Schema-beskrivningar injiceras i `structural_analysis`
  - Kant-validering vid RE-CATEGORIZE via SchemaValidator
  - Context-pruning simulering vid MERGE

---

### EPIC-01 Leverabler (alla KLARA)

#### OBJEKT-68: Arkitekturanalys (KLAR 2026-01-17)
- ✅ Tre-fas pipeline definierad (Collect → Ingest → Dream)
- ✅ `services/engines/` katalog skapad
- ✅ `dreamer.py` flyttad från `agents/` till `engines/`
- ✅ `ingestion_engine.py` skapad (fd. `doc_converter.py`)
- ✅ LLM-anrop konsoliderade till `LLMService`
- ✅ Svenska → engelska funktionsnamn
- *Se:* Konflikt 62 i `my_mem_koncept_logg.md`

#### OBJEKT-63: Metadata-testkedja (KLAR 2026-01-15)
- ✅ E2E-test: `tools/test_property_chain.py`
- ✅ Validerar hela kedjan: Schema → DocConverter → Lake → Vector → Graf
- ✅ HARDFAIL vid brutna kedjor
- ✅ `include_in_vector`-flagga i scheman

#### OBJEKT-69: Generell Typvalidering (KLAR 2026-01-17)
- ✅ `item_schema` i `graph_schema_template.json` för `node_context`
- ✅ `_validate_type()` i SchemaValidator
- ✅ `normalize_value()` för typnormalisering vid ingestion
- ✅ 5 felaktiga test-noder raderade
- *Lärdom:* LLM kan returnera `text` som lista - måste normaliseras explicit.

#### OBJEKT-64: Config-Driven Refaktorering (KLAR 2026-01-15)
- ✅ Nya config-sektioner: `search`, `collectors`, `validation`, `dreamer.thresholds`
- ✅ Inga hårdkodade värden i Python-kod

#### OBJEKT-65: Extractor + Critic POC (KLAR 2026-01-15)
- ✅ POC genomförd med positiva resultat
- ✅ Dokumenterat i `tools/test_results/poc_extractor_critic_2026-01-15.md`
- *Slutsats:* Extractor + Critic + canonical_name-injection fungerar.

#### OBJEKT-66: Extractor + Critic i Produktion (KLAR 2026-01-17)
- ✅ `critic_filter_entities()` - LLM-baserad filtrering av extraherade entiteter
- ✅ `resolve_entities()` returnerar `canonical_name` vid LINK (hämtas från graf)
- ✅ Semantic metadata genereras EFTER entity extraction (pipeline-ordning ändrad)
- ✅ Kanoniska namn injiceras i `generate_semantic_metadata()` prompten
- ✅ Ny prompt `entity_critic` i `config/services_prompts.yaml`

**Pipeline-flöde:**
```
extract_text → extract_entities_mcp → critic_filter_entities → resolve_entities → generate_semantic_metadata → write_*
```

#### OBJEKT-67: Dreamer Core Operations (KLAR 2026-01-17)
- ✅ Unified ingestion pipeline
- ✅ Borttagen `vector_indexer.py` (redundant)
- ✅ EntityGatekeeper-logik flyttad till `GraphService.find_node_by_name()`
- ✅ `search_graph_nodes` söker i hela properties JSON
- ✅ Dreamer dryrun använder `batch_generate()` för parallella LLM-anrop
- ✅ `validate_rules.py` skärpt
- ✅ Schema-beskrivningar injiceras i `structural_analysis`
- ✅ Kant-validering vid RE-CATEGORIZE
- ✅ Context-pruning efter MERGE

---

### Historik & Lärdomar (EPIC-01)

**Konflikt 46 (2025-12-03):** Statisk metadata vid insamling ledde till att viktiga fakta missades vid sökning. Insikt: Metadata måste vara "levande" - därav Dreamer.

**Konflikt 42 (2025-11-XX):** Felstavade namn ("Sänk" vs "Cenk Bisgen") behandlades som olika personer. Lösning: Entity Resolution med aliases i grafen.

**Konflikt 54 (2025-12-21):** One-shot classification missade nyanser. Lösning: Multipass Extraction - parallella LLM-anrop per domän.

**Konflikt 56 (2025-12-23):** Automatisk extraktion skapar oundvikligen fel. Lösning: Human-in-the-loop validering (Dream Directives).

---

## Aktiva Objekt

### Prio 1 - Rebuild & Infrastruktur

#### OBJEKT-73: Rebuild-process Refaktorering (KLAR 2026-01-17)
*Status:* ✅ Implementerat
*Prioritet:* HÖG
*Bakgrund:* Efter EPIC-01 har ingestion-pipelinen genomgått omfattande förbättringar. Rebuild-processen måste synkroniseras.

*Scope:*
1. ✅ Säkerställ att rebuild använder exakt samma pipeline som realtids-ingestion
2. ✅ Integrera Dreamer-faser i staged rebuild
3. ✅ Lägg till validering och dokumentation

*Implementation:*
- **shared_lock.py** - Process-säker låsning med `fcntl.flock()` för koordinering mellan processer
- **orchestrator.py** - Använder nu `ingestion_engine.process_document()` direkt istället för watchdog
- **process_manager.py** - `ServiceManager` borttagen (obsolet), `CompletionWatcher` kvar
- **dreamer_daemon.py** - Tar lås innan Dreamer-cykel
- **ingestion_engine.py** - `resource_lock` per dokument vid realtids-ingestion, `_lock_held` parameter för rebuild

*Låsningsarkitektur (Alternativ B - anroparen tar lås):*
| Komponent | Lås | Scope |
|-----------|-----|-------|
| Rebuild (orchestrator) | `graph` + `vector` | Per dag |
| Dreamer | `graph` + `vector` | Hela cykeln |
| Ingestion (realtid) | `graph` + `vector` | Per dokument |
| MCP-sökningar | shared | Per query |

*Stresstest:* `tools/test_shared_lock_stress.py` - 400 simultana skrivningar utan datakorruption

*Relation:* Bygger på OBJEKT-66, relaterat till OBJEKT-72

---

### Prio 2 - Dreamer Utökningar (fd. EPIC-01 icke-påbörjade)

#### OBJEKT-74: Dream Directives - MCP Integration (NY, fd. OBJEKT-67 kvarstående)
*Status:* EJ PÅBÖRJAD
*Prioritet:* MEDEL
*Bakgrund:* Kvarstående arbete från OBJEKT-67. Core Dreamer operations är klara, men användar-interaktion saknas.

*Scope:*
- [ ] MCP Tools: `report_observation()`, `get_pending_dreams()`, `confirm_dream()`
- [ ] `dream_candidates` tabell i GraphStore
- [ ] User confirmation workflow

*Koncept:* MCP-klienten observerar brus under arbete och förbereder "dreams" för användarbekräftelse.

#### OBJEKT-75: Relation Discovery & Metadata Enrichment (NY, fd. OBJEKT-70)
*Status:* EJ PÅBÖRJAD
*Prioritet:* LÅG
*Problem:* Dreamer städar grafen men upptäcker inte NYA relationer eller berikar metadata.

*Scope:*
1. **Implicit Relation Discovery:** Hitta nod-par som co-förekommer ofta → skapa edge
2. **Metadata Enrichment:** Extrahera role, keywords från rik node_context
3. **Cross-Document Inference:** Koppla ihop information från flera dokument

*Risk:* Nya relationer kan blockera RE-CATEGORIZE. Lösning: `source: "inferred"` för auto-borttagning.

*Konceptuellt problem (2026-01-17):* RE-CATEGORIZE kan vilja ändra en nod till en typ som inte är tillåten som target för MENTIONS (t.ex. Business_relation). Detta blockeras korrekt av kant-valideringen, men indikerar att:
1. MENTIONS target_type-listan kanske behöver utökas, ELLER
2. Dreamer föreslår fel typ (prompten bör förtydligas), ELLER
3. Noden borde inte ha MENTIONS-kant alls (skapades felaktigt vid ingestion)

#### OBJEKT-76: Dreamer Trigger-mekanism (KLAR 2026-01-17, fd. OBJEKT-61)
*Status:* ✅ Implementerat
*Prioritet:* LÅG
*Problem:* Dreamer körs bara vid rebuild. Grafen blir "smutsig" mellan körningar.

*Beslutad design:*
- **Mekanism:** Threshold-baserad triggning
- **Räknare:** JSON-fil (`~/MyMemory/Index/.dreamer_state.json`)
- **Trigger:** Separat daemon (`dreamer_daemon.py`) som pollar räknaren
- **Threshold:** Konfigurerbart i `my_mem_config.yaml` (default ~15 noder)
- **Fallback:** Max 24h sedan senaste körning
- **Körning:** Launchd - förbereder för framtida menubar-app

*Implementation (KLAR 2026-01-17):*
1. [x] Lägg till threshold-config i `my_mem_config.yaml`
2. [x] Skapa `dreamer_daemon.py` med poll-loop
3. [x] Uppdatera ingestion att öka räknare vid nya graf-noder
4. [x] Skapa `com.mymemory.dreamer.plist` för launchd

*Filer:*
- `config/my_mem_config.yaml` - daemon-sektion under `dreamer:`
- `services/engines/dreamer_daemon.py` - daemon med `--status`, `--once` flaggor
- `config/launchd/com.mymemory.dreamer.plist` - launchd-konfiguration

*Installation:*
```bash
# Installera daemon
cp config/launchd/com.mymemory.dreamer.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.mymemory.dreamer.plist

# Kolla status
python services/engines/dreamer_daemon.py --status
```

*Se:* Konflikt 61 i `my_mem_koncept_logg.md`

#### OBJEKT-77: Dreamer Batch LLM-anrop (KLAR 2026-01-17)
*Status:* ✅ Implementerat
*Prioritet:* MEDEL
*Problem:* Dreamer körde LLM-anrop sekventiellt - långsamt vid många kandidater.

*Implementation:*
- `batch_structural_analysis()` - Kör N prompts parallellt via `batch_generate()`
- `batch_evaluate_merges()` - Kör M merge-par parallellt via `batch_generate()`
- `run_resolution_cycle()` refaktorerad till 3 faser:
  1. **Fas 1:** Batch structural analysis
  2. **Fas 2:** Batch merge evaluation
  3. **Fas 3:** Causal semantic update

*Prestandavinst:*
- Idag (sekventiellt): 50 kandidater × ~2s = **~100 sekunder**
- Med batch (30 parallella): ~4 omgångar × 2s = **~8 sekunder**

*Relation:* Använder `LLMService.batch_generate()` (AdaptiveThrottler, max 30 workers)

#### OBJEKT-44: Entity Resolution & Alias Learning (AKTIV)
*Status:* Delvis implementerat (EntityGatekeeper finns)
*Prioritet:* MEDEL
*Kvarstående:*
- Flytande Canonical (swap-mekanism: "Jocke" → "Joakim Ekman")
- LLM-bedömning av trovärdighet
- Dreamer-integration för lärande

---

### Prio 3 - Ingestion-förbättringar

#### OBJEKT-45: Levande Metadata vid Insamling (AKTIV)
*Status:* Delvis klar (graf-kontext injiceras)
*Prioritet:* MEDEL
*Kvarstående:*
- Extraktion av `dates_mentioned`, `actions`, `deadlines`
- Bättre context injection i Transcriber

#### OBJEKT-62: Transcription Truncation (KLAR 2026-01-17)
*Status:* ✅ Implementerat
*Prioritet:* MEDEL
*Problem:* Långa transkriptioner trunkerades (58 MB ljud → 9 KB transkript).
*Rotorsak:* Gemini Pro output-token-gräns (~8k tokens).

*Lösning (implementerad i transcriber.py):*
- Pass 1: Hämtar `raw_transcript` (full text via Flash-modell)
- Pass 2: Returnerar INTE full transcript, bara `speaker_map` + metadata
- Python applicerar `speaker_map` på `raw_transcript` (rad 442-448)

---

### Prio 4 - Test & Kvalitet

#### OBJEKT-72: Robust Testsvit (NY)
*Status:* Delvis klar
*Prioritet:* MEDEL

*Principer:*
1. **HARDFAIL på allt kritiskt** - Inga tysta fel
2. **Validera att operationer faktiskt kördes** - Inte bara "ingen exception"
3. **Minsta förväntade resultat** - Assertions på konkreta värden
4. **Explicit felmeddelanden** - Tydlig orsak vid failure
5. **Referentiell integritet** - Kanter måste peka på existerande noder

*Testsvit-struktur:*

| Test | Syfte | Status |
|------|-------|--------|
| **test_property_chain.py** | Schema → DocConverter → Lake → Vector → Graf | ✅ KLAR |
| **test_shared_lock_stress.py** | Process-säker låsning (400 simultana skrivningar) | ✅ KLAR |
| **test_mcp_search.py** | MCP-verktyg returnerar korrekta resultat | Att granska |
| **test_graph_integrity.py** | Graf-konsistens och referentiell integritet | Att skapa |
| **test_ingestion_e2e.py** | End-to-end ingestion med alla steg | Att skapa |
| **test_dreamer_operations.py** | Dreamer MERGE/SPLIT/RENAME/RE-CATEGORIZE | Att skapa |

*Identifierade testfall för test_graph_integrity.py:*
1. **MENTIONS-kanter har giltig source:** Alla MENTIONS-kanter måste ha source som är en Document-nod
2. **Alla kanter pekar på existerande noder:** Ingen kant får ha source/target som inte finns i nodes-tabellen
3. **Nodtyper följer schema:** Alla noder har typ som finns i graph_schema_template.json
4. **Kanttyper följer schema:** Alla kanter har typ som finns i schemat med giltiga source/target-typer
5. **Inga orphan-noder:** Noder utan kanter flaggas (varning, inte fel)

*Lärdomar (lägg till nya här):*
- 2026-01-17: MENTIONS-kanter skapades utan att Document-nod existerade för source. Dreamer såg "Unknown" typ och blockerade RE-CATEGORIZE. **Fångat av:** Skulle fångats av test 1-2 ovan.

#### OBJEKT-71: Loggningsarkitektur (NY)
*Status:* EJ PÅBÖRJAD
*Prioritet:* LÅG
*Problem:* Alla tjänster loggar till samma fil.

*Förslag:* Separata loggfiler per tjänst, rotation, strukturerad loggning.

---

## Parkerade Objekt

Dessa objekt är fortfarande potentiellt relevanta men inte prioriterade.


* **OBJEKT-40 (PARKERAD):** Harvest Integration.
    * *Relevans:* Tidrapporteringsdata kan berika minnet.

* **OBJEKT-37 (PARKERAD):** "The Bio-Graph".
    * *Relevans:* Kan vara relevant för MCP-kontext. Användarprofil.

* **OBJEKT-38 (PARKERAD):** "Weekly Intelligence Agent".
    * *Relevans:* Kan implementeras som MCP-verktyg eller extern tjänst.

* **OBJEKT-27 (PARKERAD):** Installer Bundle.
    * *Relevans:* Distribution. Aktuellt när systemet är mer moget.

* **OBJEKT-25 (PARKERAD):** Retention Policy.
    * *Relevans:* Datahantering. Blir aktuellt vid större datamängder.

---

*Senast uppdaterad: 2026-01-17 (OBJEKT-77 Dreamer Batch LLM-anrop KLAR)*
*Se `my_mem_koncept_logg.md` för resonemang bakom beslut.*
