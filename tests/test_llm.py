from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from google.genai import errors as genai_errors

from src.llm import LLMClient, LLMConfig, REQUEST_TIMEOUT_SECONDS, load_llm_config


class LLMFallbackTests(unittest.TestCase):
    def test_load_llm_config_parses_and_deduplicates_fallback_models(self) -> None:
        env = {
            "LLM_PROVIDER": "gemini",
            "GEMINI_API_KEY": "test-key",
            "LLM_MODEL": "gemini-3.5-flash-lite",
            "LLM_FALLBACK_MODELS": " gemini-3.5-flash , , gemini-3.5-flash-lite, gemini-3.6-flash, gemini-3.5-flash ",
        }
        with patch("src.llm.load_dotenv"), patch.dict(os.environ, env, clear=True):
            cfg = load_llm_config()

        self.assertEqual(cfg.model, "gemini-3.5-flash-lite")
        self.assertEqual(
            cfg.fallback_models,
            ("gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.5-flash"),
        )
        self.assertEqual(
            cfg.models,
            ["gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash"],
        )

    def test_gemini_fallback_switches_on_503_504_and_empty_response(self) -> None:
        cfg = LLMConfig(
            provider="gemini",
            model="m1",
            api_key="test-key",
            fallback_models=("m2", "m3", "m4"),
        )
        called_models: list[str] = []

        def fake_generate_content(*, model: str, contents: str, config: object) -> SimpleNamespace:
            called_models.append(model)
            if model == "m1":
                raise genai_errors.APIError(
                    503,
                    {"error": {"code": 503, "status": "UNAVAILABLE", "message": "Overloaded"}},
                )
            if model == "m2":
                raise genai_errors.APIError(
                    504,
                    {"error": {"code": 504, "status": "DEADLINE_EXCEEDED", "message": "Timeout"}},
                )
            if model == "m3":
                return SimpleNamespace(text="   ")
            return SimpleNamespace(text="  Готовый дайджест  ")

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = fake_generate_content

        with patch("google.genai.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value = mock_client
            result = LLMClient(cfg).generate("sys", "user")

        self.assertEqual(result, "Готовый дайджест")
        self.assertEqual(called_models, ["m1", "m2", "m3", "m4"])
        mock_client_cls.assert_called_once()
        http_opts = mock_client_cls.call_args.kwargs["http_options"]
        self.assertEqual(http_opts.timeout, REQUEST_TIMEOUT_SECONDS * 1000)

    def test_non_fallback_auth_errors_raise_immediately(self) -> None:
        for code, status in ((401, "UNAUTHENTICATED"), (403, "PERMISSION_DENIED")):
            with self.subTest(code=code):
                cfg = LLMConfig(
                    provider="gemini",
                    model="m1",
                    api_key="bad-key",
                    fallback_models=("m2", "m3"),
                )
                called_models: list[str] = []

                def fake_generate_content(*, model: str, contents: str, config: object) -> SimpleNamespace:
                    called_models.append(model)
                    raise genai_errors.APIError(
                        code,
                        {"error": {"code": code, "status": status, "message": "Auth failed"}},
                    )

                mock_client = MagicMock()
                mock_client.models.generate_content.side_effect = fake_generate_content

                with patch("google.genai.Client") as mock_client_cls:
                    mock_client_cls.return_value.__enter__.return_value = mock_client
                    with self.assertRaises(genai_errors.APIError):
                        LLMClient(cfg).generate("sys", "user")

                self.assertEqual(called_models, ["m1"])

    def test_all_models_failed_raises_compact_summary(self) -> None:
        cfg = LLMConfig(
            provider="gemini",
            model="m1",
            api_key="test-key",
            fallback_models=("m2", "m3"),
        )

        def fake_generate_content(*, model: str, contents: str, config: object) -> SimpleNamespace:
            if model == "m1":
                raise genai_errors.APIError(
                    503,
                    {"error": {"code": 503, "status": "UNAVAILABLE", "message": "Overloaded"}},
                )
            if model == "m2":
                raise genai_errors.APIError(
                    429,
                    {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota"}},
                )
            return SimpleNamespace(text="")

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = fake_generate_content

        with patch("google.genai.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value = mock_client
            with self.assertRaises(RuntimeError) as ctx:
                LLMClient(cfg).generate("sys", "user")

        err_msg = str(ctx.exception)
        self.assertTrue(err_msg.startswith("Все LLM-модели недоступны:"))
        self.assertIn("- m1: 503 UNAVAILABLE", err_msg)
        self.assertIn("- m2: 429 RESOURCE_EXHAUSTED", err_msg)
        self.assertIn("- m3: Gemini API вернул пустой ответ.", err_msg)
        self.assertNotIn("{", err_msg)


if __name__ == "__main__":
    unittest.main()
