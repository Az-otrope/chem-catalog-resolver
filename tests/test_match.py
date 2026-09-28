"""The central claim of the project, as assertions.

If the hydration gate were removed, several of these would fail — which is the
point of having them.
"""

from chemspec.match import Decision, Method, Thresholds, match_products, hydration_conflict
from chemspec.sources.base import ReferenceSubstance, SupplierProduct


def product(name, cas=None, formula=None, mass=None, source="loba", sku="X1"):
    return SupplierProduct(
        source=source, sku=sku, name=name, cas_raw=cas, formula=formula, molar_mass=mass
    ).enrich()


def reference(name, cas, formula=None, mass=None, ref_id=None, synonyms=()):
    return ReferenceSubstance(
        source="pubchem",
        ref_id=ref_id or cas,
        name=name,
        cas=cas,
        all_cas=[cas],
        formula=formula,
        molar_mass=mass,
        synonyms=list(synonyms),
    ).enrich()


ANHYDROUS = reference("Copper(II) sulfate", "7758-98-7", "CuO4S", 159.6, ref_id="24462")
PENTAHYDRATE = reference(
    "Copper(II) sulfate pentahydrate", "7758-99-8", "CuH10O9S", 249.69, ref_id="24463"
)
REFS = [ANHYDROUS, PENTAHYDRATE]


class TestHydrationGate:
    def test_hydrate_does_not_match_anhydrous_reference(self):
        # Name similarity between these two is well above the accept threshold.
        # Only the gate stops the merge.
        p = product("Copper(II) sulphate pentahydrate", mass=249.69)
        [res] = match_products([p], [ANHYDROUS])
        assert res.decision is not Decision.ACCEPT
        assert any("hydration_gate_excluded" in r for r in res.reasons)

    def test_hydrate_matches_its_own_reference(self):
        p = product("Copper(II) sulphate pentahydrate", mass=249.69, formula="CuH10O9S")
        [res] = match_products([p], REFS)
        assert res.decision is Decision.ACCEPT
        assert res.reference_id == "24463"

    def test_anhydrous_matches_anhydrous_not_hydrate(self):
        p = product("Copper(II) sulfate anhydrous", mass=159.6)
        [res] = match_products([p], REFS)
        assert res.decision is Decision.ACCEPT
        assert res.reference_id == "24462"

    def test_silence_about_hydration_is_not_a_conflict(self):
        assert not hydration_conflict(None, 5.0)
        assert not hydration_conflict(5.0, None)
        assert hydration_conflict(0.0, 5.0)
        assert not hydration_conflict(5.0, 5.0)

    def test_unspecified_hydration_does_not_auto_accept_between_two_candidates(self):
        # "Copper(II) sulfate" with no hydration word and no mass could be
        # either reference. The matcher must not pick one confidently.
        p = product("Copper(II) sulfate")
        [res] = match_products([p], REFS)
        assert res.decision is Decision.REVIEW


class TestCASTier:
    def test_exact_cas_wins_immediately(self):
        p = product("Blue vitriol crystals", cas="7758-99-8")
        [res] = match_products([p], REFS)
        assert res.decision is Decision.ACCEPT
        assert res.method is Method.CAS_EXACT
        assert res.reference_id == "24463"

    def test_invalid_cas_falls_through_to_name_matching(self):
        p = product("Copper(II) sulfate pentahydrate", cas="7758-99-7", mass=249.69)
        [res] = match_products([p], REFS)
        assert res.method is Method.NAME_SIMILARITY
        assert any("checksum_failed" in r for r in res.reasons)

    def test_cas_match_contradicted_by_name_goes_to_review(self):
        # Supplier states the anhydrous CAS on a page named as the hydrate.
        # Accepting this silently would launder the supplier's error.
        p = product("Copper(II) sulfate pentahydrate", cas="7758-98-7")
        [res] = match_products([p], REFS)
        assert res.decision is Decision.REVIEW
        assert any("hydration_conflict_vs_cas" in r for r in res.reasons)

    def test_cas_match_with_disagreeing_mass_goes_to_review(self):
        p = product("Copper(II) sulfate pentahydrate", cas="7758-99-8", mass=159.6)
        [res] = match_products([p], REFS)
        assert res.decision is Decision.REVIEW
        assert any("molar_mass_disagrees" in r for r in res.reasons)


class TestAmbiguity:
    def test_near_tie_is_demoted_to_review(self):
        a = reference("Sodium phosphate dibasic", "7558-79-4", ref_id="A")
        b = reference("Sodium phosphate dibasic", "7558-79-4", ref_id="B")
        p = product("Sodium phosphate dibasic")
        [res] = match_products([p], [a, b])
        assert res.decision is Decision.REVIEW
        assert any("ambiguous_runner_up" in r for r in res.reasons)

    def test_no_candidate_is_unresolved_not_wrong(self):
        p = product("Polyethylene glycol 400")
        [res] = match_products([p], REFS)
        assert res.decision in (Decision.UNRESOLVED, Decision.REJECT)
        assert res.reference_id is None


class TestEvidence:
    def test_trade_name_matches_via_reference_synonyms(self):
        ref = reference(
            "Copper(II) sulfate pentahydrate",
            "7758-99-8",
            mass=249.69,
            ref_id="24463",
            synonyms=["Coppervitriole", "Blue vitriol"],
        )
        p = product("Coppervitriole pentahydrate", mass=249.69)
        [res] = match_products([p], [ref])
        assert res.decision is Decision.ACCEPT

    def test_formula_mismatch_penalises(self):
        p = product("Copper(II) sulfate pentahydrate", formula="NaCl")
        [res] = match_products([p], [PENTAHYDRATE])
        assert any("formula_mismatch" in r for r in res.reasons)
        assert res.decision is not Decision.ACCEPT


class TestNormalisationReachesTheMatcher:
    """Variants that must all resolve to the same reference."""

    def test_spelling_oxidation_state_and_latinate_variants_all_match(self):
        for name in [
            "Copper(II) sulphate pentahydrate",   # British spelling
            "Copper(II) sulfate pentahydrate",    # American
            "Cupric sulphate pentahydrate",       # Latinate
            "Copper(II) sulfate pentahydrate, 2.5 kg, >=99.5 %, p.a., ACS",
        ]:
            [res] = match_products([product(name)], [PENTAHYDRATE])
            assert res.decision is Decision.ACCEPT, name
            assert res.reference_id == "24463", name

    def test_hill_notation_formula_agrees_with_conventional(self):
        # The supplier writes CuSO4.5H2O, PubChem writes CuH10O9S.
        p = product("Copper(II) sulfate pentahydrate", formula="CuSO4.5H2O")
        [res] = match_products([p], [PENTAHYDRATE])
        assert "formula_match" in res.reasons
        assert res.decision is Decision.ACCEPT


class TestThresholds:
    def test_one_candidate_moves_through_all_three_bands(self):
        # Name and hydration agree, molar mass does not — which lands the
        # score mid-range, where the thresholds are what decide the outcome.
        p = product("Copper(II) sulfate pentahydrate", mass=200.0)
        bands = {
            "accept": Thresholds(accept=0.75, review=0.50),
            "review": Thresholds(accept=0.92, review=0.78),
            "reject": Thresholds(accept=0.95, review=0.90),
        }
        outcomes = {
            label: match_products([p], [PENTAHYDRATE], t)[0]
            for label, t in bands.items()
        }
        assert outcomes["accept"].decision is Decision.ACCEPT
        assert outcomes["review"].decision is Decision.REVIEW
        assert outcomes["reject"].decision is Decision.REJECT
        assert outcomes["reject"].reference_id is None, (
            "a rejected match must not name a reference"
        )
        # Same evidence throughout; only the policy changed.
        assert len({round(o.score, 6) for o in outcomes.values()}) == 1


class TestLobaSeedFiltering:
    """Category crawling must be steered to the seed set, or the catalogues
    do not overlap and cross-supplier matching measures nothing."""

    URLS = [
        "https://x/Inorganic-Salts-01/COPPER-SULPHATE-PENTAHYDRATE-CASNO-7758-99-8.aspx",
        "https://x/Inorganic-Salts-02/ALUMINIUM-HYDROXIDE-GEL-CASNO-21645-51-2.aspx",
        "https://x/Inorganic-Bases-03/CALCIUM-CHLORIDE-DIHYDRATE-CASNO-10035-04-8.aspx",
        "https://x/Inorganic-Salts-04/SOME-EXOTIC-REAGENT-CASNO-1111-11-1.aspx",
    ]

    def test_seed_cas_matches_are_ranked_first(self):
        from chemspec.sources.loba import select_product_urls

        ranked = select_product_urls(self.URLS, {"7758-99-8", "10035-04-8"}, None)
        assert "CASNO-7758-99-8" in ranked[0] or "CASNO-10035-04-8" in ranked[0]
        assert "CASNO-7758-99-8" in ranked[1] or "CASNO-10035-04-8" in ranked[1]

    def test_seed_term_matches_rank_above_unrelated(self):
        from chemspec.sources.loba import select_product_urls

        ranked = select_product_urls(self.URLS, set(), ["aluminium hydroxide"])
        assert "ALUMINIUM-HYDROXIDE" in ranked[0]
        assert "EXOTIC-REAGENT" in ranked[-1]

    def test_ranking_never_drops_products(self):
        # It prioritises rather than filters: keeping the non-seed rows is
        # what exercises the matcher on substances no seed anticipated.
        from chemspec.sources.loba import select_product_urls

        ranked = select_product_urls(self.URLS, {"7758-99-8"}, ["aluminium hydroxide"])
        assert sorted(ranked) == sorted(self.URLS)

    def test_no_seeds_preserves_original_order(self):
        from chemspec.sources.loba import select_product_urls

        assert select_product_urls(self.URLS, None, None) == self.URLS
