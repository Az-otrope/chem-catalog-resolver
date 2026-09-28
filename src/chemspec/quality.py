"""Data-quality findings across the harvested catalogues.

These are the checks that make the dataset worth more than the sum of the
pages it came from. A single supplier page looks fine in isolation; defects
only become visible when you hold it against its own safety data sheet, against
the other suppliers, and against a public reference.

Findings carry a severity because they are not equivalent. A CAS that fails its
check digit is unambiguously wrong. A molar mass that disagrees with PubChem by
3% probably means the supplier quoted the hydrate's mass against the anhydrous
CAS — also wrong, but requiring a judgement call about which field is at fault.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from . import cas as cas_mod
from .match import MatchResult
from .sds import SDSRecord
from .sources.base import ReferenceSubstance, SupplierProduct

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class Finding:
    check: str
    severity: str
    source: str
    subject: str
    detail: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def check_cas_integrity(products: Iterable[SupplierProduct]) -> list[Finding]:
    findings = []
    for p in products:
        if not p.cas_raw:
            findings.append(
                Finding(
                    check="cas_missing",
                    severity="warning",
                    source=p.source,
                    subject=p.key,
                    detail=f"No CAS published for {p.name!r}",
                    evidence={"url": p.url},
                )
            )
            continue
        chk = cas_mod.validate(p.cas_raw)
        if not chk.well_formed:
            findings.append(
                Finding(
                    check="cas_malformed",
                    severity="error",
                    source=p.source,
                    subject=p.key,
                    detail=f"CAS {p.cas_raw!r} is not a well-formed registry number",
                    evidence={"url": p.url, "raw": p.cas_raw},
                )
            )
        elif not chk.checksum_valid:
            findings.append(
                Finding(
                    check="cas_checksum_failed",
                    severity="error",
                    source=p.source,
                    subject=p.key,
                    detail=(
                        f"CAS {p.cas_raw!r} is well-formed but fails its check digit — "
                        f"almost certainly a transcription error"
                    ),
                    evidence={"url": p.url, "raw": p.cas_raw},
                )
            )
    return findings


def check_hydration_consistency(products: Iterable[SupplierProduct]) -> list[Finding]:
    """A name claiming a hydrate against a formula with no water, or vice versa."""
    findings = []
    for p in products:
        if p.hydration is None or not p.formula:
            continue
        formula_has_water = "h2o" in p.formula.lower().replace(" ", "")
        if p.hydration > 0 and not formula_has_water:
            findings.append(
                Finding(
                    check="hydration_name_formula_mismatch",
                    severity="warning",
                    source=p.source,
                    subject=p.key,
                    detail=(
                        f"Name implies {p.hydration:g} waters but formula "
                        f"{p.formula!r} contains none"
                    ),
                    evidence={"url": p.url, "name": p.name, "formula": p.formula},
                )
            )
        elif p.hydration == 0 and formula_has_water:
            findings.append(
                Finding(
                    check="anhydrous_with_water_in_formula",
                    severity="error",
                    source=p.source,
                    subject=p.key,
                    detail=f"Named anhydrous but formula {p.formula!r} contains water",
                    evidence={"url": p.url, "name": p.name, "formula": p.formula},
                )
            )
    return findings


def check_catalogue_vs_sds(
    products: Iterable[SupplierProduct], sds_by_url: dict[str, SDSRecord]
) -> list[Finding]:
    """The highest-value check: a supplier disagreeing with its own SDS."""
    findings = []
    for p in products:
        for url in p.sds_urls:
            record = sds_by_url.get(url)
            if record is None:
                continue
            document_cas = record.document_cas or record.cas_numbers
            if not document_cas:
                findings.append(
                    Finding(
                        check="sds_no_cas",
                        severity="info",
                        source=p.source,
                        subject=p.key,
                        detail=f"No valid CAS extracted from SDS ({record.fmt})",
                        evidence={"sds_url": url, "errors": record.parse_errors},
                    )
                )
                continue
            if not p.cas:
                continue

            if p.cas not in document_cas:
                # The catalogue's registry number appears nowhere in the whole
                # document. That is a real conflict.
                findings.append(
                    Finding(
                        check="catalogue_sds_cas_conflict",
                        severity="error",
                        source=p.source,
                        subject=p.key,
                        detail=(
                            f"Catalogue lists CAS {p.cas}, which appears nowhere in "
                            f"its own SDS (which states {document_cas[:3]})"
                        ),
                        evidence={
                            "url": p.url,
                            "sds_url": url,
                            "catalogue_cas": p.cas,
                            "sds_cas": document_cas[:5],
                        },
                    )
                )
            elif record.primary_cas and p.cas != record.primary_cas:
                # Present, but not the first component listed. Normal for
                # solutions, where the solvent leads the composition table —
                # a dilute acid's SDS names water before the acid. Worth
                # noting, never an error.
                findings.append(
                    Finding(
                        check="sds_primary_cas_is_another_component",
                        severity="info",
                        source=p.source,
                        subject=p.key,
                        detail=(
                            f"SDS lists {record.primary_cas} before the catalogue's "
                            f"{p.cas}; typical of a solution listing its solvent first"
                        ),
                        evidence={
                            "url": p.url,
                            "sds_url": url,
                            "catalogue_cas": p.cas,
                            "sds_cas": document_cas[:5],
                        },
                    )
                )
    return findings


def check_cross_supplier_conflicts(
    products: Iterable[SupplierProduct],
) -> list[Finding]:
    """Same normalised name and hydration state, different CAS across suppliers."""
    grouped: dict[tuple[str, str], list[SupplierProduct]] = defaultdict(list)
    for p in products:
        if p.cas and p.base_name:
            grouped[(p.base_name, str(p.hydration))].append(p)

    findings = []
    for (name, hydration), group in grouped.items():
        distinct = {p.cas for p in group if p.cas}
        if len(distinct) > 1:
            sources = sorted({p.source for p in group})
            if len(sources) < 2:
                continue
            findings.append(
                Finding(
                    check="cross_supplier_cas_conflict",
                    severity="error",
                    source="+".join(sources),
                    subject=name,
                    detail=(
                        f"{name!r} (hydration={hydration}) is listed under "
                        f"{len(distinct)} different CAS numbers across suppliers"
                    ),
                    evidence={
                        "cas_numbers": sorted(distinct),
                        "skus": [f"{p.key}={p.cas}" for p in group][:8],
                    },
                )
            )
    return findings


def check_against_reference(
    products: Iterable[SupplierProduct],
    references: dict[str, ReferenceSubstance],
    results: Iterable[MatchResult],
    mass_tolerance: float = 0.02,
) -> list[Finding]:
    """Compare accepted matches against the canonical spine."""
    by_key = {p.key: p for p in products}
    findings = []
    for res in results:
        if not res.reference_id or res.reference_id not in references:
            continue
        product = by_key.get(res.product_key)
        ref = references[res.reference_id]
        if product is None:
            continue

        if product.molar_mass and ref.molar_mass:
            delta = abs(product.molar_mass - ref.molar_mass) / max(
                product.molar_mass, ref.molar_mass
            )
            if delta > mass_tolerance:
                findings.append(
                    Finding(
                        check="molar_mass_vs_reference",
                        severity="warning",
                        source=product.source,
                        subject=product.key,
                        detail=(
                            f"Molar mass {product.molar_mass} differs from PubChem "
                            f"{ref.molar_mass} by {delta:.1%} — commonly a hydrate/"
                            f"anhydrous mix-up"
                        ),
                        evidence={"url": product.url, "reference": ref.name},
                    )
                )

        if product.cas and ref.all_cas and product.cas not in ref.all_cas:
            findings.append(
                Finding(
                    check="cas_not_in_reference",
                    severity="warning",
                    source=product.source,
                    subject=product.key,
                    detail=(
                        f"CAS {product.cas} is valid but is not a registry number "
                        f"PubChem associates with {ref.name!r}"
                    ),
                    evidence={"url": product.url, "reference_cas": ref.all_cas[:5]},
                )
            )
    return findings


def run_all(
    products: list[SupplierProduct],
    references: dict[str, ReferenceSubstance],
    results: list[MatchResult],
    sds_by_url: dict[str, SDSRecord],
) -> list[Finding]:
    findings = [
        *check_cas_integrity(products),
        *check_hydration_consistency(products),
        *check_catalogue_vs_sds(products, sds_by_url),
        *check_cross_supplier_conflicts(products),
        *check_against_reference(products, references, results),
    ]
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.check, f.subject))
    return findings


def summarise(findings: list[Finding]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for f in findings:
        out[f.check][f.severity] += 1
        out[f.check]["total"] += 1
    return {k: dict(v) for k, v in out.items()}
