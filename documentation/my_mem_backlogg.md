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

## EPIC-01: Dreamer - Intelligent Grafförädling

**Status:** PÅGÅENDE
**Startdatum:** 2026-01-15
**Syfte:** Implementera ett komplett system för kontinuerlig förädling av kunskapsgrafen.

### Vision
Dreamer är den "sovande" intelligensen som analyserar hela kunskapsbasen och optimerar för användaren. Till skillnad från Ingestion (som hanterar ny data) arbetar Dreamer med helheten - hittar dubbletter, löser up entiteter, städar brus, och förbättrar datakvaliteten över tid.

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

### Operationer
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

### EPIC-01 Steg 1: Grundinfrastruktur (KLAR)

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

---

### EPIC-01 Steg 2: Entity Resolution (PÅGÅENDE)

#### OBJEKT-65: Extractor + Critic POC (KLAR 2026-01-15)
- ✅ POC genomförd med positiva resultat
- ✅ Dokumenterat i `tools/test_results/poc_extractor_critic_2026-01-15.md`
- *Slutsats:* Extractor + Critic + canonical_name-injection fungerar.

#### OBJEKT-66: Extractor + Critic i Produktion (AKTIV)
*Bakgrund:* POC (OBJEKT-65) visade tydliga förbättringar. Redo för implementation.
*Scope:*
1. Låta `EntityGatekeeper.resolve_entity()` returnera `canonical_name` vid LINK
2. Flytta semantic metadata-generering till EFTER extraktion i `ingestion_engine.py`
3. Injicera kanoniska namn i prompten för `relations_summary`
4. Implementera Critic-steget mellan Extractor och Gatekeeper
*Påverkan:*
- `services/engines/ingestion_engine.py` (pipeline-ordning)
- `services/utils/entity_gatekeeper.py` (canonical_name-retur)
- `config/services_prompts.yaml` (ny Critic-prompt)
*POC-referens:* `tools/poc_extractor_critic.py`

#### OBJEKT-44: Entity Resolution & Alias Learning (AKTIV)
*Status:* Delvis implementerat. EntityGatekeeper finns.
*Kvarstående:*
- Flytande Canonical (swap-mekanism: "Jocke" → "Joakim Ekman")
- LLM-bedömning av trovärdighet
- Dreamer-integration för lärande

---

### EPIC-01 Steg 3: Dreamer Operationer (PÅGÅENDE)

#### OBJEKT-67: Dream Directives (PÅGÅENDE)
*Koncept:* MCP-klienten observerar brus under arbete och förbereder "dreams" för användarbekräftelse.

**Klart (2026-01-17):**
- ✅ Unified ingestion pipeline
- ✅ Borttagen `vector_indexer.py` (redundant)
- ✅ EntityGatekeeper-logik flyttad till `GraphService.find_node_by_name()`
- ✅ `search_graph_nodes` söker i hela properties JSON
- ✅ Dreamer dryrun använder `batch_generate()` för parallella LLM-anrop
- ✅ `validate_rules.py` skärpt
- ✅ POC: Schema-beskrivningar injiceras i `structural_analysis`
- ✅ POC: Kant-validering vid RE-CATEGORIZE
- ✅ POC: Context-pruning simulering vid MERGE

**Kvarstår:**
- [ ] **PRODUKTIONSFIX:** `recategorize_node()` validerar inte kanter efter typbyte
- [ ] **PRODUKTIONSFIX:** `merge_nodes()` anropar inte `prune_context()` efteråt
- [ ] MCP Tools: `report_observation()`, `get_pending_dreams()`, `confirm_dream()`
- [ ] `dream_candidates` tabell i GraphStore
- [ ] User confirmation workflow

#### OBJEKT-70: Relation Discovery & Metadata Enrichment (NY)
*Status:* EJ PÅBÖRJAD
*Problem:* Dreamer städar grafen (MERGE/SPLIT/DELETE) men upptäcker inte NYA relationer eller berikar metadata på befintliga noder.

*Nuläge:*
- `run_resolution_cycle()` hanterar: MERGE, SPLIT, RENAME, DELETE, RE-CATEGORIZE
- `propagate_changes()` uppdaterar Lake-filer som påverkats av ändringar
- **Saknas:** Aktiv upptäckt av relationer som BORDE finnas men inte skapades vid ingestion

*Exempel på vad som saknas:*
1. **Implicit Relation Discovery:**
   - Person A och Person B nämns i samma dokument 5 gånger → borde ha `WORKS_WITH` relation
   - Projekt X nämns tillsammans med Organisation Y i 3 dokument → borde ha `OWNED_BY` relation
2. **Metadata Enrichment:**
   - Nod har `node_context` från 10 dokument men saknar `context_keywords` → LLM kan extrahera
   - Person har många mentions men saknar `role` property → kan infereras från kontext
3. **Cross-Document Inference:**
   - Dokument A säger "Joakim leder projektet", Dokument B säger "Projektledare: J. Ekman" → koppla

*Förslag på implementation:*
```
discover_relations():
    1. Hitta nod-par som ofta co-förekommer (via node_context.origin)
    2. Fråga LLM: "Finns implicit relation mellan A och B?"
    3. Om ja: skapa edge med låg confidence (0.6)
    4. Dreamer kan höja confidence vid nästa körning om mönstret bekräftas

enrich_metadata():
    1. Hitta noder med rik node_context men fattig metadata
    2. Fråga LLM: "Extrahera role, keywords, etc. från kontext"
    3. Uppdatera nod-properties
```

*Relation till andra objekt:*
- Bygger på OBJEKT-67 (Dream Directives) - nya relationer kan vara "dreams" för bekräftelse
- Kompletterar OBJEKT-66 (Extractor + Critic) - fångar vad som missades vid ingestion

*Risk från kant-validering (OBJEKT-67):*
RE-CATEGORIZE använder HARDFAIL vid ogiltiga kanter. Detta kan skapa problem för OBJEKT-70:
- Nya relationer skapade av `discover_relations()` kan blockera framtida RE-CATEGORIZE
- **Lösning krävs:** Antingen "soft delete" av ogiltiga kanter, eller review-queue för manuell hantering
- Alternativt: Nya relationer från OBJEKT-70 skapas med `source: "inferred"` och kan auto-tas bort vid typbyte

---

### EPIC-01 Steg 4: Trigger & Scheduling (EJ PÅBÖRJAD)

#### OBJEKT-61: Dreamer Trigger-mekanism (AKTIV)
*Problem:* Dreamer körs bara vid rebuild. Grafen blir "smutsig" mellan.
*Alternativ:*
1. **Schema:** Kör varje natt (cron/launchd)
2. **Watchdog:** Kör när Lake uppdateras (inotify)
3. **Threshold:** Kör när X nya entiteter skapats
4. **On-demand:** MCP-verktyg som triggar Dreamer
*Status:* Öppen designfråga. Behöver beslut.
*Se:* Konflikt 61 i `my_mem_koncept_logg.md`

---

### EPIC-01 Steg 5: Ingestion-förbättringar (AKTIV)

#### OBJEKT-45: Levande Metadata vid Insamling (AKTIV)
*Status:* Delvis. Graf-kontext injiceras.
*Kvarstående:*
- Extraktion av `dates_mentioned`, `actions`, `deadlines`
- Bättre context injection i Transcriber

#### OBJEKT-62: Transcription Truncation (PÅGÅENDE)
*Problem:* Långa transkriptioner trunkeras (58 MB ljud → 9 KB transkript).
*Rotorsak:* Gemini Pro output-token-gräns (~8k tokens).
*Lösning:* Pass 2 returnerar INTE `transcript`, bara:
- `speaker_map`: {"Talare 1": "Anna"}
- `metadata`: title, summary, keywords, entities
- Python applicerar `speaker_map` på `raw_transcript` från Pass 1

---

### Historik & Lärdomar

**Konflikt 46 (2025-12-03):** Statisk metadata vid insamling ledde till att viktiga fakta missades vid sökning. Insikt: Metadata måste vara "levande" - därav Dreamer.

**Konflikt 42 (2025-11-XX):** Felstavade namn ("Sänk" vs "Cenk Bisgen") behandlades som olika personer. Lösning: Entity Resolution med aliases i grafen.

**Konflikt 54 (2025-12-21):** One-shot classification missade nyanser. Lösning: Multipass Extraction - parallella LLM-anrop per domän.

**Konflikt 56 (2025-12-23):** Automatisk extraktion skapar oundvikligen fel. Lösning: Human-in-the-loop validering (Dream Directives).

---

## Övriga Aktiva Objekt

### Prio 2 - Infrastruktur

*(OBJEKT-68 detaljer finns under EPIC-01 Steg 1 ovan)*

#### OBJEKT-71: Loggningsarkitektur (NY)
*Status:* EJ PÅBÖRJAD
*Problem:* Systemloggen (`my_mem_system.log`) växer snabbt och blir svårhanterlig. Alla tjänster loggar till samma fil.

*Nuläge:*
- En enda loggfil: `~/MyMemory/Logs/my_mem_system.log`
- Alla tjänster (TRANS, VECTOR, SLACK, RETRIEVER, GMAIL, CALENDAR, Dreamer) loggar hit
- Svårt att filtrera och analysera

*Förslag:*
1. **Separata loggfiler per tjänst:**
   - `dreamer.log` - Dreamer-beslut och operationer
   - `ingestion.log` - Fil-bearbetning
   - `mcp.log` - MCP-server-anrop
   - `system.log` - Övergripande systemhändelser
2. **Rotation:** Daglig rotation, behåll 7 dagar
3. **Strukturerad loggning:** JSON-format för maskinell analys

*Prioritet:* Låg (infrastruktur, kan vänta)

#### OBJEKT-72: Robust Testsvit (NY)
*Status:* EJ PÅBÖRJAD
*Syfte:* Skapa en 100% robust testsvit som garanterar dataintegritet och systemstabilitet.

*Bakgrund:*
`test_property_chain.py` visade att tester som returnerar PASS trots att kritiska operationer misslyckades är värdelösa. Testet uppdaterades 2026-01-17 med HARDFAIL-kontroller.

*Principer:*
1. **HARDFAIL på allt kritiskt** - Inga tysta fallbacks, inga "hoppar över" som ger PASS
2. **Validera att operationer faktiskt kördes** - Räkna LLM-anrop, kontrollera confidence > 0
3. **Minsta förväntade resultat** - Om test-input ska producera 3 entiteter, faila vid < 3
4. **Explicit felmeddelanden** - Varje FAIL ska förklara exakt vad som gick fel

*Scope:*
1. **test_property_chain.py** (KLAR 2026-01-17)
   - ✅ MIN_EXPECTED_ENTITIES validering
   - ✅ Prompt-laddning valideras
   - ✅ LLM-anrop räknas och valideras
   - ✅ Alla entiteter måste få svar

2. **test_mcp_search.py** (att granska)
   - [ ] Validera att sökresultat faktiskt returneras
   - [ ] HARDFAIL om MCP-server inte startar
   - [ ] Validera response-struktur

3. **Ny: test_ingestion_e2e.py** (att skapa)
   - [ ] Testa hela ingestion-flödet isolerat
   - [ ] Validera Lake-fil skapas med rätt frontmatter
   - [ ] Validera graf-noder och kanter skapas
   - [ ] Validera vektor-indexering

4. **Ny: test_dreamer_operations.py** (att skapa)
   - [ ] Testa MERGE, SPLIT, RENAME, DELETE, RE-CATEGORIZE isolerat
   - [ ] Mock-data för kontrollerade scenarier
   - [ ] Validera att operationer faktiskt ändrar grafen

5. **CI-integration** (framtida)
   - [ ] Köra tester vid varje commit
   - [ ] Blocka push vid FAIL

*Prioritet:* Medel (testinfrastruktur, men kritisk för kvalitet)

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

*Senast uppdaterad: 2026-01-17*
*Se `my_mem_koncept_logg.md` för resonemang bakom beslut.*
