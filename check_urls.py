import requests

SOURCE_FILE = "MAH-IPTV.m3u"
TIMEOUT = 15

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
}

def is_url_working(url):
    """Check if the URL is reachable and follow redirects to validate final URL."""
    try:
        response = requests.get(url, headers=HEADERS, stream=True, timeout=TIMEOUT, allow_redirects=True)
        final_url = response.url
        status_ok = 200 <= response.status_code < 400
        content_type = response.headers.get("Content-Type", "")

        # Optional: Add logic to detect if it's a streaming resource
        if (".m3u8" in final_url or "mpegurl" in content_type.lower() or "video" in content_type.lower()) and status_ok:
            print(f"✅ Stream detected: {final_url}")
            return True
        elif status_ok:
            print(f"ℹ️ Final URL looks valid (but not clear if it's a stream): {final_url}")
            return True
        else:
            print(f"❌ Not working: {url}")
            return False

    except requests.RequestException as e:
        print(f"❌ Exception for {url}: {e}")
        return False

def check_and_update_m3u():
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
                if last_metadata_line:
                    updated_lines.append(last_metadata_line + "\n")
                updated_lines.append(url + "\n")
            else:
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
