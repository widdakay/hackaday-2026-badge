"""Per-badge settings stored in /data/settings.json.

    {"name": "Kaveh"}

`name` is drawn on the nametags (lib/name_render.py) and broadcast by the
ranging service, at most 16 bytes. Edit the file on the badge and reset: no
firmware rebuild needed. Without a name there, the name typed into the stock
Nametag app (its set_text("...") lines in /apps/nametag.py) is used, and
failing that badge-XXXXXX from the MAC.
Optional: "initiator_bias_m", see lib/ranging.py.
"""

import json

_PATH = "/data/settings.json"
NAME_MAX_BYTES = 16


def load():
    try:
        with open(_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def nametag_app_name():
    """The name on a stock Hack-a-Day badge: the non-empty strings in the
    Nametag app's set_text("...") calls, joined with spaces. None if unset."""
    try:
        with open("/apps/nametag.py") as f:
            src = f.read()
    except OSError:
        return None
    parts = []
    i = 0
    while True:
        i = src.find("set_text(", i)
        if i < 0:
            break
        i += 9
        q = src[i:i + 1]
        if q not in ("'", '"'):
            continue
        j = src.find(q, i + 1)
        if j < 0:
            break
        text = src[i + 1:j].strip()
        if text:
            parts.append(text)
        i = j + 1
    return " ".join(parts) or None


def badge_name(default):
    name = load().get("name") or nametag_app_name()
    if not name:
        return default
    raw = name.encode()[:NAME_MAX_BYTES]
    # Don't cut a multi-byte character in half.
    while raw:
        try:
            return raw.decode()
        except UnicodeError:
            raw = raw[:-1]
    return default


def default_name():
    """badge-XXXXXX from the last three bytes of the Wi-Fi AP MAC."""
    try:
        import network
        mac = network.WLAN(network.AP_IF).config("mac")
        return "badge-%02X%02X%02X" % (mac[3], mac[4], mac[5])
    except Exception:
        return "badge"
