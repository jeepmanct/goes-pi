#!/usr/bin/env python3
"""Back up the small files that are NOT in git (passwords, mail settings, share links, histories, state markers) to
~/goes/data/backup/ on the big drive, as state-YYYYMMDD-HHMM.tar.gz (owner-only), keeping the newest KEEP.

The drive is a different device from the SD card, so this survives an SD failure; it does not survive theft, fire
or a power surge, because the drive stays plugged into the Pi. Code and settings tracked in git are on GitHub.
The panel's watch thread runs this weekly; by hand:  backup_state.py [--dest DIR] [--keep N]

To restore on a rebuilt Pi: clone the repo, then  tar -xzf state-....tar.gz -C /  (paths inside are absolute from
the home directory, relative to /), and re-add the crontab from crontab.txt inside the archive.
"""
import argparse
import datetime as dt
import io
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HOME = Path.home()
GOES = HOME / "goes"
DEST = GOES / "data" / "backup"
KEEP = 4
FILES = [          # relative to ~/goes; missing ones are skipped
    "webui/password.txt", "mail-password.txt", "mail.conf", "share-token.txt", "share-tokens.json", "share-visits.json",
    "signal-history.jsonl", "pi-history.jsonl", "metar-kbdr.jsonl", "keep-offices.txt", "storm.json",
    ".digest-sent", ".digest-failed", ".digest-skipnote", ".digest-weekly-sent", ".alerts-seen",
    ".storm-announced", ".health-sent", ".bestimage-sent", ".skywarn-state.json", "notify-settings.json", "notify-log.jsonl", "disk-history.jsonl", "goesrecv.conf", "goesproc.conf",
]
DIRS = ["webui/tls"]                      # TLS certificates, if any
EXTRA = ["/etc/systemd/system/goes-webui.service"]
MIN_BYTES = 1000


def _add_text(tar, name, text):
    data = text.encode()
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = len(data), time.time(), 0o600
    tar.addfile(info, io.BytesIO(data))


def make(dest=DEST, keep=KEEP):
    """Write one archive and prune old ones. Returns (path, number of files)."""
    old_umask = os.umask(0o077)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        dest.chmod(0o700)
        out = dest / f"state-{dt.datetime.now():%Y%m%d-%H%M}.tar.gz"
        tmp = out.with_name(out.name + ".part")
        n = 0
        with tarfile.open(tmp, "w:gz") as tar:
            paths = [GOES / f for f in FILES] + [Path(p) for p in EXTRA]
            for d in DIRS:
                paths += [p for p in (GOES / d).rglob("*") if p.is_file()] if (GOES / d).is_dir() else []
            for p in paths:
                if p.is_file():
                    tar.add(p, arcname=str(p).lstrip("/"), recursive=False)
                    n += 1
            cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            if cron.returncode == 0:
                _add_text(tar, "crontab.txt", cron.stdout)
            _add_text(tar, "RESTORE.txt", __doc__)
        if tmp.stat().st_size < MIN_BYTES or n == 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("backup came out empty")
        tmp.chmod(0o600)
        tmp.replace(out)
        for old in sorted(dest.glob("state-*.tar.gz"))[:-keep]:
            old.unlink()
        return out, n
    finally:
        os.umask(old_umask)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", type=Path, default=DEST)
    ap.add_argument("--keep", type=int, default=KEEP)
    a = ap.parse_args()
    out, n = make(a.dest, max(1, a.keep))
    print(f"wrote {out} ({n} files, {out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
