"""Carl ROTH — SAP Commerce (Hybris) OCC JSON API.

The storefront is an Angular single-page app, so the HTML is a shell. It
declares its backend in a meta tag (``occ-backend-base-url``), and that API
answers unauthenticated JSON. Reading the API instead of driving a headless
browser is faster, far lighter on the origin, and produces a stable contract
rather than CSS selectors that break on the next deploy.

Two wrinkles this source contributes:

* CAS is not a field. It is embedded in a ``characteristics`` HTML blob
  alongside molar mass, density and melting point, all of which use German
  decimal commas — "249,69 g/mol". Parsing that as an English decimal gives
  24969, which would sail through a plausibility check.
* Names use British spelling ("sulphate"), which will not string-match the
  American spelling used by PubChem and Chem-Impex.
"""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import quote

from ..http import PoliteSession
from ..normalize import parse_number
from .base import SupplierProduct

log = logging.getLogger(__name__)

SOURCE = "carlroth"
HOST = "api.carlroth.com"
SITE_HOST = "www.carlroth.com"
BASE = f"https://{HOST}/occ/v2"
BASE_SITE = "carlroth_COM"
MEDIA_BASE = f"https://{SITE_HOST}"

# "CAS No. 7758-99-8", "Molar mass (M) 249,69 g/mol", "EG-Nr. 616-477-9"
_CHAR_FIELD_RES = {
    "cas": re.compile(r"(?i)CAS[\s-]*No\.?\s*([\d-]+)"),
    "ec": re.compile(r"(?i)E[GC][\s-]*Nr\.?\s*([\d-]+)"),
    "un": re.compile(r"(?i)UN[\s-]*Nr\.?\s*(\d+)"),
    "molar_mass": re.compile(r"(?i)Molar\s*mass[^\d]*([\d.,]+)"),
    "density": re.compile(r"(?i)Density[^\d]*([\d.,]+)"),
    "melting_point": re.compile(r"(?i)Melting\s*point[^\d-]*(-?[\d.,]+)"),
    "formula": re.compile(r"(?i)Empirical\s*formula\s*(.+?)(?:<br\s*/?>|$)"),
}


def _strip_tags(text: str) -> str:
    """Remove markup and decode HTML entities.

    Carl ROTH's JSON carries HTML in its text fields, so purity comes through
    as "&ge;99,5 %, p.a., ACS" and a brand name as "ROTI&reg;METIC". Replacing
    a hand-picked list of entities misses the long tail; ``html.unescape``
    handles all of them.
    """
    cleaned = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", html.unescape(cleaned)).strip()


def parse_characteristics(blob: str | None) -> dict[str, str]:
    """Pull labelled values out of the characteristics HTML blob."""
    if not blob:
        return {}
    # Keep <br> as a separator, drop the rest (notably <sub> inside formulas).
    text = re.sub(r"(?i)<br\s*/?>", "\n", blob)
    out: dict[str, str] = {}
    for key, pattern in _CHAR_FIELD_RES.items():
        m = pattern.search(text if key == "formula" else _strip_tags(text))
        if m:
            out[key] = _strip_tags(m.group(1)).strip()
    return out


def search(
    session: PoliteSession, query: str, page_size: int = 40, max_pages: int = 1
) -> list[dict]:
    results: list[dict] = []
    for page in range(max_pages):
        url = (
            f"{BASE}/{BASE_SITE}/products/search?query={quote(query)}"
            f"&pageSize={page_size}&currentPage={page}&lang=en&curr=EUR"
        )
        res = session.get(url, headers={"Accept": "application/json"})
        if res.status != 200:
            break
        payload = res.json()
        batch = payload.get("products", [])
        results.extend(batch)
        pagination = payload.get("pagination", {})
        if page + 1 >= pagination.get("totalPages", 0) or not batch:
            break
    return results


def fetch_product(session: PoliteSession, code: str) -> SupplierProduct | None:
    url = f"{BASE}/{BASE_SITE}/products/{quote(code)}?fields=FULL&lang=en"
    res = session.get(url, headers={"Accept": "application/json"})
    if res.status != 200:
        return None
    d = res.json()

    chars = parse_characteristics(d.get("characteristics"))

    # SDS and specification sheets come in many locales; take English.
    def _english(entries, folder_hint: str) -> list[str]:
        out = []
        for e in entries or []:
            lang = (e.get("language") or {}).get("isocode", "")
            code_ = (e.get("code") or "").upper()
            if lang == "en" or code_.endswith("_EN"):
                out.append(MEDIA_BASE + e["url"] if e.get("url", "").startswith("/") else e.get("url", ""))
        return [u for u in out if u]

    synonyms = []
    if d.get("synonyms"):
        synonyms = [s.strip() for s in re.split(r"[;,]", _strip_tags(d["synonyms"])) if s.strip()]

    hstatements = d.get("hstatements") or ""
    hazards = [h for h in re.split(r"[-,\s]+", _strip_tags(hstatements)) if h.startswith("H")]

    product = SupplierProduct(
        source=SOURCE,
        sku=d.get("code", code),
        name=_strip_tags(d.get("name", "")),
        url=(MEDIA_BASE + d["url"]) if d.get("url", "").startswith("/") else d.get("url", ""),
        cas_raw=chars.get("cas"),
        formula=re.sub(r"\s+", "", chars.get("formula", "")) or None,
        molar_mass=parse_number(chars.get("molar_mass")),
        purity=_strip_tags(d.get("purityLevel")) or None,
        synonyms=synonyms,
        sds_urls=_english(d.get("securityDatasheets"), "Security datasheet"),
        spec_urls=_english(d.get("otherDatasheets"), "Specification"),
        ghs_hazards=hazards,
        extra={
            "ec_number": chars.get("ec"),
            "un_number": chars.get("un"),
            "density": chars.get("density"),
            "melting_point": chars.get("melting_point"),
            "base_product": d.get("baseProduct"),
            "intended_use": _strip_tags(d.get("intendedUse") or "") or None,
        },
    )
    return product.enrich()


def scrape(
    session: PoliteSession, queries: list[str], limit: int | None = None
) -> list[SupplierProduct]:
    """Search each seed term, then pull full detail for every distinct SKU.

    Search results carry no CAS, so detail fetches are unavoidable. Pack-size
    variants of one substance appear as separate codes sharing a
    ``baseProduct``; they are kept as separate SKUs and collapsed later.
    """
    products: list[SupplierProduct] = []
    seen: set[str] = set()

    for query in queries:
        for hit in search(session, query):
            code = hit.get("code")
            if not code or code in seen:
                continue
            seen.add(code)
            try:
                parsed = fetch_product(session, code)
            except Exception as exc:  # noqa: BLE001
                log.warning("carlroth: %s failed: %s", code, exc)
                continue
            if parsed:
                products.append(parsed)
            if limit and len(products) >= limit:
                return products
    return products
