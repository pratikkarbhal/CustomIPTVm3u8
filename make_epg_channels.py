"""Pick the guide channels that match the channels in playlist.m3u.

Reads the tvg-ids from playlist.m3u, looks them up in the iptv-org Airtel Xstream
channel list, and writes a small channels file (epg_channels.xml) for the grabber.
"""
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

PLAYLIST = "MAH-IPTV.m3u"   # file in this repo, or a full https:// link to a playlist
SITE_FILE = "epg-tool/sites/airtelxstream.in/airtelxstream.in.channels.xml"
OUTPUT = "epg_channels.xml"


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


def main():
    ids = playlist_ids()
    print(f"Playlist has {len(ids)} channels with a tvg-id")

    entries = [
        c for c in ET.parse(SITE_FILE).getroot().findall("channel")
        if (c.get("xmltv_id") or "").strip()
    ]
    by_exact, by_base = {}, {}
    for c in entries:
        by_exact.setdefault(c.get("xmltv_id").lower(), c)
        by_base.setdefault(base(c.get("xmltv_id")), c)

    root = ET.Element("channels")
    used_sites = set()
    for tvg_id in ids:
        c = by_exact.get(tvg_id.lower())
        if c is None:
            c = by_base.get(base(tvg_id))
        if c is None:
            print(f"NO GUIDE  {tvg_id}")
            continue
        if c.get("site_id") in used_sites:
            continue
        used_sites.add(c.get("site_id"))
        # use the playlist's id so the player can match guide and channel
        new = ET.SubElement(root, "channel", {
            "site": c.get("site"),
            "site_id": c.get("site_id"),
            "lang": c.get("lang", "en"),
            "xmltv_id": tvg_id,
        })
        new.text = c.text
        print(f"OK        {tvg_id} -> {c.text}")

    if len(root) == 0:
        print("No channels matched - nothing to download.")
        sys.exit(1)

    ET.indent(root)
    ET.ElementTree(root).write(OUTPUT, encoding="utf-8", xml_declaration=True)
    print(f"Wrote {len(root)} channels to {OUTPUT}")


if __name__ == "__main__":
    main()
