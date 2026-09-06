#!/usr/bin/env python3
"""
Validate HLS (.m3u8) stream URLs from a playlist and split them into
working / dead lists.

Why this is more reliable than a plain curl/requests check:
- Uses ffprobe to actually try to demux the stream, confirming real
  playable media, not just an HTTP 200.
- Spoofs a VLC-like User-Agent (some servers block generic clients).
- Retries transient failures before declaring a stream dead.
- Logs the actual failure reason (timeout, refused, etc.) so you can
  tell "genuinely dead" apart from "blocked because of where this ran".
"""

import subprocess
import concurrent.futures
import argparse
import time

USER_AGENT = "VLC/3.0.20 LibVLC/3.0.20"
TIMEOUT_SECONDS = 20
MAX_RETRIES = 2
RETRY_DELAY = 3
MAX_WORKERS = 6  # keep modest - too many parallel requests can trigger rate limits


def parse_m3u(path):
    """Return list of (extinf_line_or_None, url) preserving order."""
    entries = []
    pending_extinf = None
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line == "#EXTM3U":
                continue
            if line.startswith("#EXTINF"):
                pending_extinf = line
            elif line.startswith("#"):
                continue  # skip other tags for now
            else:
                entries.append((pending_extinf, line))
                pending_extinf = None
    return entries


def check_url(url):
    """
    Returns (ok: bool, reason: str).
    ffprobe actually tries to read stream info, confirming the server
    responds AND returns demuxable media.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-user_agent", USER_AGENT,
        "-rw_timeout", str(TIMEOUT_SECONDS * 1_000_000),  # microseconds
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1",
        url,
    ]
    last_err = "unknown error"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=TIMEOUT_SECONDS + 5,
            )
            if result.returncode == 0:
                return True, "ok"
            stderr_lines = result.stderr.decode(errors="ignore").strip().splitlines()
            last_err = stderr_lines[-1] if stderr_lines else "unknown ffprobe error"
        except subprocess.TimeoutExpired:
            last_err = "timeout"
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
    return False, last_err


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="Path to source .m3u/.m3u8 playlist")
    parser.add_argument("--good", default="working.m3u")
    parser.add_argument("--bad", default="dead.log")
    args = parser.parse_args()

    entries = parse_m3u(args.input)
    print(f"Loaded {len(entries)} entries from {args.input}")

    good_lines = ["#EXTM3U"]
    bad_lines = []
    good_count = 0
    bad_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_to_entry = {pool.submit(check_url, url): (extinf, url) for extinf, url in entries}
        for future in concurrent.futures.as_completed(future_to_entry):
            extinf, url = future_to_entry[future]
            ok, reason = future.result()
            if ok:
                print(f"[OK]   {url}")
                if extinf:
                    good_lines.append(extinf)
                good_lines.append(url)
                good_count += 1
            else:
                print(f"[DEAD] {url}  -> {reason}")
                bad_lines.append(f"{url}  -> {reason}")
                bad_count += 1

    with open(args.good, "w", encoding="utf-8") as f:
        f.write("\n".join(good_lines) + "\n")

    with open(args.bad, "w", encoding="utf-8") as f:
        f.write("\n".join(bad_lines) + "\n")

    print(f"\n{good_count} working streams written to {args.good}")
    print(f"{bad_count} dead streams logged to {args.bad}")


if __name__ == "__main__":
    main()
    
