"""Per-badge settings stored in /data/settings.json.

    {"name": "Kaveh"}

`name` is shown on the nametag (scripts/make_nametag.py reads the same file)
and broadcast by the ranging service, at most 16 bytes.
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
