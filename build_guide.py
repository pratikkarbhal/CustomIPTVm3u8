"""Build guide.xml for the channels in the playlist, using several iptv-org EPG sites.

Each channel is first downloaded from the first site (in SITES order) that lists it.
Channels that come back with no programmes are retried with the next site that lists
them. All results are merged into one guide.xml.
"""
import glob
import os
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET

PLAYLIST = "MAH-IPTV.m3u"   # file in this repo, or a full https:// link to a playlist
SITES = ["dishtv.in", "airtelxstream.in"]   # priority order, first = preferred
TOOL_DIR = "epg-tool"
OUTPUT = "guide.xml"
DAYS = 3
MAX_CONNECTIONS = 5
GRAB_TIMEOUT = 15 * 60      # seconds per download round


def read_playlist():
    if PLAYLIST.startswith("http"):
        req = urllib.request.Request(PLAYLIST, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8", errors="replace").splitlines()
    with open(PLAYLIST, encoding="utf-8") as f:
        return f.read().splitlines()


def playlist_ids():
    ids = []
    for line in read_playlist():
        if line.startswith("#EXTINF"):
            m = re.search(r'tvg-id="([^"]+)"', line)
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
    return ids


def base(xmltv_id):
    return xmltv_id.split("@")[0].lower()


def load_site(site):
    exact, bases = {}, {}
    paths = sorted(glob.glob(f"{TOOL_DIR}/sites/{site}/*.channels.xml"))
    if not paths:
        print(f"WARNING: no channel list found for {site}")
    for path in paths:
        for c in ET.parse(path).getroot().findall("channel"):
            xid = (c.get("xmltv_id") or "").strip()
            if xid:
                exact.setdefault(xid.lower(), c)
                bases.setdefault(base(xid), c)
    return exact, bases


def find_candidates(tvg_id, site_data):
    """One entry per site that lists this channel, in SITES order."""
    out = []
    for site, (exact, bases) in site_data.items():
        c = exact.get(tvg_id.lower())
        if c is None:
            c = bases.get(base(tvg_id))
        if c is not None:
            out.append((site, c))
    return out


def grab(batch, tag):
    """Download guides for batch = [(tvg_id, site, element)]. Returns the guide path."""
    root = ET.Element("channels")
    for tvg_id, _site, c in batch:
        attrs = dict(c.attrib)
        attrs["xmltv_id"] = tvg_id      # use the playlist's id so players can match it
        ET.SubElement(root, "channel", attrs).text = c.text
    ET.indent(root)
    ch_path = os.path.abspath(f"epg_channels_{tag}.xml")
    out_path = os.path.abspath(f"guide_{tag}.xml")
    ET.ElementTree(root).write(ch_path, encoding="utf-8", xml_declaration=True)
    if os.path.exists(out_path):
        os.remove(out_path)
    cmd = ["npm", "run", "grab", "---", f"--channels={ch_path}", f"--output={out_path}",
           f"--days={DAYS}", f"--maxConnections={MAX_CONNECTIONS}"]
    try:
        subprocess.run(cmd, cwd=TOOL_DIR, timeout=GRAB_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("WARNING: download took too long, using whatever was saved")
    return out_path


def programme_channels(path):
    """Ids of channels that have at least one programme in the guide."""
    ids = set()
    if not os.path.exists(path):
        return ids
    try:
        for _, el in ET.iterparse(path):
            if el.tag == "programme":
                ids.add(el.get("channel"))
                el.clear()
    except ET.ParseError as e:
        print(f"WARNING: could not fully read {os.path.basename(path)}: {e}")
    return ids


def merge(resolved):
    """resolved: {tvg_id: (site, guide_path, used_backup)} -> guide.xml"""
    by_path = {}
    for tvg_id, (_site, path, _b) in resolved.items():
        by_path.setdefault(path, set()).add(tvg_id)
    tv = ET.Element("tv", {"generator-info-name": "CustomIPTVm3u8"})
    programmes = []
    for path, wanted in by_path.items():
        for el in ET.parse(path).getroot():
            if el.tag == "channel" and el.get("id") in wanted:
                tv.append(el)
            elif el.tag == "programme" and el.get("channel") in wanted:
                programmes.append(el)
    tv.extend(programmes)
    ET.ElementTree(tv).write(OUTPUT, encoding="utf-8", xml_declaration=True)
    print(f"Wrote {len(resolved)} channels and {len(programmes)} programmes to {OUTPUT}")


def main():
    ids = playlist_ids()
    print(f"Playlist has {len(ids)} channels with a tvg-id")
    site_data = {s: load_site(s) for s in SITES}
    for s, (exact, _) in site_data.items():
        print(f"{s}: {len(exact)} channels with an id")

    pending, attempts = {}, {}
    for tvg_id in ids:
        cands = find_candidates(tvg_id, site_data)
        if cands:
            pending[tvg_id], attempts[tvg_id] = cands, 0
        else:
            print(f"NO GUIDE  {tvg_id} (not on any site)")

    resolved, round_no = {}, 0
    while pending:
        batch, seen = [], set()
        for tvg_id, cands in pending.items():
            site, c = cands[attempts[tvg_id]]
            key = (site, c.get("site_id"))
            if key in seen:             # same site channel as another id - next round
                continue
            seen.add(key)
            batch.append((tvg_id, site, c))
        print(f"\nRound {round_no + 1}: downloading {len(batch)} channels")
        path = grab(batch, round_no)
        got = programme_channels(path)
        for tvg_id, site, _c in batch:
            if tvg_id in got:
                resolved[tvg_id] = (site, path, attempts[tvg_id] > 0)
                del pending[tvg_id]
            else:
                attempts[tvg_id] += 1
                if attempts[tvg_id] >= len(pending[tvg_id]):
                    print(f"NO GUIDE  {tvg_id} (no programmes from any site)")
                    del pending[tvg_id]
                else:
                    print(f"RETRY     {tvg_id} (nothing from {site})")
        round_no += 1

    print()
    for tvg_id, (site, _p, backup) in resolved.items():
        print(f"OK        {tvg_id} ({site})" + (" [backup site]" if backup else ""))

    if not resolved:
        print("No guide data from any site - not writing guide.xml.")
        sys.exit(1)
    merge(resolved)


if __name__ == "__main__":
    main()
  
