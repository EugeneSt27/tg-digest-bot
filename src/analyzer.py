from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import yaml

from src.fetcher import Post
from src.llm import LLMClient

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DEFAULT_PROMPTS_PATH = CONFIG_DIR / "prompts" / "ai.yaml"


def format_posts_for_prompt(posts: list[Post]) -> str:
    """
    Форматирует список объектов Post в компактный и однозначный текстовый блок для LLM.
    """
    blocks: list[str] = []
    for idx, post in enumerate(posts, start=1):
        date_str = post.published_at.strftime("%Y-%m-%d %H:%M UTC")
        links_str = ", ".join(post.external_links) if post.external_links else "нет"
        block = (
            f"--- POST #{idx} ---\n"
            f"Channel: @{post.channel}\n"
            f"Date: {date_str}\n"
            f"Post URL: {post.url}\n"
            f"External Links: {links_str}\n"
            f"Text:\n{post.text}\n"
        )
        blocks.append(block)
    return "\n".join(blocks)


def clean_telegram_html(raw_output: str) -> str:
    """
    Убирает случайные markdown-обертки (```html ... ```) и неподдерживаемые теги <br>,
    если LLM все же их сгенерировала.
    """
    cleaned = raw_output.strip()
    cleaned = re.sub(r"^```(?:html)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def build_digest(
    posts: list[Post],
    days_lookback: int = 3,
    prompts_path: Path = DEFAULT_PROMPTS_PATH,
    llm_client: Optional[LLMClient] = None,
) -> str:
    """
    Принимает список собранных постов, загружает промпты из YAML
    и генерирует готовый HTML-дайджест через выбранного LLM-провайдера.
    """
    if not posts:
        return "За выбранный период новых постов в отслеживаемых каналах не найдено."

    prompts_data = yaml.safe_load(prompts_path.read_text(encoding="utf-8"))
    system_prompt: str = prompts_data["system_prompt"]
    user_template: str = prompts_data["user_prompt_template"]
    temperature: float = float(prompts_data.get("temperature", 0.2))

    posts_block = format_posts_for_prompt(posts)
    user_prompt = user_template.format(
        days=days_lookback,
        total_posts=len(posts),
        posts_block=posts_block,
    )

    client = llm_client or LLMClient()
    raw_digest = client.generate(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=temperature,
    )
    return clean_telegram_html(raw_digest)
