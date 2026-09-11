#!/usr/bin/env python3
"""
One-off diagnostic: fetch a single liverangewater.com city page (from a real
network-enabled environment, e.g. a GitHub Actions runner) and print its
actual link/DOM structure, so parse_city_page() in rangewater_scraper.py can
be tuned against real markup instead of guesswork.

Not part of the scraper pipeline itself -- run manually / via the
rangewater_debug_inspect workflow, read the printed output, then delete
once parse_city_page() is confirmed working.
"""

import os
import re

import requests
from bs4 import BeautifulSoup, Comment

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CITY_URL = os.environ.get("RANGEWATER_DEBUG_CITY_URL", "https://www.liverangewater.com/city/auburn")


def class_chain(tag, depth=4):
    """e.g. a.card-link < div.card-body < div.property-card < div.grid"""
    parts = []
    node = tag
    for _ in range(depth):
        if node is None or node.name in ("html", "[document]"):
            break
        classes = ".".join(node.get("class", []))
        parts.append(f"{node.name}.{classes}" if classes else node.name)
        node = node.parent
    return " < ".join(parts)


def main():
    print(f"Fetching {CITY_URL} ...\n")
    resp = requests.get(CITY_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    print(f"HTTP {resp.status_code}, {len(resp.text)} bytes\n")
    if resp.status_code != 200:
        print("Non-200 response, dumping first 2000 chars of body:")
        print(resp.text[:2000])
        return

    soup = BeautifulSoup(resp.text, "html.parser")

    title = soup.find("title")
    print(f"<title>: {title.get_text(strip=True) if title else '(none)'}\n")

    print("=" * 70)
    print("ALL <a href> TAGS (href | ancestor class chain | first 80 chars of text)")
    print("=" * 70)
    for i, link in enumerate(soup.find_all("a", href=True)):
        href = link["href"]
        text = link.get_text(" ", strip=True)[:80]
        chain = class_chain(link)
        print(f"[{i}] href={href!r}")
        print(f"     chain: {chain}")
        print(f"     text:  {text!r}")

    print("\n" + "=" * 70)
    print("REPEATED CONTAINER CLASSES (candidates for 'one property card')")
    print("=" * 70)
    class_counts = {}
    for tag in soup.find_all(True, class_=True):
        key = f"{tag.name}.{'.'.join(tag.get('class', []))}"
        class_counts[key] = class_counts.get(key, 0) + 1
    # A real per-property card class will repeat roughly once per property.
    for key, count in sorted(class_counts.items(), key=lambda kv: -kv[1]):
        if 2 <= count <= 60:
            print(f"  {count:3d}x  {key}")

    print("\n" + "=" * 70)
    print("PHONE-NUMBER-LIKE STRINGS FOUND ON PAGE (to locate card boundaries)")
    print("=" * 70)
    phone_re = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
    body_text = soup.get_text(" ", strip=True)
    for m in phone_re.finditer(body_text):
        start = max(0, m.start() - 60)
        end = min(len(body_text), m.end() + 20)
        print(f"  ...{body_text[start:end]}...")

    # Base64 image data URIs are huge and worthless for structure inspection --
    # strip them so the character budget goes to actual markup instead.
    for tag in soup.find_all(True):
        for attr in ("src", "data-src", "srcset", "data-srcset"):
            val = tag.get(attr)
            if val and "base64," in val:
                tag[attr] = "[BASE64_STRIPPED]"
            elif val and val.startswith("data:"):
                tag[attr] = "[DATA_URI_STRIPPED]"
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()

    print("\n" + "=" * 70)
    print("'OUR PROPERTIES' / PROPERTY-LISTING SECTION (full, untruncated)")
    print("=" * 70)
    heading = soup.find(string=re.compile(r"our properties", re.I))
    if heading:
        section = heading.find_parent(["section", "div"])
        # walk up a bit further to make sure we captured the whole card grid
        for _ in range(2):
            if section and section.parent:
                section = section.parent
        print(section.prettify() if section else "(could not resolve parent section)")
    else:
        print("(no element containing 'Our Properties' text found)")

    print("\n" + "=" * 70)
    print("FULL PRETTIFIED <body> HTML (script/style/base64 stripped, truncated to 60000 chars)")
    print("=" * 70)
    body = soup.body or soup
    pretty = body.prettify()
    print(pretty[:60000])
    if len(pretty) > 60000:
        print(f"\n...[TRUNCATED, {len(pretty) - 60000} more chars]...")


if __name__ == "__main__":
    main()
