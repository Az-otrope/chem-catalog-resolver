import pytest

from chemspec import normalize as norm


class TestHydrationExtraction:
    @pytest.mark.parametrize(
        "name,waters",
        [
            ("Copper(II) sulfate pentahydrate", 5),
            ("Sodium carbonate decahydrate", 10),
            ("Magnesium sulfate heptahydrate", 7),
            ("Calcium chloride dihydrate", 2),
            ("Lithium hydroxide monohydrate", 1),
            ("Aluminium potassium sulfate dodecahydrate", 12),
            ("Barium hydroxide octahydrate", 8),
            ("Calcium sulfate hemihydrate", 0.5),
            ("Aluminium sulfate sesquihydrate", 1.5),
        ],
    )
    def test_word_prefixes(self, name, waters):
        assert norm.parse(name).hydration == waters

    @pytest.mark.parametrize(
        "name,waters",
        [
            ("CuSO4 · 5 H2O", 5),
            ("CuSO4.5H2O", 5),
            ("MgSO4 x 7 H2O", 7),
            ("CuSO4(H2O)5", 5),
            ("Ba(OH)2.8H2O", 8),
        ],
    )
    def test_formula_notation(self, name, waters):
        assert norm.parse(name).hydration == waters

    def test_anhydrous_is_zero_not_none(self):
        parsed = norm.parse("Copper(II) sulfate anhydrous")
        assert parsed.hydration == 0.0
        assert parsed.hydration_key == "0"

    def test_silence_is_not_anhydrous(self):
        # The distinction the whole matcher rests on: a name that says nothing
        # about hydration is unknown, not zero.
        parsed = norm.parse("Copper(II) sulfate")
        assert parsed.hydration is None
        assert parsed.hydration_key == "unspecified"

    def test_bare_hydrate_is_recorded_with_its_source(self):
        parsed = norm.parse("Calcium chloride hydrate")
        assert parsed.hydration == 1.0
        assert parsed.hydration_source == "bare_hydrate"

    def test_undeca_matches_before_deca(self):
        assert norm.parse("Sodium salt undecahydrate").hydration == 11


class TestNameNormalisation:
    def test_british_and_american_spelling_converge(self):
        assert norm.parse("Copper(II) sulphate").base == norm.parse("Copper(II) sulfate").base

    def test_latinate_names_converge_on_stock_names(self):
        assert norm.parse("Cupric sulfate").base == norm.parse("Copper(II) sulfate").base
        assert norm.parse("Ferrous sulfate").base == norm.parse("Iron(II) sulfate").base

    def test_pack_size_is_stripped(self):
        assert norm.parse("Copper(II) sulphate pentahydrate, 2.5 kg").base == (
            norm.parse("Copper(II) sulfate pentahydrate").base
        )

    def test_grade_boilerplate_is_stripped(self):
        a = norm.parse("Sodium chloride, >=99.5 %, p.a., ACS, ISO").base
        b = norm.parse("Sodium chloride").base
        assert a == b

    def test_hydrate_and_anhydrous_share_a_base_name(self):
        # They must, or the hydration gate would be redundant with the name.
        hydrate = norm.parse("Copper(II) sulfate pentahydrate")
        anhydrous = norm.parse("Copper(II) sulfate anhydrous")
        assert hydrate.base == anhydrous.base
        assert hydrate.hydration != anhydrous.hydration


class TestBlocking:
    def test_key_is_order_independent(self):
        a = norm.blocking_key(norm.parse("Sodium chloride"))
        b = norm.blocking_key(norm.parse("Chloride, sodium"))
        assert a == b

    def test_hydrate_and_anhydrous_share_a_block(self):
        # They must land in the same bucket to ever be compared — and then be
        # separated by the gate, not by blocking.
        assert norm.blocking_key(norm.parse("Magnesium sulfate")) == norm.blocking_key(
            norm.parse("Magnesium sulfate heptahydrate")
        )


class TestNumberParsing:
    def test_european_decimal_comma(self):
        assert norm.parse_number("249,69 g/mol") == pytest.approx(249.69)

    def test_english_decimal_point(self):
        assert norm.parse_number("249.69") == pytest.approx(249.69)

    def test_thousands_separator_is_not_a_decimal(self):
        assert norm.parse_number("1,234") == pytest.approx(1234.0)

    def test_mixed_separators_resolve_by_rightmost(self):
        assert norm.parse_number("1.234,56") == pytest.approx(1234.56)
        assert norm.parse_number("1,234.56") == pytest.approx(1234.56)

    def test_negative_values(self):
        assert norm.parse_number("-18 °C") == pytest.approx(-18.0)

    def test_unparseable_is_none(self):
        assert norm.parse_number("n/a") is None
        assert norm.parse_number(None) is None
