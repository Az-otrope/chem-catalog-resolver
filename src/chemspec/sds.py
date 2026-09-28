"""Safety data sheet parsing, for cross-checking the catalogue against itself.

SDS documents follow the GHS 16-section layout, which makes them unusually
tractable for unstructured documents: section 1 is identification, section 3
is composition (where the CAS lives), section 9 is physical properties.

The three suppliers deliver them in two different formats — Loba Chemie serves
HTML pages in eight languages, Carl ROTH and Chem-Impex serve PDFs — so this
module normalises both to plain text before parsing.

The point of reading them is **cross-checking, not enrichment**: if a
supplier's catalogue page says one CAS and its own safety data sheet says
another, that is a defect worth surfacing, and it is invisible unless you read
both.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from . import cas as cas_mod
from .http import PoliteSession
from .normalize import parse_number

log = logging.getLogger(__name__)

SECTION_PATTERNS = {
    "identification": r"(?i)section\s*1\b|^\s*1\.\s|identification of the substance",
    "composition": r"(?i)section\s*3\b|^\s*3\.\s|composition.{0,20}information on ingredients",
    "physical": r"(?i)section\s*9\b|^\s*9\.\s|physical and chemical properties",
    "stability": r"(?i)section\s*10\b|^\s*10\.\s|stability and reactivity",
}

_HEADING_RE = re.compile(
    r"(?im)^\s*(?:section\s*)?(\d{1,2})[.:)]?\s+([A-Za-z][^\n]{3,60})$"
)


@dataclass
class SDSRecord:
    url: str
    fmt: str
    product_name: str | None = None
    cas_numbers: list[str] = field(default_factory=list)
    """CAS numbers found in the composition section, in document order."""
    document_cas: list[str] = field(default_factory=list)
    """Every valid CAS anywhere in the document.

    Kept separately because the composition section of a solution lists its
    solvent too — a dilute hydrochloric acid SDS names water (7732-18-5)
    before the acid itself. Reporting "supplier disagrees with its own SDS"
    off the first CAS found would be a false accusation, so the consistency
    check runs against this wider set.
    """
    primary_cas: str | None = None
    ec_number: str | None = None
    molar_mass: float | None = None
    melting_point: str | None = None
    ph_value: str | None = None
    hazard_codes: list[str] = field(default_factory=list)
    sections: dict[str, str] = field(default_factory=dict)
    parse_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        # Section bodies are large and only useful during debugging.
        d["sections"] = {k: v[:400] for k, v in self.sections.items()}
        return d


def pdf_to_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        raise RuntimeError("pypdf is required to parse PDF safety data sheets")

    reader = PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 - a bad page should not lose the document
            log.debug("pdf page extraction failed: %s", exc)
    return "\n".join(parts)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def split_sections(text: str) -> dict[str, str]:
    """Slice the document at GHS section headings."""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return {}
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[f"{m.group(1)}. {m.group(2).strip()}"] = text[m.start() : end]
    return out


def _find_section(sections: dict[str, str], number: int) -> str:
    for key, body in sections.items():
        if key.split(".")[0].strip() == str(number):
            return body
    return ""


def parse_text(text: str, url: str, fmt: str) -> SDSRecord:
    record = SDSRecord(url=url, fmt=fmt)
    if not text.strip():
        record.parse_errors.append("empty_text")
        return record

    record.sections = split_sections(text)

    identification = _find_section(record.sections, 1) or text[:3000]
    composition = _find_section(record.sections, 3)
    physical = _find_section(record.sections, 9)

    record.document_cas = cas_mod.extract_all(text)

    # Prefer the CAS stated in section 3, which is the authoritative one for
    # the substance itself; section 1 sometimes carries a product-family CAS.
    for scope in (composition, identification, text):
        found = cas_mod.extract_all(scope)
        if found:
            record.cas_numbers = found
            record.primary_cas = found[0]
            break
    if not record.document_cas:
        record.parse_errors.append("no_valid_cas_found")

    m = re.search(r"(?i)\bEC[\s-]*(?:No|Number|Nr)\.?\s*[:\s]*([\d-]{7,12})", text)
    if m:
        record.ec_number = m.group(1)

    m = re.search(r"(?i)(?:molar|molecular)\s*(?:mass|weight)[^\d]{0,20}([\d.,]+)", text)
    if m:
        record.molar_mass = parse_number(m.group(1))

    scope = physical or text
    m = re.search(r"(?i)melting\s*point[^\n:]*[:\s]\s*([^\n]{1,40})", scope)
    if m:
        record.melting_point = m.group(1).strip()
    m = re.search(r"(?i)\bpH\b[^\n:]*[:\s]\s*([^\n]{1,30})", scope)
    if m:
        record.ph_value = m.group(1).strip()

    # Tried in priority order. "Product form" sits right above "Product name"
    # in the GHS section 1 layout and its value is the useless word
    # "Substance", so a loose pattern reliably picks the wrong line.
    for pattern in (
        r"(?i)\bproduct\s*name\s*[:\-]\s*([^\n]{2,90})",
        r"(?i)\btrade\s*name\s*[:\-]\s*([^\n]{2,90})",
        r"(?i)\bsubstance\s*name\s*[:\-]\s*([^\n]{2,90})",
        r"(?i)\bproduct\s*identifier\s*[:\-]\s*([^\n]{2,90})",
    ):
        m = re.search(pattern, identification)
        if m:
            value = m.group(1).strip(" :\t")
            if value.lower() not in {"substance", "mixture", "", "-"}:
                record.product_name = value
                break

    record.hazard_codes = sorted(set(re.findall(r"\bH\d{3}[A-Za-z]?\b", text)))
    return record


def sniff_format(content: bytes) -> str:
    """Decide PDF vs HTML from the bytes, never from the URL.

    Loba Chemie serves its safety data sheets from ``.aspx`` URLs that return
    a PDF body. Trusting the extension meant decoding PDF bytes as text and
    extracting nothing at all — a silent failure, since an SDS with no CAS in
    it looks like a finding rather than a bug.
    """
    head = content[:1024].lstrip()
    if head.startswith(b"%PDF"):
        return "pdf"
    return "html"


def fetch_and_parse(session: PoliteSession, url: str) -> SDSRecord | None:
    try:
        res = session.get(url, binary=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("sds: fetch failed %s: %s", url, exc)
        return None
    if res.status != 200:
        log.info("sds: %s returned %s", url, res.status)
        return None

    fmt = sniff_format(res.content)
    if fmt == "pdf":
        try:
            text = pdf_to_text(res.content)
        except Exception as exc:  # noqa: BLE001
            log.warning("sds: pdf parse failed %s: %s", url, exc)
            return SDSRecord(url=url, fmt="pdf", parse_errors=[f"pdf_error:{exc}"])
        return parse_text(text, url, "pdf")

    html = res.content.decode("utf-8", errors="replace")
    return parse_text(html_to_text(html), url, "html")
