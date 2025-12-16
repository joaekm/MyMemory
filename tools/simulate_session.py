#!/usr/bin/env python3
"""
Simuleringsverktyg för MyMemory
==============================
Stresstestar systemet genom att låta en AI-persona (Förhörsledaren) 
försöka lösa uppgifter genom att ställa frågor till MyMemory.

Features:
  - Smart avslut: Interrogator avgör själv när uppgiften är löst
  - Tidsmätning: Mäter tid per anrop
  - Effektivitetsmått: Rundor använda vs max
  - Inkrementell sparning: Sparar efter varje uppgift (inte bara i slutet)

Genererar två filer:
  - evaluation_report_[TIMESTAMP].md - Mänsklig översikt med mätbara resultat
  - technical_log_[TIMESTAMP].md - Fullständig debug-trace för AI-analys

Användning:
    # Enskild uppgift (max 10 rundor, avslutas när klar)
    python tools/simulate_session.py -r 10 -t "Skapa en veckorapport"
    
    # Batch från fil
    python tools/simulate_session.py -r 10 -f tools/simulation_tasks.txt
    
    # Generell testning (utan specifik uppgift)
    python tools/simulate_session.py -r 10
"""

import os
import sys
import argparse
import datetime
import json
import time
import yaml
import logging
from pathlib import Path

LOGGER = logging.getLogger('SimulateSession')

# Lägg till parent-mappen i path så vi kan importera services
sys.path.insert(0, str(Path(__file__).parent.parent))

from google import genai
from services.interface.chat import process_query, CONFIG, PROMPTS, MODEL_LITE

# --- SETUP ---
API_KEY = CONFIG['ai_engine']['api_key']
LOGS_DIR = Path(__file__).parent.parent / "logs"


def load_tasks_from_file(filepath: str) -> list:
    """
    Laddar uppgifter från en textfil.
    Format: Titel | Beskrivning (en per rad)
    """
    tasks = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '|' in line:
                title, description = line.split('|', 1)
                tasks.append({
                    "title": title.strip(),
                    "description": description.strip()
                })
            else:
                tasks.append({
                    "title": line[:50] + "..." if len(line) > 50 else line,
                    "description": line
                })
    return tasks


def get_swedish_weekday():
    """Returnerar svensk veckodag."""
    weekdays = ['måndag', 'tisdag', 'onsdag', 'torsdag', 'fredag', 'lördag', 'söndag']
    return weekdays[datetime.datetime.now().weekday()]


def generate_question(ai_client, task: dict, conversation_history: list) -> str:
    """
    Genererar nästa fråga från Förhörsledaren baserat på uppgift och historik.
    """
    history_text = ""
    if conversation_history:
        history_text = "\n".join([
            f"FRÅGA: {item['question']}\nSVAR: {item['answer'][:500]}..."
            if len(item['answer']) > 500 else f"FRÅGA: {item['question']}\nSVAR: {item['answer']}"
            for item in conversation_history
        ])
    else:
        history_text = "(Ingen tidigare konversation)"
    
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    weekday = get_swedish_weekday()
    
    instruction = PROMPTS['interrogator']['instruction'].format(
        task=task['description'],
        conversation_history=history_text,
        timestamp=timestamp,
        weekday=weekday
    )
    
    try:
        response = ai_client.models.generate_content(
            model=MODEL_LITE,
            contents=instruction
        )
        question = response.text.strip()
        question = question.strip('"\'')
        return question
    except Exception as e:
        LOGGER.warning(f"Fråge-generering misslyckades: {e}")
        return f"[FEL VID FRÅGE-GENERERING: {e}]"


def check_task_completion(ai_client, task: dict, conversation_history: list) -> dict:
    """
    Kontrollerar om Interrogator anser sig ha tillräcklig information,
    eller om hen vill ge upp (abort).
    
    Returns:
        dict: {"done": bool, "abort": bool, "confidence": 1-10, "reason": "..."}
    """
    history_text = "\n".join([
        f"FRÅGA: {item['question']}\nSVAR: {item['answer'][:500]}..."
        if len(item['answer']) > 500 else f"FRÅGA: {item['question']}\nSVAR: {item['answer']}"
        for item in conversation_history
    ])
    
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    weekday = get_swedish_weekday()
    
    instruction = PROMPTS['interrogator_check']['instruction'].format(
        task=task['description'],
        conversation_history=history_text,
        timestamp=timestamp,
        weekday=weekday
    )
    
    try:
        response = ai_client.models.generate_content(
            model=MODEL_LITE,
            contents=instruction
        )
        text = response.text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        return result
    except Exception as e:
        LOGGER.warning(f"Task completion check misslyckades: {e}")
        return {"done": False, "confidence": 0, "reason": f"Fel: {e}"}


def evaluate_task_completion(ai_client, task: dict, conversation_history: list) -> dict:
    """
    Utvärderar hur väl MyMemory hjälpte användaren lösa uppgiften.
    """
    conversation_text = "\n\n".join([
        f"FRÅGA {i+1}: {item['question']}\n\nSVAR {i+1}: {item['answer']}"
        for i, item in enumerate(conversation_history)
    ])
    
    instruction = PROMPTS['evaluator']['instruction'].format(
        task=task['description'],
        conversation=conversation_text
    )
    
    try:
        response = ai_client.models.generate_content(
            model=MODEL_LITE,
            contents=instruction
        )
        text = response.text.replace("```json", "").replace("```", "").strip()
        evaluation = json.loads(text)
        return evaluation
    except Exception as e:
        LOGGER.warning(f"Utvärdering misslyckades: {e}")
        return {
            "score": 0,
            "verdict": "Utvärderingsfel",
            "summary": f"Kunde inte utvärdera: {e}",
            "strengths": [],
            "gaps": ["Utvärdering misslyckades"],
            "reasoning": str(e)
        }


def run_simulation(task: dict, max_rounds: int, ai_client) -> dict:
    """
    Kör en simulering för en uppgift.
    Avslutas när Interrogator anser sig klar, ger upp (abort), eller max_rounds nås.
    """
    print(f"\n{'='*60}")
    print(f"UPPGIFT: {task['title']}")
    print(f"{'='*60}")
    
    conversation_history = []
    chat_history = []
    rounds_data = []
    total_duration = 0
    completed_early = False
    aborted = False
    completion_reason = ""
    
    for round_num in range(1, max_rounds + 1):
        print(f"\n  Runda {round_num}/{max_rounds}...", end=" ", flush=True)
        
        # Generera fråga
        question = generate_question(ai_client, task, conversation_history)
        
        # Mät tid för MyMemory-anropet
        start_time = time.time()
        result = process_query(question, chat_history, collect_debug=True)
        duration = time.time() - start_time
        total_duration += duration
        
        answer = result['answer']
        sources = result['sources']
        debug_trace = result['debug_trace']
        debug_trace['duration_seconds'] = round(duration, 2)
        
        # Uppdatera historik
        conversation_history.append({
            "question": question,
            "answer": answer,
            "sources": sources,
            "debug_trace": debug_trace
        })
        chat_history.append({"role": "user", "content": question})
        chat_history.append({"role": "assistant", "content": answer})
        
        # Spara rund-data
        rounds_data.append({
            "round": round_num,
            "question": question,
            "answer": answer,
            "sources": sources,
            "debug_trace": debug_trace,
            "duration_seconds": round(duration, 2)
        })
        
        # Status (hanterar både v5.2 och v6.0 format)
        if debug_trace.get('context_builder'):
            # V6.0 format
            stats = debug_trace.get('context_builder', {}).get('stats', {})
            hits_l = stats.get('lake_hits', 0)
            hits_v = stats.get('vector_hits', 0)
            intent = debug_trace.get('intent_router', {}).get('intent', '?')
            print(f"✓ ({duration:.1f}s, {intent} L:{hits_l} V:{hits_v} S:{len(sources)})")
        else:
            # V5.2 format
            hits_h = debug_trace.get('hits_hunter', 0)
            hits_v = debug_trace.get('hits_vector', 0)
            print(f"✓ ({duration:.1f}s, H:{hits_h} V:{hits_v} S:{len(sources)})")
        
        # Kolla om Interrogator är klar eller vill avbryta (efter minst 2 rundor)
        if round_num >= 2:
            check = check_task_completion(ai_client, task, conversation_history)
            
            # Kollar om användaren gav upp (abort)
            if check.get('abort'):
                aborted = True
                completion_reason = check.get('reason', 'Användaren gav upp - verktyget hjälpte inte')
                print(f"\n  ❌ AVBROTT efter {round_num} rundor - användaren gav upp!")
                print(f"     Anledning: {completion_reason[:80]}...")
                break
            
            # Kollar om användaren är nöjd (done)
            if check.get('done') and check.get('confidence', 0) >= 7:
                completed_early = True
                completion_reason = check.get('reason', 'Uppgiften bedöms löst')
                print(f"\n  ✅ Interrogator klar efter {round_num} rundor (confidence: {check.get('confidence')}/10)")
                print(f"     Anledning: {completion_reason[:60]}...")
                break
    
    # Utvärdera uppgiften
    print(f"\n  Utvärderar uppgift...", end=" ", flush=True)
    evaluation = evaluate_task_completion(ai_client, task, conversation_history)
    print(f"✓ Score: {evaluation.get('score', '?')}/10 - {evaluation.get('verdict', '?')}")
    
    # Beräkna effektivitet
    rounds_used = len(rounds_data)
    efficiency_pct = round((1 - rounds_used / max_rounds) * 100) if completed_early else 0
    avg_duration = total_duration / rounds_used if rounds_used > 0 else 0
    
    return {
        "task": task,
        "rounds": rounds_data,
        "max_rounds": max_rounds,
        "rounds_used": rounds_used,
        "completed_early": completed_early,
        "aborted": aborted,
        "completion_reason": completion_reason,
        "evaluation": evaluation,
        "timing": {
            "total_seconds": round(total_duration, 2),
            "avg_per_round": round(avg_duration, 2),
            "efficiency_pct": efficiency_pct
        }
    }


# =====================================================================
# INKREMENTELL LOGGNING
# =====================================================================

class IncrementalLogger:
    """Hanterar inkrementell sparning av rapporter."""
    
    def __init__(self, timestamp: str, total_tasks: int, max_rounds: int):
        self.timestamp = timestamp
        self.total_tasks = total_tasks
        self.max_rounds = max_rounds
        self.eval_path = LOGS_DIR / f"evaluation_report_{timestamp}.md"
        self.tech_path = LOGS_DIR / f"technical_log_{timestamp}.md"
        self.results = []
        self.start_time = time.time()
        
    def initialize_files(self):
        """Skapar filerna med initiala headers."""
        # Evaluation report header
        eval_header = f"""# Utvärderingsrapport: MyMemory Simulering

**Datum:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**Session ID:** {self.timestamp}
**Antal uppgifter:** {self.total_tasks}
**Max rundor/uppgift:** {self.max_rounds}
**Status:** 🔄 Pågående...

---
## Uppgiftsresultat (uppdateras löpande)

"""
        with open(self.eval_path, 'w', encoding='utf-8') as f:
            f.write(eval_header)
        
        # Technical log header
        tech_header = f"""# Teknisk Sessionslogg: MyMemory Simulering

**Session ID:** {self.timestamp}
**Genererad:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**Antal uppgifter:** {self.total_tasks}
**Status:** 🔄 Pågående...

---

*Detta dokument innehåller fullständig teknisk information för varje interaktion.*
*Avsett för AI-analys och debugging.*
*Uppdateras inkrementellt efter varje uppgift.*

"""
        with open(self.tech_path, 'w', encoding='utf-8') as f:
            f.write(tech_header)
        
        print(f"\n📁 Loggar skapade (sparas inkrementellt):")
        print(f"   📊 {self.eval_path}")
        print(f"   🔧 {self.tech_path}")
    
    def append_task_result(self, result: dict, task_num: int):
        """Lägger till en uppgifts resultat till båda filerna."""
        self.results.append(result)
        
        # Append till evaluation report
        eval_section = self._generate_eval_task_section(result, task_num)
        with open(self.eval_path, 'a', encoding='utf-8') as f:
            f.write(eval_section)
        
        # Append till technical log
        tech_section = self._generate_tech_task_section(result, task_num)
        with open(self.tech_path, 'a', encoding='utf-8') as f:
            f.write(tech_section)
        
        print(f"   💾 Uppgift {task_num}/{self.total_tasks} sparad till loggarna")
    
    def finalize_reports(self):
        """Lägger till sammanfattningar i slutet av filerna."""
        total_elapsed = time.time() - self.start_time
        
        # Finalize evaluation report
        eval_summary = self._generate_eval_summary(total_elapsed)
        with open(self.eval_path, 'a', encoding='utf-8') as f:
            f.write(eval_summary)
        
        # Finalize technical log
        tech_summary = self._generate_tech_summary(total_elapsed)
        with open(self.tech_path, 'a', encoding='utf-8') as f:
            f.write(tech_summary)
    
    def _generate_eval_task_section(self, res: dict, task_num: int) -> str:
        """Genererar evaluation-sektion för en uppgift."""
        lines = []
        eval_data = res.get('evaluation', {})
        timing = res.get('timing', {})
        
        # Markera avbrutna uppgifter tydligt
        abort_marker = " ❌ AVBRUTEN" if res.get('aborted') else ""
        lines.append(f"### {task_num}. {res['task']['title']}{abort_marker}")
        lines.append("")
        lines.append(f"**Uppgiftsbeskrivning:** {res['task']['description']}")
        lines.append("")
        lines.append(f"| | |")
        lines.append(f"|---|---|")
        lines.append(f"| Score | {eval_data.get('score', 'N/A')}/10 |")
        lines.append(f"| Verdict | {eval_data.get('verdict', 'N/A')} |")
        lines.append(f"| Rundor | {res['rounds_used']}/{res['max_rounds']} |")
        lines.append(f"| Total tid | {timing.get('total_seconds', 0):.1f}s |")
        lines.append(f"| Snitt/runda | {timing.get('avg_per_round', 0):.2f}s |")
        if res.get('aborted'):
            lines.append(f"| **AVBRUTEN** | Användaren gav upp |")
        elif res.get('completed_early'):
            lines.append(f"| Tidigt avslut | Ja ({timing.get('efficiency_pct', 0)}% sparade) |")
        if eval_data.get('time_saved') is not None:
            saved = "Ja" if eval_data.get('time_saved') else "Nej"
            lines.append(f"| Sparade tid? | {saved} |")
        lines.append("")
        
        lines.append(f"**Sammanfattning:** {eval_data.get('summary', 'N/A')}")
        lines.append("")
        
        if eval_data.get('concrete_facts_delivered'):
            lines.append("**Konkreta fakta som levererades:**")
            for fact in eval_data.get('concrete_facts_delivered', []):
                lines.append(f"- ✓ {fact}")
            lines.append("")
        
        if eval_data.get('strengths'):
            lines.append("**Styrkor:**")
            for s in eval_data.get('strengths', []):
                lines.append(f"- {s}")
            lines.append("")
        
        if eval_data.get('gaps'):
            lines.append("**Brister:**")
            for g in eval_data.get('gaps', []):
                lines.append(f"- {g}")
            lines.append("")
        
        if eval_data.get('reasoning'):
            lines.append(f"**Utvärderingsresonemang:**")
            lines.append(f"> {eval_data.get('reasoning', '')}")
            lines.append("")
        
        # Kort konversationsöversikt
        lines.append("**Konversation:**")
        lines.append("")
        for rnd in res['rounds']:
            q_short = rnd['question'][:80] + "..." if len(rnd['question']) > 80 else rnd['question']
            a_short = rnd['answer'][:150] + "..." if len(rnd['answer']) > 150 else rnd['answer']
            duration = rnd.get('duration_seconds', 0)
            lines.append(f"- **R{rnd['round']}** ({duration:.1f}s): {q_short}")
            lines.append(f"  - *Svar:* {a_short}")
        lines.append("")
        lines.append("---")
        lines.append("")
        
        return "\n".join(lines)
    
    def _generate_tech_task_section(self, res: dict, task_num: int) -> str:
        """Genererar teknisk logg-sektion för en uppgift."""
        lines = []
        
        lines.append("=" * 80)
        abort_marker = " ❌ AVBRUTEN" if res.get('aborted') else ""
        lines.append(f"# UPPGIFT {task_num}: {res['task']['title']}{abort_marker}")
        lines.append("=" * 80)
        lines.append("")
        lines.append(f"**Beskrivning:** {res['task']['description']}")
        lines.append(f"**Rundor:** {res['rounds_used']}/{res['max_rounds']}")
        if res.get('aborted'):
            lines.append(f"**STATUS:** ❌ AVBRUTEN - Användaren gav upp")
            lines.append(f"**Avbrottsorsak:** {res.get('completion_reason', 'N/A')}")
        elif res.get('completed_early'):
            lines.append(f"**Tidigt avslut:** Ja")
            lines.append(f"**Avslutningsorsak:** {res.get('completion_reason', 'N/A')}")
        else:
            lines.append(f"**Tidigt avslut:** Nej (nådde max rundor)")
        lines.append("")
        
        # Timing
        timing = res.get('timing', {})
        lines.append("## TIMING")
        lines.append("```json")
        lines.append(json.dumps(timing, indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")
        
        # Utvärdering
        eval_data = res.get('evaluation', {})
        lines.append("## UTVÄRDERING")
        lines.append("```json")
        lines.append(json.dumps(eval_data, indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")
        
        # Per runda
        for rnd in res['rounds']:
            lines.append("-" * 60)
            lines.append(f"## RUNDA {rnd['round']} ({rnd.get('duration_seconds', 0):.2f}s)")
            lines.append("-" * 60)
            lines.append("")
            
            lines.append("### FRÅGA")
            lines.append("```")
            lines.append(rnd['question'])
            lines.append("```")
            lines.append("")
            
            lines.append("### SVAR")
            lines.append("")
            lines.append(rnd['answer'])
            lines.append("")
            
            lines.append("### KÄLLOR")
            if rnd['sources']:
                for src in rnd['sources']:
                    lines.append(f"- {src}")
            else:
                lines.append("- *Inga källor*")
            lines.append("")
            
            # Debug Trace
            dt = rnd['debug_trace']
            lines.append("### DEBUG TRACE")
            lines.append("")
            
            # Detektera pipeline-version
            pipeline_version = dt.get('pipeline_version', 'v5.2')
            lines.append(f"**Pipeline:** {pipeline_version}")
            lines.append("")
            
            if pipeline_version == 'v6.0' or dt.get('intent_router'):
                # === V6.0 FORMAT ===
                
                # 1. IntentRouter
                lines.append("#### 1. INTENT ROUTER")
                ir = dt.get('intent_router', {})
                lines.append("```")
                lines.append(f"Intent: {ir.get('intent', 'N/A')}")
                lines.append(f"Keywords: {ir.get('keywords', [])}")
                lines.append(f"Vector Query: {ir.get('vector_query', '')}")
                lines.append(f"Graph Paths: {ir.get('graph_paths', [])}")
                lines.append(f"Time Filter: {ir.get('time_filter', None)}")
                lines.append(f"Context Resolved: {ir.get('context_resolved', {})}")
                lines.append(f"Reasoning: {ir.get('reasoning', '')}")
                lines.append("```")
                lines.append("")
                
                # IntentRouter LLM raw
                if dt.get('intent_router_raw'):
                    lines.append("**LLM Raw Response:**")
                    lines.append("```")
                    lines.append(dt.get('intent_router_raw', '')[:1000])
                    lines.append("```")
                    lines.append("")
                
                # 2. ContextBuilder
                lines.append("#### 2. CONTEXT BUILDER")
                cb = dt.get('context_builder', {})
                lines.append("```")
                lines.append(f"Intent: {cb.get('intent', 'N/A')}")
                lines.append(f"Original Keywords: {cb.get('keywords_original', [])}")
                lines.append(f"Expanded Keywords: {cb.get('keywords_expanded', 'N/A')}")
                if cb.get('stats'):
                    stats = cb.get('stats', {})
                    lines.append(f"Lake Hits: {stats.get('lake_hits', 0)}")
                    lines.append(f"Vector Hits: {stats.get('vector_hits', 0)}")
                    lines.append(f"After Dedup: {stats.get('after_dedup', 0)}")
                    lines.append(f"Graph Paths Used: {stats.get('graph_paths_used', [])}")
                lines.append("```")
                lines.append("")
                
                # Top kandidater
                if dt.get('context_builder_candidates'):
                    lines.append("**Top 10 Kandidater:**")
                    lines.append("| ID | Source | Score |")
                    lines.append("|-----|--------|-------|")
                    for c in dt.get('context_builder_candidates', [])[:10]:
                        lines.append(f"| {c.get('id', '')[:30]} | {c.get('source', '')} | {c.get('score', 0)} |")
                    lines.append("")
                
                # 3. Planner
                lines.append("#### 3. PLANNER")
                pr = dt.get('planner_report', {})
                lines.append("```")
                lines.append(f"Status: {pr.get('status', 'N/A')}")
                lines.append(f"Sources Used: {pr.get('sources_used', [])}")
                lines.append(f"Gaps: {pr.get('gaps', [])}")
                lines.append(f"Confidence: {pr.get('confidence', 0)}")
                lines.append(f"Report Length: {pr.get('report_length', 0)} chars")
                lines.append("```")
                lines.append("")
                
                if dt.get('planner_selected_ids'):
                    lines.append(f"**Selected IDs:** {dt.get('planner_selected_ids', [])}")
                    lines.append("")
                
                # Planner LLM raw responses
                if dt.get('planner_selection_llm_raw'):
                    lines.append("**Selection LLM Raw:**")
                    lines.append("```")
                    lines.append(dt.get('planner_selection_llm_raw', '')[:1000])
                    lines.append("```")
                    lines.append("")
                
                if dt.get('planner_report_llm_raw'):
                    lines.append("**Report LLM Raw:**")
                    lines.append("```")
                    lines.append(dt.get('planner_report_llm_raw', '')[:2000])
                    lines.append("```")
                    lines.append("")
                
                # 4. SYNTHESIZER
                lines.append("#### 4. SYNTHESIZER")
                if dt.get('synthesizer_llm_raw'):
                    lines.append("**LLM Raw Response:**")
                    lines.append("```")
                    lines.append(dt.get('synthesizer_llm_raw', '')[:1500])
                    lines.append("```")
                    lines.append("")
                
                # 5. Timing
                lines.append("#### 5. TIMING")
                lines.append("```")
                lines.append(f"Pipeline Duration: {dt.get('pipeline_duration', 0):.2f}s")
                lines.append(f"Total Duration: {dt.get('total_duration', 0):.2f}s")
                lines.append("```")
                lines.append("")
                
            else:
                # === V5.2 FORMAT (Legacy) ===
                
                lines.append("#### 1. PLANERING")
                lines.append("```")
                lines.append(f"Nyckelord (hunter_keywords): {dt.get('hunter_keywords', [])}")
                lines.append(f"Vektorfråga (vector_query): {dt.get('vector_query', '')}")
                lines.append(f"Rankningskriterier: {dt.get('ranking_criteria', '')}")
                lines.append(f"Planeringsresonemang: {dt.get('plan_reasoning', '')}")
                lines.append("```")
                lines.append("")
                
                lines.append("#### 2. JÄGAREN (Keyword Search)")
                lines.append(f"**Träffar:** {dt.get('hits_hunter', 0)}")
                lines.append("")
                if dt.get('hunter_files'):
                    lines.append("| Fil | Matchat Nyckelord |")
                    lines.append("|-----|-------------------|")
                    for hf in dt.get('hunter_files', [])[:10]:
                        lines.append(f"| {hf.get('filename', '')} | {hf.get('matched_keyword', '')} |")
                    if len(dt.get('hunter_files', [])) > 10:
                        lines.append(f"| ... | (+{len(dt.get('hunter_files', [])) - 10} filer) |")
                    lines.append("")
                
                lines.append("#### 3. VEKTORN (Semantic Search)")
                lines.append(f"**Träffar:** {dt.get('hits_vector', 0)}")
                lines.append("")
                if dt.get('vector_files'):
                    lines.append("| Fil | Distans | Duplicate |")
                    lines.append("|-----|---------|-----------|")
                    for vf in dt.get('vector_files', [])[:10]:
                        dup = "Ja" if vf.get('already_in_candidates') else "Nej"
                        lines.append(f"| {vf.get('filename', '')} | {vf.get('distance', '')} | {dup} |")
                    lines.append("")
                
                lines.append("#### 4. DOMAREN (Re-ranking)")
                lines.append(f"**Input:** {dt.get('judge_input_count', 0)} dokument")
                lines.append("")
                lines.append("**Resonemang:**")
                lines.append(f"> {dt.get('judge_reasoning', 'Inget resonemang')}")
                lines.append("")
                
                lines.append("#### 5. SYNTES")
                lines.append("```")
                lines.append(f"Dokument till AI: {dt.get('docs_selected', 0)}")
                lines.append(f"Tecken till AI: {dt.get('total_chars', 0)}")
                lines.append(f"Totala kandidater: {dt.get('total_candidates', 0)}")
                lines.append(f"Tid: {dt.get('duration_seconds', 0):.2f}s")
                lines.append("```")
                lines.append("")
                
                if dt.get('pipeline_summary'):
                    lines.append("#### PIPELINE SUMMARY")
                    lines.append("```json")
                    lines.append(json.dumps(dt.get('pipeline_summary', {}), indent=2, ensure_ascii=False))
                    lines.append("```")
                    lines.append("")
        
        lines.append("")
        return "\n".join(lines)
    
    def _generate_eval_summary(self, total_elapsed: float) -> str:
        """Genererar sammanfattning för evaluation report."""
        lines = []
        
        total_score = sum(r.get('evaluation', {}).get('score', 0) for r in self.results)
        avg_score = total_score / len(self.results) if self.results else 0
        total_rounds = sum(r['rounds_used'] for r in self.results)
        avg_time = total_elapsed / total_rounds if total_rounds > 0 else 0
        
        lyckade = sum(1 for r in self.results if r.get('evaluation', {}).get('verdict') == 'Lyckad')
        delvis = sum(1 for r in self.results if r.get('evaluation', {}).get('verdict') == 'Delvis lyckad')
        misslyckade = len(self.results) - lyckade - delvis
        early_completions = sum(1 for r in self.results if r.get('completed_early'))
        aborted_count = sum(1 for r in self.results if r.get('aborted'))
        time_saved_count = sum(1 for r in self.results if r.get('evaluation', {}).get('time_saved'))
        
        lines.append("---")
        lines.append("# SAMMANFATTNING")
        lines.append("")
        lines.append(f"**Status:** ✅ Klar")
        lines.append(f"**Slutförd:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**Total tid:** {total_elapsed:.1f}s")
        lines.append("")
        
        lines.append("## Övergripande Resultat")
        lines.append("")
        lines.append(f"| Mätvärde | Resultat |")
        lines.append(f"|----------|----------|")
        lines.append(f"| **Genomsnittligt Score** | **{avg_score:.1f}/10** |")
        lines.append(f"| Lyckade uppgifter | {lyckade}/{len(self.results)} |")
        lines.append(f"| Delvis lyckade | {delvis}/{len(self.results)} |")
        lines.append(f"| Misslyckade | {misslyckade}/{len(self.results)} |")
        lines.append(f"| **❌ Avbrutna (gave up)** | **{aborted_count}/{len(self.results)}** |")
        lines.append(f"| ✅ Tidigt avslutade (nöjd) | {early_completions}/{len(self.results)} |")
        lines.append(f"| Sparade tid | {time_saved_count}/{len(self.results)} |")
        lines.append(f"| Totalt antal rundor | {total_rounds} |")
        lines.append(f"| **Snitt tid/runda** | **{avg_time:.2f}s** |")
        lines.append("")
        
        lines.append("## Resultatöversikt")
        lines.append("")
        lines.append("| # | Uppgift | Score | Verdict | Status | Rundor | Tid |")
        lines.append("|---|---------|-------|---------|--------|--------|-----|")
        
        for i, res in enumerate(self.results, 1):
            task_title = res['task']['title'][:25]
            eval_data = res.get('evaluation', {})
            score = eval_data.get('score', 0)
            verdict = eval_data.get('verdict', 'N/A')
            rounds_info = f"{res['rounds_used']}/{res['max_rounds']}"
            timing = res.get('timing', {})
            task_time = timing.get('total_seconds', 0)
            
            # Status-ikon
            if res.get('aborted'):
                status = "❌ Abort"
            elif res.get('completed_early'):
                status = "✅ Klar"
            else:
                status = "⏱️ Max"
            
            lines.append(f"| {i} | {task_title} | {score}/10 | {verdict} | {status} | {rounds_info} | {task_time:.1f}s |")
        
        lines.append("")
        lines.append(f"---")
        lines.append(f"*Se teknisk logg för fullständig debug-information: `technical_log_{self.timestamp}.md`*")
        lines.append("")
        
        return "\n".join(lines)
    
    def _generate_tech_summary(self, total_elapsed: float) -> str:
        """Genererar sammanfattning för teknisk logg."""
        lines = []
        
        total_rounds = sum(r['rounds_used'] for r in self.results)
        total_time = sum(r['timing']['total_seconds'] for r in self.results)
        
        lines.append("=" * 80)
        lines.append("# SESSIONSSAMMANFATTNING")
        lines.append("=" * 80)
        lines.append("")
        lines.append(f"**Status:** ✅ Klar")
        lines.append(f"**Total körtid:** {total_elapsed:.1f}s")
        lines.append(f"**Total MyMemory-tid:** {total_time:.1f}s")
        lines.append(f"**Antal uppgifter:** {len(self.results)}")
        lines.append(f"**Totalt antal rundor:** {total_rounds}")
        lines.append("")
        
        return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Simuleringsverktyg för MyMemory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exempel:
  python tools/simulate_session.py -r 10 -t "Skapa en veckorapport"
  python tools/simulate_session.py -r 10 -f tools/simulation_tasks.txt
  python tools/simulate_session.py -r 10  # Generell testning

Notera: Simuleringen avslutas automatiskt när Interrogator bedömer
att den har tillräcklig information (confidence >= 7).

Rapporter sparas inkrementellt efter varje uppgift - om simuleringen
avbryts behålls data för avklarade uppgifter.
        """
    )
    parser.add_argument('-r', '--rounds', type=int, default=10, 
                        help='Max antal rundor per uppgift (default: 10)')
    parser.add_argument('-t', '--task', type=str, 
                        help='Enskild uppgift att testa (text)')
    parser.add_argument('-f', '--tasks-file', type=str, 
                        help='Fil med uppgifter (en per rad: Titel | Beskrivning)')
    
    args = parser.parse_args()
    
    # Skapa logs-mappen om den inte finns
    LOGS_DIR.mkdir(exist_ok=True)
    
    # Initiera AI-klient
    print("Initierar AI-klient...")
    ai_client = genai.Client(api_key=API_KEY)
    
    # Ladda uppgifter
    tasks = []
    
    if args.tasks_file:
        print(f"Laddar uppgifter från {args.tasks_file}...")
        tasks = load_tasks_from_file(args.tasks_file)
        print(f"  {len(tasks)} uppgifter laddade")
    elif args.task:
        tasks = [{
            "title": args.task[:50] + "..." if len(args.task) > 50 else args.task,
            "description": args.task
        }]
    else:
        tasks = [{
            "title": "Generell testning",
            "description": "Utforska systemet fritt. Ställ varierade frågor om innehållet - fråga om projekt, personer, möten, beslut, och annat som kan finnas i minnet."
        }]
    
    print(f"\n📋 Startar simulering med {len(tasks)} uppgift(er), max {args.rounds} rundor var")
    print(f"   (Avslutas automatiskt när Interrogator är nöjd)")
    
    # Skapa timestamp och logger
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    logger = IncrementalLogger(timestamp, len(tasks), args.rounds)
    logger.initialize_files()
    
    # Kör simuleringar med inkrementell sparning
    start_total = time.time()
    
    for i, task in enumerate(tasks, 1):
        print(f"\n[{i}/{len(tasks)}] ", end="")
        result = run_simulation(task, args.rounds, ai_client)
        logger.append_task_result(result, i)
    
    total_elapsed = time.time() - start_total
    
    # Lägg till sammanfattningar
    logger.finalize_reports()
    
    # Sammanfattning i terminalen
    print(f"\n{'='*60}")
    print(f"✅ Simulering klar! (Total tid: {total_elapsed:.1f}s)")
    print(f"")
    print(f"📊 Utvärderingsrapport: {logger.eval_path}")
    print(f"🔧 Teknisk logg: {logger.tech_path}")
    print(f"")
    
    total_score = sum(r.get('evaluation', {}).get('score', 0) for r in logger.results)
    avg_score = total_score / len(logger.results) if logger.results else 0
    total_rounds = sum(r['rounds_used'] for r in logger.results)
    max_possible = sum(r['max_rounds'] for r in logger.results)
    early_count = sum(1 for r in logger.results if r.get('completed_early'))
    
    abort_count = sum(1 for r in logger.results if r.get('aborted'))
    
    print(f"SAMMANFATTNING:")
    print(f"  Genomsnittligt score: {avg_score:.1f}/10")
    print(f"  Rundor: {total_rounds}/{max_possible} ({early_count} nöjda, {abort_count} avbrutna)")
    print(f"  Snitt tid/runda: {total_elapsed/total_rounds:.2f}s" if total_rounds > 0 else "  Snitt tid/runda: N/A")
    print(f"")
    for res in logger.results:
        eval_data = res.get('evaluation', {})
        timing = res.get('timing', {})
        if res.get('aborted'):
            status = "❌"
        elif res.get('completed_early'):
            status = "✅"
        else:
            status = "⏱️"
        print(f"  {status} {res['task']['title']}: {eval_data.get('score', '?')}/10 ({res['rounds_used']}r, {timing.get('total_seconds', 0):.1f}s)")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
