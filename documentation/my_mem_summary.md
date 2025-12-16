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

# Projektets Konceptuella Sammanfattning (v8.2)

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

## 4. Konsumtion: MyMem Chat (v8.2 "Pivot or Persevere")

**Pipeline:**
```
SessionEngine → IntentRouter → ContextBuilder → Planner (ReAct) → Synthesizer
```

1. **IntentRouter (v7.0):** Skapar Mission Goal, parsar tid, extraherar keywords/entities.
2. **ContextBuilder (v7.5):** Time-Aware Reranking, parallel Lake+Vektor-sökning.
3. **Planner (v8.2):** ReAct-loop som bygger "Tornet" (arbetshypotes) + "Bevisen" (fakta).
4. **Synthesizer:** Genererar svar från Planner-rapport.

**Nyckelkoncept:**
- **Tornet:** Iterativt byggd arbetshypotes
- **Bevisen:** Append-only faktalista
- **Pivot or Persevere:** Befintligt Torn+Facts skickas till nya frågor
- **Librarian Loop:** Two-stage retrieval (scan + deep read)

**Chattkommandon:**
- `/show` - Visa filnamn från senaste sökningen
- `/export` - Exportera top 10 filer till hotfolder (symlinks)
- `/learn` - Lär systemet nya alias

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

## 6. Senaste Förbättringar (v8.2)

### Pipeline v8.2 "Pivot or Persevere" (LÖST-55)
Helt ny pipeline-arkitektur med:
- **SessionEngine:** Central orchestrator för session state
- **IntentRouter v7.0:** Mission Goal + temporal parsing
- **ContextBuilder v7.5:** Time-Aware Reranking
- **Planner v8.2:** ReAct-loop med Tornet + Bevisen

### DuckDB Pivot (LÖST-54)
Migrerade från KuzuDB till DuckDB med:
- `GraphStore`-klass i `graph_service.py`
- `aliases`-kolumn för Entity Resolution
- `upgrade_canonical()` för flytande canonical

### DateService (LÖST-56)
Central datumhantering med prioritet: Frontmatter → Filnamn → PDF-metadata → Filesystem → HARDFAIL.

### Summary-First Search (LÖST-57)
- `TOP_N_FULLTEXT = 3`: Endast topp 3 får fulltext
- Time-Aware Reranking boostar nyare dokument
- Relevance Gate förhindrar spam-boost

## 7. Nästa Fas: Prioriterade Objekt

| Prio | Objekt | Titel | Status |
|------|--------|-------|--------|
| **0.5** | OBJEKT-42 | Temporal Intelligence | ⚠️ Delvis - filtering saknas |
| **1** | OBJEKT-41 | Aggregerad Insikt | ⚠️ Utvärdera Tornet |
| **1** | OBJEKT-44 | Entity Resolution | ⚠️ Delvis - inlärning saknas |
| **1** | OBJEKT-45 | Levande Metadata | ⚠️ Delvis - Context Injection saknas |
| **1.5** | OBJEKT-47 | Embedding-migration | ⚠️ **DEADLINE 2026-01-14** |
| **1.5** | OBJEKT-51 | Separera Entiteter | ⚠️ Delvis - städning behövs |
| **2** | OBJEKT-48 | Sessioner som Lärdomar | Öppen |
| **2** | OBJEKT-49 | MyMemory Engine | ⚠️ Delvis - SessionEngine finns |

## 8. Utvecklingsregler

Definierade i `.cursorrules`:

1. **HARDFAIL > Silent Fallback** – Inga tysta gissningar
2. **Fail Fast, Fail Loud** – Validera tidigt, logga allt
3. **Ingen AI-cringe** – Professionella konceptnamn (ej "Trädgårdsmästaren")

## 9. Teknisk Stack

| Komponent | Teknologi |
|-----------|-----------|
| Språk | Python 3.12 |
| Vektordatabas | ChromaDB |
| Grafdatabas | DuckDB (relationell graf via nodes/edges) |
| AI-modeller | Gemini Pro/Flash (Google) |
| Embeddings | all-MiniLM-L6-v2 (Lokal) |
| UI | Rich (CLI) |

---
*Senast uppdaterad: 2025-12-16*
*Se `my_mem_arkitektur.md` för teknisk implementation.*
*Se `my_mem_backlogg.md` för aktiva objekt.*
