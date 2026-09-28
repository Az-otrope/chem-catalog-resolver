"""Loba Chemie — server-rendered ASP.NET HTML.

The most permissive of the three targets: robots.txt is ``Allow: /`` with an
explicit ``Crawl-delay: 5`` and a block naming crawler user-agents. The
published sitemap lists category pages rather than products, so discovery runs
category page -> product links, with the CAS embedded in the product URL.

Spec data arrives as four-column key/value tables (label, value, label, value)
plus a genuine impurity-limits table, which is the closest thing any of the
three suppliers publishes to a certificate of analysis.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http import PoliteSession
from ..normalize import parse_number
from .base import SupplierProduct

log = logging.getLogger(__name__)

SOURCE = "loba"
HOST = "www.lobachemie.com"
BASE_URL = f"https://{HOST}/"
SITEMAP = f"{BASE_URL}sitemap.xml"

PRODUCT_HREF_RE = re.compile(r"(?i)casno-[\d-]+\.aspx$")

# Categories carrying inorganic salts and hydrates. Restricting discovery to
# these keeps the crawl small and, more importantly, guarantees that the three
# suppliers overlap — matching is only measurable where the same substances
# appear on more than one catalogue.
SALT_CATEGORIES = [
    "Inorganic-Bases",
    "Inorganic-Salts",
    "Inorganic-Acids",
    "HPLC-Buffer-salts",
    "Buffer-Salts",
    "AAS-Standard-solutions",
]


def category_urls(session: PoliteSession, categories: list[str] | None = None) -> list[str]:
    """Pick category pages out of the sitemap by slug."""
    wanted = [c.lower() for c in (categories or SALT_CATEGORIES)]
    res = session.get(SITEMAP)
    locs = re.findall(r"<loc>([^<]+)</loc>", res.text)
    out = []
    for loc in locs:
        slug = loc.rsplit("/", 1)[-1].lower().removesuffix(".aspx")
        if slug in wanted:
            out.append(loc)
    missing = set(wanted) - {u.rsplit("/", 1)[-1].lower().removesuffix(".aspx") for u in out}
    if missing:
        log.info("loba: categories not present in sitemap: %s", sorted(missing))
    return out


def product_urls(session: PoliteSession, category_url: str) -> list[str]:
    res = session.get(category_url)
    soup = BeautifulSoup(res.text, "lxml")
    urls: dict[str, None] = {}
    for a in soup.find_all("a", href=True):
        if PRODUCT_HREF_RE.search(a["href"]):
            urls.setdefault(urljoin(category_url, a["href"]), None)
    return list(urls)


def _kv_pairs(table) -> dict[str, str]:
    """Flatten a (label, value, label, value) row layout into a dict."""
    out: dict[str, str] = {}
    for row in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        for i in range(0, len(cells) - 1, 2):
            label, value = cells[i].strip().rstrip(":"), cells[i + 1].strip()
            if label and value:
                out[label.lower()] = value
    return out


def _clean_formula(raw: str | None) -> str | None:
    """Repair formulas flattened out of <sub> markup.

    "Ba(OH) 2 .8H 2 O" is what ``get_text`` leaves behind. Removing the spaces
    that the subscript tags used to occupy restores "Ba(OH)2.8H2O".
    """
    if not raw:
        return None
    return re.sub(r"\s+", "", raw) or None


def parse_product(html: str, url: str) -> SupplierProduct | None:
    soup = BeautifulSoup(html, "lxml")

    def hidden(name: str) -> str | None:
        el = soup.find("input", {"id": f"ContentPlaceHolder1_{name}"})
        return (el.get("value") or "").strip() if el else None

    sku = hidden("hdnartno")
    cas = hidden("hdncasno")

    h1 = soup.find("h1")
    name = h1.get_text(" ", strip=True) if h1 else ""
    if not name or not sku:
        log.warning("loba: could not parse %s", url)
        return None

    # The second heading, where present, is a trade or common name.
    synonyms = []
    h2 = soup.find("h2")
    if h2:
        alt = h2.get_text(" ", strip=True)
        if alt and alt.lower() != name.lower():
            synonyms.append(alt)

    merged: dict[str, str] = {}
    spec_limits: dict[str, str] = {}
    for idx, table in enumerate(soup.find_all("table")):
        pairs = _kv_pairs(table)
        if not pairs:
            continue
        # The first two tables are identity and physical properties; later
        # tables are the impurity specification.
        if idx < 2 or any(k in pairs for k in ("cas no.", "molecular formula")):
            merged.update(pairs)
        else:
            spec_limits.update(pairs)

    sds_urls = [
        urljoin(url, a["href"])
        for a in soup.find_all("a", href=True)
        if "lab-chemical-msds" in a["href"].lower() and a["href"].upper().endswith("-EN.ASPX")
    ]

    product = SupplierProduct(
        source=SOURCE,
        sku=sku,
        name=name,
        url=url,
        cas_raw=cas or merged.get("cas no."),
        formula=_clean_formula(merged.get("molecular formula")),
        molar_mass=parse_number(merged.get("molecular weight")),
        purity=merged.get("purity"),
        synonyms=synonyms,
        sds_urls=sds_urls,
        extra={
            "grade": merged.get("grade"),
            "hs_code": merged.get("h.s. code"),
            "shelf_life": merged.get("shelf life"),
            "physical_state": merged.get("physical state at 20 °c"),
            "melting_point": merged.get("melting point/ freezing point [°c]"),
            "density": merged.get("density [g/cm3]"),
            "spec_limits": spec_limits,
        },
    )
    return product.enrich()


def _url_cas(url: str) -> str | None:
    m = re.search(r"(?i)casno-(\d{2,7}-\d{2}-\d)", url)
    return m.group(1) if m else None


def select_product_urls(
    urls: list[str],
    wanted_cas: set[str] | None,
    wanted_terms: list[str] | None,
) -> list[str]:
    """Order discovered product URLs so the seed substances come first.

    Loba is crawled by category rather than by search, so taking the first N
    products in a category gives a set that overlaps almost nothing the other
    two suppliers returned for the seed terms. Overlap is not a nicety:
    cross-catalogue matching is only measurable where the same substances
    actually appear in more than one catalogue.

    This *prioritises* rather than filters. Seed matches are taken first, then
    the rest of the category tops up the remaining budget — dropping
    everything else would trade one bias (arbitrary products) for another
    (only products we already knew to look for), and the non-seed rows are
    what exercise the matcher against substances no seed anticipated.

    Ranking is free: the CAS is in the product URL, so no request is needed to
    decide.
    """
    if not wanted_cas and not wanted_terms:
        return urls

    term_tokens = [
        {t for t in term.lower().replace("(", " ").replace(")", " ").split() if len(t) > 3}
        for term in (wanted_terms or [])
    ]
    # Individual words from the seed terms, for a weaker "related substance"
    # signal than a full term match.
    seed_words = {w for group in term_tokens for w in group}

    exact_cas: list[str] = []
    term_match: list[str] = []
    related: list[str] = []
    rest: list[str] = []

    for url in urls:
        cas = _url_cas(url)
        slug_words = set(url.rsplit("/", 1)[-1].lower().replace("-", " ").split())
        if wanted_cas and cas and cas in wanted_cas:
            exact_cas.append(url)
        elif any(group and group <= slug_words for group in term_tokens):
            term_match.append(url)
        elif seed_words & slug_words:
            related.append(url)
        else:
            rest.append(url)

    return exact_cas + term_match + related + rest


def scrape(
    session: PoliteSession,
    categories: list[str] | None = None,
    limit: int | None = None,
    wanted_cas: set[str] | None = None,
    wanted_terms: list[str] | None = None,
) -> list[SupplierProduct]:
    # Collect every candidate across all categories *before* ranking. Ranking
    # per category and then capping would let the first category's filler
    # crowd out the second category's seed matches.
    discovered: dict[str, None] = {}
    for cat in category_urls(session, categories):
        for purl in product_urls(session, cat):
            discovered.setdefault(purl, None)

    candidates = select_product_urls(list(discovered), wanted_cas, wanted_terms)
    log.info("loba: %d product URLs discovered", len(candidates))

    products: list[SupplierProduct] = []
    for purl in candidates:
        try:
            res = session.get(purl)
        except Exception as exc:  # noqa: BLE001 - one bad page must not stop a crawl
            log.warning("loba: fetch failed %s: %s", purl, exc)
            continue
        if res.status != 200:
            continue
        parsed = parse_product(res.text, purl)
        if parsed:
            products.append(parsed)
        if limit and len(products) >= limit:
            break
    return products
