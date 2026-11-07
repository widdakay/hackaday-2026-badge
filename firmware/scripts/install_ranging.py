#!/usr/bin/env python3
"""Add the Ranging app to a badge running the stock Hack-a-Day firmware,
keeping everything else on it: apps, nametag, gallery, data.

    pip install esptool mpremote
    python install_ranging.py            # badge plugged in over USB

What it does:

1. Backs up the badge's code and settings (.py, .json, .csv, .txt) to
   badge-backup-<time>/ here; --full-backup copies images too (slow: the
   serial link moves ~3 KB/s, ~15 min for a stock badge).
2. Reads the badge's partition table and stops unless it is the stock layout
   (3 MB app slot at 0x10000, filesystem at 0x310000), so the filesystem
   can't be touched.
3. Writes only the MicroPython app image (micropython.bin) at 0x10000: same
   firmware plus the `ftm` (Wi-Fi time-of-flight ranging) and `graphlayout`
   (map solver) modules. Bootloader, partition table, NVS and filesystem are
   left as they are.
4. Copies in only the Ranging app's own files (FILES below). None of them
   exist on a stock badge; no stock file is replaced.

Your name: the script asks for the name Ranging broadcasts and shows
(Enter keeps the one it found) and saves it in /data/settings.json. Without
that file Ranging uses the name typed into the stock Nametag app
(apps/nametag.py). --name NAME skips the question. The Nametag app itself
is not changed.

Inputs: by default the latest GitHub release of RELEASE_REPO
(micropython.bin and badge-files.zip, checked against its SHA256SUMS.txt).
It is used only if its Ranging supports stock badges; otherwise, or offline,
the files next to this script are used (a release folder, or the repo's
firmware/scripts/). To choose:
  --local           skip GitHub, use the files next to the script
  --firmware PATH   micropython.bin
  --files PATH      badge-files.zip, or a badge/ directory
Other options: --port, --no-flash (files only), --no-backup, --full-backup,
--name NAME.

Undo: flash the stock micropython.bin from the Hack-a-Day repo
(firmware/bin/) at 0x10000 the same way, and delete the files listed below.
"""

import argparse
import glob
import hashlib
import json
import re
import os
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

# The Ranging app and what it needs that a stock badge doesn't have.
FILES = [
    "apps/ranging.py",
    "lib/ranging.py",
    "lib/firefly.py",
    "lib/graph_layout.py",
    "lib/log.py",
    "lib/name_render.py",
    "lib/settings.py",
    "data/fonts/audiowide_120.bbf",
    "data/graphics/nametag_small_blank.png",
]

RELEASE_REPO = "widdakay/hackaday-2026-badge"
RELEASE_ASSETS = ("micropython.bin", "badge-files.zip", "SHA256SUMS.txt")
# In apps/ranging.py only from the release that runs on stock badge files.
STOCK_MARKER = b"STOCK_FRAME_MS"

NAME_MAX_BYTES = 16          # lib/settings.py
APP_OFFSET = 0x10000
PT_OFFSET = 0x8000
PT_SIZE = 0xC00
# Stock layout (Hack-a-Day firmware/bin/partition-table.bin), name -> (offset, size)
STOCK_PARTITIONS = {
    "factory": (0x10000, 0x300000),
    "vfs": (0x310000, 0x4F0000),
}

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def die(msg):
    print("\nERROR: " + msg)
    sys.exit(1)


def ask(msg):
    input(msg + "  [Enter when done] ")


# ---- inputs ---------------------------------------------------------------

class ReleaseError(Exception):
    pass


def _get(url, timeout=60):
    # github.com release links, not the API: no rate limit, no token.
    req = urllib.request.Request(url, headers={"User-Agent": "install-ranging"})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_latest(dest):
    """Download the latest release's assets into dest. Returns the tag."""
    try:
        with _get("https://github.com/%s/releases/latest" % RELEASE_REPO, 20) as r:
            final = r.geturl()
    except OSError as ex:
        raise ReleaseError("can't reach GitHub: %s" % ex)
    if "/releases/tag/" not in final:
        raise ReleaseError("no published release")
    tag = final.rsplit("/releases/tag/", 1)[1]
    print("Latest release: %s (%s)" % (tag, final))
    for name in RELEASE_ASSETS:
        url = "https://github.com/%s/releases/download/%s/%s" % (RELEASE_REPO, tag, name)
        try:
            with _get(url) as r:
                data = r.read()
        except OSError as ex:
            raise ReleaseError("%s: %s" % (name, ex))
        with open(os.path.join(dest, name), "wb") as fh:
            fh.write(data)
        print("  downloaded %s (%d KB)" % (name, len(data) // 1024))
    sums = {}
    with open(os.path.join(dest, "SHA256SUMS.txt")) as fh:
        for line in fh:
            parts = line.split()
            if len(parts) == 2:
                sums[parts[1].lstrip("*")] = parts[0].lower()
    for name in RELEASE_ASSETS[:2]:
        with open(os.path.join(dest, name), "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        if sums.get(name) != digest:
            raise ReleaseError("%s doesn't match SHA256SUMS.txt" % name)
    with zipfile.ZipFile(os.path.join(dest, "badge-files.zip")) as z:
        app = next((n for n in z.namelist() if n.endswith("apps/ranging.py")), None)
        if app is None or STOCK_MARKER not in z.read(app):
            raise ReleaseError("its Ranging app predates stock-badge support")
    print("  checksums match")
    return tag


def find_firmware(arg):
    cands = [arg] if arg else [
        os.path.join(HERE, "micropython.bin"),
        os.path.join(HERE, "..", "lv_micropython", "ports", "esp32",
                     "build-ESP32_GENERIC_S3-BILLBOARD_BADGE", "micropython.bin"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    die("micropython.bin not found; pass --firmware PATH")


def load_files(arg):
    """{badge path: bytes} for FILES, from a zip or a badge/ directory."""
    cands = [arg] if arg else [os.path.join(HERE, "badge-files.zip"),
                               os.path.join(HERE, "..", "badge")]
    src = next((c for c in cands if c and os.path.exists(c)), None)
    if src is None:
        die("badge files not found; pass --files badge-files.zip (or a badge/ dir)")
    out = {}
    if os.path.isdir(src):
        for f in FILES:
            p = os.path.join(src, f)
            if not os.path.isfile(p):
                die("missing %s in %s" % (f, src))
            with open(p, "rb") as fh:
                out[f] = fh.read()
    else:
        with zipfile.ZipFile(src) as z:
            names = z.namelist()
            for f in FILES:
                hit = next((n for n in names if n == f or n.endswith("/" + f)), None)
                if hit is None:
                    die("missing %s in %s" % (f, src))
                out[f] = z.read(hit)
    print("App files from %s" % os.path.abspath(src))
    return out


# ---- badge ----------------------------------------------------------------

def find_port(arg):
    if arg:
        return arg
    try:
        from serial.tools import list_ports
        ports = [p.device for p in list_ports.comports()
                 if (p.vid == 0x303A) or "usbmodem" in p.device]
    except ImportError:
        ports = glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*")
    if len(ports) == 1:
        return ports[0]
    if not ports:
        die("no badge found on USB. Plug it in (and turn it on), or pass --port")
    die("several serial ports %s; pass --port" % ports)


def wait_port(port, timeout_s=30):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if os.name == "nt" or os.path.exists(port):
            return True
        time.sleep(0.5)
    return False


def mpremote(port, *args, check=True, capture=False):
    cmd = [PY, "-m", "mpremote", "connect", port] + list(args)
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        if capture:
            print(r.stdout, r.stderr)
        die("mpremote failed: %s" % " ".join(args))
    return r


def badge_exec(port, code, retries=1):
    for i in range(retries):
        r = mpremote(port, "exec", code, check=False, capture=True)
        if r.returncode == 0:
            return r.stdout
        time.sleep(2)
    return None


def esptool(port, *args, before="default_reset", after="no_reset"):
    cmd = [PY, "-m", "esptool", "--chip", "esp32s3", "-p", port, "-b", "460800",
           "--before", before, "--after", after] + list(args)
    return subprocess.run(cmd).returncode == 0


def parse_partitions(blob):
    parts = {}
    for i in range(0, len(blob) - 31, 32):
        e = blob[i:i + 32]
        if e[:2] != b"\xaa\x50":
            break
        _, _, off, size = struct.unpack("<BBII", e[2:12])
        parts[e[12:28].rstrip(b"\0").decode(errors="replace")] = (off, size)
    return parts


BACKUP_EXT = ("py", "json", "csv", "txt")


def backup(port, full=False):
    dest = os.path.abspath("badge-backup-%s" % time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(dest)
    if full:
        print("\nBacking up all the badge's files to %s (slow) ..." % dest)
        mpremote(port, "cp", "-r", ":", dest + os.sep)
        return dest
    print("\nBacking up the badge's code and settings to %s ..." % dest)
    code = ("import os\ndef w(d):\n for n,t,*_ in os.ilistdir(d):\n  p=d.rstrip('/')+'/'+n\n"
            "  if t==0x4000: w(p)\n  elif n.rsplit('.',1)[-1] in %r: print(p)\nw('/')\n") % (BACKUP_EXT,)
    listing = badge_exec(port, code, retries=3) or ""
    for path in listing.split():
        local = os.path.join(dest, path.lstrip("/"))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        mpremote(port, "cp", ":" + path, local, capture=True)
    print("  %d files" % len(listing.split()))
    return dest


def flash(port, firmware):
    size = os.path.getsize(firmware)
    with tempfile.TemporaryDirectory() as tmp:
        pt = os.path.join(tmp, "pt.bin")
        print("\nReading the badge's partition table ...")
        if not esptool(port, "read_flash", hex(PT_OFFSET), hex(PT_SIZE), pt):
            print("\nCouldn't put the badge in download mode by itself.")
            ask("Unplug USB, hold BOOT, plug USB back in, then release BOOT.")
            if not wait_port(port):
                die("badge didn't come back on %s" % port)
            if not esptool(port, "read_flash", hex(PT_OFFSET), hex(PT_SIZE), pt, before="no_reset"):
                die("still can't reach the bootloader")
        with open(pt, "rb") as fh:
            parts = parse_partitions(fh.read())
    for name, want in STOCK_PARTITIONS.items():
        if parts.get(name) != want:
            die("partition table isn't the stock layout (%s: %s, expected %s). Not "
                "flashing, so your files stay safe." % (name, parts.get(name), want))
    if size > STOCK_PARTITIONS["factory"][1]:
        die("micropython.bin (%d bytes) doesn't fit the app slot" % size)
    print("Partition table is the stock layout; filesystem at 0x310000 won't be touched.")
    print("\nWriting micropython.bin (%d KB) at 0x%x ..." % (size // 1024, APP_OFFSET))
    if not esptool(port, "write_flash", "--flash_mode", "keep", "--flash_size", "keep",
                   hex(APP_OFFSET), firmware, before="no_reset", after="hard_reset"):
        die("flashing failed. The badge is still in download mode: run this "
            "script again, or flash the stock micropython.bin to get it back.")


def wait_repl(port):
    """Wait for MicroPython with the ftm module. True if it's there."""
    for attempt in range(2):
        time.sleep(3)
        if wait_port(port, 20):
            out = badge_exec(port, "import ftm, graphlayout; print('FW_OK')", retries=5)
            if out and "FW_OK" in out:
                return True
            if out is not None:
                die("the badge booted but the new modules are missing; flash didn't take")
        if attempt == 0:
            ask("If the screen didn't change, press RESET on the badge.")
    return False


def current_name(port):
    """The name Ranging would use now: /data/settings.json, else the strings
    in the stock Nametag app's set_text("...") calls (as lib/settings.py)."""
    out = badge_exec(port, "import json\ntry: print(json.load(open('/data/settings.json')).get('name') or '')\n"
                     "except Exception: print('')") or ""
    if out.strip():
        return out.strip()
    src = badge_exec(port, "print(open('/apps/nametag.py').read())") or ""
    parts = [t.strip() for t in re.findall(r"""set_text\(\s*["']([^"']*)["']""", src) if t.strip()]
    return " ".join(parts)


def ask_name(port):
    found = current_name(port)
    while True:
        prompt = "\nYour name for Ranging (max %d bytes)%s: " % (
            NAME_MAX_BYTES, " [%s]" % found if found else "")
        try:
            name = input(prompt).strip()
        except EOFError:          # no terminal: keep what was found
            if not found:
                die("no name on the badge and no terminal to ask; pass --name")
            return found
        name = name or found
        if not name:
            print("No name found on the badge; type one (Ranging would show badge-XXXXXX).")
            continue
        if len(name.encode()) > NAME_MAX_BYTES:
            print("Too long: %d bytes, the limit is %d." % (len(name.encode()), NAME_MAX_BYTES))
            continue
        return name


def install_files(port, files, name):
    print("\nCopying the Ranging app ...")
    with tempfile.TemporaryDirectory() as tmp:
        dirs = sorted({os.path.dirname(f) for f in FILES})
        # Make each directory level that's missing.
        code = ("import os\nfor d in %r:\n p=''\n for part in d.split('/'):\n"
                "  p+='/'+part\n  try: os.mkdir(p)\n  except OSError: pass\n") % dirs
        if badge_exec(port, code, retries=3) is None:
            die("couldn't reach the badge's REPL")
        for f, data in files.items():
            local = os.path.join(tmp, f.replace("/", "_"))
            with open(local, "wb") as fh:
                fh.write(data)
            print("  /" + f)
            mpremote(port, "cp", local, ":/" + f)
        if name:
            local = os.path.join(tmp, "settings.json")
            existing = badge_exec(port, "print(open('/data/settings.json').read())") or ""
            try:
                cfg = json.loads(existing.strip() or "{}")
            except ValueError:
                cfg = {}
            cfg["name"] = name
            with open(local, "w") as fh:
                json.dump(cfg, fh)
            print("  /data/settings.json (name: %s)" % name)
            mpremote(port, "cp", local, ":/data/settings.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port")
    ap.add_argument("--firmware")
    ap.add_argument("--files")
    ap.add_argument("--local", action="store_true", help="don't download the latest release")
    ap.add_argument("--no-flash", action="store_true", help="only copy the app files")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--full-backup", action="store_true", help="back up images too (slow)")
    ap.add_argument("--name", help="name for Ranging (writes /data/settings.json)")
    args = ap.parse_args()

    for mod in ("esptool", "mpremote"):
        try:
            __import__(mod)
        except ImportError:
            die("%s isn't installed: %s -m pip install esptool mpremote" % (mod, PY))

    fw_arg, files_arg = args.firmware, args.files
    if not (args.local or fw_arg or files_arg):
        dest = tempfile.mkdtemp(prefix="ranging-release-")
        try:
            fetch_latest(dest)
            fw_arg = os.path.join(dest, "micropython.bin")
            files_arg = os.path.join(dest, "badge-files.zip")
        except ReleaseError as ex:
            print("Not using the latest release (%s); using the files next to "
                  "this script." % ex)
    firmware = None if args.no_flash else find_firmware(fw_arg)
    files = load_files(files_arg)
    if STOCK_MARKER not in files["apps/ranging.py"]:
        die("this Ranging app predates stock-badge support; use a newer release")
    port = find_port(args.port)
    print("Badge on %s" % port)

    out = badge_exec(port, "import os; print('NT' if 'nametag.py' in os.listdir('/apps') else 'NONT')", retries=3)
    if out is None:
        die("couldn't talk to MicroPython on %s. Is it the badge, and is nothing "
            "else (Thonny, a serial monitor) using the port?" % port)

    name = args.name or ask_name(port)

    if not args.no_backup:
        backup(port, args.full_backup)
    if firmware:
        flash(port, firmware)
        if not wait_repl(port):
            die("the badge didn't come back. Press RESET and run with --no-flash "
                "to finish copying the files.")
        out = badge_exec(port, "import os; print(len(os.listdir('/apps')))", retries=3)
        print("Firmware updated; filesystem still there (%s files in /apps)." % (out or "?").strip())
    install_files(port, files, name)
    print("\nRestarting the badge ...")
    mpremote(port, "reset", check=False)
    print("\nDone. Pick \"Ranging\" from the badge's app menu.")


if __name__ == "__main__":
    main()
