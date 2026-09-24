"""Regenerate ``data/prices.yaml`` from the AWS Price List bulk API.

Prices are AWS's own published figures rather than numbers typed in by hand, and the file
records the offer URL and AWS's publication date so every estimate the tool prints can say
how old it is.

    python scripts/fetch_prices.py            # all regions
    python scripts/fetch_prices.py --regions eu-west-1 us-east-1

Each region's offer file is around 3.7 MB. They are streamed and parsed one at a time and
never written to disk, because the whole set would be well over a hundred megabytes.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path
from typing import Any

import yaml

PRICING_HOST = "https://pricing.us-east-1.amazonaws.com"
OFFER_INDEX = f"{PRICING_HOST}/offers/v1.0/aws/AmazonSageMaker/current/region_index.json"
OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "sagemaker_idle_finder"
    / "data"
    / "prices.yaml"
)

#: The price-list component that corresponds to a real-time inference endpoint instance.
HOSTING_COMPONENT = "Hosting"
#: The product family covering serverless inference.
SERVERLESS_FAMILY = "ML Serverless"
#: Serverless on-demand usage types look like "EU-ServerlessInf:Mem-4GB": a per-second
#: price for one of the six memory sizes a serverless endpoint can be configured with.
#: Provisioned-concurrency usage types are deliberately not collected — the remedy this
#: tool suggests is plain serverless, which is the configuration that costs nothing idle.
SERVERLESS_ON_DEMAND = re.compile(r"ServerlessInf:Mem-(\d+)GB$")

TIMEOUT_SECONDS = 120


def _get_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
        loaded = json.load(response)
    if not isinstance(loaded, dict):
        raise SystemExit(f"{url} did not return a JSON object")
    return loaded


def _on_demand_price(terms: dict[str, Any], sku: str) -> tuple[float, str] | None:
    """Return the on-demand USD price and unit for a SKU, if it has one."""
    offer = terms.get(sku)
    if not offer:
        return None
    dimensions = next(iter(offer.values()))["priceDimensions"]
    dimension = next(iter(dimensions.values()))
    usd = dimension["pricePerUnit"].get("USD")
    if usd is None:
        return None
    return float(usd), str(dimension["unit"])


def extract(document: dict[str, Any]) -> tuple[dict[str, float], dict[str, float]]:
    """Pull hosting instance prices and serverless prices out of one region's offer file."""
    products = document["products"]
    terms = document["terms"]["OnDemand"]

    hosting: dict[str, float] = {}
    serverless: dict[str, float] = {}

    for sku, product in products.items():
        attributes = product.get("attributes", {})
        priced = _on_demand_price(terms, sku)
        if priced is None:
            continue
        price, unit = priced

        if attributes.get("component") == HOSTING_COMPONENT:
            name = attributes.get("instanceName") or attributes.get("instanceType", "")
            name = name.replace(f"-{HOSTING_COMPONENT}", "")
            if name.startswith("ml.") and price > 0:
                hosting[name] = round(price, 6)

        elif product.get("productFamily") == SERVERLESS_FAMILY and price > 0:
            memory_gb = _serverless_memory_gb(attributes, unit)
            if memory_gb is not None:
                serverless[str(memory_gb)] = round(price, 10)

    return hosting, serverless


def _serverless_memory_gb(attributes: dict[str, Any], unit: str) -> int | None:
    """Return the memory size a serverless on-demand price applies to, if it is one."""
    if unit.lower() not in {"seconds", "second"}:
        return None
    match = SERVERLESS_ON_DEMAND.search(str(attributes.get("usagetype", "")))
    return int(match.group(1)) if match else None


def build(regions: list[str] | None = None) -> dict[str, Any]:
    """Fetch every requested region and assemble the document to write."""
    index = _get_json(OFFER_INDEX)
    available = index["regions"]
    wanted = sorted(regions) if regions else sorted(available)

    missing = [region for region in wanted if region not in available]
    if missing:
        raise SystemExit(f"the price list has no offer for: {', '.join(missing)}")

    hosting: dict[str, dict[str, float]] = {}
    serverless: dict[str, dict[str, float]] = {}
    for region in wanted:
        url = PRICING_HOST + available[region]["currentVersionUrl"]
        print(f"fetching {region} ...", flush=True)
        region_hosting, region_serverless = extract(_get_json(url))
        if region_hosting:
            hosting[region] = dict(sorted(region_hosting.items()))
        if region_serverless:
            serverless[region] = dict(sorted(region_serverless.items()))

    return {
        "schema_version": 1,
        "meta": {
            "source": OFFER_INDEX,
            "offer": "AmazonSageMaker",
            "publication_date": index["publicationDate"],
            "regions_covered": len(hosting),
            "note": (
                "On-demand USD prices published by AWS, fetched by scripts/fetch_prices.py. "
                "Hosting prices are per instance-hour for real-time inference endpoints. "
                "They exclude data transfer, storage and any discount, so every figure this "
                "tool derives from them is an estimate."
            ),
        },
        "hosting_instance_hour_usd": hosting,
        "serverless_on_demand_second_usd_by_memory_gb": serverless,
    }


def main(argv: list[str] | None = None) -> int:
    """Write the price file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regions", nargs="*", help="Regions to fetch. Default: all.")
    arguments = parser.parse_args(argv)

    document = build(arguments.regions)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        yaml.safe_dump(document, sort_keys=False, default_flow_style=False), encoding="utf-8"
    )
    instances = sum(len(values) for values in document["hosting_instance_hour_usd"].values())
    print(
        f"wrote {OUTPUT.name}: {document['meta']['regions_covered']} regions, "
        f"{instances:,} instance prices, {OUTPUT.stat().st_size:,} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
