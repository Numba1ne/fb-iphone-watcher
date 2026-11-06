"""Drives the real parser against a synthetic Marketplace-shaped page.

Facebook's actual markup uses hashed class names, so this fixture deliberately
mimics the *structure* (anchor wrapper -> image alt -> price -> location) with
arbitrary class names. If parsing passes here but fails live, the fixture is
out of date; if it fails here, the parser is broken.

Run:  python tests/test_parsing.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from watcher.marketplace import parse_cards  # noqa: E402

CARD = """
<a href="/marketplace/item/{id}/?ref=search&tracking=abc">
  <div class="x1lliihq">
    <div class="x9f619"><img alt="{alt}" src="x.png" /></div>
    <div class="x78zum5">
      <span class="x193iq5w">£{price}</span>
      <span class="x1q0g3np">{location}</span>
    </div>
  </div>
</a>
"""


def page_html() -> str:
    cards = "".join(
        CARD.format(**kwargs)
        for kwargs in [
            dict(id="111", alt="iPhone 13 128GB Blue", price="280", location="Croydon, London"),
            dict(id="222", alt="Photo of item", price="1,200", location="Romford, London"),
            # Same id as #111: must be deduped even with a different ref param.
            dict(id="111", alt="iPhone 13 128GB Blue", price="280", location="Croydon"),
            dict(id="333", alt="iPhone 14 Pro for parts", price="150", location="Epsom"),
            # Same-line shape: price and location in ONE span, no separator.
            dict(id="444", alt="iPhone 15 256GB", price="900", location="Sutton, London"),
        ]
    )
    cards += """
<a href="/marketplace/item/555/">
  <div><img alt="iPhone 12 mini" src="x.png" />
    <span>£320 · Dartford, London</span></div>
</a>"""
    return f"<html><body><div role='main'>{cards}</div></body></html>"


async def main() -> int:
    failures = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(page_html())

        listings, unparsed = await parse_cards(page, limit=30)
        await browser.close()

    by_id = {x.id: x for x in listings}

    def check(label: str, condition: bool) -> None:
        print(f"  {'PASS ' if condition else 'FAIL '} {label}")
        if not condition:
            failures.append(label)

    check(f"parsed 5 unique cards (got {len(listings)})", len(listings) == 5)
    check(f"unparsed count is 0 (got {unparsed})", unparsed == 0)
    check("duplicate id deduped", len([x for x in listings if x.id == "111"]) == 1)

    first = by_id.get("111")
    check("title from image alt", first is not None and first.title == "iPhone 13 128GB Blue")
    check("price parsed", first is not None and first.price == 280)
    check("location parsed", first is not None and first.location == "Croydon, London")
    check(
        "url rebuilt without tracking params",
        first is not None and first.url == "https://www.facebook.com/marketplace/item/111",
    )

    second = by_id.get("222")
    check("title fallback when alt is a placeholder", second is not None and "£" not in second.title)
    check("thousands separator handled", second is not None and second.price == 1200)
    check("location parsed when same span", second is not None and second.location == "Romford, London")

    dotted = by_id.get("555")
    check("dot-separated same-span location", dotted is not None and dotted.location == "Dartford, London")
    check("dot-separated price", dotted is not None and dotted.price == 320)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all parsing checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
