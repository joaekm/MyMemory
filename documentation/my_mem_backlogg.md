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

## Aktiva Objekt

### Prio 1 - Datakvalitet

* **OBJEKT-65 (LÖST - POC):** **Extractor + Critic Pattern** för entity-extraktion.
    * *Status:* POC genomförd 2026-01-15. Resultat positiva.
    * *Testresultat:* Se `tools/test_results/poc_extractor_critic_2026-01-15.md`
    * *Sammanfattning:*
        - 72% färre nya noder (47 → 13) - undviker dubbletter
        - 40% brusreduktion via Critic
        - 0 missade normaliseringar (baseline hade 3)
        - 20% rikare metadata i summaries
    * *Slutsats:* POC bekräftar att Extractor + Critic + canonical_name-injection fungerar.
    * *Nästa steg:* Se OBJEKT-66 för implementation.

* **OBJEKT-66 (AKTIV):** Implementera **Extractor + Critic Pipeline** i produktion.
    * *Bakgrund:* POC (OBJEKT-65) visade tydliga förbättringar. Redo för implementation.
    * *Scope:*
        1. Låta `EntityGatekeeper.resolve_entity()` returnera `canonical_name` vid LINK
        2. Flytta semantic metadata-generering till EFTER extraktion i `doc_converter.py`
        3. Injicera kanoniska namn i prompten för `relations_summary`
        4. Implementera Critic-steget mellan Extractor och Gatekeeper
    * *Påverkan:*
        - `services/processors/doc_converter.py` (pipeline-ordning)
        - `services/utils/entity_gatekeeper.py` (canonical_name-retur)
        - `config/services_prompts.yaml` (ny Critic-prompt, uppdaterad semantic-prompt)
    * *Förväntad effekt:*
        - Färre dubbletter i grafen
        - Konsistenta namn i Lake metadata
        - Renare graf med mindre brus
    * *POC-referens:* `tools/poc_extractor_critic.py`, `tools/test_results/poc_extractor_critic_2026-01-15.md`

* **OBJEKT-62 (PÅGÅENDE):** Fixa **Transcription Truncation**.
    * *Problem:* Långa transkriptioner trunkeras i Lake-filer.
    * *Rotorsak:* Gemini Pro (Pass 2) har output-token-gräns (~8k tokens). Prompten ber om hela transkriptet i JSON-fältet `"transcript"`, vilket kapas vid långa möten.
    * *Bevis:* Filer som `Inspelning_20251212_1400` (58 MB ljud → 9 KB transkript) slutar mitt i en mening.
    * *Lösning:* Ändra Pass 2 prompt så den INTE returnerar `transcript`. Returnera istället:
        - `speaker_map`: {"Talare 1": "Anna", "Talare 2": "Erik"}
        - `metadata`: title, summary, location, keywords, entities
        - Python applicerar `speaker_map` på `raw_transcript` från Pass 1 (Flash)
    * *Påverkan:* `services/processors/transcriber.py`, `config/services_prompts.yaml`

* **OBJEKT-63 (LÖST):** Implementera **Rigorös Metadata-testkedja**.
    * *Lösning:* E2E-regressionstest implementerat i `tools/test_property_chain.py`.
    * *Detaljer:*
        - Testar hela kedjan: DocConverter → Lake → VectorIndexer → Dreamer → Graf
        - Skapar testfil, kör pipeline, validerar properties i varje steg
        - HARDFAIL vid brutna kedjor, okända properties, eller saknade required fields
        - `include_in_vector`-flagga i scheman styr vilken metadata som indexeras
        - Nya schema: `config/lake_metadata_template.json` (SSOT för Lake frontmatter)
        - Uppdaterat: `config/graph_schema_template.json` med `include_in_vector`-flaggor
    * *Användning:* `python tools/test_property_chain.py` (kör fullständigt test), `--dry-run` (visa schema), `--keep` (behåll testdata)

* **OBJEKT-69 (LÖST 2026-01-17):** Implementera **Generell Typvalidering** i SchemaValidator.
    * *Problem:* `node_context[].text` sparades ibland som lista istället för sträng, vilket kraschade `vector_service.py` och `index_search_mcp.py` vid `' | '.join()`.
    * *Rotorsak:* SchemaValidator saknade typvalidering - bara required och enum validerades.
    * *Lösning:*
        - Lade till `item_schema` i `graph_schema_template.json` för `node_context` med explicit typning `{text: string, origin: string}`
        - Ny metod `_validate_type()` i SchemaValidator för djup typvalidering inkl. nästlade strukturer
        - Ny funktion `normalize_value()` för typnormalisering vid ingestion
        - Normalisering av `node_context` i `ingestion_engine.py` (LLM kan returnera lista)
        - Strikt typvalidering i `test_property_chain.py` - FAILA om `node_context[].text` inte är sträng
    * *Påverkade filer:*
        - `config/graph_schema_template.json`
        - `services/utils/schema_validator.py`
        - `services/engines/ingestion_engine.py`
        - `tools/test_property_chain.py`
    * *Städning:* 5 felaktiga test-noder raderades från grafen.

* **OBJEKT-44 (AKTIV):** Implementera **"Entity Resolution & Alias Learning"**.
    * *Status:* Delvis implementerat. EntityGatekeeper finns. Alias-learning saknas.
    * *Kvarstående:*
        - Flytande Canonical (swap-mekanism)
        - LLM-bedömning av trovärdighet
        - Dreamer-integration för lärande

* **OBJEKT-45 (AKTIV):** Implementera **"Levande Metadata vid Insamling"**.
    * *Status:* Delvis. Graf-kontext injiceras. Rikare extraktion saknas.
    * *Kvarstående:*
        - Extraktion av `dates_mentioned`, `actions`, `deadlines`
        - Bättre context injection i Transcriber

### Prio 2 - Infrastruktur

* **OBJEKT-64 (LÖST):** Fullständig **Config-Driven Refaktorering**.
    * *Status:* Alla violations fixade 2026-01-15.
    * *Lösning:* Nya config-sektioner: `search`, `collectors`, `validation`, utökad `processing`.
    * *Fixade filer:*
        - `index_search_mcp.py`: Använder nu `SEARCH_CONFIG`
        - `slack_collector.py`: Använder nu `collectors.slack.page_size`
        - `doc_converter.py`: Använder nu `processing.summary_max_chars`, `header_scan_chars`
        - `date_service.py`: Använder nu `validation.min_year`
        - `validator_mcp.py`: Använder nu `get_model_lite()`
        - `dreamer.py`: Använder nu `dreamer.thresholds`
        - `tool_validate_system.py`: Använder nu `VectorService`
        - `export_graph_to_obsidian.py`: Använder nu relativa sökvägar

* **OBJEKT-61 (AKTIV):** Designa **Dreamer Trigger-mekanism**.
    * *Problem:* Dreamer körs bara vid rebuild. Grafen blir "smutsig" mellan.
    * *Alternativ:* Schema (nattlig), Watchdog, Threshold, On-demand via MCP.
    * *Se:* Konflikt 61 i `my_mem_koncept_logg.md`

* **OBJEKT-67 (PÅGÅENDE):** Implementera **Dream Directives** - Observational Learning för Dreamer.
    * *Status:* Grundarbete klart 2026-01-17. POC-verktyg utökat 2026-01-17.
    * *Klart:*
        - Unified ingestion pipeline (DocConverter → Lake → Vector → Graf i ett flöde)
        - Borttagen `vector_indexer.py` (redundant)
        - EntityGatekeeper-logik flyttad till `GraphStore.find_node_by_name()`
        - `search_graph_nodes` söker nu i hela properties JSON (name, node_context, etc.)
        - Dreamer dryrun använder `batch_generate()` för parallella LLM-anrop
        - `validate_rules.py` skärpt: fångar `except Exception` med logging men utan raise
        - `node_context` prompt uppdaterad för kortare, entitets-fokuserade beskrivningar
        - ~~`structural_analysis` prompt saknas~~ KORRIGERING: Prompten finns (rad 162-190 i services_prompts.yaml)
        - **POC: Schema-beskrivningar injiceras i structural_analysis** - LLM får nu nodtyp-definitioner från schemat för bättre RE-CATEGORIZE/DELETE-beslut
        - **POC: Kant-validering vid RE-CATEGORIZE** - Använder `SchemaValidator.validate_edge()` för att logga vilka relationer som blir ogiltiga vid typbyte
        - **POC: Context-pruning simulering vid MERGE** - Visar hur `node_context` skulle reduceras efter merge (triggas vid 15+ entries)
    * *Kvarstår:*
        - **PRODUKTIONSFIX:** `recategorize_node()` i graph_service.py validerar inte kanter efter typbyte - bör använda SchemaValidator
        - **PRODUKTIONSFIX:** `merge_nodes()` anropar inte `prune_context()` efteråt - node_context kan växa ohämmat
        - MCP Tools: `report_observation()`, `get_pending_dreams()`, `confirm_dream()`
        - dream_candidates tabell i GraphStore
        - User confirmation workflow
    * *Koncept:* MCP-klienten (Claude Desktop) observerar brus under arbete och förbereder "dreams" för användarbekräftelse.
    * *Operationer:* MERGE, SPLIT, RENAME, DELETE, RECATEGORIZE
    * *Relation:* Bygger på OBJEKT-61 (trigger-mekanism) och kompletterar OBJEKT-66 (entity resolution)
    * *POC-verktyg:* `tools/tool_dreamer_dryrun.py` - teknisk ritning för produktionsimplementation

* **OBJEKT-68 (PÅGÅENDE):** Arkitekturanalys - **Kunskapsflödets Helhet**.
    * *Bakgrund:* Innan vi lägger till fler komponenter (Dream Directives, MCP-verktyg) behöver vi förstå helheten.
    * *Användarens fråga (2026-01-17):*
      > "Vi tittar på hanteringen av ny kunskap som en 'helhet' och avgör vilka steg som behövs för att uppnå ett önskat resultat."
    * *Kärnfrågor och SVAR:*
      1. **Vad är det önskade resultatet?**
         > Bra datakvalitet i samtliga datakällor (graf/vektor/Lake) för systemets grundsyfte: Hjälpa användaren att nå all samlad kunskap i vilken dimension som helst. När som helst.
      2. **Var ska intelligensen ligga?**
         > BÅDA. Men med olika perspektiv:
         > - **Ingestion:** Föda NY data (för första gången) och ge den grundformat enligt systemets schema, samt koppla den till resten av datat enligt rådande ögonblicksbild.
         > - **Dreaming:** Övergripande dataförädling för att påverka ALL data som helhet - optimera för användaren.
      3. **Är `structural_analysis` redundant?**
         > "Vi får se." - Avgörs efter att vi definierat stegen tydligare.
    * *BESLUTAD ARKITEKTUR (2026-01-17):*
      ```
      ┌─────────────────────────────────────────────────────────────────┐
      │ FAS 1: COLLECT & NORMALIZE                                      │
      │ Ansvar: Hämta data från källor + normalisera till enhetligt     │
      │ Perspektiv: Per källa                                           │
      ├─────────────────────────────────────────────────────────────────┤
      │ collectors/                                                     │
      │   ├── file_retriever.py      # DropZone → Assets (UUID-namn)   │
      │   ├── slack_collector.py     # Slack API → normaliserad text   │
      │   ├── gmail_collector.py     # Gmail API → normaliserad text   │
      │   ├── calendar_collector.py  # Calendar API → normaliserad text│
      │   └── (framtida: harvest, teams, etc.)                         │
      │                                                                 │
      │ processors/                                                     │
      │   ├── transcriber.py         # Ljud → text                     │
      │   └── text_extractor.py      # PDF/DOCX/TXT → text (NY FIL)    │
      │                                                                 │
      │ Output: Enhetlig text + source_metadata                        │
      └─────────────────────────────────────────────────────────────────┘
                                    ↓
      ┌─────────────────────────────────────────────────────────────────┐
      │ FAS 2: INGESTION                                                │
      │ Ansvar: Integrera normaliserad data i kunskapssystemet          │
      │ Perspektiv: Ögonblicksbilden (koppla till befintligt)          │
      ├─────────────────────────────────────────────────────────────────┤
      │ engines/                                                        │
      │   └── ingestion_engine.py    # Orchestrerar hela flödet        │
      │       ├── generate_semantic_metadata()                          │
      │       ├── extract_entities()                                    │
      │       ├── resolve_against_graph()                               │
      │       ├── write_lake()                                          │
      │       ├── write_graph()                                         │
      │       └── write_vector()                                        │
      │                                                                 │
      │ Output: Lake (.md) + Graf (noder/kanter) + Vektor (ChromaDB)   │
      └─────────────────────────────────────────────────────────────────┘
                                    ↓
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
    * *NAMNKONVENTIONER (beslutade 2026-01-17):*
      | Kategori | Konvention | Exempel |
      |----------|------------|---------|
      | Språk | Engelska | `process_document()`, inte `processa_dokument()` |
      | Services | `*Service` | `GraphService`, `VectorService`, `LakeService` |
      | Collectors | `*_collector.py` | `file_collector.py`, `slack_collector.py` |
      | Engines | `*_engine.py` | `ingestion_engine.py` |
      | Funktioner | `verb_object()` | `extract_text()`, `process_audio()` |
    * *REFAKTORERING (KLAR 2026-01-17):*
      | Nuvarande | Nytt | Status |
      |-----------|------|--------|
      | `file_retriever.py` | `file_collector.py` | ✅ |
      | `doc_converter.py` | `engines/ingestion_engine.py` | ✅ |
      | `agents/dreamer.py` | `engines/dreamer.py` | ✅ |
      | `GraphStore` | `GraphService` | ✅ |
      | `LakeEditor` | `LakeService` | ✅ |
      | `EntityResolver` | `Dreamer` | ✅ |
      | `processa_dokument()` | `process_document()` | ✅ |
      | `processa_mediafil()` | `process_audio()` | ✅ |
      | `ladda_yaml()` | `load_yaml()` | ✅ |
    * *Genomförda steg:*
      1. ✅ Skapa `services/engines/` katalog
      2. ✅ Bryt ut `extract_text()` → `processors/text_extractor.py`
      3. ✅ Flytta + byt namn: `doc_converter.py` → `engines/ingestion_engine.py`
      4. ✅ Flytta: `dreamer.py` → `engines/dreamer.py`
      5. ✅ Byt namn: `file_retriever.py` → `file_collector.py`
      6. ✅ Byt klassnamn: `GraphStore` → `GraphService`, `LakeEditor` → `LakeService`, `EntityResolver` → `Dreamer`
      7. ✅ Byt funktionsnamn: svenska → engelska
      8. ✅ Konsolidera LLM-anrop till `LLMService`
    * *LLM-KONSOLIDERING (KLAR 2026-01-17):*
      Alla LLM-anrop går nu genom `services/utils/llm_service.py`:
      | Fil | Metod |
      |-----|-------|
      | `engines/ingestion_engine.py` | `LLMService.generate()` |
      | `engines/dreamer.py` | `LLMService.generate()` |
      | `processors/transcriber.py` | `LLMService.client` (multimodal audio) |
      | `agents/validator_mcp.py` | `LLMService.client` (multi-turn) |

      Borttaget: 4 separata `genai.Client`, `LLMClient` klass, duplicerad API-nyckelhantering.
    * *Status:* OBJEKT-68 KLAR - arkitektur renodlad, namnkonventioner införda, LLM centraliserad.

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
