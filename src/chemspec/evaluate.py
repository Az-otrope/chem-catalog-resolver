"""Scoring the matcher against ground truth it never sees.

A matcher nobody measured is an opinion. Two independent labels are available
here without hand-annotating anything:

1. **Chem-Impex publishes the PubChem CID** on its product pages. That is a
   direct supplier-to-reference link produced by someone other than this
   pipeline, so it can be held out and used as a label.
2. **Valid CAS numbers resolve in PubChem.** For any product carrying a CAS
   that PubChem recognises, the resolved substance is the correct answer. The
   CAS can then be withheld from the matcher and the name-only path scored on
   whether it recovers the same substance.

Label set 2 is the larger one and the reason the evaluation is worth running:
it measures exactly the case the pipeline exists for — a row whose identifier
is missing or wrong, where the name is all you have.

The ablation runs the same matcher with the hydration gate disabled, in two
evidence regimes, because the measured effect turned out not to be the one the
design assumed.

With formula and molar mass present, those carry the discrimination and the
gate barely matters — a hydrate and its anhydrous form differ by enough mass
that the wrong candidate is already penalised below threshold. Strip them, so
the name is the only evidence, and the gate's real contribution appears: it is
not that it blocks wrong merges, but that it lets the matcher *answer at all*.
Without hydration to separate them, an anhydrous salt and its hydrate score
almost identically, the near-tie rule fires, and both go to review.

That distinction is only visible because the ablation was run. It is reported
as measured rather than as intended.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from .match import Decision, MatchResult, Thresholds, match_products
from .sources.base import ReferenceSubstance, SupplierProduct


@dataclass
class EvalMetrics:
    label_source: str
    labelled: int = 0
    predicted: int = 0
    """Rows where the matcher committed to an answer (accepted)."""
    correct: int = 0
    exact_id: int = 0
    """Predicted the same reference record as the label."""
    equivalent_record: int = 0
    """Predicted a *different* reference record for the same substance.

    PubChem carries several CIDs for one substance — parent records, depositor
    duplicates, and hydrate variants. Chem-Impex's published CID is one valid
    choice among them, so treating CID identity as the definition of
    correctness scores a right answer as a miss. These are counted as correct
    and reported separately, because the number is itself a finding: it is how
    often a "canonical identifier" turns out not to be canonical.
    """
    label_less_specific: int = 0
    """The label points at a vaguer record than the prediction.

    PubChem carries deliberately unspecific records — "Calcium chloride
    xhydrate" (22691-02-7) stands for the hydrate without saying which. When a
    supplier cross-references one of those from a page selling the dihydrate,
    the matcher naming the dihydrate is *more* correct than the label. Scored
    neither right nor wrong: it is a defect in the label, and counting it
    either way would misdescribe the matcher.
    """
    wrong: int = 0
    abstained: int = 0
    """Rows sent to review or left unresolved instead of being answered."""
    errors: list[dict] = field(default_factory=list)
    less_specific_examples: list[dict] = field(default_factory=list)

    @property
    def scorable(self) -> int:
        """Answers given, excluding those whose label was too vague to judge."""
        return self.predicted - self.label_less_specific

    @property
    def precision(self) -> float:
        """Of the judgeable answers given, how many were right."""
        return self.correct / self.scorable if self.scorable else 0.0

    @property
    def recall(self) -> float:
        """Of the rows with a judgeable right answer, how many were found."""
        denominator = self.labelled - self.label_less_specific
        return self.correct / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def to_dict(self) -> dict:
        d = {
            "label_source": self.label_source,
            "labelled": self.labelled,
            "answered": self.predicted,
            "correct": self.correct,
            "correct_exact_record": self.exact_id,
            "correct_equivalent_record": self.equivalent_record,
            "label_less_specific": self.label_less_specific,
            "wrong": self.wrong,
            "abstained": self.abstained,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }
        if self.errors:
            d["sample_errors"] = self.errors[:10]
        if self.less_specific_examples:
            d["label_less_specific_examples"] = self.less_specific_examples[:6]
        return d


def _blind_copy(product: SupplierProduct) -> SupplierProduct:
    """A copy with the CAS removed, forcing the name-similarity path."""
    clone = dataclasses.replace(product)
    clone.cas_raw = None
    clone.cas = None
    clone.cas_problem = "withheld_for_evaluation"
    return clone


def labels_from_pubchem_cid(
    products: list[SupplierProduct],
) -> dict[str, str]:
    """Supplier-published CID cross-references."""
    labels = {}
    for p in products:
        cid = (p.extra or {}).get("pubchem_cid")
        if cid and str(cid).strip().isdigit():
            labels[p.key] = str(cid).strip()
    return labels


def labels_from_cas(
    products: list[SupplierProduct], references: list[ReferenceSubstance]
) -> dict[str, str]:
    """Resolve each product's valid CAS to a reference id."""
    by_cas: dict[str, str] = {}
    for ref in references:
        for cas in ref.all_cas or ([ref.cas] if ref.cas else []):
            by_cas.setdefault(cas, ref.ref_id)
    return {p.key: by_cas[p.cas] for p in products if p.cas and p.cas in by_cas}


def label_is_vaguer(
    predicted: ReferenceSubstance | None, expected: ReferenceSubstance | None
) -> bool:
    """True when the label names a less specific record than the prediction.

    Two shapes of vagueness, both real in PubChem:

    * an explicit placeholder hydrate — "Calcium chloride xhydrate" — where
      the prediction names a specific one of the same salt;
    * a record carrying no registry number at all, where the prediction does.

    In both cases the prediction must still be about the same base substance,
    so this cannot excuse an unrelated match.
    """
    if predicted is None or expected is None:
        return False
    if predicted.base_name != expected.base_name:
        return False

    if (
        expected.hydration_source == "placeholder_hydrate"
        and predicted.hydration is not None
    ):
        return True
    return not expected.all_cas and bool(predicted.all_cas)


def same_substance(
    a: ReferenceSubstance | None, b: ReferenceSubstance | None
) -> bool:
    """Whether two reference records describe the same substance.

    Record identity is too strict. PubChem holds multiple CIDs per substance,
    so two different CIDs are frequently the same chemical. Two pieces of
    evidence settle it without guessing:

    * **InChIKey** — a hash of the structure. Equal keys mean equal structures,
      including hydration state, which is the property that must not be
      blurred here.
    * **Overlapping registry numbers** — if both records claim the same CAS,
      the registry itself is asserting they are the same substance.

    Anything else counts as a genuine miss.
    """
    if a is None or b is None:
        return False
    if a.ref_id == b.ref_id:
        return True
    if a.inchikey and b.inchikey and a.inchikey == b.inchikey:
        return True
    return bool(set(a.all_cas) & set(b.all_cas))


def score(
    results: list[MatchResult],
    labels: dict[str, str],
    label_source: str,
    references: dict[str, ReferenceSubstance] | None = None,
) -> EvalMetrics:
    references = references or {}
    metrics = EvalMetrics(label_source=label_source, labelled=len(labels))
    for res in results:
        truth = labels.get(res.product_key)
        if truth is None:
            continue
        if res.decision is not Decision.ACCEPT:
            metrics.abstained += 1
            continue
        metrics.predicted += 1

        if res.reference_id == truth:
            metrics.correct += 1
            metrics.exact_id += 1
            continue

        predicted_ref = references.get(res.reference_id or "")
        expected_ref = references.get(truth)
        if same_substance(predicted_ref, expected_ref):
            metrics.correct += 1
            metrics.equivalent_record += 1
            continue

        if label_is_vaguer(predicted_ref, expected_ref):
            metrics.label_less_specific += 1
            metrics.less_specific_examples.append({
                "product": res.product_name,
                "predicted": predicted_ref.name if predicted_ref else None,
                "predicted_cas": predicted_ref.all_cas[:1] if predicted_ref else [],
                "label": expected_ref.name if expected_ref else None,
                "label_cas": expected_ref.all_cas[:1] if expected_ref else [],
            })
            continue

        metrics.wrong += 1
        metrics.errors.append(
            {
                "product": res.product_name,
                "sku": res.product_key,
                "predicted": res.reference_name,
                "predicted_id": res.reference_id,
                "predicted_cas": predicted_ref.all_cas[:3] if predicted_ref else [],
                "expected_id": truth,
                "expected_name": expected_ref.name if expected_ref else None,
                "expected_cas": expected_ref.all_cas[:3] if expected_ref else [],
                "expected_resolved": expected_ref is not None,
                "score": res.score,
                "reasons": res.reasons,
            }
        )
    return metrics


def run_evaluation(
    products: list[SupplierProduct],
    references: list[ReferenceSubstance],
    thresholds: Thresholds | None = None,
) -> dict:
    thresholds = thresholds or Thresholds()

    cid_labels = labels_from_pubchem_cid(products)
    cas_labels = labels_from_cas(products, references)
    ref_index = {r.ref_id: r for r in references}

    blinded = [_blind_copy(p) for p in products]
    blind_results = match_products(blinded, references, thresholds)

    # A label pointing at a CID the spine never resolved cannot be scored
    # either way, so it is excluded and counted rather than held against the
    # matcher.
    unresolved_cid_labels = {k: v for k, v in cid_labels.items() if v not in ref_index}
    scorable_cid_labels = {k: v for k, v in cid_labels.items() if v in ref_index}

    report = {
        "thresholds": dataclasses.asdict(thresholds),
        "name_only": {
            "supplier_published_cid": {
                **score(
                    blind_results,
                    scorable_cid_labels,
                    "chemimpex pubchem_cid",
                    ref_index,
                ).to_dict(),
                "labels_unresolvable_in_spine": len(unresolved_cid_labels),
            },
            "cas_resolved": score(
                blind_results,
                cas_labels,
                "CAS resolved via PubChem (withheld)",
                ref_index,
            ).to_dict(),
        },
        "hydration_gate_ablation": {
            "with_full_evidence": _ablation(
                blinded, references, thresholds, cas_labels, ref_index
            ),
            "name_evidence_only": _ablation(
                [_strip_structural_evidence(p) for p in blinded],
                references,
                thresholds,
                cas_labels,
                ref_index,
            ),
            "note": (
                "Two regimes. With formula and molar mass present, those carry "
                "most of the discrimination and the gate rarely changes an "
                "outcome. Stripping them isolates the case the gate exists for: "
                "a row where the name is the only evidence available."
            ),
        },
    }
    return report


def _strip_structural_evidence(product: SupplierProduct) -> SupplierProduct:
    """Remove formula and molar mass, leaving the name as the only evidence."""
    clone = dataclasses.replace(product)
    clone.formula = None
    clone.molar_mass = None
    return clone


def _ablation(
    blinded: list[SupplierProduct],
    references: list[ReferenceSubstance],
    thresholds: Thresholds,
    labels: dict[str, str],
    ref_index: dict[str, ReferenceSubstance] | None = None,
) -> dict:
    """Measure what the hydration gate buys, by turning it off.

    Disabling the gate is done by erasing hydration state from both sides, which
    is precisely what a matcher that treats "pentahydrate" as a descriptive
    token does.
    """
    flat_products = []
    for p in blinded:
        clone = dataclasses.replace(p)
        clone.hydration = None
        flat_products.append(clone)
    flat_refs = []
    for r in references:
        clone = dataclasses.replace(r)
        clone.hydration = None
        flat_refs.append(clone)

    ungated = match_products(flat_products, flat_refs, thresholds)
    gated = match_products(blinded, references, thresholds)

    ref_index = ref_index or {}
    gated_metrics = score(gated, labels, "gated", ref_index)
    ungated_metrics = score(ungated, labels, "ungated", ref_index)

    def _is_correct(res) -> bool:
        truth = labels.get(res.product_key)
        if truth is None or res.decision is not Decision.ACCEPT:
            return False
        if res.reference_id == truth:
            return True
        return same_substance(ref_index.get(res.reference_id or ""), ref_index.get(truth))

    gated_by_key = {r.product_key: r for r in gated}
    newly_wrong = []
    for res in ungated:
        truth = labels.get(res.product_key)
        if truth is None or res.decision is not Decision.ACCEPT or _is_correct(res):
            continue
        before = gated_by_key.get(res.product_key)
        if before is not None and _is_correct(before):
            expected = ref_index.get(truth)
            newly_wrong.append(
                {
                    "product": res.product_name,
                    "merged_into": res.reference_name,
                    "merged_into_cas": (
                        ref_index[res.reference_id].cas
                        if res.reference_id in ref_index else None
                    ),
                    "should_be": expected.name if expected else None,
                    "should_be_cas": expected.cas if expected else None,
                    "score": res.score,
                }
            )

    return {
        "with_gate": gated_metrics.to_dict(),
        "without_gate": ungated_metrics.to_dict(),
        # Two distinct effects, reported separately because they are easy to
        # conflate. The gate can stop a wrong merge outright, and it can also
        # let the matcher answer at all: without hydration to separate them,
        # an anhydrous salt and its hydrate score almost identically, the
        # near-tie rule fires, and both go to review. The second effect is
        # usually the larger one.
        "wrong_merges_prevented_by_gate": len(newly_wrong),
        "answers_enabled_by_gate": gated_metrics.predicted - ungated_metrics.predicted,
        "correct_answers_enabled_by_gate": gated_metrics.correct - ungated_metrics.correct,
        "extra_errors_from_gate": gated_metrics.wrong - ungated_metrics.wrong,
        "examples": newly_wrong[:10],
    }
