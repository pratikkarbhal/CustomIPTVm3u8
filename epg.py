
#!/usr/bin/env python3

import json
import logging
import re
import sys
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# ==========================================
# CHANNEL NAME : JIO CHANNEL ID
# Add IDs as you collect them.
# None means not mapped yet.
# ==========================================

CHANNELS = {
    # Marathi News
    "ABP Majha": None,
    "Saam TV": None,
    "News18 Marathi": None,
    "NDTV Marathi": None,
    "TV9 Marathi": None,
    "Zee 24 Tass": None,
    "Lokshahi News": None,
    "Jai Maharashtra": None,
    "RT India": None,

    # Marathi Movies
    "Zee Talkies": 153,
    "ZEE Chitra Mandir": None,

    # Marathi Entertainment
    "Shemaroo Marathi Bana": None,
    "Fakt Marathi": None,
    "Zee Yuva": None,
    "DD Sahyadri": None,
    "Sun Marathi": None,
    "Zee Marathi": None,
    "Star Pravah": None,
    "Colors Marathi": None,
    "Sony Marathi": None,

    # Marathi Songs
    "9x Jhakaas": None,
    "Maiboli": None,
    "Sangeet Marathi": None,

    # Hindi Entertainment
    "Zee Comedy Nation": None,
    "Zee Horror Nights": None,
    "Sony Pal": None,
    "Sony Wah": None,
    "Sony Sab": None,
    "Sony Entertainment Television": None,
    "Zee South Flix": None,
    "Zee Cine Classic": None,

    # Hindi Movies
    "Shemaroo Bollywood": None,
    "B4U Movies": None,
    "B4U Kadak": None,
    "Colors Cineplex": None,
    "Colors Cineplex Bollywood": None,
    "Colors Cineplex Superhits": None,
    "Sony Max": None,
    "Zee Cinema": None,
    "Star Gold": None,
    "&pictures": None,
    "Goldmines": None,

    # Kids
    "Green Gold TV": None,
    "Cartoon Network": None,
    "Pogo": None,
    "Hungama TV": None,
    "Nickelodeon": None,
    "Disney Channel": None,
    "Sonic": None,
    "Discovery Kids": None,
    "Mr Bean Animated": None,

    # Infotainment
    "Xplore": None,
    "Food Food": None,
    "INWILD": None,
    "Discovery Channel": None,
    "Animal Planet": None,
    "Adventure Earth": None,
    "Sony BBC Earth": None,
    "BBC Earth US": None,
    "BBC Earth": None,
    "History TV18": None,
    "Modern Marvels": None,
    "Discovery Turbo": None,
    "Discovery Science": None,

    # Extras
    "MotorVision": None,
    "PowerNation": None,
    "Star Movies": None,
    "Sony Pix": None,
    "Warner TV": None,
    "HBO": None,
    "Red Bull": None,
    "Watch Mojo": None,
    "InWonder": None,
}

# ==========================================
# SETTINGS
# ==========================================

API_URL = "https://jiotvapi.cdn.jio.com/apis/v1.3/getepg/get"

OFFSETS = [0, 1]

OUTPUT_FILE = Path("epg.xml")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.jiotv.com/"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)


# ==========================================
# FETCH JIO EPG
# ==========================================

def fetch_epg(channel_id, offset):

    params = urllib.parse.urlencode({
        "channel_id": channel_id,
        "offset": offset
    })

    url = f"{API_URL}?{params}"

    request = urllib.request.Request(
        url,
        headers=HEADERS
    )

    for attempt in range(3):

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))

        except Exception as error:

            if attempt == 2:
                raise

            logging.warning(
                "Request failed for ID %s: %s. Retrying...",
                channel_id,
                error
            )

            time.sleep(attempt + 1)


# ==========================================
# TIME CONVERSION
# ==========================================

def convert_time(epoch):

    return datetime.fromtimestamp(
        int(epoch) / 1000,
        tz=timezone.utc
    ).strftime("%Y%m%d%H%M%S +0000")


# ==========================================
# XML HELPER
# ==========================================

def add_text(parent, tag, value):

    if value is not None and str(value).strip():

        ET.SubElement(parent, tag).text = str(value).strip()


# ==========================================
# MAIN GENERATOR
# ==========================================

def main():

    mapped = {
        name: int(channel_id)
        for name, channel_id in CHANNELS.items()
        if channel_id is not None
    }

    logging.info(
        "Loaded %s channels, %s have Jio IDs",
        len(CHANNELS),
        len(mapped)
    )

    if not mapped:
        logging.error("No channel IDs configured.")
        return 1

    tv = ET.Element(
        "tv",
        {"generator-info-name": "Jio EPG"}
    )

    total_programmes = 0
    successful_channels = 0

    for channel_name, channel_id in mapped.items():

        logging.info(
            "Fetching %s (ID: %s)",
            channel_name,
            channel_id
        )

        try:

            programmes = {}

            for offset in OFFSETS:

                data = fetch_epg(channel_id, offset)

                entries = data.get("epg", [])

                for item in entries:

                    start = item.get("startEpoch")
                    end = item.get("endEpoch")
                    title = item.get("showname")

                    if not start or not end or not title:
                        continue

                    key = (int(start), int(end), str(title))

                    programmes[key] = item

            if not programmes:

                logging.warning(
                    "No EPG found for %s",
                    channel_name
                )

                continue

            channel_xml_id = (
                re.sub(
                    r"[^a-z0-9]+",
                    "-",
                    channel_name.lower()
                ).strip("-") + ".jio"
            )

            channel_node = ET.SubElement(
                tv,
                "channel",
                {"id": channel_xml_id}
            )

            add_text(
                channel_node,
                "display-name",
                channel_name
            )

            for (start, end, title), item in sorted(programmes.items()):

                programme = ET.SubElement(
                    tv,
                    "programme",
                    {
                        "start": convert_time(start),
                        "stop": convert_time(end),
                        "channel": channel_xml_id
                    }
                )

                add_text(
                    programme,
                    "title",
                    title
                )

                add_text(
                    programme,
                    "desc",
                    item.get("episode_desc")
                    or item.get("description")
                )

                add_text(
                    programme,
                    "category",
                    item.get("showCategory")
                )

                for genre in item.get("showGenre", []):

                    add_text(
                        programme,
                        "category",
                        genre
                    )

                episode = item.get("episode_num")

                if episode not in (None, -1):
                    add_text(
                        programme,
                        "episode-num",
                        episode
                    )

                total_programmes += 1

            successful_channels += 1

            logging.info(
                "%s: %s programmes fetched",
                channel_name,
                len(programmes)
            )

        except Exception as error:

            logging.error(
                "Failed %s (ID %s): %s",
                channel_name,
                channel_id,
                error
            )

    # Never replace an existing guide with an empty result.

    if total_programmes == 0:

        logging.error(
            "No programmes fetched. Existing EPG preserved."
        )

        return 1

    ET.indent(tv, space="  ")

    temporary_file = OUTPUT_FILE.with_suffix(".xml.tmp")

    ET.ElementTree(tv).write(
        temporary_file,
        encoding="utf-8",
        xml_declaration=True
    )

    temporary_file.replace(OUTPUT_FILE)

    logging.info("--------------------------------")
    logging.info("EPG GENERATION COMPLETED")
    logging.info("Channels: %s", successful_channels)
    logging.info("Programmes: %s", total_programmes)
    logging.info("Output: %s", OUTPUT_FILE)
    logging.info("--------------------------------")

    return 0


if __name__ == "__main__":
    sys.exit(main())
