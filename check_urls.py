import requests

# Define the file path in the repository
SOURCE_FILE = "MAH-IPTV.m3u"  # Update with the relative path if needed
TIMEOUT = 10  # Timeout for each request in seconds

def is_url_working(url):
    """Check if the given URL is reachable."""
    try:
        response = requests.head(url, timeout=TIMEOUT)
        return response.status_code == 200
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
            # Save the metadata line to pair it with the next URL
            last_metadata_line = line
            continue

        if line.startswith("http://") or line.startswith("https://"):
            # Process the URL line
            url = line.strip()
            if is_url_working(url):
                print(f"Working: {url}")
                # Add metadata and URL if working
                if last_metadata_line:
                    updated_lines.append(last_metadata_line + "\n")
                updated_lines.append(line + "\n")
            else:
                print(f"Not working: {url}")
                # Comment out metadata and URL if not working
                if last_metadata_line:
                    updated_lines.append(f"## {last_metadata_line}\n")
                updated_lines.append(f"## {line}\n")
            # Clear the last metadata line
            last_metadata_line = None
        else:
            # Copy any non-metadata or non-URL lines as is (e.g., #EXTM3U)
            updated_lines.append(line + "\n")

    with open(SOURCE_FILE, "w", encoding="utf-8") as file:
        file.writelines(updated_lines)

if __name__ == "__main__":
    check_and_update_m3u()
