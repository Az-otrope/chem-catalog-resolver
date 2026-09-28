"""CAS Registry Number handling.

A CAS RN looks like ``7758-99-8``: two to seven digits, two digits, then a
single check digit. The check digit is a positional mod-10 checksum, which
makes it cheap to catch transcription errors in scraped data before they
propagate into matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Deliberately anchored and bounded. An unanchored \d+-\d\d-\d will happily
# match fragments of phone numbers, catalogue codes and UN numbers.
CAS_RE = re.compile(r"^(\d{2,7})-(\d{2})-(\d)$")

# For pulling candidates out of free text (SDS bodies, HTML blobs). Word
# boundaries only; callers must still validate the check digit.
CAS_IN_TEXT_RE = re.compile(r"\b\d{2,7}-\d{2}-\d\b")


@dataclass(frozen=True)
class CASCheck:
    raw: str
    normalized: str | None
    well_formed: bool
    checksum_valid: bool

    @property
    def usable(self) -> bool:
        return self.well_formed and self.checksum_valid

    @property
    def reason(self) -> str | None:
        if not self.well_formed:
            return "malformed"
        if not self.checksum_valid:
            return "checksum_failed"
        return None


def check_digit(body: str) -> int:
    """Compute the CAS check digit for the part before the final hyphen.

    Digits are weighted by their position from the right, starting at 1,
    and the result is taken mod 10.

    >>> check_digit("775899")
    8
    """
    digits = [int(c) for c in body if c.isdigit()]
    total = sum(d * i for i, d in enumerate(reversed(digits), start=1))
    return total % 10


def validate(raw: str | None) -> CASCheck:
    """Validate a single CAS string without raising."""
    if raw is None:
        return CASCheck(raw="", normalized=None, well_formed=False, checksum_valid=False)

    candidate = str(raw).strip()
    # Suppliers write these as "CAS 7758-99-8", "CAS-No. 7758-99-8", "[7758-99-8]".
    # Covers "CAS 7758-99-8", "CAS No. ...", "CAS-No. ...", "CAS RN: ...".
    candidate = re.sub(r"(?i)^\s*cas[\s.-]*(rn|no|number)?\.?[:\s-]*", "", candidate)
    candidate = candidate.strip("[]() \t ")
    # Unicode hyphens and non-breaking spaces show up in PDF-extracted text.
    candidate = candidate.replace("‐", "-").replace("‑", "-")
    candidate = candidate.replace("‒", "-").replace("–", "-")
    candidate = candidate.replace(" ", "").replace(" ", "")

    m = CAS_RE.match(candidate)
    if not m:
        return CASCheck(raw=raw, normalized=None, well_formed=False, checksum_valid=False)

    body = m.group(1) + m.group(2)
    expected = check_digit(body)
    ok = expected == int(m.group(3))
    return CASCheck(
        raw=raw,
        normalized=candidate if ok else None,
        well_formed=True,
        checksum_valid=ok,
    )


def is_valid(raw: str | None) -> bool:
    return validate(raw).usable


def normalize(raw: str | None) -> str | None:
    """Return the canonical CAS string, or None if it does not validate."""
    return validate(raw).normalized


def extract_all(text: str, *, valid_only: bool = True) -> list[str]:
    """Pull CAS numbers out of free text, preserving first-seen order.

    ``valid_only`` drops candidates that fail the checksum. That matters when
    scraping SDS PDFs, where UN numbers and EC numbers sit next to the CAS and
    share its shape closely enough to fool a bare regex.
    """
    seen: dict[str, None] = {}
    for candidate in CAS_IN_TEXT_RE.findall(text or ""):
        chk = validate(candidate)
        if valid_only and not chk.usable:
            continue
        key = chk.normalized or candidate
        seen.setdefault(key, None)
    return list(seen)
