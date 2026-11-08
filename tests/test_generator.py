import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_epg as epg


class GeneratorTests(unittest.TestCase):
    def test_channel_list_has_73_channels(self):
        config = epg.read_json(ROOT / "config/channels.json")
        flattened = epg.flatten_config(config)
        self.assertEqual(len(flattened), 73)
        self.assertEqual(len({item["xmltv_id"] for item in flattened}), 73)

    def test_name_normalization(self):
        self.assertEqual(epg.normalize_name("&pictures"), "pictures")
        self.assertEqual(epg.normalize_name("Zee 24 Tass"), "zee24tass")

    def test_catalogue_response_shape(self):
        payload = {"code": 200, "result": [{"channel_id": 153, "channel_name": "ABP Majha", "logoUrl": "https://example.test/logo.png"}]}
        entries = epg.extract_catalogue_entries(payload)
        channel = epg.catalogue_channel_from_dict(entries[0])
        self.assertEqual(channel.channel_id, 153)
        self.assertEqual(channel.name, "ABP Majha")

    def test_alias_match_is_exact_after_normalization(self):
        configured = {
            "name": "Zee 24 Tass",
            "aliases": ["Zee 24 Taas"],
        }
        catalogue = [epg.CatalogueChannel(10, "Zee 24 Taas")]
        matched, note = epg.resolve_channel(configured, catalogue, {})
        self.assertEqual(matched.channel_id, 10)
        self.assertIn("Zee 24 Taas", note)

    def test_ambiguous_match_is_not_guessed(self):
        configured = {"name": "Sony Sab", "aliases": ["Sony SAB"]}
        catalogue = [epg.CatalogueChannel(1, "Sony SAB"), epg.CatalogueChannel(2, "Sony SAB")]
        matched, note = epg.resolve_channel(configured, catalogue, {})
        self.assertIsNone(matched)
        self.assertIn("ambiguous", note)

    def test_programme_parsing_millisecond_epoch(self):
        local_zone = ZoneInfo("Asia/Kolkata")
        item = {
            "startEpoch": 1794120000000,
            "endEpoch": 1794123600000,
            "showname": "Sample Show",
            "description": "Description",
            "showCategory": "Entertainment",
            "episodePoster": "poster.jpg",
        }
        programme = epg.parse_programme(item, local_zone, "https://cdn.test/posters/")
        self.assertIsNotNone(programme)
        self.assertEqual(programme.title, "Sample Show")
        self.assertEqual(programme.category, "Entertainment")
        self.assertEqual(programme.icon, "https://cdn.test/posters/poster.jpg")

    def test_programme_parsing_epoch_seconds(self):
        local_zone = ZoneInfo("Asia/Kolkata")
        item = {"startEpoch": 1794120000, "endEpoch": 1794123600, "showname": "Seconds"}
        programme = epg.parse_programme(item, local_zone, "")
        self.assertIsNotNone(programme)
        self.assertEqual(programme.stop_ms - programme.start_ms, 3_600_000)

    def test_xmltv_uses_india_timezone_and_expected_channel_id(self):
        local_zone = ZoneInfo("Asia/Kolkata")
        programme = epg.Programme(1794120000000, 1794123600000, "Sample Show", "Desc", "News")
        xml_bytes = epg.render_xmltv(
            [{"name": "ABP Majha", "xmltv_id": "abp-majha", "logo": ""}],
            [("abp-majha", programme)],
            local_zone,
        )
        root = ET.fromstring(xml_bytes)
        self.assertEqual(root.find("channel").get("id"), "abp-majha")
        self.assertEqual(root.find("programme").get("channel"), "abp-majha")
        self.assertTrue(root.find("programme").get("start").endswith(" +0530"))
        self.assertEqual(root.findtext("programme/title"), "Sample Show")

    def test_empty_epg_is_valid_but_missing_epg_key_is_not(self):
        self.assertEqual(epg.extract_epg_items({"epg": []}), [])
        with self.assertRaises(RuntimeError):
            epg.extract_epg_items({"message": "no data"})


    def test_generation_writes_xml_and_gzip_feed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config").mkdir()
            (root / "docs").mkdir()
            (root / "reports").mkdir()
            (root / "config" / "channels.json").write_text(json.dumps({
                "categories": [{"name": "News", "channels": [{"name": "Test News", "aliases": []}]}]
            }), encoding="utf-8")
            (root / "config" / "channel_overrides.json").write_text("{}", encoding="utf-8")
            settings = {
                "timezone": "Asia/Kolkata",
                "epg_offsets": [0, 1],
                "request_timeout_seconds": 5,
                "request_retries": 0,
                "worker_count": 1,
                "minimum_fresh_programmes_to_publish": 1,
                "channel_catalog_urls": [],
                "epg_url_templates": [],
                "poster_base_url": "https://cdn.test/posters/",
                "output_xml": "docs/epg.xml",
                "output_gzip": "docs/epg.xml.gz",
                "status_json": "docs/epg-status.json",
                "matching_report_json": "reports/channel-matching.json",
                "unmatched_report_txt": "reports/unmatched-channels.txt"
            }
            (root / "config" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            programme = epg.Programme(1794120000000, 1794123600000, "Test Programme", "A description", "News")
            catalogue = [epg.CatalogueChannel(42, "Test News", "https://cdn.test/logo.png")]
            with mock.patch.object(epg, "ROOT", root), \
                 mock.patch.object(epg, "fetch_catalogue", return_value=catalogue), \
                 mock.patch.object(epg, "fetch_channel_programmes", return_value=[programme]):
                self.assertEqual(epg.run_generation(), 0)
            xml_path = root / "docs" / "epg.xml"
            gzip_path = root / "docs" / "epg.xml.gz"
            self.assertTrue(xml_path.exists())
            self.assertTrue(gzip_path.exists())
            xml_root = ET.parse(xml_path).getroot()
            self.assertEqual(xml_root.find("channel").get("id"), "test-news")
            self.assertEqual(xml_root.findtext("programme/title"), "Test Programme")
            self.assertEqual(gzip.decompress(gzip_path.read_bytes()), xml_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
