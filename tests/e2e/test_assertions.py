from __future__ import annotations

from .assertions import SENSITIVE_PLACEHOLDER, assert_no_sensitive_diagnostics, redact_sensitive


def test_redact_sensitive_masks_telegram_tokens_ids_file_ids_sessions_and_utm_urls() -> None:
    raw = (
        "bot token 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi "
        "chat_id=-1001234567890 user_id=987654321 file_id=AgACAgIAAxkBAAIC "
        "session=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "
        "url=https://example.com/landing?utm_source=telegram_e2e&utm_medium=test&safe=1"
    )

    redacted = redact_sensitive(raw)

    assert "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi" not in redacted
    assert "-1001234567890" not in redacted
    assert "987654321" not in redacted
    assert "AgACAgIAAxkBAAIC" not in redacted
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in redacted
    assert "utm_source=telegram_e2e" not in redacted
    assert SENSITIVE_PLACEHOLDER in redacted
    assert_no_sensitive_diagnostics(redacted)


def test_assert_no_sensitive_diagnostics_fails_on_known_secret() -> None:
    secret = "known-secret-value"

    try:
        assert_no_sensitive_diagnostics(f"leaked {secret}", secrets=[secret])
    except AssertionError as exc:
        assert "known secret" in str(exc)
    else:  # pragma: no cover - defensive assertion branch.
        raise AssertionError("expected known secret assertion to fail")
