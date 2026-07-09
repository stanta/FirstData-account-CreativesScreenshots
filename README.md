# reports_ad

`reports_ad` — MVP Python-приложение для формирования proof-скриншота рекламного размещения через Telegram-бота в режиме polling.

## Что реализовано в MVP

- Telegram polling bot на `python-telegram-bot` 21+.
- Пошаговый сценарий ввода:
  1. URL страницы рекламной площадки;
  2. тип/формат баннера;
  3. PNG/JPEG-креатив через Telegram-файл/фото или локальный путь в тестовом режиме;
  4. URL landing page;
  5. UTM-параметры строкой;
  6. место вставки: CSS-селектор или template ID;
  7. подтверждение генерации.
- Валидация URL, UTM, MIME/размера/размерности изображения и placement.
- Playwright/Chromium генератор: открывает страницу, вставляет статичный баннер, назначает click URL с UTM, эмулирует клик, получает landing page и собирает итоговое изображение, где нижние 20% заняты landing page.
- Нет сохранения истории задач; состояние диалога и per-user locks только в памяти процесса.
- Временные файлы удаляются после отправки результата, отмены или ошибки.

## Быстрый старт

```bash
cd /512-2/FD/reports_ad
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
export REPORTS_AD_TELEGRAM_TOKEN="<telegram-bot-token>"
python -m reports_ad
```

Альтернативно после установки доступна команда:

```bash
reports-ad-bot
```

## Docker

Контейнерный runtime рассчитан на polling-бота и headless Playwright Chromium внутри Linux-контейнера. Браузер и системные зависимости устанавливаются на этапе сборки образа, поэтому интерактивный `playwright install` при старте контейнера не нужен.

### Сборка образа

```bash
cd /512-2/FD/reports_ad
docker build -t reports-ad:local .
```

### Запуск через docker run

1. Подготовьте переменные окружения, минимум `REPORTS_AD_TELEGRAM_TOKEN`.
2. Запустите контейнер:

```bash
cd /512-2/FD/reports_ad
docker run --rm \
  --name reports-ad-bot \
  --env-file .env \
  --shm-size=1gb \
  --tmpfs /tmp/reports-ad:size=268435456,mode=1777 \
  reports-ad:local
```

### Запуск через Docker Compose

1. Создайте локальный файл окружения на основе примера:

```bash
cd /512-2/FD/reports_ad
cp .env.example .env
```

2. Заполните реальный `REPORTS_AD_TELEGRAM_TOKEN` в `.env`.
3. Соберите и запустите сервис:

```bash
cd /512-2/FD/reports_ad
docker compose up -d --build
```

Полезные команды:

```bash
cd /512-2/FD/reports_ad
docker compose logs -f reports-ad-bot
docker compose down
```

Контейнер использует entrypoint `python -m reports_ad` и читает конфигурацию только из environment variables.

## Переменные окружения

| Переменная                             | Значение по умолчанию | Назначение                                                         |
| -------------------------------------- | --------------------: | ------------------------------------------------------------------ |
| `REPORTS_AD_TELEGRAM_TOKEN`            |                   нет | обязательный токен Telegram bot API                                |
| `REPORTS_AD_HEADLESS`                  |                   `1` | запуск Chromium без UI                                             |
| `REPORTS_AD_ALLOW_LOCAL_URLS`          |                   `0` | разрешить `localhost`/private IP только для локальных smoke-тестов |
| `REPORTS_AD_ALLOW_LOCAL_CREATIVE_PATH` |                   `0` | разрешить ввод локального пути вместо Telegram upload              |
| `REPORTS_AD_VIEWPORT_WIDTH`            |                `1365` | ширина viewport/final screenshot                                   |
| `REPORTS_AD_VIEWPORT_HEIGHT`           |                 `768` | высота viewport/final screenshot                                   |
| `REPORTS_AD_MAX_CREATIVE_BYTES`        |            `10485760` | максимальный размер креатива                                       |
| `REPORTS_AD_MAX_CREATIVE_PIXELS`       |            `20000000` | максимальное число пикселей входного креатива                      |
| `REPORTS_AD_REQUEST_TIMEOUT_SECONDS`   |                  `90` | общий timeout генерации для одного запроса                         |
| `REPORTS_AD_PAGE_LOAD_TIMEOUT_MS`      |               `30000` | timeout загрузки страницы в Playwright                             |
| `REPORTS_AD_CLICK_TIMEOUT_MS`          |                `7000` | timeout ожидания popup/click flow                                  |
| `REPORTS_AD_SCREENSHOT_TIMEOUT_MS`     |               `15000` | timeout создания screenshot                                        |
| `REPORTS_AD_LOCALE`                    |               `ru-RU` | locale browser context                                             |
| `REPORTS_AD_TIMEZONE`                  |       `Europe/Moscow` | timezone browser context                                           |
| `REPORTS_AD_TEMP_ROOT`                 |                   нет | корневая директория временных файлов; в Docker используется tmpfs  |

## Placement syntax

Поддерживаются два варианта:

- `selector: .ad-slot` — явный CSS-селектор. Это основной production-safe режим MVP.
- `template: top-banner`, `template: sidebar-rect`, `template: in-content` — ограниченные fallback-шаблоны. Если известный слот не найден, генератор создает контролируемый proof-контейнер в DOM страницы.

## UTM syntax

Можно передать строку вида:

```text
utm_source=telegram&utm_medium=cpc&utm_campaign=test
```

или с начальным `?`. Если передан полный URL, из него берется query-string. Все ключи должны начинаться с `utm_`; существующие query-параметры landing URL сохраняются, одноименные UTM перезаписываются.

## Тесты

Юнит-тесты не требуют реального Telegram token и не ходят во внешнюю сеть:

```bash
cd /512-2/FD/reports_ad
pytest
```

Если Playwright установлен, но Chromium browser binaries отсутствуют, установите их командой:

```bash
python -m playwright install chromium
```

## Docker-проверки

Если в среде доступен Docker, минимальные проверки для container assets такие:

```bash
cd /512-2/FD/reports_ad
docker compose config
docker build -t reports-ad:local .
docker run --rm --env-file .env reports-ad:local
```

- `docker compose config` валидирует YAML и interpolation переменных.
- `docker build` проверяет сборку Python/Playwright runtime.
- `docker run` проверяет, что контейнер стартует с существующим entrypoint и читает обязательные environment variables.

## Ограничения MVP

- Генератор поддерживает статичные PNG/JPEG-креативы. Видео/in-stream и библиотека маркировок вынесены за рамки текущего MVP.
- Итоговый screenshot является viewport-композицией без реального Chrome UI/системной панели. Headful/Xvfb desktop capture потребуется отдельным этапом.
- Произвольная DOM-вставка на любых сайтах может быть нестабильной из-за CSP, cookie overlays, lazy-load, iframe, anti-bot и динамической верстки; для надежной работы предпочтителен явный CSS-селектор.
