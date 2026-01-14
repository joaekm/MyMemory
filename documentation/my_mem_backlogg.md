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

Detta dokument spårar vårt aktiva arbete. Uppdaterad 2026-01-14.

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

---

## Aktiva Objekt

### Prio 0 - KRITISK

* **OBJEKT-47 (AKTIV - AKUT):** Migrera till **gemini-embedding-001**.
    * *Status:* **DEADLINE PASSERAD (2026-01-14)**
    * *Notifiering:* Google meddelade 2025-12-03 att `text-embedding-004` fasas ut.
    * *Påverkan:* Alla embeddings i ChromaDB använder nuvarande modell.
    * *Migrationsplan:*
        1. Uppdatera embedding-funktion i `vector_indexer.py`
        2. Re-embeda ALL data i Lake (kräver full re-indexering)
        3. Testa sökkvalitet efter migrering

### Prio 1 - Datakvalitet

* **OBJEKT-62 (AKTIV - NY):** Fixa **Transcription Truncation**.
    * *Problem:* Långa transkriptioner trunkeras i Lake-filer.
    * *Rotorsak:* Gemini Pro har output-token-gräns. Prompten ber om hela transkriptet i JSON-fältet `"transcript"`, vilket kapas vid långa möten.
    * *Lösning:* Separera metadata-extraktion från transkript-output. Behåll `raw_transcript` (från Flash), applicera bara annoteringarna.
    * *Påverkan:* `services/processors/transcriber.py`, `config/services_prompts.yaml`

* **OBJEKT-63 (AKTIV - NY):** Implementera **Rigorös Metadata-testkedja**.
    * *Problem:* Hela datacykeln för metadata (Schema → Ingestion → Dreamer → Validator → Lake) saknar end-to-end-tester. Kedjan bryts ofta utan att det upptäcks.
    * *Krav:*
        - Test som verifierar att en property definierad i schema-template propageras korrekt genom hela flödet
        - HARDFAIL om kedjan bryts (ingen tyst fallback)
        - Täcker: Schema-validering → DocConverter/Transcriber → GraphStore → Dreamer → MCP-validator
    * *Filosofi:* Om en property sätts i schemat ska den gå hela vägen. Om den inte gör det ska det vara högljutt.

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

* **OBJEKT-61 (AKTIV - NY):** Designa **Dreamer Trigger-mekanism**.
    * *Problem:* Dreamer körs bara vid rebuild. Grafen blir "smutsig" mellan.
    * *Alternativ:* Schema (nattlig), Watchdog, Threshold, On-demand via MCP.
    * *Se:* Konflikt 61 i `my_mem_koncept_logg.md`

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

*Senast uppdaterad: 2026-01-14*
*Se `my_mem_koncept_logg.md` för resonemang bakom beslut.*
