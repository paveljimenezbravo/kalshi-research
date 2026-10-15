"""Reads the recorder's hourly files back into tables.

The files are gzip, written in pieces with a flush every few seconds. A file whose
recorder was killed ends mid-piece, so this reader decompresses piece by piece and, if a
piece is damaged, skips ahead to the next intact one instead of giving up on the file.
"""
import json
import zlib

import pandas as pd

from config import HOME

LIVE_DIR = HOME / "live"
GZIP_MAGIC = b"\x1f\x8b\x08"


def read_lines(path, chunk=1 << 16):
    """Yields every complete JSON line in one recorder file, recovering around damage."""
    data = path.read_bytes()
    pos, text = data.find(GZIP_MAGIC), ""
    while 0 <= pos < len(data):
        d, at, broken = zlib.decompressobj(31), pos, False
        while at < len(data) and not d.eof:
            try:
                text += d.decompress(data[at:at + chunk]).decode("utf-8", "replace")
            except zlib.error:
                broken = True
                break
            at += chunk
            *done, text = text.split("\n")
            yield from done
        if d.eof:  # this piece ended cleanly; the next one starts right after it
            pos = len(data) - len(d.unused_data) if d.unused_data else len(data)
        elif broken:  # skip to the next piece's header
            pos = data.find(GZIP_MAGIC, at - chunk + 1)
        else:  # the file simply ends here (still being written, or cut off)
            break
        text = ""


def load(kinds=None, since=None):
    """Recorded rows as {kind: DataFrame}, optionally only some kinds and only after `since` (unix time)."""
    rows = {}
    for path in sorted(LIVE_DIR.glob("*.jsonl.gz")):
        for line in read_lines(path):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            k = d.pop("k")
            if (kinds is None or k in kinds) and (since is None or d["t"] >= since):
                rows.setdefault(k, []).append(d)
    out = {k: pd.DataFrame(v).sort_values("t", kind="stable").reset_index(drop=True) for k, v in rows.items()}
    if "trade" in out:  # two recorders running at once (e.g. during a restart) record the same trades
        out["trade"] = out["trade"].drop_duplicates(["m", "ts", "p", "q", "side"]).reset_index(drop=True)
    return out
