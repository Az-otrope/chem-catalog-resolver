"""Chem-Impex — Shopify storefront, server-rendered.

The most cooperative target of the three: robots.txt is ``Allow: /`` with no
rule touching products or search, and the store publishes a
``sitemap_agentic_discovery.xml`` alongside its product sitemaps — an explicit
invitation to automated clients.

This source earns its place for one reason beyond being a third catalogue: it
publishes the **PubChem CID** on the product page. That is an independent
identifier linking the supplier's row to the canonical spine, which makes it
possible to score the name-based matcher against ground truth the matcher
never sees. See ``evaluate.py``.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from ..http import PoliteSession
from ..normalize import parse_number
from .base import SupplierProduct

log = logging.getLogger(__name__)

SOURCE = "chemimpex"
HOST = "www.chemimpex.com"
BASE_URL = f"https://{HOST}"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _clean_formula(raw: str | None) -> str | None:
    """"C 6 H 13 NO 4" -> "C6H13NO4"."""
    if not raw:
        return None
    return re.sub(r"\s+", "", raw) or None


def search(session: PoliteSession, query: str, limit: int = 10) -> list[dict]:
    url = (
        f"{BASE_URL}/search/suggest.json?q={quote(query)}"
        f"&resources%5Btype%5D=product&resources%5Blimit%5D={limit}"
    )
    res = session.get(url, headers={"Accept": "application/json"})
    if res.status != 200:
        return []
    try:
        return res.json()["resources"]["results"]["products"]
    except (KeyError, ValueError):
        return []


def parse_product(html: str, url: str) -> SupplierProduct | None:
    soup = BeautifulSoup(html, "lxml")

    fields: dict[str, str] = {}
    labels = soup.select(".panel-content-item-label")
    values = soup.select(".panel-content-item-value")
    for label, value in zip(labels, values):
        key = _clean(label.get_text(" ", strip=True)).rstrip(":").lower()
        if key and key not in fields:
            fields[key] = _clean(value.get_text(" ", strip=True))

    cas_el = soup.select_one(".cas-number-value")
    cas = _clean(cas_el.get_text(strip=True)) if cas_el else fields.get("cas number")

    title = soup.find("meta", {"property": "og:title"})
    name = _clean(title["content"]) if title and title.get("content") else ""
    if not name and soup.title:
        name = _clean(soup.title.get_text()).removesuffix(" – Chem-Impex").strip()

    sku = url.rstrip("/").rsplit("/", 1)[-1]
    if not name:
        log.warning("chemimpex: no name for %s", url)
        return None

    synonyms = [
        s.strip()
        for s in re.split(r"[;,\n]", fields.get("synonyms", ""))
        if s.strip()
    ]

    sds_urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"(?i)_sds\.pdf|sds\.pdf", href):
            sds_urls.append(urljoin(BASE_URL, href if href.startswith("http") else href))

    product = SupplierProduct(
        source=SOURCE,
        sku=sku,
        name=name,
        url=url,
        cas_raw=cas,
        formula=_clean_formula(fields.get("molecular formula")),
        molar_mass=parse_number(fields.get("molecular weight")),
        purity=fields.get("purity"),
        synonyms=synonyms,
        sds_urls=sds_urls,
        extra={
            # Published cross-reference to the canonical spine. Held out of
            # matching and used only to score it.
            "pubchem_cid": fields.get("pubchem id"),
            "mdl_number": fields.get("mdl number"),
            "melting_point": fields.get("melting point"),
            "appearance": fields.get("appearance"),
            "storage": fields.get("conditions"),
            "prop65": fields.get("prop 65 warning"),
        },
    )
    return product.enrich()


def fetch_product(session: PoliteSession, handle_or_url: str) -> SupplierProduct | None:
    url = (
        handle_or_url
        if handle_or_url.startswith("http")
        else f"{BASE_URL}/products/{handle_or_url}"
    )
    res = session.get(url)
    if res.status != 200:
        return None
    return parse_product(res.text, url)


def scrape(
    session: PoliteSession, queries: list[str], limit: int | None = None
) -> list[SupplierProduct]:
    products: list[SupplierProduct] = []
    seen: set[str] = set()

    for query in queries:
        for hit in search(session, query):
            url = hit.get("url") or ""
            if url and not url.startswith("http"):
                url = urljoin(BASE_URL, url)
            # Strip Shopify variant/tracking query strings.
            url = url.split("?")[0]
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                parsed = fetch_product(session, url)
            except Exception as exc:  # noqa: BLE001
                log.warning("chemimpex: %s failed: %s", url, exc)
                continue
            if parsed:
                products.append(parsed)
            if limit and len(products) >= limit:
                return products
    return products
