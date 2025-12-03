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
* **LÖST-31:** Integrera **Kùzu (Graf)** i Chatten (Hybrid Search).
    * *Lösning:* Chatten använder `taxonomy.json` för att identifiera Masternoder och hämtar exakta relationer.
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

## Öppna Objekt (Nästa Fas)

* **OBJEKT-46 (Prio 0 - ARKITEKTUR):** Implementera **Pipeline v6.0** (Refaktorering).
    * *Beslut:* 2025-12-03 – Överenskommelse om ny sök-pipeline.
    * *Nuvarande (v5.2):* Planering → Jägaren + Vektorn → Domaren → Syntes (3 AI-anrop, otydlig SOC)
    * *Ny pipeline (v6.0):*
        ```
        Input → IntentRouter → ContextBuilder → Planner → Synthesizer → Output
                    (AI)           (Kod)         (AI)        (AI)
                Klassificera     Hämta data   Bygg rapport   Svara
        ```
    * *Komponenter:*
        1. **IntentRouter** (`services/intent_router.py`) - AI (Flash Lite)
            - Klassificera intent: `FACT` (specifik data) vs `INSPIRATION` (idéer)
            - Bestäm strategi: `STRICT` (bara Jägaren) vs `RELAXED` (båda parallellt)
            - Parsa tidsreferenser ("igår" → absolut datum)
            - Upplös kontext från historik ("det projektet" → "Adda PoC")
        2. **ContextBuilder** (`services/context_builder.py`) - **Kod (Python)**
            - Deterministisk informationshämtning
            - `STRICT`: Endast `search_lake` (nyckelord)
            - `RELAXED`: `search_lake` + `vector_db` parallellt
            - Ingen AI – snabbt, förutsägbart, debuggbart
        3. **Planner** (`services/planner.py`) - AI (Flash Lite)
            - Tar kandidater från ContextBuilder
            - Skapar en kurerad **rapport** (kondenserad, relevant information)
            - Synthesizer får rapporten – INTE råa dokument
        4. **Synthesizer** (befintlig) - AI (Pro)
            - Genererar svar baserat på rapporten
            - Framtid: Kan begära ny rapport om resultatet är svagt
    * *Principer:*
        - **Tydlig SOC:** Varje komponent har ETT ansvar
        - **HARDFAIL:** Varje steg rapporterar explicit om det misslyckas
        - **Rapport > Dokument:** Synthesizer får aldrig rådata
    * *Framgångskriterium:* Samma eller bättre kvalitet på svar, men tydligare flöde och debuggbarhet.
    * *Implementationsordning:*
        1. IntentRouter (med temporal parsing)
        2. ContextBuilder (ersätter nuvarande sök-logik)
        3. Planner (ersätter Domaren, skapar rapport)
        4. Integration i `my_mem_chat.py`
    * *Framtid (v7.0):* Agentic loop där Planner kan iterera vid svagt resultat.

* **OBJEKT-41 (Prio 0 - KRITISK):** Implementera **"Aggregerad Insikt"** ("The Inverted T").
    * *Problem:* Chatten fungerar som arkiv (returnerar data) istället för minne (ger insikt).
    * *Mål:* Synthesizern ska ge **mervärde** genom att koppla ihop information från olika kontexter.
    * *Krav:*
        1. Ny synthesizer-prompt: "Användaren VAR DÄR. Ge inte data – ge insikt."
        2. Aktivt leta efter *relaterad* information som förstärker/kontrasterar frågan.
        3. Temporal koppling: "Du sa X innan Y, vilket indikerar Z."
        4. Graf-integration: Utnyttja relationer mellan entiteter för att hitta kopplingar.
    * *Framgångskriterium:* Svaret ska innehålla minst EN insikt som användaren INTE kunde få genom att läsa källdokumentet direkt.
    * *Se:* Konflikt 41 i `my_mem_koncept_logg.md`

* **OBJEKT-42 (Prio 0 - KRITISK):** Implementera **"Temporal Intelligence"**.
    * *Problem:* Systemet förstår inte relativa tidsreferenser ("igår", "förra veckan", "nyligen"). Det tolkar dem bokstavligt eller ignorerar dem helt.
    * *Bevis (Simulering 2025-12-03):*
        - "Inköpslänken Scoping": Användaren sa "igår" men systemet svarade om möte från 25 november.
        - "Beläggning Q1 2026": Systemet motsade sig själv om tidsperioder mellan frågor.
        - Flera uppgifter fick kommentaren "det var ju förra veckan!"
    * *Mål:* Chatten ska konvertera relativa tidsuttryck till absoluta datum och prioritera dokument nära den tidsperioden.
    * *Implementation:*
        1. **Query Enrichment:** Planerings-steget extraherar tidsreferenser och beräknar absoluta datum.
        2. **Temporal Filter:** Jägaren/Vektorn prioriterar dokument med `timestamp_created` inom relevant intervall.
        3. **Context Injection:** Syntesen får explicit kontext om "frågedatum" och "relevant tidsperiod".
    * *Framgångskriterium:* "Vad hände igår?" returnerar BARA dokument från gårdagen.

* **OBJEKT-43 (Prio 0.5 - Prestanda):** Implementera **"Summary-First Search"**.
    * *Problem:* Systemet skickar hela dokument till Domaren och Syntesen, vilket skapar lång svarstid.
    * *Bevis (Simulering 2025-12-03):*
        - Snitttid per runda: **50.6 sekunder**
        - Max tid: **130 sekunder** (en enda fråga!)
        - Syntesen (MODEL_PRO + 100k tecken) står för **~70% av tiden**
        - `MAX_CHARS = 100000` i `my_mem_chat.py` rad 366
    * *Insikt:* Varje Lake-dokument har redan en AI-genererad `summary` i YAML-headern (~200 tecken).
    * *Mål:* Minska kontextbelastning genom att använda sammanfattningar strategiskt.
    * *Implementation:*
        1. **Jägaren**: Sök i `keywords` + `entities` (redan metadata).
        2. **Domaren**: Bedöm relevans baserat på `summary` istället för fulltext.
        3. **Syntesen**: Läs fulltext BARA för de 2-3 dokument som faktiskt behövs.
        4. **Sänk MAX_CHARS** från 100k till 30k som första steg.
    * *Förväntad effekt:* ~70% mindre kontext till AI, ~60% snabbare svar.

* **OBJEKT-44 (Prio 1 - Lärande):** Implementera **"Entity Resolution & Alias Learning"**.
    * *Problem:* Transkribering skapar felstavade namn ("Sänk" istället för "Cenk Bisgen"). Systemet kan inte koppla ihop varianter.
    * *Insikt:* Med aggregerad data från flera dokument kan systemet lära sig att "Sänk" = "Cenk Bisgen".
    * *Princip:* Assets orörda, Lake stabil, **Graf lär sig över tid**.
    * *Implementation:*
        1. **Graf-nod för Person**: `canonical_name`, `aliases[]`, `team`, `confidence`.
        2. **Konsolidering**: Fuzzy-matcha namn över dokument, bygg alias-lista.
        3. **Sök-tid**: Slå upp aliases och sök efter ALLA varianter.
    * *Exempel:* Fråga om "Cenk" → söker "Sänk", "Cenk", "Bisgen", "Cenk Bisgen".
    * *Se:* Konflikt 42 i `my_mem_koncept_logg.md`

* **OBJEKT-45 (Prio 1 - Insamling):** Implementera **"Context Injection vid Insamling"**.
    * *Problem:* DocConverter och Transcriber "jobbar i mörkret" – de har ingen kännedom om existerande entiteter.
    * *Bevis (Kodanalys 2025-12-03):*
        - DocConverter laddar taxonomin men använder den BARA för validering av `graph_master_node`.
        - Transcriber har INGEN kontakt med taxonomi eller graf.
        - Båda gissar entiteter fritt → skapar inkonsekvent metadata ("Sänk" vs "Cenk Bisgen").
    * *Mål:* Ge insamlingsagenterna kontext om existerande kunskap INNAN de genererar metadata.
    * *Implementation:*
        1. **Graf-lookup vid start:** Hämta kända personer, projekt och aliases från KùzuDB.
        2. **Context Injection i prompts:** Ge AI-modellen en lista på kända entiteter.
        3. **Namn-normalisering:** Om transkribering gissar "Sänk", matcha mot känd alias och skriv "Cenk Bisgen".
    * *Exempel:*
        ```python
        context = {
            "known_persons": ["Joakim Ekman", "Cenk Bisgen", "Marie Björkengren"],
            "known_aliases": {"Sänk": "Cenk Bisgen", "Jocke": "Joakim Ekman"},
            "active_projects": ["Adda PoC", "MyMemory", "Stacken.ai"]
        }
        prompt = f"KÄNDA TALARE: {context['known_persons']}..."
        ```
    * *Förväntad effekt:* Bättre metadata från dag 1, mindre städning i konsolidering.
    * *Se:* Konflikt 44 i `my_mem_koncept_logg.md`

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