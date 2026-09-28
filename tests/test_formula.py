"""Formula parsing.

Three notations reach this code and all three must reduce to the same element
counts, because the matcher uses formula agreement as corroborating evidence.
Comparing them as strings — which an earlier version did — turned the single
strongest signal for a correct match into a penalty against it.
"""

import pytest

from chemspec.normalize import formula_elements, infer_anhydrous


class TestEquivalentNotations:
    @pytest.mark.parametrize(
        "supplier,pubchem",
        [
            ("CuSO4", "CuO4S"),                 # conventional vs Hill order
            ("CuSO4.5H2O", "CuH10O9S"),         # hydrate dot vs merged
            ("MgSO4.7H2O", "MgH14O11S"),
            ("Ba(OH)2.8H2O", "BaH18O10"),       # parenthesised group + hydrate
            ("Na2CO3.10H2O", "CH20Na2O13"),
            ("CuSO4 · 5 H2O", "CuSO4.5H2O"),    # unicode dot and spacing
        ],
    )
    def test_notations_agree(self, supplier, pubchem):
        assert formula_elements(supplier) == formula_elements(pubchem)

    def test_distinct_compounds_do_not_agree(self):
        assert formula_elements("CuSO4") != formula_elements("CuSO4.5H2O")
        assert formula_elements("NaCl") != formula_elements("KCl")


class TestCounts:
    def test_parenthesised_multiplier(self):
        assert formula_elements("Ba(OH)2") == {"Ba": 1, "O": 2, "H": 2}

    def test_hydrate_multiplier_applies_to_whole_fragment(self):
        assert formula_elements("X.7H2O")["H"] == 14
        assert formula_elements("X.7H2O")["O"] == 7

    def test_nested_groups(self):
        assert formula_elements("Al2(SO4)3") == {"Al": 2, "S": 3, "O": 12}

    def test_two_letter_elements_are_not_split(self):
        assert formula_elements("NaCl") == {"Na": 1, "Cl": 1}
        assert "C" not in formula_elements("NaCl")

    def test_empty_and_junk(self):
        assert formula_elements(None) == {}
        assert formula_elements("") == {}
        assert formula_elements("n/a") == {}

    def test_unbalanced_parens_do_not_raise(self):
        # Scraped text is not guaranteed well-formed.
        assert formula_elements("Ba(OH2")  # should produce counts, not an error


class TestAnhydrousInference:
    def test_hydrogen_free_formula_with_silent_name_reads_as_anhydrous(self):
        # PubChem titles the anhydrous salt "Copper(II) sulfate" — no
        # "anhydrous" word — so without this the hydration gate never fires
        # against the record it most needs to.
        assert infer_anhydrous(None, "CuO4S") is True

    def test_hydrogen_bearing_formula_stays_unknown(self):
        # Sodium hydroxide contains hydrogen and is anhydrous; we cannot tell
        # from the formula, so we must not guess.
        assert infer_anhydrous(None, "NaOH") is False
        assert infer_anhydrous(None, "C6H8O7") is False

    def test_stated_hydration_is_never_overridden(self):
        assert infer_anhydrous(5.0, "CuO4S") is False
        assert infer_anhydrous(0.0, "CuO4S") is False

    def test_missing_formula_stays_unknown(self):
        assert infer_anhydrous(None, None) is False
        assert infer_anhydrous(None, "") is False
