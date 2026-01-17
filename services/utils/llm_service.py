"""
LLMService - Central tjänst för alla LLM-anrop.

Hanterar:
- Modellval baserat på task_type (pro/fast/lite)
- Parallella anrop för moln-LLM (batch_generate)
- Sekventiella anrop för lokal modell
- Adaptiv throttling - ökar gradvis tills rate limit, backar och stabiliserar
- Retry-logik med exponential backoff
- Centraliserad felhantering och logging
"""

import os
import logging
import yaml
import time
import threading
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum

from google import genai
from google.genai import types

LOGGER = logging.getLogger("LLMService")


class TaskType(Enum):
    """Typ av uppgift - styr modellval."""
    TRANSCRIPTION = "transcription"      # Hög kvalitet (pro)
    ENRICHMENT = "enrichment"            # Snabbhet (fast)
    CONSOLIDATION = "consolidation"      # Hög kvalitet (pro)
    VALIDATION = "validation"            # Snabb (lite)
    STRUCTURAL_ANALYSIS = "structural"   # Dreamer strukturell (lite)
    ENTITY_RESOLUTION = "entity"         # Dreamer merge (lite)


@dataclass
class LLMResponse:
    """Resultat från LLM-anrop."""
    text: str
    success: bool
    error: Optional[str] = None
    model: Optional[str] = None
    tokens_used: Optional[int] = None


class AdaptiveThrottler:
    """
    Adaptiv throttling för API-anrop.

    Ökar gradvis anrop/sekund tills rate limit träffas,
    backar sedan och stabiliserar på en säker nivå.
    """

    def __init__(
        self,
        initial_rps: float = 1.0,
        min_rps: float = 0.5,
        max_rps: float = 20.0,
        increase_factor: float = 1.2,
        decrease_factor: float = 0.6,
        stabilize_after: int = 5
    ):
        self.current_rps = initial_rps
        self.min_rps = min_rps
        self.max_rps = max_rps
        self.increase_factor = increase_factor
        self.decrease_factor = decrease_factor
        self.stabilize_after = stabilize_after

        self._lock = threading.Lock()
        self._last_call_time = 0.0
        self._consecutive_successes = 0
        self._stabilized = False
        self._stable_rps = None

    def wait(self):
        """Vänta rätt tid mellan anrop."""
        with self._lock:
            now = time.time()
            min_interval = 1.0 / self.current_rps
            elapsed = now - self._last_call_time

            if elapsed < min_interval:
                sleep_time = min_interval - elapsed
                time.sleep(sleep_time)

            self._last_call_time = time.time()

    def report_success(self):
        """Rapportera lyckat anrop - öka hastigheten gradvis."""
        with self._lock:
            if self._stabilized:
                return  # Redan stabil, ändra inte

            self._consecutive_successes += 1

            if self._consecutive_successes >= self.stabilize_after:
                # Öka RPS
                old_rps = self.current_rps
                self.current_rps = min(self.current_rps * self.increase_factor, self.max_rps)

                if self.current_rps != old_rps:
                    LOGGER.debug(f"Throttle: ökar RPS {old_rps:.2f} → {self.current_rps:.2f}")

                self._consecutive_successes = 0

    def report_rate_limit(self):
        """Rapportera rate limit - backa och stabilisera."""
        with self._lock:
            old_rps = self.current_rps
            self.current_rps = max(self.current_rps * self.decrease_factor, self.min_rps)

            # Markera som stabil på denna nivå
            self._stabilized = True
            self._stable_rps = self.current_rps
            self._consecutive_successes = 0

            LOGGER.info(f"Throttle: rate limit! Backar {old_rps:.2f} → {self.current_rps:.2f} RPS (stabiliserad)")

    def report_error(self):
        """Rapportera annat fel - återställ success-räknare."""
        with self._lock:
            self._consecutive_successes = 0

    def get_stats(self) -> dict:
        """Hämta aktuell status."""
        with self._lock:
            return {
                "current_rps": self.current_rps,
                "stabilized": self._stabilized,
                "stable_rps": self._stable_rps
            }


class LLMService:
    """Central tjänst för LLM-anrop med batch-stöd."""

    _instance = None

    def __new__(cls):
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.config = self._load_config()
        self.client = self._init_client()
        self.models = self._load_models()
        self.task_model_map = self._load_task_mapping()

        # Adaptiv throttling
        self.throttler = AdaptiveThrottler(
            initial_rps=20.0,     # Starta aggressivt med 20 anrop/sek
            min_rps=0.5,          # Minst 1 anrop per 2 sek
            max_rps=50.0,         # Max 50 anrop/sek
            increase_factor=1.3,  # Öka 30% vid succé
            decrease_factor=0.5,  # Halvera vid rate limit
            stabilize_after=5     # Öka efter 5 lyckade i rad
        )

        # Retry-inställningar
        self.max_parallel = 30  # Max parallella anrop mot moln
        self.retry_attempts = 3
        self.retry_delay = 1.0  # Sekunder mellan retries

        self._initialized = True
        LOGGER.info("LLMService initialized")

    def _load_config(self) -> dict:
        """Ladda konfiguration."""
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        config_path = os.path.join(base_dir, "config", "my_mem_config.yaml")

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            LOGGER.error(f"Kunde inte ladda config: {e}")
            return {}

    def _init_client(self):
        """Initiera Gemini-klient."""
        api_key = self.config.get('ai_engine', {}).get('api_key')
        if not api_key:
            LOGGER.error("HARDFAIL: API-nyckel saknas!")
            return None
        return genai.Client(api_key=api_key)

    def _load_models(self) -> dict:
        """Ladda modellnamn från config."""
        models = self.config.get('ai_engine', {}).get('models', {})
        return {
            'pro': models.get('model_pro', 'models/gemini-pro-latest'),
            'fast': models.get('model_fast', 'models/gemini-flash-latest'),
            'lite': models.get('model_lite', 'models/gemini-flash-lite-latest'),
        }

    def _load_task_mapping(self) -> dict:
        """Mappa task_type till modell."""
        tasks = self.config.get('ai_engine', {}).get('tasks', {})
        return {
            TaskType.TRANSCRIPTION: tasks.get('transcription', 'model_pro'),
            TaskType.ENRICHMENT: tasks.get('enrichment', 'model_fast'),
            TaskType.CONSOLIDATION: tasks.get('consolidation', 'model_pro'),
            TaskType.VALIDATION: 'model_lite',
            TaskType.STRUCTURAL_ANALYSIS: 'model_fast',  # Lite klarar ej strukturell analys
            TaskType.ENTITY_RESOLUTION: 'model_lite',
        }

    def _get_model_for_task(self, task_type: TaskType) -> str:
        """Hämta rätt modell för given uppgift."""
        model_key = self.task_model_map.get(task_type, 'model_lite')
        # Konvertera "model_pro" → faktiskt modellnamn
        if model_key.startswith('model_'):
            short_key = model_key.replace('model_', '')
            return self.models.get(short_key, self.models['lite'])
        return model_key

    def _is_rate_limit_error(self, error: Exception) -> bool:
        """Kolla om felet är rate limiting."""
        error_str = str(error).lower()
        return any(keyword in error_str for keyword in [
            "rate limit", "quota", "429", "resource exhausted",
            "too many requests", "rate_limit"
        ])

    def generate(self, prompt: str, task_type: TaskType = TaskType.VALIDATION) -> LLMResponse:
        """
        Generera svar för en prompt.

        Args:
            prompt: Prompten att skicka
            task_type: Typ av uppgift (styr modellval)

        Returns:
            LLMResponse med text eller fel
        """
        if not self.client:
            return LLMResponse(text="", success=False, error="Ingen LLM-klient tillgänglig")

        model = self._get_model_for_task(task_type)

        for attempt in range(self.retry_attempts):
            # Vänta enligt throttling
            self.throttler.wait()

            try:
                response = self.client.models.generate_content(
                    model=model,
                    contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
                )

                # Kontrollera att svaret inte är tomt
                if not response.text or not response.text.strip():
                    LOGGER.warning(f"LLM returnerade tomt svar för modell {model}")
                    return LLMResponse(
                        text="",
                        success=False,
                        error="LLM returnerade tomt svar",
                        model=model
                    )

                # Rapportera framgång till throttler
                self.throttler.report_success()

                return LLMResponse(
                    text=response.text,
                    success=True,
                    model=model
                )

            except Exception as e:
                if self._is_rate_limit_error(e):
                    # Rate limit - backa throttler och vänta extra
                    self.throttler.report_rate_limit()
                    LOGGER.warning(f"Rate limit träffad, väntar innan retry...")
                    time.sleep(2.0 + attempt * 2.0)  # Vänta 2/4/6 sek
                else:
                    # Annat fel
                    self.throttler.report_error()
                    LOGGER.warning(f"LLM-anrop misslyckades (försök {attempt + 1}/{self.retry_attempts}): {e}")

                if attempt < self.retry_attempts - 1:
                    time.sleep(self.retry_delay * (attempt + 1))
                else:
                    return LLMResponse(text="", success=False, error=str(e), model=model)

        return LLMResponse(text="", success=False, error="Max retries exceeded")

    def batch_generate(
        self,
        prompts: List[str],
        task_type: TaskType = TaskType.VALIDATION,
        parallel: bool = True
    ) -> List[LLMResponse]:
        """
        Generera svar för flera prompts.

        Args:
            prompts: Lista med prompts
            task_type: Typ av uppgift
            parallel: True = parallellt (moln), False = sekventiellt (lokal)

        Returns:
            Lista med LLMResponse i samma ordning som prompts
        """
        if not prompts:
            return []

        if not parallel:
            # Sekventiell körning (för lokal modell)
            return [self.generate(p, task_type) for p in prompts]

        # Parallell körning med ThreadPoolExecutor
        results = [None] * len(prompts)

        with ThreadPoolExecutor(max_workers=self.max_parallel) as executor:
            # Skapa futures med index för att bevara ordning
            future_to_idx = {
                executor.submit(self.generate, prompt, task_type): idx
                for idx, prompt in enumerate(prompts)
            }

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as e:
                    LOGGER.error(f"Batch generate fel vid index {idx}: {e}")
                    results[idx] = LLMResponse(text="", success=False, error=str(e))

        return results

    def generate_simple(self, prompt: str) -> str:
        """
        Enkel wrapper för bakåtkompatibilitet.
        Returnerar bara texten (tomsträng vid fel).
        """
        response = self.generate(prompt, TaskType.VALIDATION)
        return response.text if response.success else ""

    def get_throttle_stats(self) -> dict:
        """Hämta aktuell throttling-status."""
        return self.throttler.get_stats()


# Bakåtkompatibilitet - alias för enkel import
def get_llm_service() -> LLMService:
    """Hämta singleton-instans av LLMService."""
    return LLMService()
