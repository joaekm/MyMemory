---
unit_id: f3b9d8a1-b8f2-4e1a-8b0a-9d6f3c7e0002
owner_id: "joakim.ekman"
access_level: "Nivå_3_Delad_Organisation"
context_id: "PROJEKT_DFM_V1"
source_type: "System_Dokument"
source_ref: "dfm_summary.md"
data_format: "text/markdown"
timestamp_created: "2025-12-03T18:00:00Z"
policy_tags: []
original_binary_ref: null
---

# Projektets Konceptuella Sammanfattning (v5.0)

Detta dokument är en "torr" sammanfattning av de slutsatser som framkommit. För fullständigt resonemang, se `my_mem_koncept_logg.md`.

## 1. Mål & Användarnytta

* **Mål:** Bygga ett "Företagsminne" (Steg 1) som möjliggör en "Proaktiv Informationsagent" (Steg 2).
* **Användarnytta:** "Noll administrativt efterarbete" och "omedelbar, automatiserad klarhet" genom agent-genererade artefakter.
* **Nyckelinsikt (2025-12-03):** Systemet ska ge **insikt**, inte bara data. Användaren var ju där – hen behöver mervärde, inte sammanfattning.

## 2. Kärnprinciper

### HARDFAIL > Silent Fallback
Systemet ska misslyckas tydligt istället för att gissa. Inga tysta fallbacks – användaren ska veta varför något inte fungerade.

### OTS-Modellen (Taxonomi)
All kunskap struktureras i tre nivåer:
* **Strategisk:** Vision, Kultur, Affär (Varför/Vart).
* **Taktisk:** Projekt, Metodik, Organisation (Hur/Vad).
* **Operativ:** Händelser, Verktyg, Admin (Görandet).

### Trippel Lagring
* **Asset Store:** Originalfiler. Heligt – aldrig röra.
* **Lake:** Normaliserad Markdown. Stabil över tid.
* **Index:** Vektor + Graf. Lär sig över tid.

## 3. Dataflöde

```
DropZone → File Retriever → Asset Store
                              ↓
              ┌───────────────┴───────────────┐
              ↓                               ↓
         Transcriber                    Doc Converter
         (Ljud → txt)                   (Dok → md)
              ↓                               ↓
         Asset Store                       Lake
              ↓                               ↓
         Doc Converter ─────────────────────→ Lake
                                              ↓
                                    ┌─────────┴─────────┐
                                    ↓                   ↓
                              Vector Indexer      Graph Builder
                               (Realtid)            (Batch)
                                    ↓                   ↓
                                ChromaDB             KùzuDB
```

## 4. Konsumtion: MyMem Chat (v5.2)

**Pipeline:**
1. **Planering:** Analyserar intention, extraherar nyckelord och formulerar semantiska frågor.
2. **Insamling (Jägaren + Vektorn):** Fysisk textsökning + semantisk sökning.
3. **Bedömning (Domaren):** Re-rankar kandidater baserat på relevans.
4. **Syntes (Hjärnan):** Genererar svaret med strikt källhänvisning.

**Styrkor:**
- Hittar exakta nyckelord (löser "Vector Blindness")
- Separerar innehåll från format
- Full transparens via debug-läge

**Prestandaproblem (Simulering 2025-12-03):**
- Snitttid: 50.6 sekunder per fråga
- Max: 130 sekunder
- Syntesen står för ~70% av tiden

## 5. Stresstestning: Första Simuleringen

**Datum:** 2025-12-03
**Uppgifter:** 12 realistiska arbetsscenarier
**Resultat:** 6.5/10 (genomsnitt)

| Mätvärde | Resultat |
|----------|----------|
| Lyckade | 5/12 (42%) |
| Delvis lyckade | 3/12 (25%) |
| Misslyckade | 4/12 (33%) |
| Användaren gav upp | 9/12 (75%) |
| Sparade tid | 8/12 (67%) |

**Styrkor identifierade:**
- Stora synteser (Veckorapport 10/10, Almedalen 9/10)
- Slack-scanning fungerar utmärkt
- Detaljrikedom i svar

**Svagheter identifierade:**
- Temporal blindhet ("igår" förstås inte)
- Kontextbyte mellan frågor (motsäger sig själv)
- Långsam responstid
- Ingen aggregerad insikt

## 6. Nästa Fas: Kritiska Objekt

| Prio | Objekt | Titel | Problem |
|------|--------|-------|---------|
| **0** | OBJEKT-41 | Aggregerad Insikt | Ger data, inte insikt |
| **0** | OBJEKT-42 | Temporal Intelligence | Förstår inte "igår" |
| **0.5** | OBJEKT-43 | Summary-First Search | 50-130s svarstid |
| **1** | OBJEKT-44 | Entity Resolution | "Sänk" ≠ "Cenk Bisgen" |
| **1** | OBJEKT-45 | Context Injection | Agenter jobbar i mörkret |

## 7. Utvecklingsregler

Definierade i `.cursorrules`:

1. **HARDFAIL > Silent Fallback** – Inga tysta gissningar
2. **Fail Fast, Fail Loud** – Validera tidigt, logga allt
3. **Ingen AI-cringe** – Professionella konceptnamn (ej "Trädgårdsmästaren")

## 8. Teknisk Stack

| Komponent | Teknologi |
|-----------|-----------|
| Språk | Python 3.12 |
| Vektordatabas | ChromaDB |
| Grafdatabas | KùzuDB |
| AI-modeller | Gemini Pro/Flash (Google) |
| Embeddings | all-MiniLM-L6-v2 (Lokal) |
| UI | Rich (CLI) |

---
*Senast uppdaterad: 2025-12-03*
*Se `my_mem_arkitektur.md` för teknisk implementation.*
*Se `my_mem_backlogg.md` för aktiva objekt.*
