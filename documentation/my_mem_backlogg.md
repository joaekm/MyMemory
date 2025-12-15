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

Detta dokument spårar vårt aktiva arbete, i enlighet med `WoW 2.4`.

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
    * *Lösning:* Chatten initierar nu nya DB-anslutningar vid varje sökning för att se nydata direkt.
* **LÖST-31:** Integrera **Graf** i Chatten (Hybrid Search).
    * *Lösning:* Chatten använder `taxonomy.json` för att identifiera Masternoder och hämtar exakta relationer.
    * *Not:* Ursprungligen med KuzuDB, migrerat till DuckDB i LÖST-54.
* **LÖST-4:** Implementera **Konsoliderings-modulen** (Taxonomi).
    * *Lösning:* En separat `Graph Builder`-agent (konsolidering) städar inkommande data mot en strikt OTS-modell.
* **LÖST-34:** Implementera **Split Indexing**.
    * *Lösning:* Indexering delades upp i `vector_indexer.py` (Realtid) och `graph_builder.py` (Batch) för att lösa databas-låsningar.
* **LÖST-35:** Implementera **Taxonomi-definitioner**.
    * *Lösning:* `my_mem_taxonomy.json` etablerad med OTS-struktur (Operativt/Taktiskt/Strategiskt).

* **LÖST-37:** Implementera **Hybrid Search v2 ("The Hunter")**.
    * *Lösning:* Införde `search_lake` för deterministisk nyckelordssökning i chatten.
* **LÖST-38:** Implementera **Re-ranking ("The Judge")**.
    * *Lösning:* Flash Lite agerar mellanlager för att bedöma relevans innan slutgiltig syntes.
* **LÖST-39:** Implementera **YAML-baserade Prompter**.
    * *Lösning:* `chat_prompts.yaml` skapad och integrerad.

* **LÖST-54:** The DuckDB Pivot.
    * *Problem:* KuzuDB (C++ inbäddad grafdatabas) kraschade vid parametriserade queries i komplexa WHERE-satser (`KU_UNREACHABLE` assertion failures).
    * *Lösning:* Migrerade från KuzuDB till DuckDB med relationell graf-modell (`nodes`/`edges`-tabeller).
    * *Ny arkitektur:* `services/graph_service.py` med `GraphStore`-klass som hanterar all graf-logik via SQL.
    * *Bonus:* Entity Resolution (OBJEKT-44) och Entitet-separation (OBJEKT-51) löses naturligt genom den nya strukturen med `aliases`-kolumn i `nodes`-tabellen.

* **LÖST-55:** Pipeline v8.2 "Pivot or Persevere".
    * *Ursprung:* OBJEKT-46 (Pipeline v6.0 Refaktorering).
    * *Implementation:* Gick längre än v6.0 - implementerade v8.2 med:
        - **IntentRouter** (v7.0): Skapar Mission Goal, parsar tid, extraherar keywords/entities
        - **ContextBuilder** (v7.5): Time-Aware Reranking, parallel Lake+Vektor-sökning
        - **Planner** (v8.2): ReAct-loop med "Tornet" (rolling hypothesis) + "Bevisen" (facts)
        - **SessionEngine** (NY): Orchestrator som hanterar session state + Pivot or Persevere
        - **Synthesizer**: Genererar svar från Planner-rapport
    * *Nyckelkoncept:*
        - "Tornet": Iterativt byggd arbetshypotes (current_synthesis)
        - "Bevisen": Append-only faktalista (facts)
        - "Pivot or Persevere": SessionEngine skickar befintligt Torn+Facts till ny fråga
        - "Librarian Loop": Two-stage retrieval med scan + deep read
    * *Filer:* `session_engine.py`, `planner.py`, `context_builder.py`, `intent_router.py`

* **LÖST-56:** DateService (Central Datumhantering).
    * *Ursprung:* OBJEKT-50.
    * *Lösning:* `services/utils/date_service.py` med prioriterad extraktionskedja:
        1. Frontmatter (timestamp_created) - mest pålitligt
        2. Slack-filnamn (Slack_kanal_2025-12-11_uuid.txt)
        3. PDF-metadata (CreationDate)
        4. Filsystem (birthtime → mtime fallback)
    * *HARDFAIL:* Om inget datum kan extraheras.

* **LÖST-57:** Summary-First Search.
    * *Ursprung:* OBJEKT-43 (Prestanda).
    * *Lösning:* Implementerat i ContextBuilder v7.5:
        - `TOP_N_FULLTEXT = 3`: Endast topp 3 dokument får fulltext
        - Time-Aware Reranking: `hybrid_score = original_score * (1 + time_boost)`
        - Relevance Gate: Dokument under threshold får ingen boost
    * *Effekt:* Dramatiskt minskad kontextbelastning till LLM.

* **STÄNGD-58:** Canonical Entity-kvalitet.
    * *Ursprung:* OBJEKT-52.
    * *Anledning:* Löst via GraphStore (LÖST-54) med:
        - `aliases`-kolumn i nodes-tabellen
        - `upgrade_canonical()` för att byta canonical
        - `find_nodes_by_alias()` för uppslagning
    * *Kvarstående:* Inlärning från sessioner (del av OBJEKT-44).

* **STÄNGD-59:** Kurerad Entity-session.
    * *Ursprung:* OBJEKT-53.
    * *Anledning:* Kan implementeras ovanpå SessionEngine. Slås ihop med OBJEKT-44/48.

## Öppna Objekt (Nästa Fas)

* **OBJEKT-41 (Prio 1 - UTVÄRDERA):** Verifiera **"Aggregerad Insikt"** i Pipeline v8.2.
    * *Ursprungligt problem:* Chatten fungerar som arkiv (returnerar data) istället för minne (ger insikt).
    * *Status:* Planner v8.2 bygger nu "Tornet" (rolling hypothesis) iterativt.
    * *Fråga:* Ger detta faktiskt "insikt" eller bara bättre sammanfattning?
    * *Nästa steg:* Kör simulering och utvärdera om Tornet ger mervärde.
    * *Framgångskriterium:* Svaret ska innehålla minst EN insikt som användaren INTE kunde få genom att läsa källdokumentet direkt.

* **OBJEKT-42 (Prio 0.5 - DELVIS LÖST):** Komplettera **"Temporal Intelligence"**.
    * *Ursprungligt problem:* Systemet förstår inte relativa tidsreferenser ("igår", "förra veckan").
    * *Vad som är löst:*
        - ✅ IntentRouter (v7.0) parsar tidsreferenser och returnerar `time_filter`
        - ✅ DateService (LÖST-56) ger pålitlig datumextraktion
        - ✅ Time-Aware Reranking boostar nyare dokument (LÖST-57)
    * *Vad som saknas:*
        - ❌ ContextBuilder använder inte `time_filter` för filtrering
        - ❌ Strikt filtrering på tidsintervall
    * *Nästa steg:* Implementera `time_filter` i `build_context()` för att filtrera kandidater.
    * *Framgångskriterium:* "Vad hände igår?" returnerar BARA dokument från gårdagen.

* **OBJEKT-44 (Prio 1 - DELVIS LÖST):** Komplettera **"Entity Resolution & Alias Learning"**.
    * *Ursprungligt problem:* Transkribering skapar felstavade namn ("Sänk" istället för "Cenk Bisgen").
    * *Vad som är löst:*
        - ✅ GraphStore har `aliases`-kolumn i nodes-tabellen
        - ✅ `find_nodes_by_alias()` för uppslagning
        - ✅ `find_nodes_fuzzy()` för fuzzy-matchning
        - ✅ `upgrade_canonical()` för att byta canonical
        - ✅ ContextBuilder använder `get_graph_context_for_search()` för alias-kontext
    * *Vad som saknas:*
        - ❌ Automatisk inlärning från sessioner (OBJEKT-48)
        - ❌ LLM-baserad alias-bedömning vid dokumentprocessning
    * *Princip:* Assets orörda, Lake stabil, **Graf lär sig över tid**.
    * *Koppling:* Del av OBJEKT-48 – lärdomar extraheras från sessioner som dokument.

* **OBJEKT-45 (Prio 1 - DELVIS LÖST):** Komplettera **"Levande Metadata vid Insamling"**.
    * *Ursprungligt problem:* DocConverter och Transcriber "jobbar i mörkret" – ingen kännedom om entiteter.
    * *Vad som är löst:*
        - ✅ ContextBuilder hämtar graf-kontext via `get_graph_context_for_search()`
        - ✅ GraphStore har API för att hämta kända entiteter och aliases
    * *Vad som saknas:*
        - ❌ Context Injection i DocConverter-prompts
        - ❌ Context Injection i Transcriber-prompts
        - ❌ Rikare extraktion (dates_mentioned, actions, deadlines)
    * *Implementation:*
        1. Lägg till `get_known_entities()` i GraphStore
        2. Injicera kända entiteter i DocConverter/Transcriber-prompts
        3. Uppdatera prompts för rikare extraktion
    * *Koppling:* Del av OBJEKT-48 ("Dreaming").

* **OBJEKT-47 (Prio 1.5 - DEADLINE):** Migrera till **gemini-embedding-001**.
    * *Notifiering:* Google meddelade 2025-12-03 att `text-embedding-004` fasas ut.
    * *Deadline:* **2026-01-14** (hård deadline från Google)
    * *Påverkan:* Alla embeddings i ChromaDB använder nuvarande modell.
    * *Ny modell:* `gemini-embedding-001` (stable, GA, högre rate limits)
    * *Migrationsplan:*
        1. Uppdatera embedding-funktion i `vector_indexer.py`
        2. Re-embeda ALL data i Lake (kräver full re-indexering)
        3. Testa sökkvalitet efter migrering
    * *Risk:* Om inte migrerat före deadline slutar vektorsökning fungera.
    * *Resurser:*
        - [Gemini Embeddings Documentation](https://ai.google.dev/gemini-api/docs/embeddings)
        - [Model Benchmarks](https://ai.google.dev/gemini-api/docs/models)
    * *Projekt som påverkas:* `gen-lang-client-0582831621`, `gen-lang-client-0704808841`

* **OBJEKT-48 (Prio 0.5 - VISION):** Implementera **"Sessioner som Lärdomar"** (Självlärande System).
    * *Problem:* Metadata genereras vid insamling och förblir statisk. Systemet lär sig inte vad som är viktigt för användaren.
    * *Bevis (2025-12-03):* Pipeline v6.0 missade "10 december" för användartester eftersom summary/keywords saknade denna info.
    * *Nyckelinsikt:* **Sessioner är bara dokument.** Samma flöde som allt annat – ingen speciallösning.
    * *Flöde:*
        1. **Session avslutas** → sparas som Lake-dokument
        2. **LLM extraherar lärdomar** till YAML-header (entities, aliases, kopplingar)
        3. **Graf-builder indexerar** → systemet har lärt sig
    * *Header-format för lärdomar:*
        ```yaml
        learned_entities:
          - canonical: "Joakim Ekman"
            aliases: ["Jocke", "Joakim"]
            type: "Person"
            confidence: high
            reason: "Användaren angav fullständigt namn"
        ```
    * *LLM bedömer trovärdighet:*
        - Ingen hårdkodad källranking
        - LLM har kontexten (källa, namnformat, befintlig kunskap)
        - LLM resonerar: "Fullständigt namn från intern Slack-kanal = hög trovärdighet"
    * *Vad som INTE behövs:*
        - ~~`session_signals.json`~~ – sessioner är dokument i Lake
        - ~~Separat signal-loggning~~ – allt går genom samma pipeline
        - ~~Hårdkodade regler för källtrovärdighet~~ – LLM bedömer
    * *Koppling till andra objekt:*
        - OBJEKT-44: Entity Resolution – canonical swap baserat på lärdomar
        - OBJEKT-45: Context Injection – kända entiteter injiceras vid insamling
        - OBJEKT-46: Pipeline drar nytta av rikare metadata
    * *Framgångskriterium:* Systemet lär sig alias-kopplingar från sessioner utan manuell input.
    * *Se:* Konflikt 46 i `my_mem_koncept_logg.md`

* **OBJEKT-49 (Prio 2 - DELVIS LÖST):** Komplettera **"MyMemory Engine"** (API-separation).
    * *Ursprungligt problem:* `my_mem_chat.py` blandar CLI med logik.
    * *Vad som är löst:*
        - ✅ SessionEngine finns som orchestrator (`session_engine.py`)
        - ✅ `run_query()` hanterar hela pipelinen
        - ✅ Session state (chat_history, planner_state) hanteras centralt
    * *Vad som saknas:*
        - ❌ `my_mem_chat.py` är fortfarande inte refaktorerad till tunn klient
        - ❌ HTTP/WebSocket-API för mobilapp/web
    * *Nästa steg:*
        1. Refaktorera `my_mem_chat.py` att endast använda SessionEngine
        2. (Framtid) Exponera via FastAPI

* **OBJEKT-51 (Prio 1.5 - DELVIS LÖST):** Separera **Entiteter från Taxonomi**.
    * *Ursprungligt problem:* Taxonomin innehåller 259 individer som borde vara i Grafen.
    * *Vad som är löst:*
        - ✅ GraphStore har Entity-noder med aliases
        - ✅ Infrastruktur för entitet-lagring i Graf finns
    * *Vad som saknas:*
        - ❌ Taxonomin är inte städad (259 individer kvar)
        - ❌ Dreamer/GraphBuilder konsoliderar fortfarande delvis till taxonomi
    * *Princip:* Taxonomin ska ENDAST innehålla kategorier, INTE individnamn.
    * *Nästa steg:*
        1. Migrera Person/Aktör/Projekt `sub_nodes` till Graf
        2. Rensa taxonomin
        3. Uppdatera Dreamer att konsolidera entiteter till Graf
    * *Framgångskriterium:* Taxonomin har 0 individnamn.

* **OBJEKT-32 (Prio 2):** Implementera **"Quick Save"** (Read/Write) i Chatten.
    * *Mål:* Möjlighet att spara text/tankar direkt till `Assets` inifrån chatten ("Kom ihåg att...").
* **OBJEKT-36 (Prio 2):** Kalender-integration.
    * *Mål:* Låta Hjärnan se vad som faktiskt står i din kalender för bättre kontext.
* **OBJEKT-40 (Prio 3):** Implementera **Harvest Integration**.
    * *Mål:* Koppla tidrapporteringsdata till företagsminnet för att kunna svara på "Vad jobbade jag med?".
* **OBJEKT-37 (Prio 4):** Implementera **"The Bio-Graph"**.
    * *Mål:* En levande konfigurationsfil (`_user_bio_graph.yaml`) som sparar användarens preferenser.
* **OBJEKT-38 (Prio 5):** Implementera **"Weekly Intelligence Agent"**.
    * *Mål:* Veckovis strategisk rapport.
* **OBJEKT-27 (Prio 6):** Skapa **"Installer Bundle"** för Distribution.
* **OBJEKT-25 (Prio 7):** Implementera **Retention Policy**.