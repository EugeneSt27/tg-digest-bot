from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


@dataclass
class Post:
    channel: str
    post_id: int
    published_at: datetime
    text: str
    url: str
    external_links: list[str] = field(default_factory=list)


def normalize_channel_name(raw_channel: str) -> str:
    """
    Приводит любую форму записи канала (@name, https://t.me/name, https://t.me/s/name/123)
    к чистому username канала.
    """
    cleaned = raw_channel.strip().rstrip("/")
    cleaned = re.sub(r"^https?://(www\.)?(t\.me|telegram\.me)/(s/)?", "", cleaned)
    cleaned = cleaned.lstrip("@")
    # Если была передана ссылка на конкретный пост (channel/1234), берем только имя канала
    return cleaned.split("/")[0]


def _extract_text_and_links(text_node: Tag) -> tuple[str, list[str]]:
    """
    Извлекает чистый текст поста с сохранением переносов строк,
    а также собирает полезные внешние ссылки (на статьи, GitHub, ArXiv и т.д.).
    """
    external_links: list[str] = []
    for a_tag in text_node.find_all("a", href=True):
        href = a_tag["href"].strip()
        if href.startswith(("http://", "https://")) and "t.me/" not in href:
            if href not in external_links:
                external_links.append(href)

    # Заменяем <br> на переносы строк перед извлечением текста
    for br in text_node.find_all("br"):
        br.replace_with("\n")

    text = text_node.get_text(separator="", strip=True)
    # Схлопываем избыточные пустые строки
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, external_links


def _parse_page_posts(html: str, channel: str) -> list[Post]:
    """Парсит HTML одной страницы t.me/s/<channel> и возвращает список постов."""
    soup = BeautifulSoup(html, "lxml")
    message_nodes = soup.select(".tgme_widget_message")
    posts: list[Post] = []

    for msg in message_nodes:
        data_post = msg.get("data-post")
        if not data_post or "/" not in data_post:
            continue

        try:
            post_id = int(data_post.split("/")[-1])
        except ValueError:
            continue

        # Ищем дату публикации
        time_node = msg.select_one(".tgme_widget_message_date time[datetime]")
        if not time_node or not time_node.get("datetime"):
            continue

        try:
            published_at = datetime.fromisoformat(str(time_node["datetime"]))
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        # Удаляем блок цитирования (reply), чтобы его текст не смешивался с текстом самого поста
        for reply_block in msg.select(".tgme_widget_message_reply"):
            reply_block.decompose()

        text_node = msg.select_one(".tgme_widget_message_text")
        if not text_node:
            # Пропускаем посты без текста (например, одиночные картинки или стикеры без подписи)
            continue

        text, external_links = _extract_text_and_links(text_node)
        if not text:
            continue

        posts.append(
            Post(
                channel=channel,
                post_id=post_id,
                published_at=published_at,
                text=text,
                url=f"https://t.me/{channel}/{post_id}",
                external_links=external_links,
            )
        )

    return posts


def fetch_channel_posts(
    raw_channel: str,
    days_lookback: int = 3,
    last_seen_id: Optional[int] = None,
    max_pages: int = 3,
    client: Optional[httpx.Client] = None,
) -> list[Post]:
    """
    Скачивает посты публичного канала за последние `days_lookback` дней
    (и новее `last_seen_id`, если он задан).
    Поддерживает пагинацию вглубь истории через `?before=<post_id>`.
    """
    channel = normalize_channel_name(raw_channel)
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_lookback)

    own_client = client is None
    if own_client:
        client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
            follow_redirects=True,
            timeout=15.0,
        )

    collected: dict[int, Post] = {}
    before_id: Optional[int] = None

    try:
        for page_num in range(1, max_pages + 1):
            url = f"https://t.me/s/{channel}"
            params = {"before": str(before_id)} if before_id is not None else None

            logger.info("Чтение канала @%s (страница %d, before=%s)...", channel, page_num, before_id)
            response = client.get(url, params=params)
            response.raise_for_status()

            page_posts = _parse_page_posts(response.text, channel)
            if not page_posts:
                logger.warning("На странице @%s (page=%d) не найдено текстовых постов.", channel, page_num)
                break

            for post in page_posts:
                if last_seen_id is not None and post.post_id <= last_seen_id:
                    continue
                if post.published_at < cutoff_date:
                    continue
                collected[post.post_id] = post

            # Проверяем самый старый пост на текущей странице, нужно ли идти глубже
            oldest_post = min(page_posts, key=lambda p: p.post_id)
            if oldest_post.published_at < cutoff_date:
                break
            if last_seen_id is not None and oldest_post.post_id <= last_seen_id:
                break

            before_id = oldest_post.post_id
    except httpx.HTTPError as exc:
        logger.error("Ошибка HTTP при чтении канала @%s: %s", channel, exc)
    finally:
        if own_client and client is not None:
            client.close()

    # Возвращаем посты в хронологическом порядке (от старых к новым)
    return sorted(collected.values(), key=lambda p: p.post_id)
