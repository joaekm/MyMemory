# Rebuild System

System för kronologisk återuppbyggnad av MyMemory efter hard reset.

## Översikt

Rebuild-systemet processerar data dag-för-dag med pauser för konsolidering mellan varje dag. Det stöder två faser:

1. **Foundation Phase**: Bygger grunden från textkällor (Slack, Docs, Mail, Calendar)
2. **Enrichment Phase**: Bearbetar ljud/transkript med kontext från grunden

## Komponenter

### `file_manager.py`
Hanterar filhantering, manifest och staging:
- **RebuildManifest**: Spårar vilka filer är processade
- **FileManager**: Samlar filer, grupperar per datum, hanterar staging

### `process_manager.py`
Hanterar processlivscykel och completion detection:
- **ServiceManager**: Startar/stoppar indexeringstjänster
- **CompletionWatcher**: Övervakar när filer är klara genom att kolla Lake och Failed-mappar

### `orchestrator.py`
Huvudorkestrator som koordinerar alla moduler:
- **RebuildOrchestrator**: Kör rebuild-processen dag-för-dag

### `hard_reset.py`
Raderar all data och återställer systemet till ursprungligt tillstånd.

## Användning

### 1. Hard Reset
```bash
python tools/tool_hard_reset.py --confirm
```

### 2. Foundation Phase
```bash
python tools/tool_staged_rebuild.py --confirm --phase foundation
```

### 3. Enrichment Phase
```bash
python tools/tool_staged_rebuild.py --confirm --phase enrichment
```

### Alternativ

- `--days N`: Begränsa till N dagar
- `--multipass`: Aktivera multipass-extraktion

## Workflow

1. **Samla filer**: Hitta alla filer för vald fas
2. **Flytta till staging**: Töm Assets-mappar
3. **För varje dag**:
   - Återställ dagens filer till Assets
   - Starta indexeringstjänster
   - Vänta på completion (filer i Lake eller Failed)
   - Stoppa tjänster
   - Kör Graph Builder (konsolidering)
   - Kör Dreamer (taxonomi-konsolidering)
   - Interaktiv granskning (om entiteter hittas)
4. **Återställ kvarvarande filer**: Flytta tillbaka från staging
5. **Rensa staging**: Ta bort staging-katalog

## Arkitektur

```
tool_staged_rebuild.py (CLI)
    ↓
RebuildOrchestrator
    ├── FileManager (manifest, file collection, staging)
    ├── ServiceManager (start/stop services)
    ├── CompletionWatcher (wait for completion)
    └── Graph Builder + Dreamer + Interactive Review
```

## Felsökning

### DuckDB-låskonflikter
- Graph Builder körs nu direkt i samma process (inte som subprocess)
- Detta ger full kontroll över GraphStore-anslutningar
- ServiceManager dödar kvarvarande processer efter stopp

### Timeout
- Standard timeout: 30 minuter utan aktivitet
- Öka `INACTIVITY_TIMEOUT_SECONDS` i `process_manager.py` om nödvändigt

### Filer processeras inte
- Kontrollera att watchdogs är aktiva (tjänster startade)
- Verifiera att filer finns i Assets efter återställning
- Kolla loggar för felmeddelanden

## Integration med Chat

Interactive review-systemet finns i `services/review/` och kan användas både under rebuild och i vanlig chat-användning:

```python
from services.review import run_interactive_review, apply_review_decisions
```
















