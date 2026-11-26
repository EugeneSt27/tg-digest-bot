from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Optional

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_OPENAI_MODEL = "google/gemini-2.5-flash"
REQUEST_TIMEOUT_SECONDS = 60
_FALLBACK_HTTP_CODES = frozenset({404, 408, 429, 500, 502, 503, 504, 529})


class _EmptyResponseError(RuntimeError):
    """Внутренняя ошибка пустого ответа от LLM, допускающая переключение на fallback."""


def _is_fallback_error(exc: Exception) -> bool:
    """Возвращает True для транзиентных ошибок, таймаутов, пустых ответов и 404/429/5xx."""
    if isinstance(exc, (_EmptyResponseError, httpx.HTTPError, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc.__cause__, (httpx.HTTPError, TimeoutError, ConnectionError)):
        return True
    if type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}:
        return True
    for attr in ("status_code", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val in _FALLBACK_HTTP_CODES
    return False


def _short_error(exc: Exception) -> str:
    """Форматирует ошибку компактно (например, '503 UNAVAILABLE') без сырого JSON."""
    code = next(
        (v for v in (getattr(exc, "status_code", None), getattr(exc, "code", None)) if isinstance(v, int)),
        None,
    )
    status = getattr(exc, "status", None)
    if code is not None:
        return f"{code} {status}" if status else f"{code} {type(exc).__name__}"
    if isinstance(exc, _EmptyResponseError):
        return str(exc)
    raw = str(exc).strip()
    first_line = raw.splitlines()[0].strip()[:120] if raw else ""
    return f"{type(exc).__name__}: {first_line}" if first_line else type(exc).__name__


@dataclass
class LLMConfig:
    provider: str  # "gemini" | "openai"
    model: str
    api_key: str
    base_url: Optional[str] = None
    fallback_models: tuple[str, ...] = ()

    @property
    def models(self) -> list[str]:
        """Возвращает дедуплицированный список моделей в порядке приоритета."""
        return list(dict.fromkeys(m for m in (self.model, *self.fallback_models) if m))


def load_llm_config() -> LLMConfig:
    """
    Читает конфигурацию LLM из переменных окружения (.env).
    Поддерживает переключение между Google Gemini SDK и любым OpenAI-совместимым API
    (OpenRouter, ProxyAPI, DeepSeek, OpenAI, локальный Ollama/vLLM).
    """
    load_dotenv()

    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    explicit_model = os.getenv("LLM_MODEL", "").strip()
    fallback_models = tuple(
        m.strip() for m in os.getenv("LLM_FALLBACK_MODELS", "").split(",") if m.strip()
    )

    if provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                "Не задан GEMINI_API_KEY в переменных окружения (.env) при LLM_PROVIDER=gemini."
            )
        model = explicit_model or DEFAULT_GEMINI_MODEL
        return LLMConfig(
            provider="gemini",
            model=model,
            api_key=api_key,
            fallback_models=fallback_models,
        )

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                "Не задан OPENAI_API_KEY в переменных окружения (.env) при LLM_PROVIDER=openai."
            )
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
        model = explicit_model or DEFAULT_OPENAI_MODEL
        return LLMConfig(
            provider="openai",
            model=model,
            api_key=api_key,
            base_url=base_url,
            fallback_models=fallback_models,
        )

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
            "Запрос к LLM (provider=%s, model=%s, fallbacks=%d, base_url=%s)...",
            self.config.provider,
            self.config.model,
            max(0, len(self.config.models) - 1),
            self.config.base_url or "default",
        )
        if self.config.provider == "gemini":
            return self._generate_gemini(system_prompt, user_prompt, temperature)
        if self.config.provider == "openai":
            return self._generate_openai(system_prompt, user_prompt, temperature)
        raise ValueError(f"Неподдерживаемый провайдер: {self.config.provider}")

    def _run_with_fallback(self, call_model: Callable[[str], str]) -> str:
        """Последовательно перебирает модели из конфигурации при транзиентных сбоях."""
        errors: list[str] = []
        for idx, model in enumerate(self.config.models):
            try:
                result = call_model(model)
            except Exception as exc:
                if not _is_fallback_error(exc):
                    raise
                short = _short_error(exc)
                errors.append(f"- {model}: {short}")
                logger.warning(
                    "Модель %s недоступна (%s), переключаемся на следующую.",
                    model,
                    short,
                )
                continue

            if idx > 0:
                logger.info(
                    "Резервная LLM-модель %s успешно ответила (после %d сбоев).",
                    model,
                    idx,
                )
            return result

        raise RuntimeError("Все LLM-модели недоступны:\n" + "\n".join(errors))

    def _generate_gemini(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> str:
        from google import genai
        from google.genai import types

        with genai.Client(
            api_key=self.config.api_key,
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_SECONDS * 1000),
        ) as client:
            def _call(model: str) -> str:
                response = client.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=temperature,
                    ),
                )
                if not response.text or not response.text.strip():
                    raise _EmptyResponseError("Gemini API вернул пустой ответ.")
                return response.text.strip()

            return self._run_with_fallback(_call)

    def _generate_openai(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> str:
        from openai import OpenAI

        with OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as client:
            def _call(model: str) -> str:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=temperature,
                )
                content = response.choices[0].message.content if response.choices else None
                if not content or not content.strip():
                    raise _EmptyResponseError("OpenAI-совместимый API вернул пустой ответ.")
                return content.strip()

            return self._run_with_fallback(_call)
