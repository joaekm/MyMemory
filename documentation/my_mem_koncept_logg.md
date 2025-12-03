---
unit_id: 11f7c3c2-4a1e-45a7-841f-818a598e9b01
owner_id: "joakim.ekman"
access_level: "Nivå_3_Delad_Organisation"
context_id: "PROJEKT_DFM_V1"
source_type: "System_Dokument"
source_ref: "dfm_koncept_logg.md"
data_format: "text/markdown"
timestamp_created: "2025-11-22T16:00:00Z"
policy_tags: []
original_binary_ref: null
---

# Projektets Konceptuella Resa (Resonemangs-logg)

Detta dokument är den primära källan ("Varför-dialogen") och fångar det fullständiga resonemanget bakom våra designval, i enlighet med `WoW 2.4` (Sektion 5.3).

## Initial Konceptualisering (Steg 1-3)

Vi började med att definiera ett trestegskoncept för att lösa "kognitiv fragmentering":
1.  **Företagsminnet (Strukturkapitalet):** Passiv insamling av kunskap (ljud, text, bilder) som "näring".
2.  **Den Proaktiva Informationsagenten:** En personlig "AI-kollega" som proaktivt visar kopplingar och ger ett "wow"-ögonblick.
3.  **Den Syntetiska Medarbetaren:** En framtida (nu parkerad) vision där "näringen" används för att "lära upp" syntetiska roller.

## Scenariot: Ledningsgruppsdagarna & "Bygga Torn"

Vi etablerade ett scenario (baserat på inspelningar, post-its, Slack) för att definiera användarnytta:
* **"Spara för nästa möte"** (Arkivering, Steg 1).
* **"Förstärka oss UNDER mötet"** (Realtidsanalys, Steg 2).
* **Resonemang:** "Bygga torn" innebär att agenten i realtid kan hämta alla fragmenterade källor (text, bild, Slack) som rör ett ämne.

## Konflikt 1: Datamodellen (Lego vs. Sand)

* **Påstående:** En statisk modell håller inte. Data måste vara som "Lego".
* **Resonemang (Kritik):** Rådata är "sand", inte "Lego". För att bli "Lego" måste varje bit ha "knoppar" (metadata).
* **Slutsats:** Vi designar inte en *datamodell*, vi designar en **Atomär Metadata-Modell (AMM)** som omsluter ostrukturerad data.

## Konflikt 2: Portabilitet vs. Prestanda (Sjö vs. Index)

* **Påstående:** Data måste vara portabel, flexibel, mänskligt läsbar och kunna "byta system".
* **Resonemang (Kritik):** Mänskligt läsbar data (text) är *o-optimerad* för AI-agenter, som behöver maskinläsbara vektorer (embeddings).
* **Slutsats:** Vi måste ha en **dubbel lagringsmodell**:
    1.  En "Kall" Databas ("Sjön") för mänskligt läsbar data (portabilitet).
    2.  En "Varm" Databas ("Indexet") för AI-optimerade vektorer (prestanda).

## Konflikt 3: "Storebrors"-risken (Säkerhet & Kontroll)

* **Påstående:** Vi får inte ha ett "storebror ser dig"-scenario. Användaren måste ha kontroll och känslig data (t.ex. personalärenden) får inte läcka.
* **Resonemang (Kritik):** Detta är ett förtroendeproblem. Lösningen måste garantera psykologisk trygghet. "Sjön" kan inte vara *en* sjö, utan måste delas upp.
* **Slutsats:** Vi etablerar en **"Privacy-First"-arkitektur** med explicita Åtkomstnivåer:
    * `Nivå 1: Privat Minne` (som "My Drive").
    * `Nivå 2-4: Delat Minne` (som "Shared Drive").

## Konflikt 4: Administrativ Friktion vs. Kontroll (HIIT-flödet)

* **Påstående:** Lämna delningsproblemet till användaren.
* **Resonemang (Kritik):** En *helt* manuell process "kommer döda allt". Det skapar för hög administrativ friktion, och "Företagsminnet" kommer att "svälta".
* **Slutsats:** Vi måste ha ett **"Agent-Assisterat Delningsflöde"**. Detta är "automatiserat med HIIT-principen påslagen".
    1.  Agenten *automatiserar* analysen (sammanfattar, hittar PII).
    2.  Användaren (HIIT) behåller 100% kontroll genom att *godkänna* delningen.

## Konflikt 5: Rådata-format (Text vs. Binärer)

* **Påstående:** Rådatan bör vara "ren text" av säkerhets-, portabilitets- och storleksskäl. Referenser kan finnas till originalfiler (ljud/bild).
* **Resonemang (Kritik):** Detta är korrekt. AI-agenter kan inte generera `beslutsprotokoll` från råa ljudfiler.
* **Slutsats:** Vår arkitektur måste ha *tre* lagringsplatser:
    1.  **"Sjön"** (endast "ren text" + AMM-metadata).
    2.  **"Asset Store"** (strukturerad lagring för binära originalfiler).
    3.  **"Indexet"** (vektorer + graf + metadata-kopia).
* **Slutsats:** Alla "Insamlare" måste vara "Transformatorer" som skapar "ren text".

## Konflikt 6: "AI-Spam" vs. "Kontext-Orkestrering"

* **Påstående:** Det blir "stormigt" (kaos) om alla deltagares agenter skapar samma `mötesanteckning`.
* **Resonemang (Kritik):** Vi måste skilja på privata (Nivå 1) och kollektiva (Nivå 2) agent-ansvar.
* **Slutsats:** Vi behöver en central **"Kontext-Orkestrerare"** (Nivå 2-agent). Privata agenter publicerar *endast* sin rådata (text) till Nivå 2. Orkestreraren bevakar detta, slår ihop datan baserat på `context_id`, och skapar den *enda* officiella artefakten (t.ex. `beslutsprotokollet`).

## Konflikt 7: Kontextuell Drift (Den "Påstridiga Agenten")

* **Påstående:** "Kontext-Orkestreraren" får inte bli "FÖR komplicerad" och behöva "gissa" vad som hör ihop.
* **Resonemang (Kritik):** Den centrala agenten ska vara "dum" och regelbaserad. Smartheten måste ligga i att *säkerställa* att `context_id` är korrekt från början.
* **Slutsats (Aha!):** Detta löses genom en "synk mellan människor". Agenten måste vara **"påstridig"** och **"BEGÄRA"** att användarna *innan* mötet startar väljer antingen en "pågående ström" eller skapar en "ny plats". Detta skapar "struktur" OCH "känsla" och garanterar att `context_id` är korrekt för alla deltagare.

## Konflikt 8: Iteration av WoW (Spårbarhet)

* **Påstående:** `koncept.md` måste vara en rik "berättelse" som fångar "resonemanget", inte bara "slutsatser".
* **Resonemang (Kritik):** De första utkasten av WoW var otydliga. `_koncept.md` var felaktigt definierat som en "summary", och `_koncept_logg.md` var inte den råa källan.
* **Slutsats:** `Way_of_Working` uppdaterades till `v2.4` för att lösa detta. Vi har nu fyra kärndokument: `dfm_koncept_logg.md` (rå-data/resonemang), `dfm_summary.md` (slutsatser), `dfm_arkitektur.md` (vad), och `dfm_backlog.md` (nu). Detta säkerställer full spårbarhet.
## Konflikt 8: Iteration av WoW (Spårbarhet)

* **Påstående:** `_koncept_logg.md` måste vara en rik "berättelse" som fångar "resonemanget", inte bara "slutsatser".
* **Resonemang (Kritik):** De första utkasten av WoW var otydliga. `_koncept.md` var felaktigt definierat som en "summary", och `_koncept_logg.md` var inte den råa källan utan en felaktig sammanfattning.
* **Slutsats:** `Way_of_Working` uppdaterades till `v2.4` för att lösa detta. Vi har nu fyra kärndokument: `dfm_koncept_logg.md` (rå-data/resonemang), `dfm_summary.md` (slutsatser), `dfm_arkitektur.md` (vad), och `dfm_backlog.md` (nu). Detta säkerställer full spårbarhet.

## Konflikt 9: MVP-Prioritering (OBJEKT-1)

* **Påstående:** Vilket gränssnitt ska vi bygga först för HIIT-UX (OBJEKT-1)? "Mobilappen" är "sugen på", men "Desktop" är mer "funktionellt som MVP".
* **Resonemang (Kritik):** Mobil-appen är kritisk för att fånga workshop-data (fysiska artefakter). Desktop-appen är dock enklare att bygga (lägre teknisk tröskel) och låter oss testa *två* insamlingsmetoder (lokala filer, fysisk mikrofon).
* **Slutsats:** Vi prioriterar **Desktop-agenten** som plattform för att bygga och testa MVP:n för de kritiska UX-flödena (OBJEKT-1 och OBJEKT-2).

## Konflikt 10: Hantering av Okategoriserad Data (Det "Stora Glappet")

* **Påstående (Initialt):** Man *kan* gå vidare i Desktop-appen utan att tagga (`context_id`) ordentligt.
* **Resonemang (Kritik):** Detta bröt mot vår "Påstridiga Agent"-princip (LÖST-6). Det skapade ett behov av en komplex "Kontext-Upptäckare".
* **Förtydligande (Aha!):** Missförstånd. Regeln måste vara olika för olika nivåer.
    1.  **Nivå 1 (Privat Minne):** *Total frihet*. Det *måste* vara möjligt att ladda upp okategoriserad data här för att kunna arbeta med "lösa trådar".
    2.  **Nivå 2 (Kollektivt Minne):** *Noll frihet*. "Ingen uppladdning... möjlig utan att kategorisering är vald".
* **Nytt Problem:** Hur kan en användare då veta om "någon jobbar på liknande spår" om all okategoriserad data är 100% privat?
* **Slutsats (Lösningen):** Vi måste implementera en **"Kollaborativ Signal-agent"**.
    * **Praktik (Hur):** 1) Den *lokala* agenten (Nivå 1) skapar en *anonymiserad, abstraherad signatur* (vektor) av den privata texten. 2) *Endast* denna anonyma signatur skickas till den centrala Signal-agenten. 3) Signal-agenten matchar anonyma signaturer från olika användare. 4) Om en match hittas, skickas en "inbjudan till samarbete" (HIIT-notis) till båda parter, som *sedan* kan välja att ta kontakt ("ta en kaffe").
    * **Resultat:** Detta löser konflikten mellan integritet och samarbete.

    ## Utvecklingsfas: OBJEKT-5 (Insamlaren)

* **Fokus:** Implementation av Desktop-agentens fil-dump-funktion.
* **Problemlösning:** Den initiala koden innehöll hårdkodade sökvägar. Det kritiserades omedelbart då det bröt mot "medarbetarkontroll" och flexibilitet.
* **LÖST (Refaktorering):** Skriptet refaktoreras för att läsa alla sökvägar (Drop Folder, Lake Store, Asset Store) från `dfm_config.yaml`, vilket återställer "medarbetarkontrollen".

* **Problemlösning (Transformation):** Första MVP misslyckades på grund av det felaktiga `tika`-biblioteket (krävde Java-server). Andra försöket (med `textract`) misslyckades på grund av trasig paketmetadata.
* **LÖST (Teknik-Pivot):** Vi gjorde en teknik-pivot till en mer robust arkitektur: att använda explicita, välunderhållna Python-bibliotek (`pypdf` och `python-docx`) för att explicit hantera filtyper.

* **Implementering av Loggning:** Ett nytt krav lades till: "centraliserad loggning" för "robust felsökning och kontroll". Detta implementerades genom att lägga till `log_path` i `dfm_config.yaml` och konfigurera en `logging.Logger` som skriver till filen, samtidigt som diskret `print` i terminalen behölls för realtidsfeedback.

* **Verifiering (Slut):** Fil-dump-funktionen är funktionellt verifierad: filen upptäcks, flyttas till Asset Store, transformeras till "ren text", och sparas som en AMM-taggad `.md`-fil i Sjön.

## Utvecklingsfas: OBJEKT-5, 6 & 9 (Intelligens & Ljud)

* **Konflikt 10: "Spretig Metadata" vs. Struktur (OBJEKT-9)**
    * **Problem:** Om vi låter AI generera metadata fritt får vi en "smutsig" folksonomi. Om vi har ett statiskt register blir det stelt.
    * **Lösning (Konsolidering):** Vi accepterar "stökig" metadata vid insamling (Folksonomi) via en snabb modell (Gemini Flash) i Desktop-agenten. Vi städar den *i efterhand* via en konsolideringsprocess ("Dumb Grouping" + "Smart Consolidation") för att bygga en ren taxonomi.

* **Konflikt 11: UI-utveckling vs. Datavärde (OBJEKT-1)**
    * **Problem:** Att bygga ett custom UI ("Hantera material") tar tid från kärnfunktionaliteten.
    * **Lösning (Pivot):** Vi parkerar OBJEKT-1 och använder **Obsidian** som temporärt gränssnitt för att "se" och söka i datan. Detta frigör resurser till att göra datan smartare ("MYCKET metadata").

* **Konflikt 12: Ljudhantering & Beroenden (OBJEKT-6)**
    * **Problem 1:** Att göra transkribering i Desktop-agenten låser datorn. -> **Löst:** Asynkron "Transformations-agent".
    * **Problem 2:** Python 3.14 saknar `audioop`, vilket kraschade `pydub` (använt för chunking).
    * **Lösning (Förenkling):** Vi tog bort chunking-logiken helt. Gemini 1.5 Pro hanterar filer upp till 2GB via File API, så manuell uppdelning var onödig komplexitet. Vi bytte till en "Förenklad Arkitektur" utan `pydub`.
    * **Stabilitet:** Vi införde en strikt **Fallback-kedja** (Pro -> Fast -> Lite) för att hantera `503 Overloaded`-fel från Google API.

## Utvecklingsfas: Stabilitet & Struktur (OBJEKT-4, 5, 6)

* **Konflikt 13: Tidsblindhet ("Ingestion vs Creation Time")**
    * **Problem:** Systemet trodde att gamla filer skapades "just nu" när de kopierades till DropZone, vilket förstörde tidslinjen i minnet.
    * **Lösning:** Vi implementerade "Deep Metadata Extraction". Agenterna försöker nu läsa datum i följande prioritetsordning: 1. Intern metadata (PDF/Docx creation date). 2. Filnamn (Regex YYYYMMDD). 3. Filsystemet (st_birthtime). Detta garanterar korrekt historik.

* **Konflikt 14: Agent-krockar i DropZone (Race Conditions)**
    * **Problem:** Både Desktop-agenten och Transformations-agenten försökte bearbeta ljudfiler, vilket ledde till dubbelarbete och logg-spam.
    * **Lösning (Traffic Control):** Vi införde strikt "Separation of Concerns" baserat på filtyp i `my_mem_config.yaml`.
        * **Desktop-agenten:** Äger DropZone. Flyttar *allt* till AssetStore. Bearbetar *endast* dokument.
        * **Transcribern:** Bevakar AssetStore. Bearbetar *endast* ljud/video.
        * Detta eliminerar krockar och gör flödet deterministiskt.

* **Konflikt 15: Taxonomi & Kaos (Konsolidering)**
    * **Problem:** Fri text-taggning från AI ger en spretig "Folksonomi" som är svår att söka i.
    * **Lösning:** Vi tillåter spretig insamling men bygger in en asynkron konsolideringsprocess i Indexeraren. Denna använder "Dumb Grouping" (Fuzzy match) och "Smart Consolidation" (LLM) för att slå ihop synonymer (t.ex. "IT-säkerhet" och "Security") till rena Koncept-noder i Grafen.

* **Konflikt 16: Infinite Loops (Watchdog)**
    * **Problem:** Transformations-agenten skapade temporära filer i mappen den själv bevakade, vilket triggade en oändlig loop.
    * **Lösning:** Vi härdade `AudioHandler` att explicit ignorera filer som börjar med `temp_upload_`.

## Konsolidering och Konsumtion (v3.0 - v3.1)

* **Konflikt 16: Insamlingens "Svarta Hål"**
    * **Problem:** Filer som redan låg i `Assets` (PDF/Docx) men inte var indexerade ignorerades av systemet eftersom `Desktop Agent` bara tittade på `DropZone`. Ljudfiler hanterades av en separat `Transcriber`.
    * **Lösning (The Functional Trinity):** Vi designade om hela insamlingslagret till tre renodlade roller:
        1.  **File Retriever:** Dum flyttare (DropZone -> Assets).
        2.  **Doc Converter:** Generell text-extraherare (Assets -> Lake).
        3.  **Transcriber:** Ljud-till-Text (Assets -> Assets).
    * **Resultat:** Systemet blev självläkande. Vid omstart skannas `Assets` och allt som saknas i `Lake` byggs om.

* **Konflikt 17: "Nyckelhålseffekten" i Chatten**
    * **Problem:** Användaren frågade "Vad sa han idag?", men vektorsökningen letade efter orden "han" och "idag", vilket gav noll träffar.
    * **Lösning (Hjärnan 2.0):** Vi införde **Query Rewriting** med en "Dual Model"-arkitektur.
        * En snabb modell (Flash Lite) skriver om frågan baserat på historik och datum ("idag" -> "2025-11-20").
        * En smart modell (Pro) genererar svaret.
        * Vi lade även till **Bio-Injection** i systemprompten så AI:n vet vem användaren är ("Joakim Ekman, Digitalist").

* **Konflikt 18: Slack-brus**
    * **Problem:** Att spara varje Slack-meddelande som en fil skulle dränka systemet.
    * **Lösning (Daily Digest):** Vi byggde en `Slack Archiver` som väntar tills dygnet är slut och sparar *en* sammanhängande textfil per kanal och dag. För DMs valdes strategin "Forward to Inbox" (HIIT).

* **UX-lyftet:**
    * Terminalen var svårläst. Vi implementerade biblioteket `rich` för att rendera Markdown, paneler och färger direkt i CLI:t.
    * Vi skapade en Mac-app (AppleScript) för one-click-start.

## UX-Polering & Launcher-Stabilitet (v3.2 - "The Overwriter")

* **Konflikt 19: Launcher UX vs. Utvecklarbehov (Debug Mode)**
    * **Problem:** Användaren ville ha en enkel start ("One Click") men ibland behövs djup insyn i loggarna (backend). Två separata filer är kladdigt.
    * **Lösning:** Vi implementerade **"Smart Startup"** i AppleScript. En dialogruta visas i 3 sekunder. Ingen åtgärd = Standard (Snyggt). Klick = Debug (Rått). Detta ger "Zero Friction" till vardags men "Full Control" vid behov.

* **Konflikt 20: "Zombien" (Race Conditions i Terminalen)**
    * **Problem:** När Launchern startade Terminalen, hann macOS ofta skapa ett tomt standardfönster ("Zombien") innan vårt script hann köra. Detta ledde till dubbla fönster och oreda.
    * **Försök 1 (The Polite Guest):** Scriptet försökte vänta snällt. Resultat: Zombien överlevde.
    * **Lösning (The Overwriter):** Vi bytte strategi till **"Aggressiv Kapning"**. Scriptet väntar nu *aktivt* på att ett fönster ska dyka upp, och "kapar" det (återanvänder det) oavsett vad det gör.
    * **Slutsats:** I UI-automatisering är deterministisk aggressivitet ofta bättre än "snäll" väntan.

* **Konflikt 21: Distribution & Miljö (The Venv Trap)**
    * **Problem:** Launchern kraschade ("File not found" / "Module not found") för att den: 1) Inte hittade rätt mapp (cd file vs dir). 2) Inte använde den virtuella miljön (`venvP312`).
    * **Lösning:** Vi hårdkodade (temporärt) sökvägarna i scriptet för stabilitet och pekade explicit ut Python-tolken i `venv`.
    * **Framtid (OBJEKT-27):** För att kunna distribuera detta till andra ("Nivå 3") krävs en "Installer" som genererar detta script dynamiskt vid installation.

## Livscykel & Reaktivitet (v3.2)

* **Konflikt 22: Det "Tidskrävande Dokumentet" (Versionshantering)**
    * **Problem:** Mänskligt minne skriver över gammal information med ny (t.ex. utkast dag 1 vs final dag 5). Systemet sparade båda som likvärdiga "sanningar", vilket skapar dubbletter och förvirring i chatten.
    * **Analys:** Vi behöver uppnå "Evolutionary Memory". Enkelt "append" fungerar inte för strukturerade dokument (Word/PDF).
    * **Strategi:** Vi identifierade tre vägar: A) Kill Switch (Ersätt), B) Linked Evolution (Graf-historik), C) Content Hashing.
    * **Beslut:** Vi utreder en hybridmodell (OBJEKT-29). För identiska filnamn i Nivå 1 lutar vi åt "Ersätt". För delade dokument lutar vi åt "Länkad historik".

* **Konflikt 23: Det "Döda" Chat-minnet (Refresh)**
    * **Problem:** Chatten laddade DB-anslutningen *en* gång vid start. Om Indexeraren lade till ny kunskap medan chatten var igång, var chatten "blind" för detta tills omstart.
    * **Lösning:** Vi flyttar DB-uppkopplingen (eller collection-hämtningen) in i sök-loopen. Detta kostar några millisekunder men garanterar att chatten alltid har "realtids-access" till det senaste minnet.

## UX-Polering & Launcher-Stabilitet (v3.2 - "The Overwriter")

* **Konflikt 19: Launcher UX vs. Utvecklarbehov (Debug Mode)**
    * **Problem:** Användaren ville ha en enkel start ("One Click") men ibland behövs djup insyn i loggarna.
    * **Lösning:** Vi implementerade **"Smart Startup"** i AppleScript. En dialogruta visas i 3 sekunder. Ingen åtgärd = Standard (Snyggt). Klick = Debug (Rått). Detta ger "Zero Friction" till vardags men "Full Control" vid behov.

* **Konflikt 20: "Zombien" (Race Conditions i Terminalen)**
    * **Problem:** När Launchern startade Terminalen, hann macOS skapa ett tomt standardfönster ("Zombien") innan scriptet hann köra, vilket ledde till dubbla fönster.
    * **Lösning (The Overwriter):** Vi bytte strategi till **"Aggressiv Kapning"**. Scriptet väntar nu *aktivt* på att ett fönster ska dyka upp och återanvänder det oavsett status.

* **Konflikt 21: Distribution & Miljö (The Venv Trap)**
    * **Problem:** Launchern kraschade ("Module not found") för att den inte använde den virtuella miljön.
    * **Lösning:** Vi hårdkodade sökvägarna (temporärt) och pekade explicit ut Python-tolken i `venv`.

## Livscykel & Reaktivitet (v3.2)

* **Konflikt 22: Versionshantering (Det "Tidskrävande Dokumentet")**
    * **Problem:** Systemet sparade både utkast och slutversioner som likvärdiga "sanningar", vilket skapade dubbletter.
    * **Beslut:** Vi utreder en hybridmodell (OBJEKT-29). För identiska filnamn i Nivå 1: "Ersätt". För delade dokument: "Länkad historik".

* **Konflikt 23: Det "Döda" Chat-minnet (Refresh)**
    * **Problem:** Chatten laddade DB-anslutningen *en* gång vid start. Nya minnen syntes inte förrän omstart.
    * **Lösning:** Vi flyttade DB-uppkopplingen in i sök-loopen. Det garanterar "realtids-access".

## Framtidsanalys: Agentic Reasoning & Dataintegritet

* **Konflikt 24: Sökning vs Resonemang (One-Shot Perfection)**
    * **Insikt:** Ett test med tidrapportering visade att enkel vektorsökning ("Hämta topp 20") missar perifer kontext.
    * **Lösning ("Hjärnan 3.0"):** Chatten måste uppgraderas till en **"Agentic Reasoning Loop"** (`Plan -> Iterate -> Synthesize`). Den ska själv bryta ner en fråga ("Min vecka") till flera del-sökningar och använda Grafen (Kùzu) för exakthet.

* **Konflikt 25: Det Enkelriktade Minnet (Read vs Write)**
    * **Problem:** Chatten är "Read-Only". Användaren kan inte kasta in snabba tankar ("Kom ihåg att...").
    * **Lösning ("Quick Save"):** Chatten ska kunna agera insamlare genom att skapa `.txt`-filer direkt i `Asset Store`.

* **Konflikt 26: Data-Brus vs Hallucinationer (Tvätten)**
    * **Problem:** Indexering av rådata (JSON/Loggar) smutsar ner sökindexet.
    * **Förslag (Avvisat):** "Narrativa berättelser" (Risk för hallucination).
    * **Beslut ("Hybrid Parser"):** Vi inför strategin **"Code First, AI Last"** i DocConverter.
        1.  Försök parsa deterministiskt med kod (Pandas/JSON).
        2.  Använd endast LLM ("Strict Washer") som fallback för trasig data.
        3.  Krav: Faktatät normalisering till Markdown. Inga narrativ.
        
* **Konflikt 27.  **Myten om Realtid (Process-Isolering):**
    * *Observation:* Användaren bevisade genom test att chatten var "blind" för nya filer trots att koden laddade om databas-klienten vid varje sökning. En omstart av processen gjorde filerna synliga direkt.
    * *Lärdom:* Databas-lagret (Chroma) har en caching/låsning på process-nivå som gör äkta realtid omöjlig utan process-omstart eller IPC (Inter-Process Communication). Vi måste designa arkitekturen utifrån detta faktum.

## Lärdomar från Fältet: Semantiskt Glapp & AI-Psykologi (v3.3)

Under arbetet med v3.2 identifierades tre fundamentala insikter om AI-driven systemutveckling:

1.  **Nödvändigheten av "Commit-protokoll" (Metod):**
    * *Observation:* Utan strikta tillstånd ("Planering" vs "Exekvering") "glider" samarbetet.
    * *Lösning:* Införandet av binära kommandon (`KÖR`, `NOTERA`) skapar nödvändig struktur.
2.  **"Lösnings-tvånget" (Avsaknad av Metakognition):**
    * *Observation:* När AI:n saknade data (fel datum), stannade den inte upp ("Vänta nu?"). Den gissade istället.
    * *Insikt:* AI-modeller drivs av sannolikhet, inte sanning. De saknar en inre "Skeptiker". Systemet måste därför designas med externa bromsar (deterministisk kod) som tvingar fram stopp när data är tvetydig.

## Operation: Strict Mode & Timezone Alignment (v4.0)

### Konflikt 28: Den Brutna Kedjan (ID-kaos)
* **Problem:** Filer tappade sina IDn när de vandrade mellan agenter. `Transcriber` genererade filer utan ID, vilket fick `DocConverter` att skapa nya IDn, vilket ledde till att `Indexeraren` skapade dubbletter.
* **Analys:** Vi saknade en strikt "Namnstandard" som tvingades igenom systemet.
* **Lösning ("The UUID Enforcer"):** Vi införde en hård regel: **Ingen fil får existera i `Assets` utan suffixet `_[UUID]`.**
    * `File Retriever` fick rollen "Normaliserare": Den flyttar alltid UUID till slutet av filnamnet.
    * `DocConverter` och `Transcriber` fick "Strict Mode": De vägrar nu röra filer som saknar detta suffix.

### Konflikt 29: Tidsförskjutningen (UTC vs Stockholm)
* **Problem:** Metadata visade UTC-tid, vilket gjorde sökningar som "Vad hände klockan 15?" felaktiga för en användare i Sverige.
* **Lösning:** Vi införde **"Timezone Awareness"** via konfiguration (`system.timezone: Europe/Stockholm`). Alla agenter konverterar nu interna tidsstämplar till denna zon innan de skrivs till disk eller databas.

### Konflikt 30: Datadubbletter (Ljud vs Text)
* **Problem:** Eftersom ljudfiler och textfiler döptes om oberoende av varandra, tappade de kopplingen. Systemet transkriberade samma ljudfil gång på gång.
* **Lösning:** Vi genomförde en "Magic Reset". Vi raderade alla genererade textfiler och tvingade systemet att bygga om dem från de (nu korrekt namngivna) ljudfilerna. Detta återställde 1:1-relationen.
### Konflikt 31: Metadata-kontraktet (DATUM_TID)
* **Problem:** `DocConverter` fick gissa om en fil var en ljudtranskribering (Header: `INSPELAT`) eller Slack-logg (Header: `DATUM`), vilket ledde till felaktiga tidsstämplar.
* **Lösning:** Vi etablerade ett strikt kontrakt. Alla producerande agenter (Slack, Transcriber) skriver nu nyckeln `DATUM_TID` med ISO-format. `DocConverter` litar blint på denna nyckel.

## Operation: Hjärnan 3.0 & OTS-Taxonomin (v4.0)

### Konflikt 31: Det Låsta Minnet (Process Concurrency)
* **Observation:** När vi försökte introducera Graf-sökning i chatten, kraschade systemet med `IO Exception: Could not set lock`.
* **Analys:** Vi försökte köra `Graph Builder` och `Chat` samtidigt mot en inbäddad databas (Kùzu). Till skillnad från en server-databas kan Kùzu bara ha en skrivare åt gången.
* **Slutsats:** Vi genomförde en "Kirurgisk Separation". Vi bröt ut `Graph Builder` från den ständigt körande `start_services.py` och gjorde den till ett **Batch-jobb**.
    * *Princip:* Konsolidering (Graf) körs i batch. Chatten körs i realtid.

### Konflikt 32: Den Luddiga Ontologin (OTS-modellen)
* **Observation:** Vår AI-genererade taxonomi skapade kategorier som "Organisation & Personal" som innehöll allt från externa bolag till sjukanmälan. Det gick inte att söka effektivt.
* **Lösning:** Vi införde en strikt hierarki baserad på abstraktionsnivå (OTS):
    1.  **Strategiskt:** (Vision, Affär, Kultur).
    2.  **Taktiskt:** (Projekt, Metodik, Organisation).
    3.  **Operativt:** (Händelser, Admin, Verktyg).
* **Genombrott:** Genom att lyfta in **Kultur** på strategisk nivå och döpa om **Kompetens** till **Organisation** fick vi en hemvist för både mjuka och hårda värden.

### Konflikt 33: "Zombie-Processer" och SDK-Syntax
* **Observation:** Under implementationen stötte vi på SDK-förändringar i `google-genai` (v1.0 syntax) där `genai.configure` inte längre fungerade.
* **Lösning:** Vi standardiserade på `genai.Client`-syntaxen i alla agenter.
* **UX:** Vi införde `rich.live` för att rendera Markdown-svar i realtid i terminalen, med felhantering för "NoneType"-chunks i strömmen.

## Operation: The Hunter & The Judge (Sök-krisen & Lösningen)

* **Konflikt 34: Semantisk Blindhet (Var är Industritorget?)**
    * **Problem:** Användaren sökte efter "Industritorget" (ett specifikt projekt/kund). Vektorsökningen (Chroma) returnerade 0 relevanta träffar trots att ordet fanns i metadata och text.
    * **Analys:** "Semantisk Utspädning". För en generell LLM är interna egennamn brus. Vektorn hittar "liknande koncept" (Arkitektur) men missar det exakta namnet.
    * **Lösning ("The Hunter"):** Vi införde en deterministisk sök-loop ("Jägaren").
        1.  AI (Flash Lite) extraherar `critical_keywords` (och rättar stavfel).
        2.  Python scannar fysiskt alla `.md`-filer i `Lake` efter dessa exakta strängar.
        3.  Dessa träffar tvingas in i kontexten med högsta prioritet.

* **Konflikt 35: "Format-fällan" (Byråkraten vs Detektiven)**
    * **Problem:** När användaren bad om en "Mötesanteckning" kastade systemet bort relevanta Slack-loggar för att de inte *var* mötesanteckningar.
    * **Lösning ("The Judge"):** Vi implementerade en **Re-ranking Pipeline**.
        1.  Planeraren instrueras att söka efter *Informationen*, inte *Formatet*.
        2.  En separat "Domare" (Flash Lite) poängsätter alla träffar (från Jägaren och Vektorn) mot användarens intention innan de skickas till slutlig syntes.
        3.  Syntesen (Gemini Pro) får i uppdrag att *skapa* formatet baserat på rådatan.

* **Konflikt 36: Hårdkodade Personligheter**
    * **Problem:** För att trimma systemets beteende krävdes kodändringar i `my_mem_chat.py`.
    * **Lösning:** Vi bröt ut alla system-prompter till `config/chat_prompts.yaml`. Detta möjliggör snabb "Prompt Engineering" utan omstart av applikationen.

* **Strategisk Insikt: Från Sök till Rådgivning ("The Bio-Graph")**
    * Under felsökningen identifierades att systemet idag är reaktivt (Svarar på frågor) men målet är proaktivt (Ger råd).
    * **Vision:** För att bli en rådgivare måste systemet känna till Användarens *Intention* och *Preferenser* innan frågan ställs. Vi konceptualiserade **"The Bio-Graph"** – en levande konfigurationsfil som styr systemets "glasögon" baserat på användarens unika världsbild.

## Operation: Code First & UI Sync (v4.2 2025-11-25)

* **Konflikt 37: Datatyp-kraschen (List vs Str)**
    * **Problem:** Transcriber kraschade med `TypeError` när Gemini-modellen returnerade transkriberingen som en lista av segment istället för en sträng.
    * **Analys:** Vår kod antog en datastruktur som API:et inte garanterade.
    * **Lösning:** Vi gjorde koden "defensiv" genom att explicit kontrollera datatyp och använda `join` om svaret är en lista.

* **Konflikt 38: "Fullbredds-sjukan" (UI-haveriet)**
    * **Problem:** Terminal-UI:t (Rich) skalade paneler till full bredd, vilket gjorde texten svårläst och ritade sönder gränssnittet vid storleksändring.
    * **Lösning:** Vi tvingade fram **"Left-Aligned Fixed Width"**. Genom att sätta `Console(width=90, justify="left")` globalt, skapade vi ett stabilt och läsbart "dokument-fokus" i terminalen.

* **Konflikt 39: Debug-glappet (Launcher vs Python)**
    * **Problem:** Launchern (AppleScript) hade en Debug-knapp, men Python-koden visste inte om den trycktes in. "Skvallret" (resonemanget) syntes inte.
    * **Lösning:** Vi byggde en **Argument-brygga**. Launchern skickar nu flaggan `--debug` till Python, som använder `argparse` för att villkorligt visa Planerings- och Jägar-panelerna. Vi ändrade även Launchern att hålla backend-fönstret synligt i debug-läge.

* **Konflikt 40: Tabell-döden (Excel som Text)**
    * **Problem:** Att låta en LLM läsa Excel/CSV som råtext är ineffektivt och leder till hallucinationer om vilken siffra som hör till vilken kolumn.
    * **Strategi ("Code First"):** Vi återgick till principen att deterministisk kod är överlägsen AI för strukturerad data.
    * **Implementation (OBJEKT-33):** Vi integrerade `pandas`, `openpyxl` och `tabulate` i `DocConverter`. Systemet konverterar nu Excel-flikar och CSV-filer till exakta Markdown-tabeller *innan* de når sjön.

## Operation: Insikt över Data (v4.3 "The Inverted T")

### Konflikt 41: Data-återgivning vs Aggregerad Insikt ("Uppochnervänd T-sökning")

* **Problem:** Under stresstestning (simulering med AI-persona) upptäcktes att MyMemory fungerade som ett *arkiv* snarare än ett *minne*. Systemet returnerade korrekt data ("Ni diskuterade Gemini Pro den 25/11") men gav inget *mervärde*. Användaren var ju redan där – hen behöver inte en sammanfattning av vad som sades.

* **Fundamental Insikt:** Hela poängen med MyMemory är att ge **aggregerade svar** – att tillföra något MER än att bara återge "vad sa vi på mötet". Systemet ska kunna förstå att en specifik kontext *stärks* av att användaren OCKSÅ sa/gjorde DETTA i en annan kontext.

* **Användarens Förväntan:** Man kan utgå från att användaren *var där*. De behöver inte data – de behöver insikt.

* **Konceptet "Uppochnervänd T":**
    ```
    Normal T-sökning:      Bred sökning → Filtrera → Smalt svar
                           ████████████
                               ████
                                ██

    Uppochnervänd T:       Specifik fråga → Rikt, kopplat svar med mervärde
                                ██
                               ████
                           ████████████
    ```

* **Konkret Exempel:**
    * **Nuvarande (Data):** "Vad sa vi på Adda-mötet?" → "Ni beslutade Gemini Pro, datum var 25/11..."
    * **Målet (Insikt):** "Vad sa vi på Adda-mötet?" → "Ni beslutade Gemini Pro. Det är intressant eftersom ni två veckor tidigare diskuterade modellagnostik med Tim, och förra fredagen nämnde du oro för vendor lock-in i Slack. Den kombinationen tyder på att ni bör dokumentera exit-strategin innan PoC:en växer."

* **Skillnaden:**
    | Arkiv | Minne |
    |-------|-------|
    | "Du sa X på mötet" | "Du sa X, och det påminner mig om att Y nämndes förra veckan, vilket betyder Z" |
    | Reaktiv data | Proaktiv insikt |
    | Svarar på frågan | Ger mervärde utöver frågan |

* **Tekniska Implikationer:**
    1. **Synthesizern måste tänka annorlunda:** Inte "sammanfatta dokument" utan "skapa insikt genom kopplingar"
    2. **Graf-data blir kritisk:** Veta att Adda-mötet → relaterat till → Tim-diskussion → relaterat till → Slack-oro
    3. **Temporal awareness:** "Du sa detta INNAN du sa detta, vilket betyder..."
    4. **Ny prompt-strategi:** "Användaren VAR DÄR. Ge inte data – ge mervärde."

* **Slutsats:** MyMemory ska inte vara Google (sök → resultat). Det ska vara en **rådgivare** (fråga → insikt baserad på aggregerad kunskap).

* **Backlogg:** OBJEKT-41 (Aggregerad Insikt-prompt)

### Konflikt 42: Felstavade Namn & Lärande Metadata ("Entity Resolution")

* **Problem:** Transkribering av möten genererar ofta felstavade namn. Exempel: "Sänk" istället för "Cenk Bisgen". Systemet behandlar dessa som olika personer och missar kopplingar.

* **Observation:** Med data från flera inspelningar och dokument har systemet tillräcklig kontext för att förstå att "Sänk" = "Cenk Bisgen" = "Cenk" = samma person.

* **Designprincip (Data-lager):**
    ```
    ┌─────────────────────────────────────────────────────────┐
    │ ASSETS (heligt - aldrig röra)                          │
    │ "Sänk sa att projektet..."                             │
    └─────────────────────────────────────────────────────────┘
                            ↓
    ┌─────────────────────────────────────────────────────────┐
    │ LAKE (stabil - berikad vid insamling)                  │
    │ entities: ["Sänk"]                                      │
    └─────────────────────────────────────────────────────────┘
                            ↓
    ┌─────────────────────────────────────────────────────────┐
    │ INDEX/GRAF (levande - lär sig över tid)                │
    │                                                         │
    │  Person: Cenk Bisgen                                   │
    │  ├── canonical_name: "Cenk Bisgen"                     │
    │  ├── aliases: ["Sänk", "Cenk", "Bisgen", "Senk"]      │
    │  ├── team: Drive                                       │
    │  ├── confidence: 0.95                                  │
    │  └── learned_from: [doc1, doc2, doc3...]              │
    │                                                         │
    └─────────────────────────────────────────────────────────┘
    ```

* **Insikt:** Grafen är det enda lagret som ska "lära sig" över tid. Assets och Lake förblir stabila.

* **Användningsfall vid sökning:**
    1. Användaren frågar: "Vad sa Cenk på mötet?"
    2. Systemet slår upp "Cenk" i Grafen
    3. Hittar aliases: `["Sänk", "Cenk", "Bisgen"]`
    4. Söker Lake efter ALLA varianter
    5. Hittar dokument där "Sänk sa att..."
    6. Svarar: "Cenk Bisgen sa att..." (kanoniskt namn)

* **Lärande-process (Konsolidering):**
    1. Skannar alla Lake-dokument periodiskt
    2. Extraherar alla entiteter av typ "Person"
    3. Fuzzy-matchar namn (Levenshtein-distans, fonetisk likhet)
    4. Hittar korrelationer: "Sänk" och "Cenk Bisgen" nämns i samma mötes-kontext
    5. Skapar/uppdaterar alias i Graf-noden
    6. Ökar `confidence` när mönstret bekräftas i fler dokument

* **Utökade tillämpningar:**
    * Smeknamn: "Jocke" → "Joakim Ekman"
    * Organisationer: "Digi" → "Digitalist Open Tech"
    * Projekt: "Adda-grejen" → "Adda AI PoC"

* **Slutsats:** Systemet ska bli smartare över tid utan att förstöra källdata. Grafen är "minnet" som lär sig, Lake är "arkivet" som bevarar.

* **Backlogg:** OBJEKT-44 (Entity Resolution & Alias Learning)

### Konflikt 43: Tidsblindhet i Frågor ("Temporal Intelligence")

* **Problem:** Chatten förstår inte relativa tidsreferenser. När användaren säger "igår" eller "förra veckan" tolkas det antingen bokstavligt (sträng-matchning) eller ignoreras helt. Systemet har ingen kontext om *när* frågan ställs.

* **Observation (Simulering 2025-12-03):**
    - Uppgift "Inköpslänken Scoping": Användaren sa "mötet igår" men fick svar om ett möte från 25 november (en vecka tidigare).
    - Uppgift "Beläggning Q1 2026": Systemet levererade detaljerad info i Svar 1, men i Svar 2 hävdade det att samma information *inte fanns*.
    - Flera gånger utbrast användaren: "25 november var ju förra veckan!"

* **Orsaksanalys:**
    1. **Planerings-steget** extraherar inte tidsreferenser som strukturerad data.
    2. **Jägaren/Vektorn** söker på "igår" som en sträng, inte som ett datum.
    3. **Syntesen** har ingen aning om vilken dag frågan ställdes.

* **Designprincip:**
    ```
    Fråga: "Vad hände igår?"
    Frågedatum: 2025-12-03
    
    ┌─────────────────────────────────────────────────────────┐
    │ PLANERING (Query Enrichment)                           │
    │ - Extrahera: "igår" → 2025-12-02                       │
    │ - Skapa filter: timestamp_created BETWEEN 12-02 00:00  │
    │                                     AND 12-02 23:59    │
    └─────────────────────────────────────────────────────────┘
                            ↓
    ┌─────────────────────────────────────────────────────────┐
    │ SÖKNING (med temporal prioritering)                    │
    │ - Filtrera dokument på timestamp                        │
    │ - Prioritera dokument nära relevant period             │
    └─────────────────────────────────────────────────────────┘
                            ↓
    ┌─────────────────────────────────────────────────────────┐
    │ SYNTES (med temporal kontext)                          │
    │ - "Frågan ställdes 2025-12-03"                         │
    │ - "Relevant period: 2025-12-02"                        │
    │ - "Tillgängliga dokument från denna period: [...]"     │
    └─────────────────────────────────────────────────────────┘
    ```

* **Slutsats:** Temporal Intelligence är en förutsättning för att MyMemory ska kännas som ett *minne* och inte ett arkiv. Ett minne vet vad som hände "nyligen".

* **Backlogg:** OBJEKT-42 (Temporal Intelligence)

### Konflikt 44: Blinda Insamlingsagenter ("Context Injection vid Insamling")

* **Problem:** DocConverter och Transcriber genererar metadata utan kontext om vad systemet redan vet. De "jobbar i mörkret".

* **Observation (Kodanalys 2025-12-03):**
    - **DocConverter** (`my_mem_doc_converter.py`):
        - Laddar `taxonomy.json` (rad 89-99) men använder den BARA för att validera `graph_master_node`.
        - Har INGEN åtkomst till kända personer, projekt eller aliases.
        - Genererar `entities` fritt → inkonsekvent med existerande data.
    - **Transcriber** (`my_mem_transcriber.py`):
        - Har INGEN kontakt med taxonomi eller graf.
        - Gissar talare som "Talare 1", "Talare 2" (rad 229).
        - Resulterar i metadata som "Sänk" istället för "Cenk Bisgen".

* **Konsekvens:**
    ```
    ┌─────────────────────────────────────────────────────────┐
    │ IDAG: Varje agent gissar för sig                       │
    ├─────────────────────────────────────────────────────────┤
    │ Inspelning 1: "Sänk sa att..."                         │
    │ Inspelning 2: "Cenk Bisgen förklarade..."              │
    │ Slack-logg:   "Cenk skrev..."                          │
    │                                                         │
    │ → Tre olika varianter = Tre olika personer i systemet! │
    └─────────────────────────────────────────────────────────┘
    ```

* **Designprincip (Context Injection):**
    ```
    ┌─────────────────────────────────────────────────────────┐
    │ FÖRE METADATA-GENERERING                               │
    ├─────────────────────────────────────────────────────────┤
    │ 1. Hämta kontext från Graf:                            │
    │    - Kända personer: ["Joakim Ekman", "Cenk Bisgen"]   │
    │    - Kända aliases: {"Sänk": "Cenk Bisgen"}            │
    │    - Aktiva projekt: ["Adda PoC", "MyMemory"]          │
    │                                                         │
    │ 2. Injicera i AI-prompt:                               │
    │    "KÄNDA TALARE (använd dessa namn om möjligt):       │
    │     Joakim Ekman, Cenk Bisgen, Marie Björkengren"      │
    │                                                         │
    │ 3. Resultat:                                            │
    │    Transkribering: "Cenk Bisgen sa att..." (normaliserat)│
    └─────────────────────────────────────────────────────────┘
    ```

* **Relation till andra objekt:**
    - **OBJEKT-44** (Entity Resolution): Lär sig NYA aliases efteråt.
    - **OBJEKT-45** (detta): Använder KÄNDA aliases vid insamling.
    - Tillsammans bildar de en "closed loop" för entitetshantering.

* **Slutsats:** Bättre metadata vid insamling = mindre städning efteråt. Agenterna ska inte gissa – de ska veta.

* **Backlogg:** OBJEKT-45 (Context Injection vid Insamling)

### Konflikt 45: Pipeline-arkitektur v6.0 ("Rapport över Dokument")

* **Problem (v5.2):** Nuvarande pipeline har otydlig separation of concerns:
    - Planering, Jägaren, Vektorn, Domaren, Syntes – vem gör vad?
    - Domaren (AI) gör re-ranking, men baserat på vad?
    - Synthesizer får 30+ råa dokument (upp till 100k tecken)
    - 3 AI-anrop men oklart värde från varje

* **Observation (Diskussion 2025-12-03):**
    - Gemini föreslog: IntentRouter → ContextBuilder → Synthesizer (2 AI-anrop)
    - Problem: Vem skapar "rapporten" som Synthesizer behöver?
    - Insikt: Synthesizer ska inte få råa dokument – den ska få en kurerad rapport

* **Resonemang:**
    ```
    Alternativ A (Ursprungligt förslag):
    IntentRouter (AI) → ContextBuilder (Kod) → Synthesizer (AI)
    Problem: Synthesizer måste själv filtrera 30 dokument → långsamt
    
    Alternativ B (Planner×2):
    Planner(Intent) → ContextBuilder → Planner(Rapport) → Synthesizer
    Problem: Otydlig SOC, en komponent med två lägen
    
    Alternativ C (Vald lösning):
    IntentRouter → ContextBuilder → Planner → Synthesizer
    ✅ Tydlig SOC: Varje komponent har ETT ansvar
    ✅ Planner skapar rapport, Synthesizer konsumerar rapport
    ✅ ContextBuilder är KOD, inte AI → snabbt, förutsägbart
    ```

* **Beslut: Pipeline v6.0**
    ```
    Input → IntentRouter → ContextBuilder → Planner → Synthesizer → Output
                (AI)           (Kod)         (AI)        (AI)
            Klassificera     Hämta data   Bygg rapport   Svara
    ```

* **Nyckelprinciper:**
    1. **Rapport > Dokument:** Synthesizer får aldrig rådata, bara en kurerad rapport
    2. **ContextBuilder är Kod:** Deterministisk, snabb, debuggbar – ingen AI
    3. **Tydlig SOC:** Varje komponent har exakt ett ansvar
    4. **HARDFAIL:** Varje steg rapporterar explicit om det misslyckas

* **Framtid (v7.0):**
    - Agentic loop: Om Synthesizer bedömer rapporten som svag → begär ny rapport
    - Planner kan fråga användaren om intent är oklar

* **Backlogg:** OBJEKT-46 (Pipeline v6.0 Refaktorering)
