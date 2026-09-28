"""Normalising supplier product names into something comparable.

The central claim of this project is that hydration state is *not* noise.
"Copper(II) sulfate" is 7758-98-7 and "Copper(II) sulfate pentahydrate" is
7758-99-8 — different substances, different specifications, different mass per
mole of copper. A fuzzy matcher that strips "pentahydrate" as a descriptive
modifier will merge them at a similarity score above 0.9, and the merge is
silent. So hydration state is parsed out and kept as a structured field that
the matcher treats as a hard gate rather than as tokens to be scored.

Everything else in a supplier product name — pack size, purity grade,
pharmacopoeia references, British spelling — genuinely is noise and gets
stripped.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

HYDRATE_PREFIXES: dict[str, float] = {
    "anhydrous": 0.0,
    "hemi": 0.5,
    "sesqui": 1.5,
    "mono": 1.0,
    "di": 2.0,
    "bi": 2.0,
    "tri": 3.0,
    "tetra": 4.0,
    "penta": 5.0,
    "hexa": 6.0,
    "hepta": 7.0,
    "octa": 8.0,
    "nona": 9.0,
    "deca": 10.0,
    "undeca": 11.0,
    "dodeca": 12.0,
}

# Ordered longest-first so "undeca" is tried before "deca", "sesqui" before "qui".
_PREFIX_ALTERNATION = "|".join(
    sorted((p for p in HYDRATE_PREFIXES if p != "anhydrous"), key=len, reverse=True)
)

_WORD_HYDRATE_RE = re.compile(rf"(?i)\b({_PREFIX_ALTERNATION})\s*-?\s*hydrate\b")
_ANHYDROUS_RE = re.compile(r"(?i)\banhydrous\b|\bwasserfrei\b|\bsicc\b")
_NUMERIC_HYDRATE_RE = re.compile(r"(?i)\b(\d+(?:[.,]\d+)?)\s*-?\s*hydrate\b")
# "· 5 H2O", ".5H2O", "x 5 H2O", "*5H2O", "(H2O)5"
_DOT_HYDRATE_RE = re.compile(
    r"(?i)[·‧⋅∙*x×.]\s*(\d+(?:[.,]\d+)?)?\s*h\s*2\s*o"
)
_PAREN_HYDRATE_RE = re.compile(r"(?i)\(\s*h\s*2\s*o\s*\)\s*(\d+(?:[.,]\d+)?)")
_BARE_HYDRATE_RE = re.compile(r"(?i)\bhydrate(d)?\b")
# PubChem's placeholder for "a hydrate, count unspecified": "Calcium chloride
# xhydrate" (22691-02-7). Note there is no word boundary inside "xhydrate",
# so the bare-hydrate pattern above does not see it.
_PLACEHOLDER_HYDRATE_RE = re.compile(r"(?i)\bx\s*-?\s*hydrate\b")

# Pack sizes: "2.5 kg", "1 l", "500 g", "100ml", "5 x 1 l"
_PACK_SIZE_RE = re.compile(
    r"(?i)(?:\b\d+\s*[x×]\s*)?\b\d+(?:[.,]\d+)?\s*"
    r"(?:mg|g|kg|t|ml|l|ltr|litre|liter|mmol|mol|pcs|pieces|units?)\b\.?"
)

# Purity / grade / regulatory boilerplate.
_GRADE_PATTERNS = [
    r"(?i)\b(?:ar|lr|gr|acs|usp|nf|bp|ep|jp|fcc|iso|din|reag)\b\.?",
    r"(?i)\bph\.?\s*eur\.?\b",
    r"(?i)\bp\.?\s*a\.?\b",
    r"(?i)\bfor\s+(?:analysis|synthesis|hplc|gc|spectroscopy|biochemistry|"
    r"molecular\s+biology|electrophoresis|microbiology)\b",
    r"(?i)\b(?:extra\s+)?pure\b",
    r"(?i)\bpurified\b",
    r"(?i)\bpractical\s+grade\b",
    r"(?i)\btechnical\s+grade\b",
    r"(?i)\breagent\s+grade\b",
    r"(?i)\banalytical\s+(?:reagent|grade)\b",
    r"(?i)\bcrystalline\b|\bcrystals?\b|\bpowder\b|\bgranular\b|\bgranules\b",
    r"(?i)\bpellets?\b|\bflakes?\b|\blumps?\b|\bbeads?\b|\bsolution\b",
    r"(?i)\bmin\.?\b|\bmax\.?\b|\bapprox\.?\b",
    r"(?i)[<>≥≤]=?\s*\d+(?:[.,]\d+)?\s*%",
    r"(?i)\b\d+(?:[.,]\d+)?\s*%",
    r"(?i)\bchapter\s+[\d.]+\b",
    r"(?i)\bcell\s+culture\s+tested\b",
    r"(?i)\bendotoxin\s+free\b",
]
_GRADE_RES = [re.compile(p) for p in _GRADE_PATTERNS]

# British/American and Latinate/Stock-name variants. Applied on the
# token-normalised string, so keys are lowercase and space-delimited.
_SYNONYMS: dict[str, str] = {
    "sulphate": "sulfate",
    "sulphite": "sulfite",
    "sulphide": "sulfide",
    "sulphuric": "sulfuric",
    "thiosulphate": "thiosulfate",
    "bisulphate": "bisulfate",
    "bisulphite": "bisulfite",
    "persulphate": "persulfate",
    "disulphide": "disulfide",
    "aluminium": "aluminum",
    "caesium": "cesium",
    "sulphur": "sulfur",
    "phosphorous acid": "phosphonic acid",
    "natrium": "sodium",
    "kalium": "potassium",
    "cupric": "copper(ii)",
    "cuprous": "copper(i)",
    "ferric": "iron(iii)",
    "ferrous": "iron(ii)",
    "stannic": "tin(iv)",
    "stannous": "tin(ii)",
    "plumbic": "lead(iv)",
    "plumbous": "lead(ii)",
    "mercuric": "mercury(ii)",
    "mercurous": "mercury(i)",
    "manganous": "manganese(ii)",
    "cobaltous": "cobalt(ii)",
    "nickelous": "nickel(ii)",
    "chromic": "chromium(iii)",
}

_ROMAN = {
    "1": "i", "2": "ii", "3": "iii", "4": "iv",
    "5": "v", "6": "vi", "7": "vii", "8": "viii",
}


@dataclass(frozen=True)
class ParsedName:
    """A supplier product name decomposed into matchable parts."""

    raw: str
    base: str
    """Normalised name with hydration state, pack size and grade removed."""
    hydration: float | None
    """Waters of crystallisation. 0.0 means explicitly anhydrous.
    None means the name says nothing — which is not the same as anhydrous."""
    hydration_source: str | None
    tokens: tuple[str, ...] = field(default=())

    @property
    def hydration_key(self) -> str:
        """Blocking/gating key for hydration state.

        ``unspecified`` is kept distinct from ``0`` on purpose: many suppliers
        omit "anhydrous" on a product that is in fact anhydrous, so treating
        silence as zero would create false merges in one direction and false
        splits in the other. Unspecified is allowed to match anything, but the
        match is flagged rather than auto-accepted.
        """
        if self.hydration is None:
            return "unspecified"
        if self.hydration == 0:
            return "0"
        return f"{self.hydration:g}"


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _strip_markup(text: str) -> str:
    """Drop tags and decode entities.

    Supplier text fields carry HTML: "&ge;99,5 %", "ROTI&reg;METIC". The
    comparison operators are spelled out rather than unescaped to their
    Unicode glyphs, because the grade-stripping patterns below match on ASCII.
    """
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = text.replace("&ge;", ">=").replace("&le;", "<=")
    return html.unescape(text)


def extract_hydration(name: str) -> tuple[str, float | None, str | None]:
    """Strip hydration markers from a name and return the count they encoded.

    Returns ``(name_without_markers, waters, source)``. ``waters`` is None when
    the name is silent about hydration.
    """
    working = name
    waters: float | None = None
    source: str | None = None

    def _num(raw: str) -> float:
        return float(raw.replace(",", "."))

    # Checked first: the placeholder means "hydrated, count unknown", which is
    # different from both a stated count and from silence. It is removed from
    # the name so the base matches the specific hydrates it stands for, but
    # ``waters`` stays None so the gate never fires against it.
    if _PLACEHOLDER_HYDRATE_RE.search(working):
        return _PLACEHOLDER_HYDRATE_RE.sub(" ", working), None, "placeholder_hydrate"

    m = _PAREN_HYDRATE_RE.search(working)
    if m:
        waters, source = _num(m.group(1)), "formula_paren"
        working = _PAREN_HYDRATE_RE.sub(" ", working)

    if waters is None:
        m = _DOT_HYDRATE_RE.search(working)
        # Guard: a bare "." before H2O is only a hydrate dot if a count follows
        # or the preceding char is not a digit (avoids mangling "2.5 H2O" runs).
        if m:
            waters = _num(m.group(1)) if m.group(1) else 1.0
            source = "formula_dot"
            working = _DOT_HYDRATE_RE.sub(" ", working)

    if waters is None:
        m = _NUMERIC_HYDRATE_RE.search(working)
        if m:
            waters, source = _num(m.group(1)), "numeric_word"
            working = _NUMERIC_HYDRATE_RE.sub(" ", working)

    if waters is None:
        m = _WORD_HYDRATE_RE.search(working)
        if m:
            waters = HYDRATE_PREFIXES[m.group(1).lower()]
            source = "prefix_word"
            working = _WORD_HYDRATE_RE.sub(" ", working)

    if waters is None and _ANHYDROUS_RE.search(working):
        waters, source = 0.0, "anhydrous_word"
        working = _ANHYDROUS_RE.sub(" ", working)

    if waters is None and _BARE_HYDRATE_RE.search(working):
        # "Calcium chloride hydrate" — hydrated, count unknown. Distinct from
        # both anhydrous and from silence, so it is recorded as NaN-ish 1.0
        # with a source that the matcher can downgrade on.
        waters, source = 1.0, "bare_hydrate"
        working = _BARE_HYDRATE_RE.sub(" ", working)

    return working, waters, source


def normalize_name(name: str) -> str:
    """Lowercase, de-accent, unify spellings and collapse punctuation."""
    text = _strip_markup(name or "")
    text = _strip_accents(text).lower()

    # Roman-numeral oxidation states: copper (2) -> copper(ii)
    text = re.sub(
        r"\(\s*([1-8])\s*\+?\s*\)",
        lambda m: f"({_ROMAN[m.group(1)]})",
        text,
    )
    text = re.sub(r"\(\s*(i{1,3}v?|vi{0,3}|iv)\s*\)", r"(\1)", text)

    for pattern in _GRADE_RES:
        text = pattern.sub(" ", text)
    text = _PACK_SIZE_RE.sub(" ", text)

    for src, dst in _SYNONYMS.items():
        text = re.sub(rf"\b{re.escape(src)}\b", dst, text)

    # Keep parentheses (oxidation states) and hyphens inside words; drop the rest.
    text = re.sub(r"[^a-z0-9()\-\s]", " ", text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s+", " ", text).strip(" -,")
    return text


def parse(name: str) -> ParsedName:
    """Full decomposition of a supplier product name."""
    stripped, waters, source = extract_hydration(_strip_markup(name or ""))
    base = normalize_name(stripped)
    return ParsedName(
        raw=name or "",
        base=base,
        hydration=waters,
        hydration_source=source,
        tokens=tuple(t for t in base.replace("(", " ").replace(")", " ").split() if t),
    )


def blocking_key(parsed: ParsedName) -> str:
    """Cheap key to bucket candidates before doing pairwise scoring.

    Uses the two longest alphabetic tokens of the base name. Long tokens carry
    the element and anion ("sodium", "chloride"); short ones are usually
    oxidation states or stray fragments. Sorting makes the key order-free, so
    "sodium chloride" and "chloride, sodium" land in the same bucket.
    """
    alpha = [t for t in parsed.tokens if t.isalpha() and len(t) > 2]
    if not alpha:
        return parsed.base[:12]
    picked = sorted(sorted(alpha, key=len, reverse=True)[:2])
    return " ".join(picked)


_FORMULA_TOKEN_RE = re.compile(r"([A-Z][a-z]?)(\d*)|(\()|(\)(\d*))")
_HYDRATE_SEPARATORS = "·⋅∙*.•"


def _parse_formula_part(part: str) -> dict[str, int]:
    """Parse one dot-free formula fragment, honouring parenthesised groups."""
    counts: dict[str, int] = {}
    stack: list[dict[str, int]] = [counts]

    for match in _FORMULA_TOKEN_RE.finditer(part):
        symbol, digits, open_paren, close_paren, close_digits = match.groups()
        if symbol:
            n = int(digits) if digits else 1
            stack[-1][symbol] = stack[-1].get(symbol, 0) + n
        elif open_paren:
            stack.append({})
        elif close_paren:
            if len(stack) == 1:
                continue  # unbalanced; ignore rather than throw on scraped text
            group = stack.pop()
            multiplier = int(close_digits) if close_digits else 1
            for element, n in group.items():
                stack[-1][element] = stack[-1].get(element, 0) + n * multiplier

    # Fold any unclosed groups back in so a malformed formula still yields counts.
    while len(stack) > 1:
        group = stack.pop()
        for element, n in group.items():
            stack[-1][element] = stack[-1].get(element, 0) + n
    return counts


def formula_elements(formula: str | None) -> dict[str, int]:
    """Total element counts for a molecular formula.

    Handles the three notations these sources actually emit:

    * conventional, from suppliers — ``CuSO4``
    * Hill order, from PubChem — ``CuO4S``
    * hydrate dot notation, from both — ``MgSO4.7H2O``, ``Ba(OH)2 · 8H2O``

    All three must reduce to the same element counts, because comparing them
    as strings is what makes a correct match look like a contradiction.

    >>> formula_elements("MgSO4.7H2O") == formula_elements("MgH14O11S")
    True
    """
    if not formula:
        return {}

    text = re.sub(r"\s+", "", str(formula))
    # Normalise every hydrate separator to a single ASCII dot.
    for sep in _HYDRATE_SEPARATORS:
        text = text.replace(sep, ".")

    totals: dict[str, int] = {}
    for part in text.split("."):
        if not part:
            continue
        # A leading integer multiplies the whole fragment: the "7" in "7H2O".
        lead = re.match(r"^(\d+)(.*)$", part)
        multiplier, body = (int(lead.group(1)), lead.group(2)) if lead else (1, part)
        if not body:
            continue
        for element, n in _parse_formula_part(body).items():
            totals[element] = totals.get(element, 0) + n * multiplier
    return totals


def infer_anhydrous(name_hydration: float | None, formula: str | None) -> bool:
    """True when a silent name plus a hydrogen-free formula implies anhydrous.

    This exists because of an asymmetry in how reference databases name things.
    PubChem's record for the pentahydrate is titled "Copper(II) sulfate
    pentahydrate", but its record for the anhydrous salt is titled simply
    "Copper(II) sulfate" — it does not say "anhydrous". So the hydrate states
    its hydration and the anhydrous form stays silent, and a gate that only
    fires when *both* sides state a value would never fire against the very
    record it most needs to.

    A formula containing no hydrogen at all is positive evidence of an
    anhydrous inorganic salt, not an absence of evidence. Where hydrogen is
    present the question is genuinely open — sodium hydroxide has hydrogen and
    is anhydrous — so this returns False and the record stays unspecified.
    """
    if name_hydration is not None:
        return False
    elements = formula_elements(formula)
    return bool(elements) and "H" not in elements


def parse_number(raw: str | None) -> float | None:
    """Parse a numeric field that may use a European decimal comma.

    Carl ROTH returns "249,69 g/mol"; PubChem returns "249.69". Getting this
    wrong turns a molar mass into a hundredfold error that silently passes a
    tolerance check in the wrong direction.
    """
    if raw is None:
        return None
    text = _strip_markup(str(raw))
    m = re.search(r"[-+]?\d[\d\s.,]*", text)
    if not m:
        return None
    token = m.group(0).strip().replace(" ", "").replace(" ", "")

    if "," in token and "." in token:
        # Whichever separator is rightmost is the decimal point.
        token = (
            token.replace(".", "").replace(",", ".")
            if token.rfind(",") > token.rfind(".")
            else token.replace(",", "")
        )
    elif "," in token:
        head, _, tail = token.rpartition(",")
        # "1,234" is ambiguous; 3 trailing digits with a leading group reads as
        # a thousands separator, anything else as a decimal comma.
        token = head + "." + tail if len(tail) != 3 or not head else head + tail
    try:
        return float(token)
    except ValueError:
        return None
