"""Secrets never reach log.txt; LAN broadcasts can't redirect lights."""

from __future__ import annotations

import logging
import time

from tuyachroma import main, scan


def _capture(logger_name: str):
    records: list[str] = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = Keep()
    handler.addFilter(main.RedactSecrets())
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    return logger, handler, records


def test_token_renewal_error_does_not_log_the_refresh_token(monkeypatch, caplog) -> None:
    from tuya_sharing import customerapi

    from tuyachroma import cloud

    now_ms = int(time.time() * 1000)
    session = {"user_code": "u", "terminal_id": "t", "endpoint": "https://example.invalid",
               "token_info": {"t": now_ms - 3 * 3600 * 1000, "expire_time": 7200, "uid": "u",
                              "access_token": "a", "refresh_token": "SECRETTOKEN"}}

    def offline(self, path, params=None):
        raise ConnectionError(f"Max retries exceeded with url: {path} (Caused by DNS)")

    monkeypatch.setattr(customerapi.CustomerApi, "get", offline)
    caplog.set_level(logging.INFO)
    assert cloud.keep_session_alive(session) is None
    assert "SECRETTOKEN" not in caplog.text


def test_log_filter_masks_keys_and_tokens() -> None:
    logger, handler, records = _capture("tuyachroma.test_redact")
    try:
        logger.error("net work error = %s", "url: /v1.0/m/token/abcDEF123 (Caused by x)")
        logger.error("response ret = %s", {"local_key": "k3y!", "accessToken": "at1",
                                           "refreshToken": "rt1"})
        logger.error("v3.3 local key %r correct?", "0123456789abcdef")
        try:
            raise OSError("url: /v1.0/m/token/INEXC")
        except OSError:
            logger.exception("failed")
        logger.error("plain message %s", 42)
    finally:
        logger.removeHandler(handler)
    text = "\n".join(records)
    for secret in ("abcDEF123", "k3y!", "at1", "rt1", "0123456789abcdef", "INEXC"):
        assert secret not in text, secret
    assert "plain message 42" in text


def test_broadcast_must_come_from_the_announced_private_ip() -> None:
    info = {"id": "b1", "ip": "192.168.1.20", "version": "3.3"}
    assert scan.trusted(info, "192.168.1.20")
    assert not scan.trusted(info, "192.168.1.66")              # someone else speaking for it
    assert not scan.trusted(dict(info, ip="8.8.8.8"), "8.8.8.8")  # not on the LAN
    assert not scan.trusted(dict(info, ip="nonsense"), "nonsense")
