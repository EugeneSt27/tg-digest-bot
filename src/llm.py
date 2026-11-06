from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_OPENAI_MODEL = "google/gemini-2.5-flash"


@dataclass
class LLMConfig:
    provider: str  # "gemini" | "openai"
    model: str
    api_key: str
    base_url: Optional[str] = None


def load_llm_config() -> LLMConfig:
    """
    Читает конфигурацию LLM из переменных окружения (.env).
    Поддерживает переключение между Google Gemini SDK и любым OpenAI-совместимым API
    (OpenRouter, ProxyAPI, DeepSeek, OpenAI, локальный Ollama/vLLM).
    """
    load_dotenv()

    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    explicit_model = os.getenv("LLM_MODEL", "").strip()

    if provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                "Не задан GEMINI_API_KEY в переменных окружения (.env) при LLM_PROVIDER=gemini."
            )
        model = explicit_model or DEFAULT_GEMINI_MODEL
        return LLMConfig(provider="gemini", model=model, api_key=api_key)

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                "Не задан OPENAI_API_KEY в переменных окружения (.env) при LLM_PROVIDER=openai."
            )
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
        model = explicit_model or DEFAULT_OPENAI_MODEL
        return LLMConfig(provider="openai", model=model, api_key=api_key, base_url=base_url)

    raise ValueError(
        f"Неизвестный LLM_PROVIDER='{provider}'. Допустимые значения: 'gemini' или 'openai'."
    )


class LLMClient:
    """Единый интерфейс генерации текста поверх разных LLM-провайдеров."""

    def __init__(self, config: Optional[LLMConfig] = None) -> None:
        self.config = config or load_llm_config()

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> str:
        logger.info(
            "Запрос к LLM (provider=%s, model=%s, base_url=%s)...",
            self.config.provider,
            self.config.model,
            self.config.base_url or "default",
        )
        if self.config.provider == "gemini":
            return self._generate_gemini(system_prompt, user_prompt, temperature)
        if self.config.provider == "openai":
            return self._generate_openai(system_prompt, user_prompt, temperature)
        raise ValueError(f"Неподдерживаемый провайдер: {self.config.provider}")

    def _generate_gemini(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.config.api_key)
        response = client.models.generate_content(
            model=self.config.model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=temperature,
            ),
        )
        if not response.text:
            raise RuntimeError("Gemini API вернул пустой ответ.")
        return response.text.strip()

    def _generate_openai(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> str:
        from openai import OpenAI

        client = OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
        )
        response = client.chat.completions.create(
            model=self.config.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
        )
        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise RuntimeError("OpenAI-совместимый API вернул пустой ответ.")
        return content.strip()
