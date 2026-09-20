# PR: Add mask-based banner generation and video-network flows

> **Base branch:** `master`  
> **Compare branch:** `new_masks2`  
> **GitHub compare:** `base: master` ← `compare: new_masks2`

## Summary

This pull request changes the active `reports_ad` Telegram workflow from browser-based proof screenshots to **Pillow-based banner composition with advertising-network masks**.

Users select a network and supported banner size, upload a PNG/JPEG creative, and receive a resized PNG with the matching mask overlaid. The PR introduces standard display-mask flows and dedicated video flows for Yandex, Mediasniper, and GetIntent.

> **Breaking workflow change:** the previous steps for an advertising-page URL, landing URL, UTM parameters, review confirmation, and browser proof generation are no longer part of the active masked-banner conversation flow.

## What changed

### 1. Mask catalog and packaged assets

- Adds the typed mask catalog in [`reports_ad/mask_catalog.py`](../reports_ad/mask_catalog.py) with network-aware size selection and safe validation.
- Adds production mask assets for:
  - Mediasniper: `240×400`, `300×500`, and `300×600`;
  - Hybrid: `240×400`, `300×500`, and `300×600`;
  - Yandex Video: `1920×1080` menu format;
  - Mediasniper Video: two mask variants (`1920_1080_all.png` and `1920_1080_br_only.png`);
  - GetIntent Video: `1920×1080` menu format.
- Adds aliases for Yandex Video, Mediasniper Video, and GetIntent Video so menu values and accepted alternative spellings resolve to stable network slugs.
- Extends package data in [`pyproject.toml`](../pyproject.toml) so mask PNG files ship with the application package.

### 2. New Telegram masked-banner flow

The active conversation in [`reports_ad/bot.py`](../reports_ad/bot.py) now follows this baseline sequence:

1. Select an advertising network.
2. Select a size supported by that network.
3. Upload a PNG or JPEG creative.
4. Generate the masked banner, or complete any network-specific follow-up step.

The bot sends every completed banner as both a Telegram photo and an uncompressed PNG document. Temporary files and in-memory conversation data are removed on completion, cancellation, or failure.

### 3. Video-network behavior

#### Yandex Video

- Adds a dedicated `1920×1080` menu format and Yandex mask resolution.
- Requests a target-site address after creative upload.
- Renders the normalized display domain and target-site favicon in the Yandex video layout.
- Adds a manual favicon-upload fallback if automatic favicon discovery fails.

#### Mediasniper Video

- Adds the `mediasniper_video` network with exactly two mask choices: `1920_1080 наложить все элементы` → `1920_1080_all.png` and `1920_1080 наложить только линию видеоплеера внизу` → `1920_1080_br_only.png`.
- Both choices generate immediately after the creative upload: no target-site step, no favicon fetch/upload, and no domain/CTA/skip/«Реклама» text overlays.
- The result is only `Image.alpha_composite(creative, mask)`.

#### GetIntent Video

- Adds the `getintent_video` network and [`GetIntentVideo/1920x1080.png`](../reports_ad/data/masks/GetIntentVideo/1920x1080.png).
- Uses the flow: **creative → target-site address → minimum age → generation**.
- Validates the minimum age as one or two ASCII digits from `0` through `99`; the rendered label automatically appends `+` (for example, `12` becomes `12+`).
- Renders the age label as centered white bold text in the configured upper-right GetIntent overlay area.
- Does **not** download, request, accept, or render a favicon for GetIntent Video.
- Preserves the normalized target-site URL/display value in the generation request. Rendering the target domain in the lower GetIntent block remains deferred because approved coordinates for that block are not included in this PR.

### 4. Image composition and validation

- Adds the generator and typed request/result contracts in [`reports_ad/masked_banner.py`](../reports_ad/masked_banner.py).
- Resizes user creatives with Pillow, overlays the selected PNG mask, validates final dimensions, and writes a PNG result.
- Adds safe target-site normalization and strict minimum-age validation in [`reports_ad/validation.py`](../reports_ad/validation.py).
- Keeps validation errors suitable for Telegram users rather than exposing internal paths or tracebacks.

### 5. Container font support

[`Dockerfile`](../Dockerfile) installs and registers Arial/Helvetica-compatible font resources, allowing video overlay text to use a predictable bold sans-serif font in the runtime container.

### 6. Documentation and tests

- Updates [`README.md`](../README.md) for the mask-based workflow, supported networks, video-specific steps, and limitations.
- Adds the design and rollout reference [`docs/yandex-video-flow-plan.md`](yandex-video-flow-plan.md).
- Extends unit coverage in [`tests/test_validation.py`](../tests/test_validation.py) and [`tests/test_composition.py`](../tests/test_composition.py) for:
  - mask catalog resolution and network-size menus;
  - PNG mask availability and dimensions;
  - creative resize/mask composition;
  - Yandex target-site/favicon behavior and the Mediasniper Video direct two-mask flow;
  - GetIntent address/age transition, strict age validation, label composition, and favicon exclusion;
  - Telegram retry/file-send behavior.
- Updates the Telegram E2E fixture and happy-path coverage in [`tests/e2e/fixtures.py`](../tests/e2e/fixtures.py) and [`tests/e2e/test_telegram_user_flow.py`](../tests/e2e/test_telegram_user_flow.py).

## Change scope

| Area | Change |
| --- | --- |
| Files changed | 21 |
| Insertions | 2,515 |
| Deletions | 548 |
| New Python modules | [`reports_ad/mask_catalog.py`](../reports_ad/mask_catalog.py), [`reports_ad/masked_banner.py`](../reports_ad/masked_banner.py) |
| New binary assets | 9 PNG mask files |
| Commits included | 9 |

## Included commits

1. `12d1909` — add initial Mediasniper and Hybrid masked-banner support.
2. `2db488f` — refactor Telegram photo/document reply handling with retries.
3. `ead821d` — add Yandex Video network and size support.
4. `55609cd` — add Arial and Helvetica font support.
5. `cf681f4` — add favicon support.
6. `ea836c9` — add Mediasniper Video network and its size.
7. `b5a30ee` — add favicon-upload fallback for video banners.
8. `ae1d8aa` — add the GetIntent Video mask alongside the updated Yandex Video mask asset.
9. `5e5d00c` — add GetIntent Video handling and minimum-age validation.

## Reviewer guide

Review the pull request in this order:

1. **Conversation contract:** verify states and transitions in [`reports_ad/bot.py`](../reports_ad/bot.py), especially that standard networks and Mediasniper Video generate after upload, while Yandex Video and GetIntent Video take their additional inputs.
2. **Mask safety:** verify that [`resolve_mask_asset()`](../reports_ad/mask_catalog.py:175) restricts resolution to declared network/size pairs and returns a safe user error when a file is absent.
3. **Composition:** inspect [`compose_masked_banner()`](../reports_ad/masked_banner.py:158) and the network-specific renderer branches.
4. **GetIntent constraints:** confirm that GetIntent requires a target site and a valid age, but does not invoke the favicon flow.
5. **Container parity:** build the image and verify a bold font is available before approving visual changes.
6. **Regression coverage:** review focused tests for ordinary masks, Yandex Video, Mediasniper Video, and GetIntent Video.

## Validation checklist

- [ ] Install dependencies: `python -m pip install -e ".[dev]"`.
- [ ] Run focused composition and validation tests: `PYTHONPATH=. pytest -c pyproject.toml tests/test_validation.py tests/test_composition.py`.
- [ ] Run the full test suite: `PYTHONPATH=. pytest -c pyproject.toml`.
- [ ] Build the container: `docker compose build reports-ad-bot`.
- [ ] Start the bot service in a safe environment: `docker compose up -d reports-ad-bot`.
- [ ] Verify one ordinary mask, Yandex Video, Mediasniper Video, and GetIntent Video output in Telegram or by a controlled composition smoke test.

## Known limitations and follow-ups

- The GetIntent target-site address is collected, validated, and retained in the request, but is not yet drawn on the lower CTA block. Add the renderer only after approved coordinates are provided.
- The GetIntent mask is stored under the menu filename `1920x1080.png`, while its runtime image-size contract must be checked against the actual packaged asset during review. The PR intentionally validates the decoded mask size before composition.
- The active implementation is a static PNG/JPEG mask compositor; it does not generate or play video media.
- This PR changes the active bot workflow. Teams that still depend on browser proof screenshots, landing-page capture, or UTM-click validation should confirm migration needs before merge.

## GitHub PR body (copy/paste)

```markdown
## Summary

Replaces the active `reports_ad` Telegram workflow with a network-mask banner compositor. Users now select a network and size, upload a PNG/JPEG creative, and receive a resized PNG with the selected mask applied.

Adds Yandex Video, Mediasniper Video, and GetIntent Video flows, including Yandex target-site/favicon handling and GetIntent minimum-age labeling. Mediasniper Video offers two masks and generates directly from the creative.

## Key changes

- Add typed mask catalog and packaged production mask assets.
- Add Pillow masked-banner generator and safe validation.
- Add target-site/domain/favicon overlays for Yandex Video; add two direct Mediasniper Video mask variants.
- Add GetIntent Video flow: target site + validated `0..99` age input rendered as `<age>+`; no favicon flow.
- Add container font dependencies for reliable bold overlay text.
- Update README, implementation plan, unit tests, and Telegram E2E fixture coverage.

## Breaking change

The active bot flow no longer asks for an advertising-page URL, landing URL, UTM parameters, or an explicit review/start confirmation. It now performs direct mask composition after the network-specific inputs are complete.

## Verification

- [ ] `PYTHONPATH=. pytest -c pyproject.toml tests/test_validation.py tests/test_composition.py`
- [ ] `PYTHONPATH=. pytest -c pyproject.toml`
- [ ] `docker compose build reports-ad-bot`
- [ ] Smoke-test ordinary, Yandex Video, Mediasniper Video, and GetIntent Video outputs.

## Follow-up

GetIntent collects and retains the target-site address but intentionally does not draw it yet because lower-block coordinates have not been approved.
```
