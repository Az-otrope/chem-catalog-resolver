"""Common record shape that every source is normalised into."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from .. import cas as cas_mod
from .. import normalize as norm


@dataclass
class SupplierProduct:
    """One catalogue row, as offered for sale.

    A supplier row is a SKU, not a substance. Loba lists ammonia solution three
    times under three product codes with one CAS between them; Carl ROTH lists
    the same salt in four pack sizes. Collapsing SKUs to substances is part of
    the reconciliation, so the SKU identity is preserved here rather than
    deduplicated away at scrape time.
    """

    source: str
    sku: str
    name: str
    url: str = ""
    cas_raw: str | None = None
    formula: str | None = None
    molar_mass: float | None = None
    purity: str | None = None
    synonyms: list[str] = field(default_factory=list)
    sds_urls: list[str] = field(default_factory=list)
    spec_urls: list[str] = field(default_factory=list)
    ghs_hazards: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    # Derived, filled by ``enrich``.
    cas: str | None = None
    cas_problem: str | None = None
    base_name: str = ""
    token_form: str = ""
    hydration: float | None = None
    hydration_source: str | None = None
    block: str = ""

    def enrich(self) -> "SupplierProduct":
        check = cas_mod.validate(self.cas_raw)
        self.cas = check.normalized
        self.cas_problem = check.reason if self.cas_raw else "missing"

        parsed = norm.parse(self.name)
        self.base_name = parsed.base
        # Oxidation states are split into their own tokens for scoring:
        # a supplier's "copper(ii) sulfate" and PubChem's "Copper Sulfate"
        # share no tokens at all while "(ii)" stays glued to "copper".
        self.token_form = " ".join(parsed.tokens)
        self.hydration = parsed.hydration
        self.hydration_source = parsed.hydration_source
        self.block = norm.blocking_key(parsed)
        return self

    @property
    def key(self) -> str:
        return f"{self.source}:{self.sku}"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class ReferenceSubstance:
    """A canonical substance from a public reference database."""

    source: str
    ref_id: str
    name: str
    cas: str | None = None
    all_cas: list[str] = field(default_factory=list)
    formula: str | None = None
    molar_mass: float | None = None
    inchikey: str | None = None
    synonyms: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    base_name: str = ""
    token_form: str = ""
    hydration: float | None = None
    hydration_source: str | None = None
    block: str = ""
    alt_blocks: list[str] = field(default_factory=list)

    def enrich(self) -> "ReferenceSubstance":
        parsed = norm.parse(self.name)
        self.base_name = parsed.base
        self.token_form = " ".join(parsed.tokens)
        self.hydration = parsed.hydration
        self.hydration_source = parsed.hydration_source
        self.block = norm.blocking_key(parsed)

        # See normalize.infer_anhydrous: reference databases state hydration on
        # the hydrate and stay silent on the anhydrous form, so silence plus a
        # hydrogen-free formula is read as anhydrous rather than as unknown.
        if norm.infer_anhydrous(self.hydration, self.formula):
            self.hydration = 0.0
            self.hydration_source = "inferred_no_hydrogen"

        # Trade names ("Coppervitriole") share no tokens with the IUPAC title,
        # so a reference is indexed under its synonyms' blocking keys as well
        # as its own. Without this, a supplier listing a substance only by its
        # trade name never reaches the scoring stage at all.
        alt: dict[str, None] = {}
        for synonym in self.synonyms[:20]:
            key = norm.blocking_key(norm.parse(synonym))
            if key and key != self.block:
                alt.setdefault(key, None)
        self.alt_blocks = list(alt)
        return self

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
