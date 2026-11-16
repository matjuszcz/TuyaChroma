"""Keep Tuya local keys and login tokens out of log.txt (the owner shares it).

Two layers: the actual secret values the program knows (registered when the
settings are loaded or saved and when Tuya hands out a new token), and
patterns for secrets libraries print that the program doesn't know (e.g. a
negotiated session key, a token in a request URL).
"""

from __future__ import annotations

import logging
import re
import threading

_known: set[str] = set()
_lock = threading.Lock()

_QUOTED = r"(?:b?'(?:[^'\\]|\\.)*'|b?\"(?:[^\"\\]|\\.)*\")"   # a repr: 'x' or b'x' (tinytuya logs %r)
_BARE = r"[^'\",\s})]+"
_NAME = r"(?:local[_ ]?key|localKey|session key|access_?token|accessToken|refresh_?token|refreshToken)"
_SECRET = re.compile(
    r"(/v1\.0/m/token/)[^\s'\"),]+"                                   # token in a request URL
    r"|(" + _NAME + r"['\"]?\s*[:=]\s*)(?:" + _QUOTED + "|" + _BARE + ")"  # key: value, key=value
    r"|(" + _NAME + r"\s+)" + _QUOTED,                                 # "local key b'...' correct?"
    re.I)


def remember(*values: object) -> None:
    """Secrets to mask wherever they show up (short values would mask normal words)."""
    with _lock:
        _known.update(v for v in values if isinstance(v, str) and len(v) >= 8)


def remember_session(session: dict) -> None:
    info = session.get("token_info") if isinstance(session, dict) else None
    if isinstance(info, dict):
        remember(info.get("access_token"), info.get("refresh_token"))


def redact(text: str) -> str:
    with _lock:
        known = sorted(_known, key=len, reverse=True)
    for value in known:
        if value in text:
            text = text.replace(value, "***")
    return _SECRET.sub(lambda m: next(g for g in m.groups() if g) + "***", text)


class RedactSecrets(logging.Filter):
    """Masks Tuya keys and tokens in every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string: leave it to the handler
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, ()
        if record.exc_info and record.exc_info[1] is not None and not record.exc_text:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True
