import requests

# File containing M3U links
SOURCE_FILE = "MAH-IPTV.m3u"
TIMEOUT = 15  # slightly increased timeout

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
}

def is_url_working(url):
    """Check if the given URL is reachable using a lightweight GET request."""
    try:
        response = requests.get(url, headers=HEADERS, stream=True, timeout=TIMEOUT, allow_redirects=True)
        return 200 <= response.status_code < 400
    except requests.RequestException:
        return False

def check_and_update_m3u():
    """Check URLs in the M3U file and comment out non-working ones."""
    with open(SOURCE_FILE, "r", encoding="utf-8") as file:
        lines = file.readlines()

    updated_lines = []
    last_metadata_line = None

    for line in lines:
        line = line.strip()

        if line.startswith("#EXTINF"):
            last_metadata_line = line
            continue

        if line.startswith("http://") or line.startswith("https://"):
            url = line
            if is_url_working(url):
                print(f"✅ Working: {url}")
                if last_metadata_line:
                    updated_lines.append(last_metadata_line + "\n")
                updated_lines.append(url + "\n")
            else:
                print(f"❌ Not working: {url}")
                if last_metadata_line:
                    updated_lines.append("## " + last_metadata_line + "\n")
                updated_lines.append("## " + url + "\n")
            last_metadata_line = None
        else:
            updated_lines.append(line + "\n")

    with open(SOURCE_FILE, "w", encoding="utf-8") as file:
        file.writelines(updated_lines)

if __name__ == "__main__":
    check_and_update_m3u()
