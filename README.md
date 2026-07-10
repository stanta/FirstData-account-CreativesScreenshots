# reports_ad

`reports_ad` — MVP Python-приложение для формирования proof-скриншота рекламного размещения через Telegram-бота в режиме polling.

## Что реализовано в MVP

- Telegram polling bot на `python-telegram-bot` 21+.
- Пошаговый сценарий ввода:
  1. выбор рекламной сети кнопкой reply-menu, построенной через `telegram_menu`;
  2. выбор типа баннера/spec кнопкой `telegram_menu`, отфильтрованной по сети;
  3. URL страницы рекламной площадки;
  4. PNG/JPEG-креатив через Telegram-файл/фото или локальный путь в тестовом режиме;
  5. URL landing page;
  6. UTM-параметры строкой;
  7. review screen со всеми параметрами и командами `edit <field>`;
  8. явное подтверждение генерации.
- Валидация URL, UTM, MIME/размера/точной размерности изображения по выбранному spec и placement.
- Локальный каталог `reports_ad/reports_ad/data/creative_catalog.json` нормализован из `validator-ai/backend/app/seed/creative_specs.json`: сохранены slug, display name, network/platform, format/dimensions, source MIME/extensions и file-size limit.
- Playwright/Chromium генератор: открывает страницу, вставляет статичный баннер, назначает click URL с UTM, эмулирует клик, получает landing page и собирает итоговое изображение, где нижние 20% заняты landing page.
- Нет сохранения истории задач; состояние диалога и per-user locks только в памяти процесса.
- Временные файлы удаляются после отправки результата, отмены или ошибки.

## Menu selection dependency note

Выбор рекламной сети и размера/spec переведен с ручного ввода slug на reply-keyboard меню, построенные через библиотеку `telegram_menu`.

- PyPI package name в lock/dependency metadata отображается как `telegram-menu`/`telegram_menu`; импорт в Python выполняется как `telegram_menu`.
- Используется легкий adapter на `telegram_menu.BaseMessage`: бот остается на существующем `python-telegram-bot` `ConversationHandler` и polling lifecycle, а `telegram_menu` применяется для построения keyboard buttons только в двух шагах выбора.
- После выбора рекламной сети бот сразу отправляет prompt `Теперь выберите тип баннера/spec кнопкой меню telegram_menu:` и показывает `telegram_menu` reply-keyboard только со spec-кнопками выбранной сети; выбранный label вида `Display [spec-slug] — размер` мапится обратно в slug.
- Полная `TelegramMenuSession` из библиотеки не включена, потому что она создает собственное приложение, persistence и handlers; это конфликтовало бы с текущим no-history runtime и существующим polling flow. Остальные шаги по-прежнему принимают текст/файл без меню.
- На случай ручного ввода бот сохраняет обратную совместимость: принимает как menu label вида `Яндекс [yandex_graphic_banners] — 12 specs`, так и сам slug.

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
docker compose build
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

## Catalog and placement behavior

Каталог содержит 96 source specs из `creative_specs.json` и 19 рекламных сетей/platform groups. Runtime-фильтр бота показывает в `telegram_menu` только specs, которые можно принять как статичный PNG/JPEG с фиксированными размерами. Если source spec допускает GIF, PSD, ZIP, MP4 или HTML5, эти данные сохранены в catalog metadata, но Telegram runtime MVP их не принимает.

После ввода advertising page URL бот выбирает placement автоматически:

- если host есть в списке `reports_ad/docs/examples/FD -- Примеры скриншотов/площадки.txt` и для него есть слабонаблюдаемый selector из HTML sample, используется selector;
- иначе применяется безопасный dimension-based fallback template по размеру выбранного spec;
- если host неизвестен, используется template по размеру или `in-content` как последний fallback.

Статус проверки площадок сохранен в catalog metadata. Проверка выполнялась из workspace через `reports_ad/.venv/bin/python` с HTML-sample запросами. Ограничения: `dzen.ru`, `m.dzen.ru`, `yandex.ru` редиректили на Yandex Passport/SSO; `kniga-online.com` вернул HTTP 403; `m.video.yandex.ru` и `m.images.yandex.ru` дали TLS hostname mismatch; `drug.vokrug` не резолвился DNS. Поэтому selectors с confidence не `observed_sample` являются fallback heuristics, а не verified production selectors.

## Manual placement syntax for internal validation helpers

Поддерживаются два варианта:

- `selector: .ad-slot` — явный CSS-селектор. Это основной production-safe режим MVP.
- `template: top-banner`, `template: sidebar-rect`, `template: in-content`, `template: mobile-banner`, `template: video-landscape` — ограниченные fallback-шаблоны. Если известный слот не найден, генератор создает контролируемый proof-контейнер в DOM страницы.

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
docker compose up --build --no-start
docker compose up -d
```

- `docker compose config` валидирует YAML и interpolation переменных.
- `docker compose up --build --no-start` проверяет сборку образа и container runtime без запуска polling-цикла.
- `docker compose up -d` проверяет реальный startup через штатный entrypoint.
- Если сборка прерывается на скачивании Chromium, причина обычно во внешней сети/CDN Playwright; в этом случае команду безопасно повторить.

## Ограничения MVP

- Генератор поддерживает статичные PNG/JPEG-креативы. Видео/in-stream и библиотека маркировок вынесены за рамки текущего MVP.
- Итоговый screenshot является viewport-композицией без реального Chrome UI/системной панели. Headful/Xvfb desktop capture потребуется отдельным этапом.
- Произвольная DOM-вставка на любых сайтах может быть нестабильной из-за CSP, cookie overlays, lazy-load, iframe, anti-bot и динамической верстки; для надежной работы предпочтителен явный CSS-селектор.
