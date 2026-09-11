#!/usr/bin/env python3
"""
RangeWater Real Estate portfolio scraper.

Crawls https://www.liverangewater.com/find-your-home to discover every
state/city market, then every property listed in each city, then follows
each property's own dedicated marketing site to extract structured data
(via Schema.org JSON-LD, plain-text regex heuristics, and an OpenAI
gpt-4o-mini fallback for anything that can't be parsed cleanly).

Output:
  rangewater_portfolio.csv        - one row per unique property
  rangewater_scrape_failures.csv  - every URL that failed, with reason

Run:
  export OPENAI_API_KEY=sk-...      # optional, enables the LLM fallback
  python3 rangewater_scraper.py

The script is resumable: it re-reads its own output CSVs on startup and
skips any property it already has a row for, so it can be killed and
restarted without losing work or re-hitting sites it already scraped.

NOTE ON SELECTORS: liverangewater.com's actual card/markup class names
were not available while writing this (no network access from the dev
environment), so the HTML parsing below uses defensive, structure-based
heuristics (nearby external links, phone-number regexes, "Multifamily" /
"Build-to-Rent" tag text) rather than hardcoded CSS classes. If a real
run shows city pages parsing to zero properties, inspect one saved city
page's HTML and tighten `parse_city_page()` accordingly - the rest of the
pipeline (property-site extraction, JSON-LD parsing, LLM fallback, CSV
writing) does not depend on liverangewater.com's markup at all.
"""

import csv
import json
import os
import re
import time
import traceback
from dataclasses import dataclass, asdict
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BASE_URL = "https://www.liverangewater.com"
FIND_YOUR_HOME_URL = f"{BASE_URL}/find-your-home"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20
REQUEST_DELAY_RANGE = (1.0, 2.0)  # polite delay between requests, seconds
MAX_RETRIES = 1  # one retry on top of the initial attempt

PROPERTY_SUBPAGES = ["contact", "contactus", "location", "amenities", "floorplans"]

PORTFOLIO_CSV = "rangewater_portfolio.csv"
FAILURES_CSV = "rangewater_scrape_failures.csv"

PORTFOLIO_FIELDS = [
    "property_name", "street_address", "city", "state", "zip", "phone",
    "property_type", "year_built", "unit_count", "rent_low", "rent_high",
    "unit_mix", "amenities", "internet_isp_mention", "leasing_status",
    "source_city_page", "address_source", "property_url",
]
FAILURE_FIELDS = ["url", "stage", "error"]

OPENAI_MODEL = "gpt-4o-mini"

AMENITY_KEYWORDS = [
    "pool", "swimming pool", "fitness center", "gym", "clubhouse",
    "dog park", "pet park", "pet spa", "business center", "coworking",
    "package concierge", "package room", "garage", "parking", "carport",
    "playground", "grilling station", "grill", "fire pit", "yoga",
    "game room", "billiards", "media room", "theater", "courtyard",
    "outdoor kitchen", "walking trail", "bike storage", "car wash",
    "sauna", "spa", "tennis court", "sport court", "rooftop", "lounge",
    "conference room", "electric car charging", "ev charging",
    "storage units", "washer and dryer", "in-unit washer",
]

ISP_PATTERN = re.compile(
    r"[^.]*\b(bulk internet|internet included|high[- ]speed internet|"
    r"gigabit internet|complimentary internet|free internet|"
    r"internet & cable|internet and cable|internet/cable|bulk cable|"
    r"cable (?:and|&) internet|google fiber|at&t fiber|xfinity|comcast|"
    r"spectrum|centurylink|frontier fiber|epb fiber|wow internet|"
    r"metronet|ziply fiber|wave broadband|kinetic by windstream|"
    r"internet service provider|isp package|bulk technology fee|"
    r"technology package)[^.]*\."
    , re.IGNORECASE,
)

YEAR_BUILT_PATTERN = re.compile(
    r"(?:built|constructed|completed)\s*(?:in)?\s*(\d{4})|year built[:\s]+(\d{4})",
    re.IGNORECASE,
)
UNIT_COUNT_PATTERN = re.compile(
    r"([\d,]{2,5})\s*(?:apartment\s*)?(?:homes|units|apartments)\b",
    re.IGNORECASE,
)
RENT_RANGE_PATTERN = re.compile(r"\$([\d,]{3,6})\s*(?:-|to|–)\s*\$([\d,]{3,6})")
RENT_SINGLE_PATTERN = re.compile(r"\$([\d,]{3,6})(?:\s*/\s*(?:mo|month))?")
UNIT_MIX_PATTERN = re.compile(
    r"(studio|[0-9]\s*bed(?:room)?s?)\s*(?:-|to|–)\s*([0-9]\s*bed(?:room)?s?)",
    re.IGNORECASE,
)
LEASING_STATUS_PATTERN = re.compile(
    r"(now leasing|pre-?leasing|coming soon|now open|now pre-?leasing)",
    re.IGNORECASE,
)
PHONE_PATTERN = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})


# --------------------------------------------------------------------------
# Small data holder for a discovered (but not yet scraped) property
# --------------------------------------------------------------------------

@dataclass
class PropertyLead:
    name: str
    city: str
    phone: str
    property_type: str
    property_url: str  # the property's own dedicated site (best guess so far)
    source_city_page: str


@dataclass
class PropertyRecord:
    property_name: str = ""
    street_address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phone: str = ""
    property_type: str = ""
    year_built: str = ""
    unit_count: str = ""
    rent_low: str = ""
    rent_high: str = ""
    unit_mix: str = ""
    amenities: str = ""
    internet_isp_mention: str = ""
    leasing_status: str = ""
    source_city_page: str = ""
    address_source: str = ""
    property_url: str = ""


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def polite_sleep():
    lo, hi = REQUEST_DELAY_RANGE
    time.sleep(lo + (hi - lo) * 0.5)


def fetch(url, stage, failures_writer, extra_headers=None):
    """GET a URL with one retry. Returns response text or None on failure.
    Any failure is appended to the failures CSV immediately."""
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = session.get(
                url, timeout=REQUEST_TIMEOUT, headers=extra_headers or {}
            )
            if resp.status_code == 200:
                polite_sleep()
                return resp.text
            last_err = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        if attempt < MAX_RETRIES:
            time.sleep(1.0)
    log_failure(failures_writer, url, stage, last_err or "unknown error")
    polite_sleep()
    return None


def log_failure(failures_writer, url, stage, error):
    failures_writer.writerow({"url": url, "stage": stage, "error": str(error)[:500]})
    failures_writer.file.flush()


# --------------------------------------------------------------------------
# Step 1: discover states + city pages
# --------------------------------------------------------------------------

def slugify(text):
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def discover_city_pages(html, failures_writer):
    """Parse the find-your-home page for every /city/{slug} link, plus
    cross-check by looking for city names in the raw page that might not be
    wrapped in an <a> tag (e.g. rendered via embedded JSON/JS data), and
    confirming those resolve by requesting the slug directly."""
    soup = BeautifulSoup(html, "html.parser")
    cities = {}  # slug -> {"name": ..., "state": ..., "url": ...}

    # Primary source: real <a href="/city/...."> links, grouped under the
    # nearest preceding state heading if one is present.
    current_state = ""
    for el in soup.find_all(["h1", "h2", "h3", "h4", "a"]):
        if el.name in ("h1", "h2", "h3", "h4"):
            txt = el.get_text(strip=True)
            # Heuristic: state headings are short, don't contain "find" etc.
            if txt and len(txt) <= 40 and not re.search(r"find your home", txt, re.I):
                current_state = txt
            continue
        href = el.get("href") or ""
        m = re.search(r"/city/([a-z0-9\-]+)", href, re.I)
        if not m:
            continue
        slug = m.group(1).lower()
        name = el.get_text(strip=True) or slug.replace("-", " ").title()
        url = urljoin(BASE_URL, href)
        cities.setdefault(slug, {"name": name, "state": current_state, "url": url})

    # Cross-check: scan the *entire* raw HTML (including embedded <script>
    # JSON blobs) for any additional /city/{slug} references not caught by
    # the anchor-tag pass above (e.g. JS-driven nav, sitemaps in a data
    # attribute). Any newly found slug is validated with a live request.
    all_slug_refs = set(re.findall(r"/city/([a-z0-9\-]+)", html, re.I))
    new_slugs = {s.lower() for s in all_slug_refs} - set(cities.keys())
    for slug in sorted(new_slugs):
        url = f"{BASE_URL}/city/{slug}"
        check_html = fetch(url, "city_page_crosscheck", failures_writer)
        if check_html:
            cities[slug] = {"name": slug.replace("-", " ").title(), "state": "", "url": url}

    return cities


# --------------------------------------------------------------------------
# Step 2: discover properties + their outbound site link, per city page
# --------------------------------------------------------------------------

def is_external(url):
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return bool(host) and "liverangewater.com" not in host



# Links that show up on every page's chrome or footer boilerplate and must
# never be mistaken for a property's own site (privacy-policy generators,
# social platforms, app stores, etc.).
NON_PROPERTY_DOMAINS = [
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "tiktok.com", "pinterest.com",
    "termsfeed.com", "iubenda.com", "cookiebot.com", "onetrust.com",
    "google.com", "apple.com", "play.google.com", "apps.apple.com",
    "maps.google.com", "goo.gl",
]


def parse_city_page(html, city_url, failures_writer):
    """Extract every property card on a city page: name, phone, property
    type tag, and outbound link to the property's own site. If a card's
    only link is internal (a liverangewater.com subpage), follow it once to
    find the real outbound link."""
    soup = BeautifulSoup(html, "html.parser")
    leads = []
    seen_names = set()

    # Site chrome (global nav, footer legal links, etc.) repeats on every
    # page and is never itself a property card -- drop it before scanning
    # so we don't mistake "Our Story" / "Our Team" / footer links for leads.
    for chrome in soup.find_all(["header", "footer", "nav"]):
        chrome.decompose()

    # Heuristic: treat each <a> that points to an external domain (or, if
    # none nearby, an internal property subpage) as anchoring one property
    # "card" -- walk up to a reasonably small containing block and pull the
    # name/phone/type text out of it.
    candidate_links = soup.find_all("a", href=True)
    for link in candidate_links:
        href = link["href"]
        # Skip obvious nav/social/anchor/JS-handler junk (real hrefs only).
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if any(d in href.lower() for d in NON_PROPERTY_DOMAINS):
            continue

        # Find a reasonably-scoped ancestor "card" container to pull text from.
        card = link
        for _ in range(4):
            if card.parent is None:
                break
            card = card.parent
            text = card.get_text(" ", strip=True)
            if text and 10 <= len(text) <= 600:
                break

        card_text = card.get_text(" ", strip=True) if card else link.get_text(" ", strip=True)
        if not card_text:
            continue

        name = (link.get("title") or link.get_text(strip=True) or "").strip()
        if not name:
            # fall back to the first line of the card text
            name = card_text.split("  ")[0][:80].strip()
        if not name or name.lower() in ("read more", "learn more", "view", "details"):
            # try alt text on an image inside the link
            img = link.find("img")
            if img and img.get("alt"):
                name = img["alt"].strip()
        if not name or len(name) < 3:
            continue

        dedupe_key = (name.lower(), href)
        if dedupe_key in seen_names:
            continue
        seen_names.add(dedupe_key)

        phone_match = PHONE_PATTERN.search(card_text)
        phone = phone_match.group(0) if phone_match else ""

        if re.search(r"build[\s-]?to[\s-]?rent", card_text, re.I):
            prop_type = "Build-to-Rent"
        elif re.search(r"multifamily", card_text, re.I):
            prop_type = "Multifamily"
        else:
            prop_type = ""

        resolved_url = urljoin(city_url, href)
        if not is_external(resolved_url):
            # Follow the internal page once to find the real outbound site link.
            inner_html = fetch(resolved_url, "resolve_property_link", failures_writer)
            resolved_url = _find_outbound_site_link(inner_html) if inner_html else resolved_url

        leads.append(PropertyLead(
            name=name,
            city="",  # filled in by caller from city metadata
            phone=phone,
            property_type=prop_type,
            property_url=resolved_url,
            source_city_page=city_url,
        ))

    return leads


def _find_outbound_site_link(html):
    """Given an internal liverangewater.com property page, find the actual
    outbound link to the property's own dedicated site."""
    soup = BeautifulSoup(html, "html.parser")
    for chrome in soup.find_all(["header", "footer", "nav"]):
        chrome.decompose()
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if any(d in href.lower() for d in NON_PROPERTY_DOMAINS):
            continue
        resolved = urljoin(BASE_URL, href)
        if is_external(resolved):
            return resolved
    return ""


# --------------------------------------------------------------------------
# Step 3: extract data from a property's own dedicated site
# --------------------------------------------------------------------------

def extract_jsonld_blocks(html):
    """Return every parseable JSON-LD object found on the page (flattening
    @graph / list-of-objects structures)."""
    soup = BeautifulSoup(html, "html.parser")
    blocks = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, list):
            blocks.extend([d for d in data if isinstance(d, dict)])
        elif isinstance(data, dict):
            if isinstance(data.get("@graph"), list):
                blocks.extend([d for d in data["@graph"] if isinstance(d, dict)])
            else:
                blocks.append(data)
    return blocks


def _address_dict_from_block(block):
    addr = block.get("address") or block.get("Address")
    if isinstance(addr, list):
        addr = next((a for a in addr if isinstance(a, dict)), None)
    if not isinstance(addr, dict):
        return None
    street = addr.get("streetAddress") or addr.get("StreetAddress")
    if not street:
        return None
    return {
        "street_address": street,
        "city": addr.get("addressLocality") or addr.get("AddressLocality") or "",
        "state": addr.get("addressRegion") or addr.get("AddressRegion") or "",
        "zip": addr.get("postalCode") or addr.get("PostalCode") or "",
    }


def find_jsonld_address(blocks):
    """Scan every JSON-LD block on a page; return the first one with a
    populated street address, along with whatever bonus fields it carries."""
    for block in blocks:
        addr = _address_dict_from_block(block)
        if not addr:
            continue
        result = dict(addr)
        result["name"] = block.get("name", "")
        result["telephone"] = block.get("telephone", "")
        result["numberOfBedrooms"] = block.get("numberOfBedrooms", "")
        result["petsAllowed"] = block.get("petsAllowed", "")
        result["amenityFeature"] = _stringify_amenity_feature(block.get("amenityFeature"))
        result["floorSize"] = _stringify_floor_size(block.get("floorSize"))
        result["offers"] = _stringify_offers(block.get("offers"))
        return result
    return None


def _stringify_amenity_feature(val):
    if not val:
        return ""
    if isinstance(val, list):
        names = []
        for item in val:
            if isinstance(item, dict):
                names.append(item.get("name") or item.get("value") or "")
            else:
                names.append(str(item))
        return ", ".join(n for n in names if n)
    if isinstance(val, dict):
        return val.get("name") or val.get("value") or ""
    return str(val)


def _stringify_floor_size(val):
    if isinstance(val, dict):
        return f"{val.get('value', '')} {val.get('unitText', val.get('unitCode', ''))}".strip()
    return str(val) if val else ""


def _stringify_offers(val):
    if isinstance(val, dict):
        return str(val.get("price") or val.get("lowPrice") or "")
    if isinstance(val, list) and val:
        prices = [str(o.get("price") or o.get("lowPrice") or "") for o in val if isinstance(o, dict)]
        return ", ".join(p for p in prices if p)
    return ""


def visible_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def extract_regex_fields(text):
    fields = {}

    m = YEAR_BUILT_PATTERN.search(text)
    if m:
        fields["year_built"] = m.group(1) or m.group(2)

    m = UNIT_COUNT_PATTERN.search(text)
    if m:
        fields["unit_count"] = m.group(1).replace(",", "")

    m = RENT_RANGE_PATTERN.search(text)
    if m:
        fields["rent_low"] = m.group(1).replace(",", "")
        fields["rent_high"] = m.group(2).replace(",", "")
    else:
        m = RENT_SINGLE_PATTERN.search(text)
        if m:
            fields["rent_low"] = m.group(1).replace(",", "")

    m = UNIT_MIX_PATTERN.search(text)
    if m:
        fields["unit_mix"] = f"{m.group(1).strip()} - {m.group(2).strip()}"

    found_amenities = [kw for kw in AMENITY_KEYWORDS if kw in text.lower()]
    if found_amenities:
        # de-dupe overlapping keywords (e.g. "pool" vs "swimming pool")
        deduped = []
        for a in sorted(found_amenities, key=len, reverse=True):
            if not any(a in d for d in deduped):
                deduped.append(a)
        fields["amenities"] = ", ".join(sorted(deduped))

    m = ISP_PATTERN.search(text)
    if m:
        fields["internet_isp_mention"] = m.group(0).strip()

    m = LEASING_STATUS_PATTERN.search(text)
    if m:
        fields["leasing_status"] = m.group(1)

    if not PHONE_PATTERN.search(text):
        pass  # phone is primarily sourced from JSON-LD / city page

    return fields


def scrape_property_site(lead, failures_writer):
    """Visit a property's homepage (and subpages as needed) and build a
    PropertyRecord. Returns (record, used_llm_fallback: bool)."""
    record = PropertyRecord(
        property_name=lead.name,
        city=lead.city,
        phone=lead.phone,
        property_type=lead.property_type,
        source_city_page=lead.source_city_page,
        property_url=lead.property_url,
    )

    if not lead.property_url or not lead.property_url.startswith("http"):
        log_failure(failures_writer, lead.property_url or "(no url)",
                    "property_site_fetch", "no outbound property URL found")
        return record, False

    pages_fetched = []
    combined_text_parts = []
    jsonld_address = None

    urls_to_try = [lead.property_url] + [
        urljoin(lead.property_url + "/", sub) for sub in PROPERTY_SUBPAGES
    ]

    for i, url in enumerate(urls_to_try):
        # Always fetch the homepage; only fetch subpages if we still need data.
        if i > 0 and jsonld_address and combined_text_parts:
            need_more = (
                not extract_regex_fields(" ".join(combined_text_parts)).get("internet_isp_mention")
            )
            if not need_more:
                break

        html = fetch(url, "property_site_fetch", failures_writer)
        if not html:
            continue
        pages_fetched.append(html)
        combined_text_parts.append(visible_text(html))

        if not jsonld_address:
            blocks = extract_jsonld_blocks(html)
            jsonld_address = find_jsonld_address(blocks)

    combined_text = " ".join(combined_text_parts)

    if jsonld_address:
        record.address_source = "json_ld"
        record.street_address = jsonld_address["street_address"]
        record.city = jsonld_address["city"] or record.city
        record.state = jsonld_address["state"]
        record.zip = jsonld_address["zip"]
        if jsonld_address.get("name"):
            record.property_name = jsonld_address["name"]
        if jsonld_address.get("telephone"):
            record.phone = jsonld_address["telephone"]
        if jsonld_address.get("amenityFeature"):
            record.amenities = jsonld_address["amenityFeature"]
        if jsonld_address.get("offers"):
            record.rent_low = jsonld_address["offers"]
        if jsonld_address.get("numberOfBedrooms"):
            record.unit_mix = str(jsonld_address["numberOfBedrooms"])

    regex_fields = extract_regex_fields(combined_text) if combined_text else {}
    for key, val in regex_fields.items():
        if not getattr(record, key, None):
            setattr(record, key, val)

    used_llm = False
    if not record.street_address:
        if not combined_text:
            log_failure(failures_writer, lead.property_url, "extraction",
                        "no pages fetched successfully; nothing to extract")
        else:
            llm_result = llm_fallback_extract(combined_text, lead, failures_writer)
            if llm_result:
                used_llm = True
                record.address_source = "llm_fallback"
                for key, val in llm_result.items():
                    if val and not getattr(record, key, ""):
                        setattr(record, key, str(val))
            else:
                log_failure(failures_writer, lead.property_url, "extraction",
                            "no JSON-LD address found and LLM fallback unavailable/failed")

    return record, used_llm


# --------------------------------------------------------------------------
# Step 4: LLM fallback (OpenAI gpt-4o-mini) for unparseable pages
# --------------------------------------------------------------------------

_openai_client = None


def _get_openai_client():
    global _openai_client
    if _openai_client is not None:
        return _openai_client
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=api_key)
    except Exception:
        _openai_client = None
    return _openai_client


LLM_SYSTEM_PROMPT = (
    "You extract structured data about a US apartment community from raw "
    "webpage text. Respond with STRICT JSON only, no markdown fences, no "
    "commentary, matching exactly this schema:\n"
    '{"property_name": string|null, "street_address": string|null, '
    '"city": string|null, "state": string|null, "zip": string|null, '
    '"unit_count": string|null, "year_built": string|null, '
    '"rent_low": string|null, "rent_high": string|null, '
    '"unit_mix": string|null, "amenities": string|null, '
    '"internet_isp_mention": string|null, "leasing_status": string|null}\n'
    "Use null for anything not clearly stated in the text. For "
    "internet_isp_mention, quote the exact sentence/phrase mentioning "
    "internet, cable, wifi, or a named ISP if one exists, else null."
)


def llm_fallback_extract(text, lead, failures_writer, max_chars=12000):
    client = _get_openai_client()
    if client is None:
        return None
    truncated = text[:max_chars]
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {"role": "user", "content": f"Property name per source directory: {lead.name}\n\nPage text:\n{truncated}"},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = response.choices[0].message.content
        return json.loads(content)
    except Exception as exc:
        log_failure(failures_writer, lead.property_url, "llm_fallback", f"{type(exc).__name__}: {exc}")
        return None


# --------------------------------------------------------------------------
# CSV I/O (resumable)
# --------------------------------------------------------------------------

class FlushingDictWriter:
    """Thin wrapper so log_failure()/writer.writerow() can call .file.flush()."""
    def __init__(self, file_obj, fieldnames):
        self.file = file_obj
        self.writer = csv.DictWriter(file_obj, fieldnames=fieldnames)

    def writeheader(self):
        self.writer.writeheader()
        self.file.flush()

    def writerow(self, row):
        self.writer.writerow(row)
        self.file.flush()


def load_existing_keys(path, key_fields):
    keys = set()
    if not os.path.exists(path):
        return keys
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            keys.add(tuple((row.get(k) or "").strip().lower() for k in key_fields))
    return keys


def open_csv_for_append(path, fieldnames):
    is_new = not os.path.exists(path) or os.path.getsize(path) == 0
    f = open(path, "a", newline="", encoding="utf-8")
    writer = FlushingDictWriter(f, fieldnames)
    if is_new:
        writer.writeheader()
    return f, writer


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    portfolio_file, portfolio_writer = open_csv_for_append(PORTFOLIO_CSV, PORTFOLIO_FIELDS)
    failures_file, failures_writer = open_csv_for_append(FAILURES_CSV, FAILURE_FIELDS)

    # Resumability: don't re-scrape properties we already have a row for.
    done_keys = load_existing_keys(PORTFOLIO_CSV, ["property_name", "street_address"])
    done_urls = load_existing_keys(PORTFOLIO_CSV, ["property_url"])

    stats = {
        "cities": 0, "properties_found": 0, "properties_scraped": 0,
        "json_ld": 0, "llm_fallback": 0, "failed_entirely": 0,
        "with_isp_mention": 0, "skipped_already_done": 0,
    }

    try:
        print(f"Fetching {FIND_YOUR_HOME_URL} ...")
        home_html = fetch(FIND_YOUR_HOME_URL, "find_your_home", failures_writer)
        if not home_html:
            print("FATAL: could not fetch find-your-home page. Aborting.")
            return

        cities = discover_city_pages(home_html, failures_writer)
        stats["cities"] = len(cities)
        print(f"Discovered {len(cities)} city pages.\n")

        all_leads = []
        city_items = list(cities.items())

        max_cities = os.environ.get("RANGEWATER_MAX_CITIES")
        if max_cities:
            city_items = city_items[:int(max_cities)]
            print(f"RANGEWATER_MAX_CITIES set — limiting this run to the first "
                  f"{len(city_items)} cities (smoke-test mode).\n")

        for idx, (slug, meta) in enumerate(city_items, start=1):
            city_url = meta["url"]
            html = fetch(city_url, "city_page_fetch", failures_writer)
            if not html:
                print(f"[{idx}/{len(city_items)}] {meta['name']} ({meta['state']}) — FAILED to fetch")
                continue
            leads = parse_city_page(html, city_url, failures_writer)
            for lead in leads:
                lead.city = lead.city or meta["name"]
            all_leads.extend(leads)
            print(f"Processing city {idx}/{len(city_items)}: {meta['name']}, {meta['state']} "
                  f"— found {len(leads)} properties")

        # De-dupe leads before scraping (same property may appear on 2 city pages).
        unique_leads = {}
        for lead in all_leads:
            key = (lead.name.strip().lower(), lead.property_url.strip().lower())
            if key not in unique_leads:
                unique_leads[key] = lead
        stats["properties_found"] = len(unique_leads)
        print(f"\n{len(unique_leads)} unique property leads to scrape "
              f"(from {len(all_leads)} listings across all cities).\n")

        leads_to_scrape = list(unique_leads.values())
        max_properties = os.environ.get("RANGEWATER_MAX_PROPERTIES")
        if max_properties:
            leads_to_scrape = leads_to_scrape[:int(max_properties)]
            print(f"RANGEWATER_MAX_PROPERTIES set — limiting this run to the first "
                  f"{len(leads_to_scrape)} properties (smoke-test mode).\n")

        for i, lead in enumerate(leads_to_scrape, start=1):
            url_key = (lead.property_url.strip().lower(),)
            if url_key in done_urls:
                stats["skipped_already_done"] += 1
                print(f"[{i}/{len(leads_to_scrape)}] Skipping (already scraped): {lead.name}")
                continue

            print(f"[{i}/{len(leads_to_scrape)}] Scraping: {lead.name} -> {lead.property_url}")
            try:
                record, used_llm = scrape_property_site(lead, failures_writer)
            except Exception as exc:
                traceback.print_exc()
                log_failure(failures_writer, lead.property_url, "unhandled_exception", str(exc))
                stats["failed_entirely"] += 1
                continue

            dedupe_key = (record.property_name.strip().lower(), record.street_address.strip().lower())
            if record.street_address and dedupe_key in done_keys:
                print("    -> duplicate of an already-recorded property, skipping row")
                continue

            if not record.street_address:
                stats["failed_entirely"] += 1
            else:
                done_keys.add(dedupe_key)
                if used_llm:
                    stats["llm_fallback"] += 1
                else:
                    stats["json_ld"] += 1
                if record.internet_isp_mention:
                    stats["with_isp_mention"] += 1

            stats["properties_scraped"] += 1
            portfolio_writer.writerow(asdict(record))

        print("\n" + "=" * 60)
        print("SCRAPE COMPLETE — SUMMARY")
        print("=" * 60)
        print(f"Total city pages found:        {stats['cities']}")
        print(f"Total unique properties found: {stats['properties_found']}")
        print(f"  - already done (resumed):    {stats['skipped_already_done']}")
        print(f"  - clean JSON-LD address:     {stats['json_ld']}")
        print(f"  - required LLM fallback:     {stats['llm_fallback']}")
        print(f"  - failed entirely:           {stats['failed_entirely']}")
        print(f"Properties with an internet/ISP mention: {stats['with_isp_mention']}")
        print(f"\nOutput written to: {PORTFOLIO_CSV}")
        print(f"Failures logged to: {FAILURES_CSV}")

    finally:
        portfolio_file.close()
        failures_file.close()


if __name__ == "__main__":
    main()
