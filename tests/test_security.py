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


def test_known_keys_are_masked_in_any_form() -> None:
    from tuyachroma import redact

    redact.remember("ab,cd\"ef gh)1234", "0123456789abcdef")
    for line in ("Status request returned an error, is version 3.3 and local key "
                 "b'0123456789abcdef' correct?",
                 "{'local_key': 'ab,cd\"ef gh)1234'}",
                 "session key: b'\\xbc&O?\\x01'"):
        out = redact.redact(line)
        assert "0123456789abcdef" not in out and "ab,cd" not in out and "\\xbc" not in out, out
    for normal in ("Session key negotiation step 2", "Local key for Lampka biurko is missing",
                   "access_token expired, renewing"):
        assert redact.redact(normal) == normal
    assert redact.redact("localKey='zz,yy'") == "localKey=***"


def test_tuya_sharing_console_handler_is_filtered(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    sharing = logging.getLogger("tuya_sharing")
    sharing_filters = sharing.filters[:]
    try:
        root.handlers = []
        main.setup_logging(False)
        logging.getLogger("tuya_sharing").error("net work error = url: /v1.0/m/token/RTSECRET99 x")
        for h in root.handlers:
            h.flush()
    finally:
        for h in root.handlers:
            h.close()
        root.handlers = saved
        root.setLevel(level)
        sharing.filters = sharing_filters
        for name in ("tuya_sharing", "urllib3", "tinytuya"):
            logging.getLogger(name).setLevel(logging.NOTSET)
    assert "RTSECRET99" not in capsys.readouterr().err
    assert "RTSECRET99" not in (tmp_path / "TuyaChroma" / "log.txt").read_text(encoding="utf-8")


def test_undecryptable_settings_are_kept_before_saving(tmp_path, monkeypatch) -> None:
    from tuyachroma import config as config_mod
    from tuyachroma import dpapi

    path = tmp_path / "config.json"
    path.write_text('{"devices": [{"id": "a", "name": "A", "local_key": "dpapi:xx"}]}',
                    encoding="utf-8")

    def broken(value):
        raise OSError("DPAPI not ready")

    monkeypatch.setattr(dpapi, "unprotect", broken)
    cfg = config_mod.load(path)
    assert cfg.devices == [] and cfg.read_errors
    config_mod.save(cfg, path)
    assert "dpapi:xx" in (tmp_path / "config.json.bad").read_text(encoding="utf-8")


def test_escaped_quote_in_unknown_key_is_masked_whole() -> None:
    from tuyachroma import redact

    out = redact.redact('{"localKey": "a\\"bcdefghijklmno"}')
    assert "bcdefghijklmno" not in out, out


def test_device_database_rejects_entries_that_break_detection() -> None:
    import pytest

    from tuyachroma import devices_db

    good = {"version": 3, "rules": [{"category": "keyboard", "keywords": ["huntsman"]}]}
    devices_db.validate(dict(good))
    for bad in ({"keyboard_layouts": {"mini": [1]}}, {"notes": [{"match": "", "note": "x"}]},
                {"version": 10**9}, {"version": True}, {"version": 0},
                {"keyboard_layouts": {"huge": ["x"]}},
                {"rules": [{"category": "keyboard", "keywords": [""]}]},
                {"pids": {"0257": {"category": "toaster"}}}):
        with pytest.raises(devices_db.DatabaseError):
            devices_db.validate({**good, **bad})


def test_malformed_broadcast_is_ignored(monkeypatch) -> None:
    import json

    monkeypatch.setattr(scan.tinytuya, "decrypt_udp", lambda d: d.decode())
    for payload in ({"gwId": ["x"], "ip": "10.0.0.2"}, {"gwId": "b", "ip": "not an ip"},
                    {"gwId": "b", "ip": 5}):
        assert scan.parse_broadcast(json.dumps(payload).encode()) is None


def test_damaged_database_cache_never_stops_the_program(tmp_path, monkeypatch) -> None:
    import json

    import pytest

    from tuyachroma import devices_db

    with pytest.raises(devices_db.DatabaseError):
        devices_db.validate({"version": 3, "rules": [{"category": "keyboard", "keywords": ["x"]}],
                             "notes": None})
    bad = tmp_path / "razer_devices.json"
    bad.write_text(json.dumps({"version": 99, "rules": [{"category": "keyboard",
                                                          "keywords": ["x"]}], "notes": 5}))
    monkeypatch.setattr(devices_db, "cache_path", lambda: bad)
    devices_db.reset()
    try:
        assert devices_db.load()["version"] < 99  # the bundled database instead
    finally:
        devices_db.reset()
