"""Per-badge settings stored in /data/settings.json.

    {"name": "Kaveh"}

`name` is drawn on the nametags (lib/name_render.py) and broadcast by the
ranging service, at most 16 bytes. Edit the file on the badge and reset: no
firmware rebuild needed. Without a name, both use badge-XXXXXX from the MAC.
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


def badge_name(default):
    name = load().get("name")
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
