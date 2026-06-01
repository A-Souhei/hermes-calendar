"""Reminder delivery via Home Assistant.

Reuses the same config pattern as ha_notify_plugin:
  env HASS_URL / HA_URL  — base URL
  env HASS_TOKEN / HA_TOKEN — long-lived access token
  env HA_NOTIFY_TARGET   — default notify service
  ~/.hermes/ha_notify.json — file overrides (non-empty keys win)

Delivery channels:
  ha_notify — push notification (title + message body)
  ha_speak  — TTS spoken on phone
  chat      — a text from Calypso in the chat (delivered out-of-band: the
              every-minute cron tick prints it to stdout, which the
              `--no-agent` cron posts into the chat; NOT sent via fire())

The stored ``alert_channel`` is a logical value (ha_notify / ha_speak / both /
chat / all / none) that ``resolve_channels`` expands into the concrete set
above.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://100.124.141.114:47821"
_DEFAULT_TARGET = "mobile_app_sm_n975u"


def _load_config() -> dict:
    cfg = {
        "url": os.environ.get("HASS_URL") or os.environ.get("HA_URL") or _DEFAULT_URL,
        "token": os.environ.get("HASS_TOKEN") or os.environ.get("HA_TOKEN") or "",
        "target": os.environ.get("HA_NOTIFY_TARGET", _DEFAULT_TARGET),
    }
    for candidate in [
        os.path.expanduser("~/.hermes/ha_notify.json"),
        "/root/.hermes/ha_notify.json",
    ]:
        try:
            if os.path.exists(candidate):
                with open(candidate) as f:
                    file_cfg = json.load(f)
                cfg.update({k: v for k, v in file_cfg.items() if v})
                break
        except Exception:
            pass
    cfg["url"] = cfg["url"].rstrip("/")
    return cfg


# Logical alert_channel value -> concrete delivery channels. "chat" is handled
# by the scheduler (printed to stdout), the rest go through fire().
_CHANNEL_MAP = {
    "none": [],
    "ha_notify": ["ha_notify"],
    "ha_speak": ["ha_speak"],
    "both": ["ha_notify", "ha_speak"],
    "chat": ["chat"],
    "all": ["ha_notify", "ha_speak", "chat"],
}


def resolve_channels(alert_channel) -> list:
    """Expand a stored alert_channel into concrete delivery channels.

    Accepts a string (the stored form) or a list of strings. Unknown values
    fall back to ha_notify. Order preserved, deduped. "none" -> [].
    """
    if alert_channel is None:
        return ["ha_notify"]
    if isinstance(alert_channel, (list, tuple)):
        out: list = []
        for c in alert_channel:
            for r in resolve_channels(c):
                if r not in out:
                    out.append(r)
        return out
    key = str(alert_channel).strip().lower()
    return list(_CHANNEL_MAP.get(key, ["ha_notify"]))


def available() -> bool:
    """Return True if the token is configured (URL has a hard default)."""
    cfg = _load_config()
    return bool(cfg.get("token"))


def fire(
    channel: str,
    title: str,
    message: str,
    target: Optional[str] = None,
) -> dict:
    """Send a reminder via HA.

    channel: "ha_notify" | "ha_speak" | "none"
    Returns {"ok": bool, "status": int|None, "error": str|None}
    """
    if channel == "none":
        return {"ok": True, "status": None, "error": None}

    cfg = _load_config()
    if not cfg.get("token"):
        return {"ok": False, "status": None, "error": "HA_TOKEN not configured"}

    effective_target = target or cfg["target"]
    url = f"{cfg['url']}/api/services/notify/{effective_target}"

    if channel == "ha_speak":
        payload = {"message": "TTS", "data": {"tts_text": message}}
    else:
        # ha_notify (and any unknown channel falls back here)
        payload = {"title": title, "message": message}

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {cfg['token']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"ok": True, "status": resp.status, "error": None}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        logger.warning("calendar notify HTTP error %s: %s", e.code, body)
        return {"ok": False, "status": e.code, "error": body}
    except Exception as e:
        logger.warning("calendar notify error: %s", e)
        return {"ok": False, "status": None, "error": str(e)}
