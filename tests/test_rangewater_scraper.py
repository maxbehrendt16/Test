"""
Offline validation of rangewater_scraper's parsing logic against hand-built
HTML fixtures. This exists because the dev environment has no network
access to the real liverangewater.com / property sites, so this is how the
extraction logic (JSON-LD parsing, regex field extraction, dedupe, resumable
CSV I/O, LLM-fallback wiring) was verified before handing the script off.

Run: python3 tests/test_rangewater_scraper.py
"""

import csv
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rangewater_scraper as rw


FIND_YOUR_HOME_HTML = """
<html><body>
<h2>Tennessee</h2>
<a href="/city/nashville-tn">Nashville</a>
<a href="/city/knoxville-tn">Knoxville</a>
<h2>North Carolina</h2>
<a href="/city/charlotte-nc">Charlotte</a>
<script>var hiddenMarkets = ["/city/atlanta-ga"];</script>
</body></html>
"""

CITY_PAGE_HTML = """
<html><body>
<div class="card">
  <a href="https://sterlingnashvillewest.com" title="Sterling Nashville West">
    <img src="thumb.jpg" alt="Sterling Nashville West">
  </a>
  <p>Nashville, TN | (615) 555-0123 | Multifamily</p>
</div>
<div class="card">
  <a href="/property/rangewater-riverside" title="RangeWater Riverside">
    <img src="thumb2.jpg" alt="RangeWater Riverside">
  </a>
  <p>Nashville, TN | (615) 555-0199 | Build-to-Rent</p>
</div>
<a href="https://www.facebook.com/rangewater">Facebook</a>
</body></html>
"""

INTERNAL_PROPERTY_PAGE_HTML = """
<html><body>
<h1>RangeWater Riverside</h1>
<a href="https://rangewaterriverside.com">Visit Site</a>
</body></html>
"""

# Two JSON-LD blocks: first is a floorplan w/ no address, second has the
# real address nested under capital-A "Address" (per the spec's edge case).
PROPERTY_SITE_HTML = """
<html><head>
<script type="application/ld+json">
{"@type": "Product", "name": "1BR Floorplan", "offers": {"price": "1450"}}
</script>
<script type="application/ld+json">
{"name": "Sterling Nashville West", "telephone": "(615) 555-0123",
 "Address": {"streetAddress": "7114 Charlotte Pike", "addressLocality": "Nashville",
             "addressRegion": "TN", "postalCode": "37209", "addressCountry": "US"},
 "numberOfBedrooms": "1-3", "petsAllowed": true,
 "amenityFeature": [{"name": "Resort-Style Pool"}, {"name": "Fitness Center"}],
 "floorSize": {"value": "850", "unitCode": "SqFt"}}
</script>
</head>
<body>
<p>Built in 1998, this community offers Studio - 3 Bed apartment homes.</p>
<p>Enjoy bulk internet included in your rent, plus a dog park and clubhouse.</p>
<p>Now Leasing! Rents starting at $1,450 - $2,100.</p>
<p>250 units in a gated community.</p>
</body></html>
"""

NO_JSONLD_PROPERTY_HTML = """
<html><body>
<nav>Home About Contact</nav>
<h1>Willow Creek Apartments</h1>
<p>Located at 200 Willow Creek Dr, Knoxville, TN 37919. Call (865) 555-0000.</p>
<p>High-speed internet and cable included with every lease.</p>
<footer>&copy; 2024</footer>
</body></html>
"""


class TestCityDiscovery(unittest.TestCase):
    def test_discover_city_pages_finds_anchors_and_crosschecks_hidden_slug(self):
        with patch.object(rw, "fetch", return_value="<html>ok</html>") as mock_fetch:
            cities = rw.discover_city_pages(FIND_YOUR_HOME_HTML, failures_writer=MagicMock())
        self.assertIn("nashville-tn", cities)
        self.assertIn("charlotte-nc", cities)
        self.assertEqual(cities["nashville-tn"]["state"], "Tennessee")
        self.assertEqual(cities["charlotte-nc"]["state"], "North Carolina")
        # the JS-embedded slug not in an <a> tag should be cross-check-discovered
        self.assertIn("atlanta-ga", cities)
        mock_fetch.assert_called_once()


class TestCityPageParsing(unittest.TestCase):
    def test_parse_city_page_external_and_internal_links(self):
        with patch.object(rw, "fetch", return_value=INTERNAL_PROPERTY_PAGE_HTML):
            leads = rw.parse_city_page(CITY_PAGE_HTML, "https://www.liverangewater.com/city/nashville-tn",
                                        failures_writer=MagicMock())
        names = {l.name: l for l in leads}
        self.assertIn("Sterling Nashville West", names)
        self.assertEqual(names["Sterling Nashville West"].property_url, "https://sterlingnashvillewest.com")
        self.assertEqual(names["Sterling Nashville West"].property_type, "Multifamily")
        self.assertEqual(names["Sterling Nashville West"].phone, "(615) 555-0123")

        self.assertIn("RangeWater Riverside", names)
        # internal link should have been followed to the real outbound site
        self.assertEqual(names["RangeWater Riverside"].property_url, "https://rangewaterriverside.com")
        self.assertEqual(names["RangeWater Riverside"].property_type, "Build-to-Rent")

        # social links must not be treated as property cards
        self.assertNotIn("Facebook", names)


class TestJsonLdExtraction(unittest.TestCase):
    def test_multiple_blocks_capital_address_key(self):
        blocks = rw.extract_jsonld_blocks(PROPERTY_SITE_HTML)
        self.assertEqual(len(blocks), 2)
        addr = rw.find_jsonld_address(blocks)
        self.assertIsNotNone(addr)
        self.assertEqual(addr["street_address"], "7114 Charlotte Pike")
        self.assertEqual(addr["city"], "Nashville")
        self.assertEqual(addr["state"], "TN")
        self.assertEqual(addr["zip"], "37209")
        self.assertEqual(addr["telephone"], "(615) 555-0123")
        self.assertIn("Resort-Style Pool", addr["amenityFeature"])
        self.assertIn("Fitness Center", addr["amenityFeature"])

    def test_lowercase_address_key_also_works(self):
        html = '<script type="application/ld+json">{"address": {"streetAddress": "1 Main St", "addressLocality": "X", "addressRegion": "TN", "postalCode": "00000"}}</script>'
        blocks = rw.extract_jsonld_blocks(html)
        addr = rw.find_jsonld_address(blocks)
        self.assertEqual(addr["street_address"], "1 Main St")

    def test_no_address_returns_none(self):
        html = '<script type="application/ld+json">{"name": "no address here"}</script>'
        blocks = rw.extract_jsonld_blocks(html)
        self.assertIsNone(rw.find_jsonld_address(blocks))

    def test_malformed_json_is_skipped_not_fatal(self):
        html = '<script type="application/ld+json">{not valid json,,,}</script>'
        blocks = rw.extract_jsonld_blocks(html)
        self.assertEqual(blocks, [])


class TestRegexFieldExtraction(unittest.TestCase):
    def test_full_text_extraction(self):
        text = rw.visible_text(PROPERTY_SITE_HTML)
        fields = rw.extract_regex_fields(text)
        self.assertEqual(fields.get("year_built"), "1998")
        self.assertEqual(fields.get("unit_count"), "250")
        self.assertEqual(fields.get("rent_low"), "1450")
        self.assertEqual(fields.get("rent_high"), "2100")
        self.assertEqual(fields.get("unit_mix"), "Studio - 3 Bed")
        self.assertIn("dog park", fields.get("amenities", ""))
        self.assertIn("clubhouse", fields.get("amenities", ""))
        self.assertIn("bulk internet", fields.get("internet_isp_mention", "").lower())
        self.assertEqual(fields.get("leasing_status").lower(), "now leasing")

    def test_isp_mention_exact_phrase_captured(self):
        text = "Some intro text. High-speed internet and cable included with every lease. More text."
        fields = rw.extract_regex_fields(text)
        self.assertIn("high-speed internet", fields["internet_isp_mention"].lower())


class TestScrapePropertySiteIntegration(unittest.TestCase):
    def test_clean_jsonld_property_end_to_end(self):
        lead = rw.PropertyLead(
            name="Sterling Nashville West", city="Nashville", phone="",
            property_type="Multifamily", property_url="https://sterlingnashvillewest.com",
            source_city_page="https://www.liverangewater.com/city/nashville-tn",
        )
        with patch.object(rw, "fetch", return_value=PROPERTY_SITE_HTML):
            record, used_llm = rw.scrape_property_site(lead, failures_writer=MagicMock())
        self.assertFalse(used_llm)
        self.assertEqual(record.address_source, "json_ld")
        self.assertEqual(record.street_address, "7114 Charlotte Pike")
        self.assertEqual(record.zip, "37209")
        self.assertTrue(record.internet_isp_mention)
        self.assertEqual(record.leasing_status.lower(), "now leasing")

    def test_llm_fallback_used_when_no_jsonld(self):
        lead = rw.PropertyLead(
            name="Willow Creek Apartments", city="Knoxville", phone="",
            property_type="Multifamily", property_url="https://willowcreekapts.com",
            source_city_page="https://www.liverangewater.com/city/knoxville-tn",
        )
        fake_llm_json = (
            '{"property_name": "Willow Creek Apartments", '
            '"street_address": "200 Willow Creek Dr", "city": "Knoxville", '
            '"state": "TN", "zip": "37919", "unit_count": null, '
            '"year_built": null, "rent_low": null, "rent_high": null, '
            '"unit_mix": null, "amenities": null, '
            '"internet_isp_mention": "High-speed internet and cable included with every lease.", '
            '"leasing_status": null}'
        )
        mock_response = MagicMock()
        mock_response.choices[0].message.content = fake_llm_json
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        def fetch_side_effect(url, stage, failures_writer, extra_headers=None):
            return NO_JSONLD_PROPERTY_HTML if url == lead.property_url else None

        with patch.object(rw, "fetch", side_effect=fetch_side_effect), \
             patch.object(rw, "_get_openai_client", return_value=mock_client):
            record, used_llm = rw.scrape_property_site(lead, failures_writer=MagicMock())

        self.assertTrue(used_llm)
        self.assertEqual(record.address_source, "llm_fallback")
        self.assertEqual(record.street_address, "200 Willow Creek Dr")
        self.assertIn("internet", record.internet_isp_mention.lower())

    def test_no_jsonld_and_no_api_key_logs_failure_gracefully(self):
        lead = rw.PropertyLead(
            name="Willow Creek Apartments", city="Knoxville", phone="",
            property_type="Multifamily", property_url="https://willowcreekapts.com",
            source_city_page="https://www.liverangewater.com/city/knoxville-tn",
        )
        failures = MagicMock()

        def fetch_side_effect(url, stage, failures_writer, extra_headers=None):
            return NO_JSONLD_PROPERTY_HTML if url == lead.property_url else None

        with patch.object(rw, "fetch", side_effect=fetch_side_effect), \
             patch.object(rw, "_get_openai_client", return_value=None):
            record, used_llm = rw.scrape_property_site(lead, failures_writer=failures)
        self.assertFalse(used_llm)
        self.assertEqual(record.street_address, "")
        failures.writerow.assert_called()  # failure was logged, not raised


class TestResumableCsv(unittest.TestCase):
    def test_load_existing_keys_and_append(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "portfolio.csv")
            f, writer = rw.open_csv_for_append(path, rw.PORTFOLIO_FIELDS)
            rec = rw.PropertyRecord(property_name="A", street_address="1 St", property_url="https://a.com")
            writer.writerow(rw.asdict(rec))
            f.close()

            keys = rw.load_existing_keys(path, ["property_name", "street_address"])
            self.assertIn(("a", "1 st"), keys)

            # re-opening for append should NOT rewrite the header
            f2, writer2 = rw.open_csv_for_append(path, rw.PORTFOLIO_FIELDS)
            f2.close()
            with open(path) as fh:
                header_count = sum(1 for line in fh if line.startswith("property_name"))
            self.assertEqual(header_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
