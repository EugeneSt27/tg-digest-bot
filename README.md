# Telegram AI Digest

Автоматический генератор тематических дайджестов из публичных Telegram-каналов с помощью LLM.

---

## Как это работает

1. **Сбор постов (Scraping)**:
   - Скрипт обращается к веб-версиям публичных каналов (`https://t.me/s/<channel>`) через `httpx` и парсит посты с помощью `BeautifulSoup4`.
   - Не требует авторизации в Telegram (User API / MTProto не используются).
   - Фильтрует посты по дате (`days_lookback`) и отслеживает последний обработанный пост (`last_seen_id`) для каждого канала.

2. **Анализ и суммаризация (LLM)**:
   - Собранные посты форматируются и передаются в модель (Gemini или OpenAI-совместимый API).
   - Промпт для каждого профиля задается отдельно (например, `config/prompts/ai.yaml` или `config/prompts/spb.yaml`).
   - Модель отбирает главное, группирует по темам и формирует структурированный дайджест в HTML-разметке Telegram.

3. **Синхронизация подписчиков (Subscribers Sync)**:
   - Перед отправкой скрипт опрашивает Telegram Bot API (`getUpdates`), фиксируя команды `/start` (добавление) и `/stop` (отписка).

4. **Рассылка (Broadcast)**:
   - Дайджест разбивается на безопасные части (до 4000 символов с сохранением абзацев и HTML-тегов).
   - Рассылается всем подписчикам и/или в указанные каналы/чаты админов.
   - Если рассылка успешна хотя бы одному получателю, обновляется `data/state.json`.

---

## Быстрый старт

### 1. Требования
- Python 3.10+
- Токен Telegram-бота (получить в [@BotFather](https://t.me/BotFather))
- Ключ API для LLM (Google Gemini или OpenRouter / OpenAI)

### 2. Установка зависимостей

Создайте виртуальное окружение и установите библиотеки:

```bash
# Переход в папку проекта
cd telegram-ai-digest

# Создание и активация виртуального окружения
python -m venv .venv

# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Windows (CMD):
.venv\Scripts\activate.bat
# Linux / macOS:
source .venv/bin/activate

# Установка зависимостей
pip install -r requirements.txt
```

### 3. Настройка переменных окружения

Скопируйте пример конфигурации `.env.example` в `.env`:

```bash
copy .env.example .env
```

Заполните `.env`

---

## Настройка профилей (`config/settings.yaml`)

Каждый дайджест настраивается как отдельный профиль:

```yaml
defaults:
  days_lookback: 3            # Глубина сбора по умолчанию (дни)
  max_pages_per_channel: 3    # Сколько страниц пагинации проверять назад

profiles:
  jokes:
    description: "Шутки и юмор"
    prompt_file: "prompts/hehe.yaml"
    days_lookback: 3
    channels:
      - https://t.me/ahahah
      - https://t.me/lololol
      # ...

```

---

## Запуск проекта

### Основные команды

```bash
# Запуск профиля по умолчанию
python main.py

# Запуск конкретного профиля
python main.py --profile jokes

# Тестовый запуск (dry-run): соберет посты, сгенерирует дайджест и выведет в консоль.
# Не отправляет сообщения в Telegram и не меняет state.json
python main.py --profile jokes --dry-run

# Сбор постов за другое количество дней
python main.py --profile jokes --days 7

# собрать все посты за последние N дней заново
python main.py --profile jokes --no-state
```

---

## Управление подписчиками и получателями

Получатели дайджеста формируются из трех источников:

1. **Динамические подписчики** (`data/subscribers.json`):
   - Пользователь пишет боту `/start` в личные сообщения.
   - При следующем запуске `main.py` функция `sync_subscribers` забирает новые апдейты через `getUpdates`, добавляет `chat_id` пользователя в базу для текущего профиля и подтверждает смещение `offset`.
   - Если пользователь пишет `/stop`, он удаляется из списка рассылки.
2. **Переменные окружения** (`TELEGRAM_CHAT_ID_<PROFILE>` или `TELEGRAM_CHAT_ID` в `.env`):
   - Удобно для отправки в закрытый канал, чат-группу или постоянному администратору.
3. **Параметр `chat_id` в `settings.yaml`**:
   - Можно закрепить чат напрямую в профиле.

Все списки объединяются и автоматически **дедуплицируются** перед отправкой.

---

## Структура проекта

```
telegram-ai-digest/
├── config/
│   ├── settings.yaml          # Список профилей, каналов и базовых параметров
│   └── prompts/               # Промпты для суммаризации под каждый профиль
│       ├── ai.yaml
│       └── spb.yaml
├── data/
│   ├── state.json             # Последние прочитанные ID постов по каналам
│   └── subscribers.json       # База подписчиков по профилям {profile: [chat_id, ...]}
├── src/
│   ├── analyzer.py            # Построение промпта и вызов LLM
│   ├── fetcher.py             # Парсинг t.me/s/<channel> и извлечение постов
│   ├── llm.py                 # Интеграция с Google Gemini и OpenAI API
│   ├── sender.py              # Нарезка и отправка сообщений в Telegram Bot API
│   ├── storage.py             # Атомарное чтение и сохранение state.json
│   └── subscribers.py         # Синхронизация подписчиков через getUpdates
├── .env                       # Переменные окружения и ключи (не коммитится)
├── .env.example               # Шаблон переменных окружения
├── main.py                    # Главная точка входа (CLI)
└── requirements.txt           # Зависимости Python
```
