# Plan: Lyfta Dream-flödet från POC till Produktion

## Bakgrund

POC-verktyget (`tools/tool_dreamer_dryrun.py`) innehåller tre viktiga förbättringar som saknas i produktions-Dreamer (`services/engines/dreamer.py`):

1. **Schema-beskrivningar i structural_analysis** - LLM får nodtyp-definitioner
2. **Kant-validering vid RE-CATEGORIZE** - Varnar/blockerar om relationer blir ogiltiga
3. **Context-pruning efter MERGE** - Automatisk kontextrensning

Dessutom finns två identifierade produktionsfixar i backloggen (OBJEKT-67):
- `recategorize_node()` validerar inte kanter
- `merge_nodes()` anropar inte `prune_context()`

## Analys: POC vs Produktion

### POC har men Produktion saknar:

| Funktion | POC (dryrun) | Produktion (dreamer.py) |
|----------|--------------|-------------------------|
| `_get_node_type_description()` | ✅ Rad 166 | ❌ Saknas |
| `_log_edge_validation()` | ✅ Rad 181 | ❌ Saknas |
| `_log_prune_simulation()` | ✅ Rad 220 | ❌ Saknas |
| `node_type_description` i prompt | ✅ Rad 633 | ❌ Prompten har placeholder men värdet sätts ej |
| Batch LLM-anrop | ✅ `batch_generate()` | ❌ Sekventiella anrop |

### Prompt-status:

`structural_analysis` prompten i `services_prompts.yaml` (rad 162-194) har redan:
- `{node_type_description}` placeholder
- Instruktion att jämföra mot SCHEMA-REGEL

Men `dreamer.py` skickar INTE denna parameter - den ignoreras.

## Implementationsplan

### Steg 1: Schema-beskrivningar i Dreamer (enkel)

**Fil:** `services/engines/dreamer.py`

1. Lägg till metod `_get_node_type_description(node_type: str) -> str`:
   - Hämta från `_get_schema_validator().schema["nodes"][node_type]["description"]`
   - Fallback till `"(Ingen beskrivning tillgänglig)"`

2. Uppdatera `check_structural_changes()`:
   - Lägg till `node_type_description=self._get_node_type_description(node.get("type"))` i `prompt.format()`

**Test:** Kör dryrun och verifiera att beskrivningen syns i loggen.

---

### Steg 2: Kant-validering vid RE-CATEGORIZE (medel)

**Filer:**
- `services/engines/dreamer.py`
- `services/utils/graph_service.py`

1. Lägg till metod `_validate_edges_for_recategorize(node_id: str, new_type: str) -> tuple[bool, list]`:
   - Hämta alla kanter via `graph_service.get_edges_from()` och `get_edges_to()`
   - Bygg `nodes_map` med den nya typen för denna nod
   - Anropa `SchemaValidator.validate_edge()` för varje kant
   - Returnera `(all_valid, invalid_edges)`

2. Uppdatera `run_resolution_cycle()`:
   - Före `recategorize_node()`: Validera kanter
   - Om ogiltiga kanter: Logga WARNING + skippa operation (HARDFAIL-strategi)
   - Alternativt: Ta bort ogiltiga kanter automatiskt (SOFT-strategi)

**Beslutspunkt:** Ska ogiltiga kanter blockera RE-CATEGORIZE eller ska de tas bort automatiskt?
- **Rekommendation:** HARDFAIL - logga och skippa. Användaren kan städa manuellt.

---

### Steg 3: Automatisk prune_context efter MERGE (enkel)

**Fil:** `services/engines/dreamer.py`

1. Uppdatera `run_resolution_cycle()`:
   - Efter `graph_service.merge_nodes()`: Anropa `self.prune_context(target_id)`
   - `prune_context()` finns redan och hanterar threshold (15+ entries)

**Notering:** `prune_context()` gör LLM-anrop. Vid många MERGE kan detta bli dyrt. Överväg att batcha pruning till slutet av cykeln.

---

### Steg 4: Batch LLM-anrop (optimering)

**Fil:** `services/engines/dreamer.py`

Nuvarande flöde:
```
för varje kandidat:
    structural_analysis() → 1 LLM-anrop
    för varje match:
        evaluate_merge() → 1 LLM-anrop
```

POC-flöde (snabbare):
```
batch_generate([alla structural prompts])  → N parallella anrop
batch_generate([alla merge prompts])        → M parallella anrop
```

1. Samla alla prompts i Fas 1 (structural)
2. Anropa `llm_service.batch_generate(prompts, TaskType.STRUCTURAL_ANALYSIS)`
3. Samla alla merge-prompts i Fas 2
4. Anropa `llm_service.batch_generate(prompts, TaskType.ENTITY_RESOLUTION)`
5. Logga resultat

**Risk:** Batch-logik är mer komplex. Om en prompt failar påverkar det inte andra.

---

## Filändringar (sammanfattning)

| Fil | Ändringar |
|-----|-----------|
| `services/engines/dreamer.py` | +`_get_node_type_description()`, +`_validate_edges_for_recategorize()`, uppdatera `check_structural_changes()`, uppdatera `run_resolution_cycle()` (prune + validering) |
| `services/utils/graph_service.py` | Ingen ändring krävs (metoder finns) |
| `config/services_prompts.yaml` | Ingen ändring krävs (placeholder finns) |

---

## Test-strategi

1. **Unit test:** `tools/test_property_chain.py` - verifiera att schema-injektion fungerar
2. **Dryrun:** `python tools/tool_dreamer_dryrun.py --limit 5` - jämför output före/efter
3. **Integration:** Kör `run_resolution_cycle(dry_run=True)` och granska loggen

---

## Ordning

```
1. Schema-beskrivningar   (30 min)  - Låg risk, hög effekt
2. Prune efter MERGE      (15 min)  - Låg risk, medel effekt
3. Kant-validering        (45 min)  - Medel risk, hög effekt
4. Batch LLM-anrop        (60 min)  - Medel risk, prestandaoptimering
```

**Rekommendation:** Börja med 1-3. Batch (4) kan vänta till nästa iteration.

---

## Öppna frågor

1. **Kant-validering strategi:** HARDFAIL (skippa) eller AUTO-CLEAN (ta bort ogiltiga kanter)?
2. **Prune batching:** Ska prune köras direkt efter varje MERGE eller i slutet av cykeln?
3. **Loggning:** Ska vi logga till separat fil (som dryrun) eller till systemloggen?
