"""MarketplaceEndpoint parsing and request shaping.

Run: python -m tests.test_endpoint
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher.endpoint import parse_payload, variables_for, CapturedCall


def check(label: str, condition: bool) -> None:
    print(f" {'PASS' if condition else 'FAIL'} {label}")
    if not condition:
        raise AssertionError(label)


def test_parse_payload() -> None:
    payload = {
        "data": {
            "search": {
                "edges": [
                    {
                        "node": {
                            "id": "photo-1",
                            "creation_time": 1_700_000_000,
                            "listing": {
                                "__typename": "GroupCommerceProductItem",
                                "id": "123",
                                "marketplace_listing_title": "iPhone 15 128GB",
                                "listing_price": {"formatted_amount": "£500", "amount": 50000},
                                "location": {
                                    "reverse_geocode": {"city": "Sunderland", "display_name": "Sunderland"}
                                },
                            },
                        }
                    }
                ]
            }
        }
    }
    cards, server = parse_payload(payload, 10)
    check("one card parsed", len(cards) == 1)
    card = cards[0]
    check("listing id comes from product item", card.id == "123")
    check("title parsed", card.title == "iPhone 15 128GB")
    check("price parsed", card.price == 500)
    check("location parsed", card.location == "Sunderland")
    check("posted epoch stored", card.posted_epoch == 1_700_000_000)


def test_variables_for() -> None:
    call = CapturedCall(
        url="https://www.facebook.com/api/graphql/",
        form={},
        headers={},
        friendly_name="CometMarketplaceSearchContentContainerQuery",
        variables={
            "buyLocation": {"latitude": 1.0, "longitude": 2.0},
            "count": 24,
            "savedSearchQuery": "iphone",
            "contextual_data": [{"name": "search_query", "value": '"old"'}],
            "params": {
                "bqf": {"query": "old"},
                "browse_request_params": {
                    "filter_price_lower_bound": 100,
                    "filter_price_upper_bound": 200,
                    "filter_radius_km": 5,
                },
            },
        },
    )
    url = (
        "https://www.facebook.com/marketplace/112404765441676/search/"
        "?query=galaxy&radiusKM=40&lat=54.9&lon=-1.3&minPrice=100&maxPrice=1700"
        "&sortBy=creation_time_descend&exact=false"
    )
    out = variables_for(call, url, limit=30)
    check("query propagated", out["savedSearchQuery"] == "galaxy")
    check("buy location updated", out["buyLocation"]["latitude"] == 54.9)
    check("price bounds converted", out["params"]["browse_request_params"]["filter_price_upper_bound"] == 170000)
    check("radius updated", out["params"]["browse_request_params"]["filter_radius_km"] == 40)


def main() -> int:
    test_parse_payload()
    test_variables_for()
    print("all endpoint checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
