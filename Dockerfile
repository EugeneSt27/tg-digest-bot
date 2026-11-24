FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

# 1. Системные пакеты и московский часовой пояс (редко меняющийся слой)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo "${TZ}" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 2. Непривилегированный пользователь, совпадающий по UID/GID с eugene на VPS
RUN groupadd --gid "${APP_GID}" eugene \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /bin/bash eugene

WORKDIR /app

# 3. Установка зависимостей отдельным слоем для кэширования при изменениях кода
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 4. Копирование кода и конфигов (идут в конце, чтобы не сбрасывать кэш pip)
COPY --chown=eugene:eugene src/ ./src/
COPY --chown=eugene:eugene config/ ./config/
COPY --chown=eugene:eugene bot.py main.py ./

RUN mkdir -p /app/data /app/logs \
    && chown -R eugene:eugene /app/data /app/logs

USER eugene

CMD ["python", "bot.py"]
