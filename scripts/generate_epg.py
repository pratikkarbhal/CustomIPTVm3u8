#!/usr/bin/env python3
"""Generate an XMLTV EPG using JioTV's public channel/EPG endpoints.

Only Python's standard library is used. The output guide is replaced atomically
and only after at least one fresh programme has been downloaded successfully.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import json
import logging
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger("jio_epg")
USER_AGENT = "okhttp/4.2.2"


@dataclass(frozen=True)
class CatalogueChannel:
    channel_id: int
    name: str
    logo: str = ""


@dataclass(frozen=True)
class Programme:
    start_ms: int
    stop_ms: int
    title: str
    description: str = ""
    category: str = ""
    icon: str = ""


@dataclass
class ChannelResult:
    name: str
    category: str
    xmltv_id: str
    jio_channel_id: int | None
    jio_name: str | None
    status: str
    programmes: list[Programme]
    fresh_programmes: int = 0
    note: str = ""
    logo: str = ""


def read_json(path: Path) -> Any:
    """Read JSON with a filename and actionable parse diagnostics."""
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON in {path.relative_to(ROOT)} at line {exc.lineno}, "
            f"column {exc.colno}: {exc.msg}. Check for missing commas, "
            "trailing commas, comments, or an empty/truncated file."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot read {path.relative_to(ROOT)}: {exc}") from exc


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        Path(temporary_name).replace(path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def normalize_name(value: str) -> str:
    """Normalize names for conservative exact matching (not fuzzy matching)."""
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def flatten_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in config.get("categories", []):
        category = str(group.get("name", "Uncategorized")).strip() or "Uncategorized"
        for raw_channel in group.get("channels", []):
            name = str(raw_channel.get("name", "")).strip()
            if not name:
                continue
            key = normalize_name(name)
            if key in seen:
                LOGGER.warning("Duplicate configured channel ignored: %s", name)
                continue
            seen.add(key)
            flattened.append({
                "name": name,
                "category": category,
                "aliases": [str(alias).strip() for alias in raw_channel.get("aliases", []) if str(alias).strip()],
                "xmltv_id": make_xmltv_id(name),
            })
    return flattened


def make_xmltv_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return slug or "channel"


def fetch_json(url: str, timeout: int, retries: int) -> Any:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Connection": "close",
    }
    request = Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if status < 200 or status >= 300:
                    raise RuntimeError(f"HTTP {status} from {url}")
                body = response.read()
            try:
                return json.loads(body.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"The endpoint did not return valid JSON: {url}: {exc}") from exc
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError) as exc:
            last_error = exc
            if attempt < retries:
                delay = min(2 ** attempt, 8)
                LOGGER.warning("Request failed (%s). Retry in %ss: %s", exc, delay, url)
                time.sleep(delay)
    raise RuntimeError(f"Request failed after {retries + 1} attempt(s): {url}: {last_error}")


def extract_catalogue_entries(payload: Any) -> list[dict[str, Any]]:
    """Accept the known Jio `result` response and a few wrapper variants."""
    candidates: list[Any] = []
    if isinstance(payload, list):
        candidates.append(payload)
    elif isinstance(payload, dict):
        for key in ("result", "channels", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.append(value)
            elif isinstance(value, dict):
                for nested_key in ("result", "channels", "data"):
                    if isinstance(value.get(nested_key), list):
                        candidates.append(value[nested_key])
    for candidate in candidates:
        valid = [item for item in candidate if isinstance(item, dict) and
                 ("channel_id" in item or "id" in item) and
                 ("channel_name" in item or "name" in item)]
        if valid:
            return valid
    raise RuntimeError("Channel catalogue JSON did not contain a recognised channel list.")


def catalogue_channel_from_dict(item: dict[str, Any]) -> CatalogueChannel | None:
    try:
        raw_id = item.get("channel_id", item.get("id"))
        raw_name = item.get("channel_name", item.get("name"))
        channel_id = int(raw_id)
        name = str(raw_name).strip()
        if not name:
            return None
        logo = str(item.get("logoUrl") or item.get("logo_url") or item.get("logo") or "").strip()
        return CatalogueChannel(channel_id=channel_id, name=name, logo=logo)
    except (TypeError, ValueError):
        return None


def fetch_catalogue(settings: dict[str, Any]) -> list[CatalogueChannel]:
    errors: list[str] = []
    for url in settings["channel_catalog_urls"]:
        try:
            payload = fetch_json(url, settings["request_timeout_seconds"], settings["request_retries"])
            raw_entries = extract_catalogue_entries(payload)
            channels = [channel for item in raw_entries if (channel := catalogue_channel_from_dict(item))]
            if channels:
                # Prefer one entry per numeric ID; retain distinct IDs for ambiguity checks.
                unique = {channel.channel_id: channel for channel in channels}
                LOGGER.info("Loaded %d channels from Jio catalogue (%s)", len(unique), url)
                return list(unique.values())
            errors.append(f"{url}: catalogue list was empty")
        except Exception as exc:  # try another known catalogue endpoint
            errors.append(f"{url}: {exc}")
    raise RuntimeError("Could not load the Jio channel catalogue. " + " | ".join(errors))


def load_overrides(path: Path) -> dict[str, int]:
    raw = read_json(path)
    overrides: dict[str, int] = {}
    if not isinstance(raw, dict):
        return overrides
    for name, value in raw.items():
        if str(name).startswith("_"):
            continue
        try:
            overrides[normalize_name(str(name))] = int(value)
        except (TypeError, ValueError):
            LOGGER.warning("Ignoring invalid channel override for %s: %r", name, value)
    return overrides


def resolve_channel(configured: dict[str, Any], catalogue: list[CatalogueChannel],
                    overrides: dict[str, int]) -> tuple[CatalogueChannel | None, str]:
    canonical = configured["name"]
    override_id = overrides.get(normalize_name(canonical))
    if override_id is not None:
        matching = next((channel for channel in catalogue if channel.channel_id == override_id), None)
        if matching:
            return matching, f"manual override: ID {override_id}"
        # An explicit ID can still be used even if the public catalogue did not list it.
        return CatalogueChannel(override_id, canonical), f"manual override: ID {override_id} (not in catalogue)"

    terms = [canonical] + configured.get("aliases", [])
    index: dict[str, list[CatalogueChannel]] = {}
    for item in catalogue:
        index.setdefault(normalize_name(item.name), []).append(item)

    for term in terms:
        key = normalize_name(term)
        hits = index.get(key, [])
        if not hits:
            continue
        by_id = {item.channel_id: item for item in hits}
        if len(by_id) == 1:
            return next(iter(by_id.values())), f"exact name/alias match: {term}"
        return None, f"ambiguous catalogue match for '{term}': " + ", ".join(
            f"{item.name} (ID {item.channel_id})" for item in by_id.values()
        )
    return None, "not found in Jio channel catalogue"


def extract_epg_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise RuntimeError("EPG response was not a JSON object or array.")
    for key in ("epg", "result", "data", "programmes", "programs"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for nested_key in ("epg", "result", "data", "programmes", "programs"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
    # An explicit empty `epg` list is a valid response; missing all known keys is not.
    raise RuntimeError("EPG JSON did not contain a recognised programme list.")


def build_url(base: str, channel_id: int, offset: int) -> str:
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode({'channel_id': channel_id, 'offset': offset})}"


def fetch_epg_payload(channel_id: int, offset: int, settings: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[str] = []
    for base in settings["epg_url_templates"]:
        url = build_url(base, channel_id, offset)
        try:
            payload = fetch_json(url, settings["request_timeout_seconds"], settings["request_retries"])
            return extract_epg_items(payload)
        except Exception as exc:  # try the alternate host
            errors.append(f"{url}: {exc}")
    raise RuntimeError("All EPG endpoints failed for this offset. " + " | ".join(errors))


def parse_epoch_ms(value: Any, local_zone: ZoneInfo) -> int | None:
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
        if numeric < 100_000_000_000:  # seconds -> milliseconds
            numeric *= 1000
        return int(numeric)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=local_zone)
        return int(parsed.timestamp() * 1000)
    except (ValueError, OverflowError):
        return None


def absolute_image_url(value: Any, base_url: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("https://") or text.startswith("http://"):
        return text
    return base_url.rstrip("/") + "/" + text.lstrip("/")


def absolute_logo_url(value: Any) -> str:
    """Only emit catalogue logos that are already absolute URLs."""
    text = str(value or "").strip()
    return text if text.startswith(("https://", "http://")) else ""


def parse_programme(item: dict[str, Any], local_zone: ZoneInfo, poster_base_url: str) -> Programme | None:
    start = parse_epoch_ms(item.get("startEpoch", item.get("start", item.get("start_time"))), local_zone)
    stop = parse_epoch_ms(item.get("endEpoch", item.get("stop", item.get("end", item.get("end_time")))), local_zone)
    title = str(item.get("showname") or item.get("title") or item.get("name") or "").strip()
    if start is None or stop is None or stop <= start or not title:
        return None
    return Programme(
        start_ms=start,
        stop_ms=stop,
        title=title,
        description=str(item.get("description") or item.get("desc") or "").strip(),
        category=str(item.get("showCategory") or item.get("category") or "").strip(),
        icon=absolute_image_url(item.get("episodePoster") or item.get("poster") or item.get("episodeThumbnail"), poster_base_url),
    )


def fetch_channel_programmes(channel_id: int, settings: dict[str, Any], local_zone: ZoneInfo) -> list[Programme]:
    collected: dict[tuple[int, int, str], Programme] = {}
    completed_offsets = 0
    offset_errors: list[str] = []
    for offset in settings["epg_offsets"]:
        try:
            items = fetch_epg_payload(channel_id, int(offset), settings)
            completed_offsets += 1
            for item in items:
                programme = parse_programme(item, local_zone, settings["poster_base_url"])
                if programme:
                    key = (programme.start_ms, programme.stop_ms, programme.title)
                    collected[key] = programme
        except Exception as exc:
            offset_errors.append(f"offset {offset}: {exc}")
    if completed_offsets == 0:
        raise RuntimeError("; ".join(offset_errors) or "No EPG offsets could be fetched")
    if offset_errors:
        LOGGER.warning("Channel ID %s had partial offset errors: %s", channel_id, "; ".join(offset_errors))
    return sorted(collected.values(), key=lambda p: (p.start_ms, p.stop_ms, p.title))


def parse_xmltv_datetime(value: str) -> int | None:
    try:
        parsed = datetime.strptime(value.strip(), "%Y%m%d%H%M%S %z")
        return int(parsed.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def load_previous_programmes(path: Path, now_ms: int) -> dict[str, list[Programme]]:
    """Read the last good XMLTV file so an individual transient API error can reuse it."""
    if not path.exists():
        return {}
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        LOGGER.warning("Could not parse previous EPG; no stale fallback will be used: %s", exc)
        return {}
    previous: dict[str, list[Programme]] = {}
    for element in root.findall("programme"):
        channel_id = element.get("channel", "")
        start = parse_xmltv_datetime(element.get("start", ""))
        stop = parse_xmltv_datetime(element.get("stop", ""))
        title = (element.findtext("title") or "").strip()
        if not channel_id or start is None or stop is None or stop <= now_ms or not title:
            continue
        previous.setdefault(channel_id, []).append(Programme(
            start_ms=start,
            stop_ms=stop,
            title=title,
            description=(element.findtext("desc") or "").strip(),
            category=(element.findtext("category") or "").strip(),
            icon=(element.find("icon").get("src", "") if element.find("icon") is not None else ""),
        ))
    return previous


def xmltv_time(epoch_ms: int, local_zone: ZoneInfo) -> str:
    dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).astimezone(local_zone)
    return dt.strftime("%Y%m%d%H%M%S %z")


def add_text_element(parent: ET.Element, tag: str, value: str, language: str = "en") -> None:
    if not value:
        return
    child = ET.SubElement(parent, tag, {"lang": language})
    child.text = value


def render_xmltv(channels: list[dict[str, Any]], programmes: list[tuple[str, Programme]], local_zone: ZoneInfo) -> bytes:
    root = ET.Element("tv", {"generator-info-name": "JioTV GitHub Actions XMLTV generator"})
    for channel in sorted(channels, key=lambda c: c["xmltv_id"]):
        element = ET.SubElement(root, "channel", {"id": channel["xmltv_id"]})
        display = ET.SubElement(element, "display-name")
        display.text = channel["name"]
        if channel.get("logo"):
            ET.SubElement(element, "icon", {"src": channel["logo"]})
    for channel_id, programme in sorted(programmes, key=lambda row: (row[1].start_ms, row[0], row[1].title)):
        element = ET.SubElement(root, "programme", {
            "channel": channel_id,
            "start": xmltv_time(programme.start_ms, local_zone),
            "stop": xmltv_time(programme.stop_ms, local_zone),
        })
        add_text_element(element, "title", programme.title)
        add_text_element(element, "desc", programme.description)
        add_text_element(element, "category", programme.category)
        if programme.icon:
            ET.SubElement(element, "icon", {"src": programme.icon})
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_reports(configured: list[dict[str, Any]], resolved: dict[str, tuple[CatalogueChannel | None, str]],
                  statuses: dict[str, ChannelResult], settings: dict[str, Any]) -> None:
    records: list[dict[str, Any]] = []
    unmatched_lines = ["Channels not included in the current EPG feed", "=" * 48, ""]
    for item in configured:
        name = item["name"]
        match, match_note = resolved.get(item["xmltv_id"], (None, "not processed"))
        result = statuses.get(item["xmltv_id"])
        row = {
            "name": name,
            "category": item["category"],
            "xmltv_id": item["xmltv_id"],
            "jio_channel_id": match.channel_id if match else None,
            "jio_channel_name": match.name if match else None,
            "match": match_note,
            "status": result.status if result else ("unmatched" if match is None else "not fetched"),
            "programme_count": len(result.programmes) if result else 0,
            "fresh_programme_count": result.fresh_programmes if result else 0,
            "note": result.note if result else "",
        }
        records.append(row)
        if row["status"] not in ("included", "included_stale_fallback"):
            reason = row["note"] or row["match"] or row["status"]
            unmatched_lines.append(f"- [{row['category']}] {name}: {row['status']} — {reason}")
    json_path = ROOT / settings["matching_report_json"]
    write_json_atomic(json_path, records)
    text_path = ROOT / settings["unmatched_report_txt"]
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text("\n".join(unmatched_lines) + "\n", encoding="utf-8")


def run_generation() -> int:
    settings = read_json(ROOT / "config/settings.json")
    configured = flatten_config(read_json(ROOT / "config/channels.json"))
    overrides = load_overrides(ROOT / "config/channel_overrides.json")
    if not configured:
        raise RuntimeError("No channels were found in config/channels.json")

    try:
        local_zone = ZoneInfo(settings.get("timezone", "Asia/Kolkata"))
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Unknown timezone in settings.json: {settings.get('timezone')}") from exc

    LOGGER.info("Loaded %d configured channels across %d categories", len(configured), len(read_json(ROOT / 'config/channels.json').get('categories', [])))
    if settings.get("manual_channel_ids_only", False):
        # Skip the catalogue endpoint entirely. This is useful when Jio's
        # channel-list API rejects cloud-hosted requests (for example HTTP 450).
        catalogue = []
        LOGGER.info("Manual-ID mode enabled; skipping Jio channel catalogue")
    else:
        try:
            catalogue = fetch_catalogue(settings)
        except Exception as exc:
            # Manual IDs allow a fully overridden configuration to work without discovery.
            all_overridden = all(normalize_name(item["name"]) in overrides for item in configured)
            if not all_overridden:
                raise
            LOGGER.warning("Catalogue unavailable; all channels have manual overrides: %s", exc)
            catalogue = []

    resolved: dict[str, tuple[CatalogueChannel | None, str]] = {}
    matched: list[tuple[dict[str, Any], CatalogueChannel, str]] = []
    for item in configured:
        match, note = resolve_channel(item, catalogue, overrides)
        resolved[item["xmltv_id"]] = (match, note)
        if match:
            matched.append((item, match, note))

    now_ms = int(time.time() * 1000)
    previous = load_previous_programmes(ROOT / settings["output_xml"], now_ms)
    results: dict[str, ChannelResult] = {}

    def work(entry: tuple[dict[str, Any], CatalogueChannel, str]) -> tuple[dict[str, Any], CatalogueChannel, str, list[Programme] | Exception]:
        item, jio_channel, match_note = entry
        try:
            programmes = fetch_channel_programmes(jio_channel.channel_id, settings, local_zone)
            return item, jio_channel, match_note, programmes
        except Exception as exc:
            return item, jio_channel, match_note, exc

    LOGGER.info("Resolved %d of %d configured channels; requesting schedules", len(matched), len(configured))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(settings.get("worker_count", 4)))) as executor:
        futures = [executor.submit(work, entry) for entry in matched]
        for future in concurrent.futures.as_completed(futures):
            item, jio_channel, match_note, fetched = future.result()
            channel_key = item["xmltv_id"]
            if isinstance(fetched, Exception):
                stale = previous.get(channel_key, [])
                if stale:
                    results[channel_key] = ChannelResult(
                        name=item["name"], category=item["category"], xmltv_id=channel_key,
                        jio_channel_id=jio_channel.channel_id, jio_name=jio_channel.name,
                        status="included_stale_fallback", programmes=stale,
                        note=f"Jio EPG request failed; reused {len(stale)} future programme(s) from the previous guide. {fetched}",
                        logo=jio_channel.logo,
                    )
                    LOGGER.warning("%s: API failed; retained %d previous programme(s)", item["name"], len(stale))
                else:
                    results[channel_key] = ChannelResult(
                        name=item["name"], category=item["category"], xmltv_id=channel_key,
                        jio_channel_id=jio_channel.channel_id, jio_name=jio_channel.name,
                        status="api_error", programmes=[], note=str(fetched), logo=jio_channel.logo,
                    )
                    LOGGER.error("%s: %s", item["name"], fetched)
                continue

            if fetched:
                results[channel_key] = ChannelResult(
                    name=item["name"], category=item["category"], xmltv_id=channel_key,
                    jio_channel_id=jio_channel.channel_id, jio_name=jio_channel.name,
                    status="included", programmes=fetched, fresh_programmes=len(fetched),
                    note=match_note, logo=jio_channel.logo,
                )
                LOGGER.info("%s (Jio ID %s): %d programmes", item["name"], jio_channel.channel_id, len(fetched))
            else:
                results[channel_key] = ChannelResult(
                    name=item["name"], category=item["category"], xmltv_id=channel_key,
                    jio_channel_id=jio_channel.channel_id, jio_name=jio_channel.name,
                    status="no_epg_data", programmes=[], note="Jio returned a valid response but no usable programmes for the configured offsets.", logo=jio_channel.logo,
                )
                LOGGER.info("%s: matched, but Jio returned no usable programme data", item["name"])

    build_reports(configured, resolved, results, settings)

    fresh_count = sum(result.fresh_programmes for result in results.values())
    minimum_fresh = int(settings.get("minimum_fresh_programmes_to_publish", 1))
    if fresh_count < minimum_fresh:
        raise RuntimeError(
            f"Only {fresh_count} fresh programmes were downloaded (minimum {minimum_fresh}). "
            "The previous docs/epg.xml and docs/epg.xml.gz were left untouched. Check reports/ and the Actions log."
        )

    output_channels: list[dict[str, Any]] = []
    output_programmes: list[tuple[str, Programme]] = []
    for item in configured:
        result = results.get(item["xmltv_id"])
        if not result or not result.programmes:
            continue
        output_channels.append({
            "name": result.name,
            "xmltv_id": result.xmltv_id,
            "category": result.category,
            "logo": absolute_logo_url(result.logo),
        })
        output_programmes.extend((result.xmltv_id, programme) for programme in result.programmes)

    if not output_channels or not output_programmes:
        raise RuntimeError("No channels with programme data are available; output files were not changed.")

    xml_bytes = render_xmltv(output_channels, output_programmes, local_zone)
    xml_path = ROOT / settings["output_xml"]
    gzip_path = ROOT / settings["output_gzip"]
    atomic_write_bytes(xml_path, xml_bytes)

    gzip_path.parent.mkdir(parents=True, exist_ok=True)
    fd, gzip_temp_name = tempfile.mkstemp(prefix=gzip_path.name + ".", suffix=".tmp", dir=gzip_path.parent)
    try:
        with os.fdopen(fd, "wb") as raw_handle:
            with gzip.GzipFile(fileobj=raw_handle, mode="wb", mtime=0) as gzip_handle:
                gzip_handle.write(xml_bytes)
        Path(gzip_temp_name).replace(gzip_path)
    except Exception:
        try:
            os.unlink(gzip_temp_name)
        except OSError:
            pass
        raise

    stale_count = sum(1 for result in results.values() if result.status == "included_stale_fallback")
    status = {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timezone": settings.get("timezone", "Asia/Kolkata"),
        "configured_channels": len(configured),
        "matched_channels": len(matched),
        "published_channels": len(output_channels),
        "programme_count": len(output_programmes),
        "fresh_programme_count": fresh_count,
        "channels_using_previous_data": stale_count,
        "unmatched_or_skipped_channels": len(configured) - len(output_channels),
        "files": ["epg.xml", "epg.xml.gz"],
        "note": "Channels with no usable Jio EPG data are omitted. Per-channel API errors may reuse still-future entries from the last successful guide.",
    }
    write_json_atomic(ROOT / settings["status_json"], status)
    LOGGER.info("Published %d channels and %d programmes to %s", len(output_channels), len(output_programmes), xml_path)
    LOGGER.info("Compressed guide: %s", gzip_path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an XMLTV EPG for the configured channels.")
    parser.add_argument("--validate-config", action="store_true", help="Validate configuration and print configured channel count; do not call Jio APIs.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        if args.validate_config:
            config = read_json(ROOT / "config/channels.json")
            channels = flatten_config(config)
            ids = [item["xmltv_id"] for item in channels]
            if len(ids) != len(set(ids)):
                raise RuntimeError("Duplicate XMLTV IDs detected")
            print(f"Configuration OK: {len(channels)} channels in {len(config.get('categories', []))} categories.")
            return 0
        return run_generation()
    except Exception as exc:
        LOGGER.error("EPG generation failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
