"""Resolving supplier SKUs to canonical substances.

The design commitment here is that **an incorrect merge costs more than an
unresolved record**. Merging copper(II) sulfate with its pentahydrate gives a
buyer a specification that is wrong by 36% on the mass of copper delivered,
and nothing downstream will flag it. Leaving one of them unresolved costs
somebody five minutes. So the matcher is built to fail towards "I don't know":

* Hydration state is a **gate**, not a feature. When two records both state a
  hydration state and the states differ, the pair is rejected outright no
  matter how similar the names are. Name similarity between
  "copper(II) sulfate" and "copper(II) sulfate pentahydrate" is above 0.9.

  Measured (see ``evaluate.py``), the gate's benefit is not the one this was
  designed for. Where the supplier publishes a formula and a molar mass, those
  already push the wrong candidate below threshold and the gate changes almost
  nothing. Where the name is the only evidence, it roughly doubles the number
  of rows the matcher can answer at all — without hydration to separate them,
  a salt and its hydrate score so alike that the near-tie rule sends both to
  review. The gate turns abstentions into answers, at unchanged precision.
* Records are **blocked** before scoring, so the comparison is O(n · bucket)
  rather than O(n²).
* Everything between the accept and reject thresholds is **routed to review**
  rather than resolved by lowering the bar.

The CAS tier runs first and is exact: a CAS that passes its check digit and
appears in the reference's registry-number list is a link, not a guess.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

from rapidfuzz import fuzz

from . import normalize as norm
from .sources.base import ReferenceSubstance, SupplierProduct

log = logging.getLogger(__name__)


class Decision(str, Enum):
    ACCEPT = "accept"
    REVIEW = "review"
    REJECT = "reject"
    UNRESOLVED = "unresolved"


class Method(str, Enum):
    CAS_EXACT = "cas_exact"
    NAME_SIMILARITY = "name_similarity"
    NONE = "none"


@dataclass
class MatchResult:
    product_key: str
    source: str
    sku: str
    product_name: str
    decision: Decision
    method: Method
    reference_id: str | None = None
    reference_name: str | None = None
    reference_cas: str | None = None
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    runners_up: list[tuple[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["decision"] = self.decision.value
        d["method"] = self.method.value
        return d


@dataclass
class Thresholds:
    accept: float = 0.92
    review: float = 0.78
    """Below ``review`` a candidate is discarded entirely."""
    mass_tolerance: float = 0.02
    """Relative tolerance when comparing molar masses (2%)."""


def hydration_conflict(a: float | None, b: float | None) -> bool:
    """True when both records state a hydration state and they disagree.

    Silence on one side is not a conflict — plenty of suppliers omit
    "anhydrous" on anhydrous products. Silence produces a penalty and a
    review flag further down, not a rejection.
    """
    if a is None or b is None:
        return False
    return abs(a - b) > 1e-9


def _mass_agreement(a: float | None, b: float | None, tol: float) -> float | None:
    if not a or not b:
        return None
    return 1.0 if abs(a - b) / max(a, b) <= tol else 0.0


def _formula_agreement(a: str | None, b: str | None) -> float | None:
    """Compare formulas by element counts, never as strings.

    Suppliers write conventional formulas ("CuSO4"); PubChem returns Hill
    notation ("CuO4S"). These are the same compound, and comparing the strings
    says they are not — which previously turned the strongest piece of
    corroborating evidence into a 0.25 penalty against the correct match.
    Hydrate dots are folded in, so "MgSO4.7H2O" and "MgH14O11S" agree.
    """
    if not a or not b:
        return None
    counts_a = norm.formula_elements(a)
    counts_b = norm.formula_elements(b)
    if not counts_a or not counts_b:
        return None
    return 1.0 if counts_a == counts_b else 0.0


def score_pair(
    product: SupplierProduct,
    reference: ReferenceSubstance,
    thresholds: Thresholds,
) -> tuple[float, list[str]]:
    """Blend name similarity with structural evidence. Returns (score, reasons)."""
    reasons: list[str] = []

    probe = product.token_form or product.base_name
    name_score = (
        max(
            fuzz.token_set_ratio(probe, reference.token_form or reference.base_name),
            # Reference synonym lists carry the trade names suppliers actually
            # use ("Coppervitriole"), which the IUPAC title never will.
            max(
                (
                    fuzz.token_set_ratio(probe, " ".join(norm.parse(s).tokens))
                    for s in reference.synonyms[:30]
                ),
                default=0,
            ),
        )
        / 100.0
    )
    score = name_score
    reasons.append(f"name_similarity={name_score:.3f}")

    formula = _formula_agreement(product.formula, reference.formula)
    if formula is not None:
        score += 0.10 if formula else -0.25
        reasons.append("formula_match" if formula else "formula_mismatch")

    mass = _mass_agreement(product.molar_mass, reference.molar_mass, thresholds.mass_tolerance)
    if mass is not None:
        score += 0.06 if mass else -0.20
        reasons.append("molar_mass_agrees" if mass else "molar_mass_disagrees")

    if product.hydration is None or reference.hydration is None:
        if product.hydration != reference.hydration:
            score -= 0.05
            reasons.append("hydration_unspecified_on_one_side")

    return max(0.0, min(1.0, score)), reasons


def match_products(
    products: list[SupplierProduct],
    references: list[ReferenceSubstance],
    thresholds: Thresholds | None = None,
) -> list[MatchResult]:
    thresholds = thresholds or Thresholds()

    by_cas: dict[str, ReferenceSubstance] = {}
    for ref in references:
        for cas in ref.all_cas or ([ref.cas] if ref.cas else []):
            by_cas.setdefault(cas, ref)

    # Multi-key blocking: a reference is reachable by its own name's key and by
    # the keys of its trade-name synonyms.
    blocks: dict[str, list[ReferenceSubstance]] = defaultdict(list)
    for ref in references:
        for key in {ref.block, *ref.alt_blocks}:
            if key:
                blocks[key].append(ref)

    results: list[MatchResult] = []

    for product in products:
        # --- Tier 1: exact CAS -----------------------------------------
        if product.cas and product.cas in by_cas:
            ref = by_cas[product.cas]
            reasons = [f"cas_exact={product.cas}"]
            decision = Decision.ACCEPT

            # Even an exact CAS gets sanity-checked. A supplier page that
            # states a pentahydrate name against an anhydrous CAS is a real
            # defect, and accepting it silently would launder the error.
            if hydration_conflict(product.hydration, ref.hydration):
                decision = Decision.REVIEW
                reasons.append(
                    f"hydration_conflict_vs_cas product={product.hydration} ref={ref.hydration}"
                )
            mass = _mass_agreement(product.molar_mass, ref.molar_mass, thresholds.mass_tolerance)
            if mass == 0.0:
                decision = Decision.REVIEW
                reasons.append(
                    f"molar_mass_disagrees product={product.molar_mass} ref={ref.molar_mass}"
                )

            results.append(
                MatchResult(
                    product_key=product.key,
                    source=product.source,
                    sku=product.sku,
                    product_name=product.name,
                    decision=decision,
                    method=Method.CAS_EXACT,
                    reference_id=ref.ref_id,
                    reference_name=ref.name,
                    reference_cas=ref.cas,
                    score=1.0,
                    reasons=reasons,
                )
            )
            continue

        # --- Tier 2: blocked name similarity ---------------------------
        candidates = list(blocks.get(product.block, []))
        if not candidates:
            # Fall back to any reference sharing a long token, so a bad
            # blocking key degrades into a wider search rather than a miss.
            tokens = {t for t in product.base_name.split() if len(t) > 3}
            candidates = [r for r in references if tokens & set(r.base_name.split())]

        scored: list[tuple[float, ReferenceSubstance, list[str]]] = []
        gated = 0
        for ref in candidates:
            if hydration_conflict(product.hydration, ref.hydration):
                gated += 1
                continue
            value, reasons = score_pair(product, ref, thresholds)
            scored.append((value, ref, reasons))

        if not scored:
            note = [f"no_candidates_in_block={product.block!r}"]
            if gated:
                note.append(f"hydration_gate_excluded={gated}")
            if product.cas_problem:
                note.append(f"cas_{product.cas_problem}")
            results.append(
                MatchResult(
                    product_key=product.key,
                    source=product.source,
                    sku=product.sku,
                    product_name=product.name,
                    decision=Decision.UNRESOLVED,
                    method=Method.NONE,
                    reasons=note,
                )
            )
            continue

        scored.sort(key=lambda t: t[0], reverse=True)
        best_score, best_ref, best_reasons = scored[0]
        runners = [(r.name, round(s, 3)) for s, r, _ in scored[1:4]]

        if best_score >= thresholds.accept:
            decision = Decision.ACCEPT
        elif best_score >= thresholds.review:
            decision = Decision.REVIEW
        else:
            decision = Decision.REJECT

        # A near-tie is not a match, however high the top score. Two references
        # within a hair of each other means the evidence does not separate
        # them, which is exactly the ambiguous tail that belongs with a human.
        if len(scored) > 1 and best_score - scored[1][0] < 0.03 and decision == Decision.ACCEPT:
            decision = Decision.REVIEW
            best_reasons.append(f"ambiguous_runner_up={scored[1][0]:.3f}")

        if gated:
            best_reasons.append(f"hydration_gate_excluded={gated}")
        if product.cas_problem and product.cas_problem != "missing":
            best_reasons.append(f"cas_{product.cas_problem}")

        results.append(
            MatchResult(
                product_key=product.key,
                source=product.source,
                sku=product.sku,
                product_name=product.name,
                decision=decision,
                method=Method.NAME_SIMILARITY,
                reference_id=best_ref.ref_id if decision != Decision.REJECT else None,
                reference_name=best_ref.name if decision != Decision.REJECT else None,
                reference_cas=best_ref.cas if decision != Decision.REJECT else None,
                score=round(best_score, 4),
                reasons=best_reasons,
                runners_up=runners,
            )
        )

    return results


def cluster_by_substance(
    products: list[SupplierProduct], results: list[MatchResult]
) -> dict[str, list[str]]:
    """Group SKU keys by the substance they resolved to.

    This is the payload a sourcing marketplace actually wants: for one
    substance, every SKU across every supplier that really is that substance.
    Only accepted matches contribute; review and unresolved rows are deliberately
    left out of the clusters rather than being folded in at lower confidence.
    """
    clusters: dict[str, list[str]] = defaultdict(list)
    for res in results:
        if res.decision is Decision.ACCEPT and res.reference_id:
            clusters[res.reference_id].append(res.product_key)
    return dict(clusters)
