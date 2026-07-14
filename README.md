# reports_ad

`reports_ad` — MVP Python-приложение для формирования proof-скриншота рекламного размещения через Telegram-бота в режиме polling.

## Что реализовано в MVP

- Telegram polling bot на `python-telegram-bot` 21+.
- Пошаговый сценарий ввода:
  1. выбор рекламной сети кнопкой reply-menu, построенной через `telegram_menu`;
  2. выбор типа баннера/spec кнопкой `telegram_menu`, отфильтрованной по сети;
  3. PNG/JPEG-креатив через Telegram-файл/фото или локальный путь в тестовом режиме;
  4. URL страницы рекламной площадки;
  5. URL landing page;
  6. UTM-параметры строкой;
  7. review screen со всеми параметрами и русским reply-menu правок, построенным через `telegram_menu`;
  8. явное подтверждение генерации кнопкой `Запустить`.
- Валидация URL, UTM, MIME/размера/точной размерности изображения по выбранному spec и placement.
- Локальный каталог `reports_ad/reports_ad/data/creative_catalog.json` нормализован из `validator-ai/backend/app/seed/creative_specs.json`: сохранены slug, display name, network/platform, format/dimensions, source MIME/extensions и file-size limit.
- Playwright/Chromium генератор: открывает страницу, вставляет статичный баннер, назначает click URL с UTM, эмулирует клик, получает landing page и собирает итоговое изображение, где нижние 20% заняты landing page.
- Нет сохранения истории задач; состояние диалога и per-user locks только в памяти процесса.
- Временные файлы удаляются после отправки результата, отмены или ошибки.
- После успешной вставки креатива дополнительно сохраняется промежуточный PNG в `reports_ad/tmp`: это снимок рекламной страницы уже с вставленным креативом, но еще до клика по landing URL. Он не отправляется пользователю вместо итогового proof.

## Menu selection dependency note

Выбор рекламной сети и размера/spec переведен с ручного ввода slug на reply-keyboard меню, построенные через библиотеку `telegram_menu`.

- PyPI package name в lock/dependency metadata отображается как `telegram-menu`/`telegram_menu`; импорт в Python выполняется как `telegram_menu`.
- Используется легкий adapter на `telegram_menu.BaseMessage`: бот остается на существующем `python-telegram-bot` `ConversationHandler` и polling lifecycle, а `telegram_menu` применяется для построения keyboard buttons только в двух шагах выбора.
- После выбора рекламной сети бот сразу отправляет prompt `Теперь выберите тип баннера/spec кнопкой меню telegram_menu:` и показывает `telegram_menu` reply-keyboard только со spec-кнопками выбранной сети; выбранный label вида `Display [spec-slug] — размер` мапится обратно в slug.
- После выбора spec бот сначала просит отправить PNG/JPEG-креатив с указанными размерами и лимитом файла, валидирует upload по выбранному spec и только после успешной проверки переходит к prompt `Введите URL рекламной страницы, где нужно разместить рекламу.`. При правке spec ранее загруженный креатив сбрасывается и требуется повторная загрузка, чтобы не сохранить файл, несовместимый с новым spec.
- На review screen бот больше не просит вводить `yes`/`edit ...` как основной способ управления. Вместо этого показывается `telegram_menu` reply-keyboard с русскими пунктами: `Изменить рекламную сеть`, `Изменить тип баннера`, `Изменить креатив`, `Изменить URL рекламной страницы`, `Изменить landing URL`, `Изменить UTM-строку`, `Запустить`. Последний пункт меню — ровно `Запустить`; при его выборе reply-keyboard снимается и начинается генерация proof-скриншота.
- При переходе из review screen в правку текстовых/файловых полей меню снимается. Для правки сети и spec показываются соответствующие `telegram_menu` reply-keyboards; правка spec по-прежнему требует повторной загрузки креатива.
- Полная `TelegramMenuSession` из библиотеки не включена, потому что она создает собственное приложение, persistence и handlers; это конфликтовало бы с текущим no-history runtime и существующим polling flow. Остальные шаги по-прежнему принимают текст/файл без меню.
- На случай ручного ввода бот сохраняет обратную совместимость: принимает как menu label вида `Яндекс [yandex_graphic_banners] — 12 specs`, так и сам slug; на review screen старые команды `yes` и `edit network`/`edit spec`/`edit creative`/`edit ad_url`/`edit landing`/`edit utm` оставлены fallback-вводом.

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
  -v "$(pwd)/tmp:/app/tmp" \
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

| Переменная                                  |                  Значение по умолчанию | Назначение                                                                                                                 |
| ------------------------------------------- | -------------------------------------: | -------------------------------------------------------------------------------------------------------------------------- |
| `REPORTS_AD_TELEGRAM_TOKEN`                 |                                    нет | обязательный токен Telegram bot API                                                                                        |
| `REPORTS_AD_HEADLESS`                       |                                    `1` | запуск Chromium без UI                                                                                                     |
| `REPORTS_AD_ALLOW_LOCAL_URLS`               |                                    `0` | разрешить `localhost`/private IP только для локальных smoke-тестов                                                         |
| `REPORTS_AD_ALLOW_LOCAL_CREATIVE_PATH`      |                                    `0` | разрешить ввод локального пути вместо Telegram upload                                                                      |
| `REPORTS_AD_VIEWPORT_WIDTH`                 |                                 `1365` | ширина viewport/final screenshot                                                                                           |
| `REPORTS_AD_VIEWPORT_HEIGHT`                |                                  `768` | высота viewport/final screenshot                                                                                           |
| `REPORTS_AD_MAX_CREATIVE_BYTES`             |                             `10485760` | максимальный размер креатива                                                                                               |
| `REPORTS_AD_MAX_CREATIVE_PIXELS`            |                             `20000000` | максимальное число пикселей входного креатива                                                                              |
| `REPORTS_AD_REQUEST_TIMEOUT_SECONDS`        |                                  `300` | общий timeout генерации для одного запроса; увеличен, чтобы fallback-цепочка успевала завершиться                          |
| `REPORTS_AD_PAGE_LOAD_TIMEOUT_MS`           |                                `12000` | timeout загрузки каждой advertising/fallback площадки в Playwright; снижен, чтобы плохие площадки не занимали по 30 секунд |
| `REPORTS_AD_CLICK_TIMEOUT_MS`               |                                 `7000` | timeout ожидания popup/click flow                                                                                          |
| `REPORTS_AD_SCREENSHOT_TIMEOUT_MS`          |                                `15000` | timeout создания screenshot                                                                                                |
| `REPORTS_AD_TIMEOUT_DIAGNOSTICS`            |                                    `1` | писать DNS/TCP/TLS diagnostics при timeout загрузки ad page                                                                |
| `REPORTS_AD_TIMEOUT_DIAGNOSTICS_TIMEOUT_MS` |                                 `5000` | timeout каждого DNS/TCP/TLS diagnostic check                                                                               |
| `REPORTS_AD_LOCALE`                         |                                `ru-RU` | locale browser context                                                                                                     |
| `REPORTS_AD_TIMEZONE`                       |                        `Europe/Moscow` | timezone browser context                                                                                                   |
| `REPORTS_AD_TEMP_ROOT`                      |                                    нет | корневая директория временных файлов; в Docker используется tmpfs                                                          |
| `REPORTS_AD_OCR_LLM_BASE_URL`               |                                    нет | OpenAI-compatible base URL для Stage 2 OCR/vision модели                                                                   |
| `REPORTS_AD_OCR_LLM_MODEL`                  | `cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit` | имя OCR/vision модели; фактическая модель может быть Gemma                                                                 |
| `REPORTS_AD_OCR_LLM_TIMEOUT_SECONDS`        |                                   `20` | timeout OCR LLM request                                                                                                    |
| `REPORTS_AD_OCR_LLM_MAX_RETRIES`            |                                    `5` | количество попыток OCR LLM request / JSON parsing                                                                          |
| `REPORTS_AD_OCR_LLM_CONFIDENCE_THRESHOLD`   |                                 `0.45` | минимальная confidence для принятия bbox                                                                                   |
| `REPORTS_AD_OCR_LLM_HEADER_AUTHORISATION`   |                                    нет | опциональный raw Authorization header; имеет приоритет над user/password                                                   |
| `REPORTS_AD_OCR_LLM_USER`                   |                                    нет | опциональный basic-auth username для OCR LLM                                                                               |
| `REPORTS_AD_OCR_LLM_PASSWORD`               |                                    нет | опциональный basic-auth password для OCR LLM                                                                               |
| `REPORTS_AD_OCR_LLM_API_KEY`                |                                    нет | зарезервировано для Gemini-style OCR config parity; текущий OpenAI-compatible клиент его не использует                     |

`REPORTS_AD_REQUEST_TIMEOUT_SECONDS=300` и `REPORTS_AD_PAGE_LOAD_TIMEOUT_MS=12000` являются согласованной парой: общий timeout достаточно длинный для перебора fallback-площадок, а timeout одного кандидата ограничивает зависание плохих площадок. Площадки, которые по Docker logs не дают пригодную загрузку в пределах 30 секунд, исключаются из активной fallback-итерации в каталоге, но остаются в JSON с `enabled: false` и `disabled_reason` для traceability.

Stage 2 LLM/OCR public configuration uses `REPORTS_AD_OCR_LLM_*` names by analogy with `validator-ai` `OCR_LLM_*`: `BASE_URL`, `MODEL`, `TIMEOUT_SECONDS`, `HEADER_AUTHORISATION`, `USER`, `PASSWORD` and optional `API_KEY`. This naming is intentionally OCR-style even when the actual OpenAI-compatible vision model is Gemma. Legacy `REPORTS_AD_GEMMA_*` names are not read by the runtime.

Начиная с bugfix 2026-07-14, Stage 2 screenshot-only flow получает runtime config напрямую из Telegram runtime path. Если OCR runtime не инициализирован или не заполнен `REPORTS_AD_OCR_LLM_BASE_URL`, бот возвращает явную безопасную ошибку конфигурации вместо `AttributeError`.

## Диагностика timeout/no-result

Генерация proof-скриншота пишет структурированные runtime-логи по фазам без Telegram token, полных URL с UTM, локальных временных путей и содержимого upload-файлов. В логах используются `request_id`, хеши пользователя/чата, host рекламной страницы и landing page, сеть/spec, размеры, viewport, placement, имя фазы, статус, `elapsed_ms`, timeout и безопасно усеченные exception class/message.

Ключевые фазы:

- `ad_page_load` — загрузка рекламной страницы. Для ad-heavy страниц используется tolerant strategy: Chromium сначала ждет `commit`, затем bounded wait for `domcontentloaded` и `body`. Если `commit` уже произошел, но `domcontentloaded`/`body` зависли, generator проверяет безопасный DOM probe (`document.readyState`, title, наличие `html`/`body`, длину HTML) и продолжает только если есть inspectable `documentElement`; `networkidle` не используется, потому что рекламные трекеры, long polling и lazy-load могут держать сеть активной до timeout.
- `ad_page_fallback_candidate` — попытка загрузить исходную advertising page или predefined fallback-площадку. Логируются только media type, индекс/количество кандидата, host, результат, длительность и безопасно усеченная причина ошибки; полные URL/query/UTM не пишутся. Активная итерация пропускает hosts с `enabled: false`.
- `creative_insert` — поиск selector/template и вставка креатива.
- `creative_insert_screenshot` — сохранение промежуточного screenshot с уже вставленным креативом в `reports_ad/tmp` до клика по landing page. Имя файла строится как `creative-stem__host__YYYYMMDD-HHMMSS.png`, где используются только санитизированные stem исходного файла креатива и host площадки; URL, UTM, Telegram IDs и локальные пути в имя не попадают. Если этот screenshot не удается сохранить из-за timeout или ошибки записи, генерация завершается понятной ошибкой; частичный итоговый proof не отправляется.
- `landing_click` / `landing_fallback_open` — клик по баннеру, ожидание popup/new page и fallback-открытие landing URL в контролируемой странице.
- `ad_screenshot` / `landing_screenshot` — создание screenshot рекламной страницы и landing page.
- `image_compose` и `proof_telegram_send_*` — сборка итогового PNG и отправка в Telegram.

Пользовательские timeout-сообщения теперь разделяют основные причины: timeout загрузки рекламной страницы, timeout клика/landing page, timeout создания screenshot и общий timeout генерации. Если пользователь видит timeout landing/click, сначала проверьте доступность host landing page из контейнера/сервера и увеличьте `REPORTS_AD_PAGE_LOAD_TIMEOUT_MS` только после проверки, что страница действительно медленная, а не заблокирована антиботом/TLS/DNS. Если timeout происходит на screenshot, проверьте перегрузку CPU/памяти контейнера и `--shm-size`/tmpfs.

При timeout фазы `ad_page_load` бот дополнительно пишет безопасные `container_network_diagnostic` события для advertising host и landing host. Эти события не содержат полных URL, query/UTM, Telegram token или файловых путей. Смотрите последовательность `dns` → `tcp_connect` → `tls_handshake`: успешный DNS при зависании TCP/TLS обычно указывает на Docker/network/proxy/firewall/egress filtering или блокировку со стороны площадки, а не на selector или Chromium dependencies.

Начиная с исправления 2026-07-10, ad-page timeout дополнительно диагностируется на уровне Chromium без раскрытия query/UTM: `ad_page_commit`, `ad_page_partial_dom_fallback` и `ad_page_load_chromium` логируют только host ответа/страницы, status, цепочку navigation hosts, счетчики/samples request failures, response statuses, console/pageerror и минимальный DOM probe. Если `commit` прошел, а `domcontentloaded`/`body` не наступили из-за redirect/challenge/anti-bot, generator больше не падает только из-за этого ожидания: он создает `body` при наличии `document.documentElement` и продолжает вставку template-креатива. Если DOM полностью недоступен или execution context постоянно уничтожается navigation, ошибка остается phase-specific и actionable.

Ручная проверка из уже запущенного контейнера:

```bash
cd /512-2/FD/reports_ad
docker compose exec -T reports-ad-bot python -m reports_ad.diagnostics dzen.ru firstdata.io
```

Логи по последней генерации без раскрытия секретов:

```bash
cd /512-2/FD/reports_ad
docker compose logs --since 30m --no-color reports-ad-bot \
  | grep -E "proof_generation|proof_phase|container_network_diagnostic"
```

### Docker external-connectivity troubleshooting

Use this checklist when the bot starts but cannot reliably reach Telegram, an advertising page, or a landing host from inside the container. Do not paste full Telegram API URLs, UTM URLs, uploaded file paths, or raw page HTML into tickets/logs.

Safe first checks:

```bash
cd /512-2/FD/reports_ad
docker compose ps reports-ad-bot
docker compose logs --since 30m --no-color reports-ad-bot \
  | sed -E 's#bot[0-9]+:[A-Za-z0-9_-]+#bot<REDACTED>#g; s#(https?://[^/?[:space:]"'"']+)[^[:space:]"'"']*#\1/<redacted>#g' \
  | grep -Ei 'proof_phase|container_network_diagnostic|chromium|browser|dns|tcp|tls|proxy|cert|timeout|temporary failure|ERR_|net::|dzen|telegram|firstdata'
docker compose exec -T reports-ad-bot reports-ad-diagnostics --timeout-ms 5000 api.telegram.org dzen.ru firstdata.io
```

Log interpretation matrix:

| Evidence in sanitized logs                                                                                                                                         | Likely category                                                                                  | What to check/fix                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Temporary failure in name resolution`, `container_network_diagnostic check='dns' status='failure'`                                                                | Container DNS failure                                                                            | Docker container inherited a resolver that is not reachable from the container namespace or the resolver/firewall is flaky. Test `/etc/resolv.conf` inside the container. Try Compose `dns:` with trusted/corporate resolvers, or daemon-wide DNS in `/etc/docker/daemon.json`. Docker networking and Compose service DNS options are documented in Docker networking docs and Compose service reference: https://docs.docker.com/engine/network/ and https://docs.docker.com/reference/compose-file/services/.                                 |
| DNS succeeds but `tcp_connect` fails or times out                                                                                                                  | Egress routing/firewall/NAT failure                                                              | Confirm host-level egress to the same host/port, Docker bridge forwarding/NAT rules, corporate firewall, VPN split tunneling, and whether `network_mode: host` is acceptable for a temporary diagnostic run on Linux. Docker Compose networking behavior is documented at https://docs.docker.com/compose/how-tos/networking/.                                                                                                                                                                                                                  |
| DNS and TCP succeed but `tls_handshake` times out                                                                                                                  | TLS path/proxy/MTU/filtering issue or remote throttling                                          | Check whether the environment requires HTTP(S) proxy variables. Configure Docker daemon proxy or service `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` only with approved values. Docker daemon proxy docs: https://docs.docker.com/engine/daemon/proxy/. If only HTTPS/TLS stalls, test path MTU and try Docker daemon/network MTU tuning with `dockerd --mtu` or network driver option; Docker `dockerd` documents `--mtu`: https://docs.docker.com/reference/cli/dockerd/.                                                                           |
| Python DNS/TCP/TLS succeeds, but Chromium logs `Page.goto` timeout, `domcontentloaded=False`, `body=False`, redirect to SSO/challenge host, or `probe_error=Error` | Chromium/page lifecycle failure after network connect; site anti-bot/challenge/redirect behavior | The current generator already uses `wait_until="commit"` and a bounded DOM/body fallback because ad-heavy sites can keep loading indefinitely. For sites such as `dzen.ru`, redirect to Yandex Passport/SSO or anti-bot challenge can leave the page without an inspectable DOM; prefer known public article URLs, approved test URLs, explicit selectors, or manual operator verification. Playwright navigation/load-state behavior is documented at https://playwright.dev/docs/api/class-page and https://playwright.dev/docs/navigations/. |
| Browser launch errors, renderer crashes, `Target closed`, `No space left on device`, `/dev/shm`, sandbox messages                                                  | Chromium resource/sandbox/shared-memory failure                                                  | Keep Compose `shm_size: "1gb"`; reduce concurrency; ensure tmpfs has enough space; use the official Playwright Docker image. Playwright Docker docs note root/sandbox behavior and recommended container setup: https://playwright.dev/python/docs/docker/.                                                                                                                                                                                                                                                                                     |
| `409 Conflict: terminated by other getUpdates request`                                                                                                             | Telegram polling conflict, not external connectivity                                             | Stop duplicate bot instances that use the same token. Only one polling process can call Telegram `getUpdates` for a token at a time.                                                                                                                                                                                                                                                                                                                                                                                                            |

Practical Compose snippets for operator experiments:

```yaml
services:
  reports-ad-bot:
    dns:
      - 1.1.1.1
      - 8.8.8.8
    environment:
      HTTPS_PROXY: ${HTTPS_PROXY:-}
      HTTP_PROXY: ${HTTP_PROXY:-}
      NO_PROXY: ${NO_PROXY:-localhost,127.0.0.1}
    shm_size: "1gb"
```

If TLS handshakes time out only inside Docker while host networking is healthy, test an MTU-specific Compose network in a temporary branch/environment:

```yaml
services:
  reports-ad-bot:
    networks:
      - reports-ad-net

networks:
  reports-ad-net:
    driver: bridge
    driver_opts:
      com.docker.network.driver.mtu: "1450"
```

Use these changes as operational experiments, not default code changes: the correct DNS, proxy, and MTU values depend on the server/VPN/cloud network. After each change, re-run `reports-ad-diagnostics` and a Chromium smoke check from the same running container.

## Catalog and placement behavior

Stage 2 не использует live browser/Playwright для production flow: каталог нужен для выбора рекламной сети/spec, подсказок размеров и контекста OCR/vision LLM. Сайты площадки и landing URL не скачиваются и не проверяются; landing URL используется только как строка, объединенная с UTM и нарисованная в нижней области proof.

Каталог содержит 96 source specs из `creative_specs.json` и 19 рекламных сетей/platform groups. Runtime-фильтр бота показывает в `telegram_menu` только specs, которые можно принять как статичный PNG/JPEG с фиксированными размерами. Если source spec допускает GIF, PSD, ZIP, MP4 или HTML5, эти данные сохранены в catalog metadata, но Telegram runtime MVP их не принимает.

После ввода advertising page URL бот выбирает placement автоматически:

- если host есть в списке `reports_ad/docs/examples/FD -- Примеры скриншотов/площадки.txt` и для него есть слабонаблюдаемый selector из HTML sample, используется selector;
- иначе применяется безопасный dimension-based fallback template по размеру выбранного spec;
- если host неизвестен, используется template по размеру или `in-content` как последний fallback.

Если исходная/current advertising page не открывается на фазе загрузки страницы (`commit`/bounded DOM/body), generator сначала фиксирует отказ по host, затем перебирает predefined fallback-площадки из `reports_ad/reports_ad/data/placement_sites.json`. Список мигрирован из `reports_ad/docs/examples/FD -- Примеры скриншотов/площадки.txt` и разделен по media type: `banner` и `video`. Runtime определяет media type по выбранному spec: source/video/in-stream/MP4 specs используют `video`, а статичные PNG/JPEG/image specs и все неясные случаи MVP используют documented default `banner`. Для каждого hostname строится только безопасный root HTTPS URL вида `https://host/`; пути, query, UTM, пользовательские URL и локальные файлы в JSON не сохраняются.

Площадки, которые в Docker/Chromium logs не загружаются в пределах 30 секунд, сохранены в JSON как disabled metadata и не участвуют в активной fallback-цепочке. Сейчас из `banner` fallback исключены `dzen.ru` и `deti-online.com`; `dzen.ru` также исключен из `video` fallback, а `deti-online.com` помечен disabled для `video`, чтобы не тратить общий timeout на известные commit/TLS/SSO-зависания. Исторические entries не удалены: у каждого disabled host есть `disabled_reason`.

Дополнительных Chromium smoke/checks для landing/target host не добавлено: landing URL по-прежнему используется только в стандартной фазе click/fallback-open текущей генерации.

Fallback применяется только к connectivity/page-download/page-load этапу до вставки креатива. Ошибки selector/template insertion остаются фазой `creative_insert` и не запускают перебор площадок. Если все кандидаты не открылись, пользователь получает русское сообщение о том, что все fallback-площадки недоступны, а в логах остаются только sanitized причины по host.

Статус проверки площадок сохранен в catalog metadata. Проверка выполнялась из workspace через `reports_ad/.venv/bin/python` с HTML-sample запросами. Ограничения: `dzen.ru`, `m.dzen.ru`, `yandex.ru` редиректили на Yandex Passport/SSO; `kniga-online.com` вернул HTTP 403; `m.video.yandex.ru` и `m.images.yandex.ru` дали TLS hostname mismatch; `drug.vokrug` не резолвился DNS. Поэтому selectors с confidence не `observed_sample` являются fallback heuristics, а не verified production selectors.

## Intermediate artifacts in `reports_ad/tmp`

Каждая успешная генерация после `creative_insert` сохраняет дополнительный artifact в project-level `reports_ad/tmp` или, в Docker Compose, в mounted `/app/tmp` → `./tmp`. Это нужно для операторской проверки, что креатив был вставлен до клика и композиции landing page. Artifact не заменяет итоговый screenshot и не отправляется в Telegram пользователю; в Telegram уходит только финальный proof image.

Безопасность имени файла:

- включает только stem исходного имени креатива, host выбранной площадки и UTC timestamp вида `YYYYMMDD-HHMMSS`;
- не включает полный advertising URL, landing URL, UTM, Telegram user/chat IDs, token или локальный путь upload-файла;
- небезопасные символы заменяются дефисами.

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
