# reports_ad user-facing system E2E test flow

## Purpose

This document defines a user-perspective system E2E flow for validating the functional behavior and reliability of the [`reports_ad`](../README.md:1) Telegram proof-screenshot bot. The flow verifies the full user journey through Telegram: network selection, spec selection, source screenshot upload, creative upload, landing URL, UTM, review/edit, generation, result delivery, cancellation, validation errors, and reliability under repeated use.

The strategy follows a testing-pyramid approach: keep most assertions in unit and integration tests, then run a small live E2E suite only for user-visible behavior that cannot be proven without Telegram delivery. Per Google-style flakiness practice, live E2E must be opt-in, isolated, timeout-bounded, and quarantined if nondeterministic failures occur.

## System under test

- Runtime: Telegram polling bot built with [`python-telegram-bot`](../pyproject.toml:17) and [`telegram_menu`](../pyproject.toml:18).
- Entry point: [`reports-ad-bot`](../pyproject.toml:28).
- Current conversation states are implemented in [`reports_ad/reports_ad/bot.py`](../reports_ad/bot.py:42).
- Start flow sends the user through network, spec, source screenshot, creative, landing URL, UTM, review, and generation in [`start()`](../reports_ad/bot.py:104).
- Generation is currently screenshot-only/Pillow from uploaded screenshots, as shown in review text [`_review_text()`](../reports_ad/bot.py:492).

## Test data inventory

### Source screenshots from the user examples

Use these files as uploaded source screenshots representing the ad-placement page:

| ID | Source screenshot | Dimensions | Size | Intended scenario |
| --- | --- | ---: | ---: | --- |
| `SRC-YA-BANNER` | [`Яндекс_графический баннер.jpeg`](../../docs/reports/examples/FD%20--%20Примеры%20скриншотов/Яндекс/Яндекс_графический%20баннер.jpeg) | `1280×832` | `121949` bytes | Primary happy path for Yandex graphic banner. |
| `SRC-YA-INSTREAM` | [`Яндекс_Видео_in-stream.jpeg`](../../docs/reports/examples/FD%20--%20Примеры%20скриншотов/Яндекс/Яндекс_Видео_in-stream.jpeg) | `1280×800` | `146708` bytes | Negative/unsupported-media path if static banner spec is selected. |
| `SRC-YA-TGO-1` | [`Яндекс_ТГО_1.png`](../../docs/reports/examples/FD%20--%20Примеры%20скриншотов/Яндекс/Яндекс_ТГО_1.png) | `3420×2214` | `2188418` bytes | Large PNG source screenshot reliability check. |
| `SRC-YA-TGO-2` | [`Яндекс_ТГО_2.png`](../../docs/reports/examples/FD%20--%20Примеры%20скриншотов/Яндекс/Яндекс_ТГО_2.png) | `3420×2214` | `4082018` bytes | Large PNG source screenshot repeatability check. |

### Creative fixtures from the user examples

Use these files as uploaded creatives:

| ID | Creative | Dimensions | Size | Intended scenario |
| --- | --- | ---: | ---: | --- |
| `CR-300-250` | [`300-250.png`](../../docs/reports/examples/FD%20--%20Примеры%20скриншотов/Медийка%20Ферст%20Дата/300-250.png) | `600×500` | `388915` bytes | Primary happy path; current runtime allows stage-2 stretch for selected `300×250` spec. |
| `CR-336-280` | [`336-280.png`](../../docs/reports/examples/FD%20--%20Примеры%20скриншотов/Медийка%20Ферст%20Дата/336-280.png) | `672×560` | `459227` bytes | Alternate static banner creative. |
| `CR-728-90` | [`728-90.png`](../../docs/reports/examples/FD%20--%20Примеры%20скриншотов/Медийка%20Ферст%20Дата/728-90.png) | `1456×180` | `319218` bytes | Wide banner edit-path test. |
| `CR-300-600` | [`300-600.png`](../../docs/reports/examples/FD%20--%20Примеры%20скриншотов/Медийка%20Ферст%20Дата/300-600.png) | `600×1200` | `186519` bytes | Tall banner robustness check. |
| `CR-320-50` | [`320-50.png`](../../docs/reports/examples/FD%20--%20Примеры%20скриншотов/Медийка%20Ферст%20Дата/320-50.png) | `960×150` | `190129` bytes | Mobile-size mismatch/alternate spec check. |

## Environment prerequisites

### Mandatory environment

- A dedicated Telegram test bot token in `REPORTS_AD_TELEGRAM_TOKEN`.
- Exactly one active polling process for the test token; duplicate polling causes Telegram `409 Conflict` as documented in [`README.md`](../README.md:187).
- A dedicated Telegram user account for live E2E driving.
- Kurigram or another Pyrogram-compatible client only for the external user driver, per [`telegram-bot-test-automation`](../../.kilocode/skills/telegram-bot-test-automation/SKILL.md:73).
- Secrets stored outside the repository: Telegram API ID, API hash, user session string, bot username, and bot token.

### Recommended runtime mode

Run one bot instance in an isolated environment with:

- `REPORTS_AD_TELEGRAM_TOKEN` set to the test bot.
- `REPORTS_AD_ALLOW_LOCAL_CREATIVE_PATH=false` for true user-facing E2E through Telegram uploads.
- A dedicated temp directory mounted or configured so cleanup can be inspected after failures.
- Logs captured to a file with token and user/chat ID redaction.

## E2E portfolio and execution cadence

| Layer | Scope | Cadence | Automation target |
| --- | --- | --- | --- |
| `E2E-SMOKE` | One happy path with real Telegram delivery. | Before release and after bot/menu changes. | Automated with Kurigram driver. |
| `E2E-REGRESSION` | Validation errors, edit flow, cancellation, repeated runs. | Nightly or release candidate. | Automated with Kurigram driver. |
| `E2E-RELIABILITY` | Repeatability, timeout handling, lock behavior, cleanup. | Nightly or pre-production. | Automated plus log/artifact audit. |
| `E2E-MANUAL-VISUAL` | Human review of output resemblance to examples. | Release candidate. | Manual review with checklist. |

## Global E2E invariants

Every E2E scenario must verify:

1. The bot responds to each user action within the configured timeout.
2. Reply keyboards appear and disappear at the correct steps.
3. User-visible Russian messages are actionable and contain no traceback, token, raw file path, or internal exception text.
4. Validation failures keep the user in the same step and allow recovery.
5. `/cancel` removes the active keyboard and clears in-memory state.
6. The generated proof is delivered as both photo and document when generation succeeds, matching current behavior tested in [`test_review_launch_menu_label_starts_generation_like_yes()`](../tests/test_validation.py:320).
7. Output image is a valid PNG with non-zero dimensions and expected composition zones.
8. Temporary files are removed after success, cancellation, validation failure, timeout, and unexpected generation failure.

## Scenario E2E-001: Primary happy path with Yandex banner

### Objective

Validate that a real user can create a proof screenshot from the Yandex source screenshot and First Data banner creative.

### Data

- Source screenshot: `SRC-YA-BANNER`.
- Creative: `CR-300-250`.
- Network menu item: Yandex network label containing `yandex_graphic_banners`.
- Spec menu item: label containing `yandex-graphic-banners-300x250-image`.
- Landing URL: `https://example.com/landing`.
- UTM: `utm_source=telegram_e2e&utm_medium=test&utm_campaign=reports_ad_yandex_banner`.

### User flow

1. Send `/start`.
2. Assert the bot explains the proof-screenshot flow and shows network menu text from [`NETWORK_MENU_PROMPT`](../reports_ad/bot.py:66).
3. Select the Yandex network menu button.
4. Assert the bot confirms the network and shows spec menu text from [`SPEC_MENU_PROMPT`](../reports_ad/bot.py:67).
5. Select the `300×250` Yandex graphic banner spec.
6. Assert the bot requests a PNG/JPEG source screenshot, matching [`collect_banner_type()`](../reports_ad/bot.py:185).
7. Upload `SRC-YA-BANNER` as a document.
8. Assert the bot replies with screenshot MIME, dimensions, and byte size from [`collect_screenshot()`](../reports_ad/bot.py:193).
9. Upload `CR-300-250` as a document.
10. Assert the bot accepts the creative and asks for landing URL from [`collect_creative()`](../reports_ad/bot.py:210).
11. Send the landing URL.
12. Assert the bot asks for UTM from [`collect_landing_url()`](../reports_ad/bot.py:227).
13. Send UTM.
14. Assert the review screen contains network, spec, source screenshot, mode, creative, landing, and UTM keys from [`_review_text()`](../reports_ad/bot.py:492).
15. Select `Запустить`.
16. Assert the reply keyboard is removed, a photo is delivered, and a document is delivered.
17. Download the document and verify it opens as PNG.

### Acceptance criteria

- The output document exists and is a readable PNG.
- Width and height are greater than zero.
- The final image contains visible source screenshot content and a lower landing/status area.
- The user receives a success caption equivalent to `Proof-скриншот готов.`.
- No secrets or internal stack traces appear in any Telegram message.

## Scenario E2E-002: Invalid screenshot recovery

### Objective

Verify that invalid source screenshot upload fails safely and the user can recover without restarting.

### Data

- Invalid file: a small generated `.txt` file uploaded as document.
- Recovery source screenshot: `SRC-YA-BANNER`.
- Creative: `CR-300-250`.

### User flow

1. Run steps 1 through 6 from `E2E-001`.
2. Upload the invalid `.txt` file.
3. Assert the bot says `Скриншот не подходит` and asks to send a PNG/JPEG screenshot again, per [`collect_screenshot()`](../reports_ad/bot.py:193).
4. Upload `SRC-YA-BANNER`.
5. Continue the happy path to successful proof generation.

### Acceptance criteria

- The invalid upload does not advance to the creative step.
- Recovery upload advances to the creative step.
- No temporary invalid file remains after the scenario.

## Scenario E2E-003: Invalid creative recovery

### Objective

Verify safe handling of unsupported creative files and recovery in the same conversation.

### Data

- Source screenshot: `SRC-YA-BANNER`.
- Invalid creative: a generated `.gif` or text file.
- Recovery creative: `CR-300-250`.

### User flow

1. Run steps 1 through 8 from `E2E-001`.
2. Upload invalid creative.
3. Assert the bot says `Креатив не подходит` and remains in the creative step, per [`collect_creative()`](../reports_ad/bot.py:210).
4. Upload `CR-300-250`.
5. Continue the happy path to successful proof generation.

### Acceptance criteria

- Invalid creative does not advance to landing URL.
- Recovery creative advances to landing URL.
- The final proof is generated successfully.

## Scenario E2E-004: Invalid landing URL recovery

### Objective

Verify validation of unsafe or malformed landing URLs from a user perspective.

### Data

- Invalid landing URL: `file:///tmp/private.html`.
- Recovery landing URL: `https://example.com/landing`.

### User flow

1. Run steps 1 through 10 from `E2E-001`.
2. Send invalid landing URL.
3. Assert the bot rejects it with a safe validation message from [`collect_landing_url()`](../reports_ad/bot.py:227).
4. Send the recovery landing URL.
5. Continue to UTM and successful proof generation.

### Acceptance criteria

- Unsafe URL schemes are rejected.
- The user remains in the landing URL step after rejection.
- The user can recover without `/start`.

## Scenario E2E-005: Invalid UTM recovery

### Objective

Verify UTM parsing errors and recovery.

### Data

- Invalid UTM: `utm_source=a&utm_source=b`.
- Recovery UTM: `utm_source=telegram_e2e&utm_medium=test&utm_campaign=recovery`.

### User flow

1. Run steps 1 through 12 from `E2E-001`.
2. Send invalid UTM.
3. Assert the bot rejects it and remains in the UTM step, per [`collect_utm()`](../reports_ad/bot.py:242).
4. Send recovery UTM.
5. Assert review screen appears.
6. Generate proof.

### Acceptance criteria

- Duplicate UTM keys are rejected.
- Recovery UTM advances to review.
- Review lists only UTM keys, not sensitive full UTM values when logs/messages should be minimized.

## Scenario E2E-006: Review edit flow

### Objective

Verify that a user can edit submitted data from review and still generate a valid proof.

### Data

- Initial creative: `CR-300-250`.
- Replacement creative: `CR-336-280` or another runtime-supported static image.

### User flow

1. Run steps 1 through 14 from `E2E-001`.
2. Select `Изменить креатив`.
3. Assert the bot asks for creative again and removes inappropriate review keyboard.
4. Upload replacement creative.
5. Assert review screen returns with updated creative dimensions.
6. Select `Запустить`.

### Acceptance criteria

- The review edit action maps to the expected state in [`REVIEW_MENU_ACTIONS`](../reports_ad/bot.py:53).
- The new creative replaces the old creative in review.
- The final proof is generated from the edited request.

## Scenario E2E-007: Cancel at every major step

### Objective

Verify that `/cancel` reliably clears state and removes temporary files from each user-visible step.

### Subflows

Run separate subcases where `/cancel` is sent at:

1. Network selection.
2. Spec selection.
3. Source screenshot upload.
4. Creative upload.
5. Landing URL input.
6. UTM input.
7. Review screen.

### Acceptance criteria

- The bot replies with the cancellation message from [`cancel()`](../reports_ad/bot.py:120).
- Reply keyboard is removed.
- Sending arbitrary follow-up text does not continue the old request.
- A subsequent `/start` begins with a fresh request ID and empty prior state.

## Scenario E2E-008: Large source screenshot reliability

### Objective

Verify reliability with large but valid Yandex PNG screenshots from the provided examples.

### Data

- Source screenshot: `SRC-YA-TGO-1` and `SRC-YA-TGO-2`.
- Creative: `CR-300-250`.

### User flow

1. Run the happy path twice, once with each large source screenshot.
2. Use the same selected network/spec and creative.
3. Generate proof for each run.

### Acceptance criteria

- Both large screenshots are accepted under the configured screenshot limit.
- Generation completes within the E2E timeout budget.
- No memory-related user-visible errors occur.
- Output files are valid PNGs.

## Scenario E2E-009: Per-user lock and duplicate generation

### Objective

Verify that a user cannot start multiple heavy generation jobs concurrently.

### User flow

1. Complete the review screen in one conversation.
2. Send `Запустить` twice quickly or send a second trigger while the first proof generation is still active.
3. Observe responses.

### Acceptance criteria

- Only one proof job is executed for the user.
- The user receives either one success and one safe busy/ignored response, or exactly one success if duplicate input is debounced.
- No duplicated temp artifacts remain.
- No traceback is sent to the user.

## Scenario E2E-010: Restart/reentry robustness

### Objective

Verify that `/start` resets an in-progress conversation and old inputs cannot corrupt a new proof request.

### User flow

1. Start a request and reach creative upload.
2. Send `/start`.
3. Assert network menu appears again.
4. Complete a different valid happy path.

### Acceptance criteria

- Old uploaded screenshot/creative is not reused after `/start`.
- The new review screen only contains the new request data.
- Temporary files from the abandoned request are cleaned.

## Visual validation checklist

For each successful proof, perform human review against the Yandex examples:

- The output is visually recognizable as a proof image, not a blank or corrupted file.
- The creative is visible in the expected source screenshot area.
- The source screenshot still occupies the major upper area.
- The lower area communicates the landing/effective click destination when applicable.
- The output does not crop away the key source page context.
- The output is acceptable for report evidence compared with [`Яндекс_графический баннер.jpeg`](../../docs/reports/examples/FD%20--%20Примеры%20скриншотов/Яндекс/Яндекс_графический%20баннер.jpeg) and [`Яндекс_ТГО_1.png`](../../docs/reports/examples/FD%20--%20Примеры%20скриншотов/Яндекс/Яндекс_ТГО_1.png).

## Automation architecture

### Driver

Use a Kurigram-based external user driver for live Telegram E2E. The driver should:

- Send messages and files to the dedicated bot username.
- Wait for matching responses with bounded timeouts.
- Download resulting documents/photos into an E2E artifact directory.
- Delete test messages where possible.
- Fail with diagnostic context that redacts tokens, session strings, chat IDs, user IDs, file IDs, and full UTM URLs.

### Test harness modules

Recommended file layout for future implementation:

| File | Responsibility |
| --- | --- |
| [`tests/e2e/test_telegram_user_flow.py`](../tests/e2e/test_telegram_user_flow.py) | Pytest scenarios from this document. |
| [`tests/e2e/telegram_driver.py`](../tests/e2e/telegram_driver.py) | Kurigram client wrapper, send/wait/download helpers. |
| [`tests/e2e/fixtures.py`](../tests/e2e/fixtures.py) | Test data paths, timeout config, secret gating. |
| [`tests/e2e/assertions.py`](../tests/e2e/assertions.py) | PNG validation, response matching, redaction assertions. |

### Required markers

Use these pytest markers once implemented:

- `telegram_e2e` for all live Telegram tests.
- `slow` for large screenshot and repeated-run tests.
- `network` for any test that talks to Telegram production APIs.
- `quarantine` for tests with confirmed flakiness until fixed.

## Reliability policy for E2E

Per Google-style flakiness governance, do not normalize flaky E2E failures:

1. Retry live E2E at most once for network-class failures.
2. If a scenario passes after retry, record it as flaky, not clean success.
3. Quarantine tests that fail nondeterministically more than twice in 20 runs.
4. Assign an owner and fix SLA for quarantined tests.
5. Keep quarantined tests out of release-blocking gates until root cause is fixed.
6. Track failure class: Telegram API/network, bot runtime, timeout, fixture, assertion, or product defect.

## Release gate recommendation

A release candidate passes user-facing system E2E when:

- `E2E-001` passes once in a clean environment.
- `E2E-002` through `E2E-007` pass in the regression run.
- `E2E-008` passes or is explicitly waived for performance/environment reasons.
- `E2E-009` and `E2E-010` pass before any change involving concurrency, generation, cleanup, or conversation state.
- Manual visual review approves at least one output created from `SRC-YA-BANNER` and `CR-300-250`.
- No release-blocking scenario is quarantined.
