#!/usr/bin/env python3
"""
Build an XMLTV EPG (guide.xml) for your M3U playlist using Jio's public TV guide API.

Workflow
--------
1. Put your channel names in  playlist_names.txt  (the list you pasted; '#' lines are ignored,
   a leading '!' or '=' is stripped, 'A | B | C' means aliases of one channel).

2. python build_epg.py --discover
      Downloads Jio's channel list, fuzzy-matches your names to Jio channel IDs and writes
      channels.json.  OPEN IT AND REVIEW: low-confidence matches get "jio_id": null with a
      "suggestion" field.  Fix by hand (set jio_id) or leave null to skip that channel.

3. python build_epg.py
      Fetches the guide for every mapped channel and writes guide.xml (+ guide.xml.gz).
      Options: --days 3  (today + next 2 days)   --back 1  (also yesterday)

4. python build_epg.py --patch-m3u my.m3u my_with_ids.m3u
      Adds tvg-id="..." to every #EXTINF line whose channel name matches, so players link
      the playlist entry to the guide.

No third-party packages needed (Python 3.8+).
"""
import argparse
import difflib
import gzip
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

CHANNEL_LIST_URL = (
    "https://jiotv.data.cdn.jio.com/apis/v3.0/getMobileChannelList/get/"
    "?os=android&devicetype=phone&usertype=tvYR7NSNn7rymo3F"
)
# First one is the URL you found in DevTools; second is a fallback host.
EPG_URLS = [
    "https://jiotvapi.cdn.jio.com/apis/v1.3/getepg/get?channel_id={id}&offset={off}",
    "https://jiotv.data.cdn.jio.com/apis/v1.3/getepg/get/?offset={off}&channel_id={id}",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}
NAMES_FILE = Path("playlist_names.txt")
MAP_FILE = Path("channels.json")
MATCH_THRESHOLD = 0.85


# ----------------------------------------------------------------- helpers
def get_json(url, tries=3):
    last = None
    for i in range(tries):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1 + i)
    raise last


def norm(name):
    """Normalise a channel name for matching: lowercase, drop 'hd', keep a-z0-9."""
    n = name.lower().replace("&", "and")
    n = re.sub(r"\bhd\b", "", n)
    return re.sub(r"[^a-z0-9]", "", n)


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower().replace("&", "and")).strip("-")


def parse_names(path):
    """-> list of alias-lists, e.g. [['ABP Majha'], ['Sony BBC Earth', 'BBC Earth US', 'BBC Earth']]"""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^[!=]+", "", line).strip()  # strip your ! and = markers
        aliases = [a.strip() for a in line.split("|") if a.strip()]
        if aliases:
            out.append(aliases)
    return out


def xml_text(s):
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(s or ""))
    return escape(s)


def xmltv_time(epoch_ms):
    return datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")


# ----------------------------------------------------------------- step 1: discover
def discover():
    if not NAMES_FILE.exists():
        sys.exit(f"Create {NAMES_FILE} first (paste your channel list into it).")
    print("Fetching Jio channel list ...")
    jio = get_json(CHANNEL_LIST_URL).get("result", [])
    Path("jio_channels.json").write_text(json.dumps(jio, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  {len(jio)} Jio channels (saved to jio_channels.json for manual lookup)")

    jio_norm = [(norm(c["channel_name"]), c) for c in jio]
    mapping, review = {}, []
    for aliases in parse_names(NAMES_FILE):
        best, best_score = None, 0.0
        for alias in aliases:
            a = norm(alias)
            for jn, c in jio_norm:
                score = 1.0 if a == jn else difflib.SequenceMatcher(None, a, jn).ratio()
                if score > best_score:
                    best, best_score = c, score
        entry = {
            "aliases": aliases,
            "tvg_id": slug(aliases[0]),
            "jio_id": int(best["channel_id"]) if best and best_score >= MATCH_THRESHOLD else None,
            "jio_name": best["channel_name"] if best and best_score >= MATCH_THRESHOLD else None,
            "score": round(best_score, 2),
        }
        if entry["jio_id"] is None:
            entry["suggestion"] = (
                f'{best["channel_name"]} (id {best["channel_id"]})' if best else None
            )
            review.append(aliases[0])
        mapping[aliases[0]] = entry

    MAP_FILE.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for e in mapping.values() if e["jio_id"])
    print(f"\nMatched {ok}/{len(mapping)} channels -> {MAP_FILE}")
    if review:
        print("\nNeed manual review (not on Jio, or name differs):")
        for n in review:
            print(f"  - {n:35s} suggestion: {mapping[n]['suggestion']}")
    print("\nOpen channels.json, fix any wrong/null entries, then run: python build_epg.py")


# ----------------------------------------------------------------- step 2: build
def fetch_day(job):
    name, cid, off = job
    for tpl in EPG_URLS:
        try:
            data = get_json(tpl.format(id=cid, off=off), tries=2)
            return name, data.get("epg", []) or []
        except Exception:  # noqa: BLE001
            continue
    print(f"  ! failed: {name} (id {cid}, offset {off})", file=sys.stderr)
    return name, []


def build(days, back):
    if not MAP_FILE.exists():
        sys.exit("channels.json not found - run with --discover first.")
    mapping = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    active = {n: e for n, e in mapping.items() if e.get("jio_id")}
    offsets = list(range(-back, days))
    jobs = [(n, e["jio_id"], off) for n, e in active.items() for off in offsets]
    print(f"Fetching {len(jobs)} requests ({len(active)} channels x {len(offsets)} days) ...")

    progs = {n: {} for n in active}  # name -> {startEpoch: item}  (dedupes overlaps)
    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, items in pool.map(fetch_day, jobs):
            for it in items:
                if it.get("startEpoch") and it.get("endEpoch"):
                    progs[name][int(it["startEpoch"])] = it

    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="jio-epg-builder">']
    for n, e in active.items():
        out.append(f'<channel id="{xml_text(e["tvg_id"])}"><display-name>{xml_text(n)}</display-name>'
                   + "".join(f"<display-name>{xml_text(a)}</display-name>" for a in e["aliases"][1:])
                   + "</channel>")
    total = 0
    for n, e in active.items():
        for start in sorted(progs[n]):
            it = progs[n][start]
            title = it.get("showname") or "Unknown"
            desc = it.get("episode_desc") or it.get("description") or ""
            out.append(f'<programme start="{xmltv_time(it["startEpoch"])}" '
                       f'stop="{xmltv_time(it["endEpoch"])}" channel="{xml_text(e["tvg_id"])}">')
            out.append(f"<title>{xml_text(title)}</title>")
            if desc and desc != title:
                out.append(f"<desc>{xml_text(desc)}</desc>")
            for g in it.get("showGenre") or []:
                out.append(f"<category>{xml_text(g)}</category>")
            out.append("</programme>")
            total += 1
    out.append("</tv>")
    xml = "\n".join(out)
    Path("guide.xml").write_text(xml, encoding="utf-8")
    with gzip.open("guide.xml.gz", "wb") as f:
        f.write(xml.encode("utf-8"))
    print(f"Wrote guide.xml / guide.xml.gz: {len(active)} channels, {total} programmes")


# ----------------------------------------------------------------- step 3: patch m3u
def patch_m3u(src, dst):
    mapping = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    lookup = {}
    for n, e in mapping.items():
        if e.get("jio_id"):
            for a in e["aliases"]:
                lookup[norm(a)] = e["tvg_id"]
    lines_out, hit, miss = [], 0, []
    for line in Path(src).read_text(encoding="utf-8").splitlines():
        if line.startswith("#EXTINF"):
            name = line.rsplit(",", 1)[-1].strip()
            tid = lookup.get(norm(name))
            if tid:
                line = re.sub(r'\s+tvg-id="[^"]*"', "", line)
                line = re.sub(r"^(#EXTINF:-?\d+(?:\.\d+)?)", rf'\1 tvg-id="{tid}"', line)
                hit += 1
            else:
                miss.append(name)
        lines_out.append(line)
    Path(dst).write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    print(f"Tagged {hit} channels in {dst}")
    if miss:
        print("No guide for:", ", ".join(miss))


# ----------------------------------------------------------------- main
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--discover", action="store_true", help="match your names to Jio channel IDs")
    ap.add_argument("--days", type=int, default=2, help="days to fetch starting today (default 2)")
    ap.add_argument("--back", type=int, default=0, help="also fetch N past days (default 0)")
    ap.add_argument("--patch-m3u", nargs=2, metavar=("IN", "OUT"), help="add tvg-id to an M3U")
    a = ap.parse_args()
    if a.discover:
        discover()
    elif a.patch_m3u:
        patch_m3u(*a.patch_m3u)
    else:
        build(a.days, a.back)
