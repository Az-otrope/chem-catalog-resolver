from chemspec import cas


class TestCheckDigit:
    def test_known_registry_numbers(self):
        # Every one of these is a real CAS RN for a substance in the seed set.
        for rn in [
            "7758-98-7",   # copper(II) sulfate, anhydrous
            "7758-99-8",   # copper(II) sulfate pentahydrate
            "497-19-8",    # sodium carbonate
            "7487-88-9",   # magnesium sulfate
            "10035-04-8",  # calcium chloride dihydrate
            "50-00-0",     # formaldehyde, the shortest common RN
        ]:
            assert cas.is_valid(rn), rn

    def test_transposed_digits_are_caught(self):
        # 7758-99-8 with the middle pair transposed. Well-formed, wrong.
        chk = cas.validate("7758-98-8")
        assert chk.well_formed
        assert not chk.checksum_valid
        assert chk.reason == "checksum_failed"

    def test_malformed_is_distinguished_from_wrong(self):
        assert cas.validate("not-a-cas").reason == "malformed"
        assert cas.validate("7758-9-8").reason == "malformed"  # middle must be 2 digits
        assert cas.validate("1-23-4").reason == "malformed"    # first must be 2+ digits

    def test_none_and_empty(self):
        assert not cas.is_valid(None)
        assert not cas.is_valid("")


class TestNormalisation:
    def test_strips_supplier_prefixes(self):
        for raw in ["CAS 7758-99-8", "CAS-No. 7758-99-8", "[7758-99-8]", " cas no: 7758-99-8 "]:
            assert cas.normalize(raw) == "7758-99-8", raw

    def test_handles_pdf_unicode_hyphens(self):
        # en-dash and non-breaking hyphen both appear in PDF-extracted SDS text
        assert cas.normalize("7758–99–8") == "7758-99-8"
        assert cas.normalize("7758‑99‑8") == "7758-99-8"

    def test_invalid_checksum_normalises_to_none(self):
        assert cas.normalize("7758-98-8") is None


class TestExtraction:
    def test_neighbouring_identifiers_are_excluded_by_shape(self):
        # An SDS header line. The EC number (NNN-NNN-N) and UN number have a
        # different digit grouping from a CAS RN, so the shape alone rejects
        # them — no checksum needed.
        text = "CAS No. 7758-99-8  EC-No. 616-477-9  UN-No. 3077"
        assert cas.extract_all(text) == ["7758-99-8"]

    def test_cas_shaped_lookalike_is_excluded_by_checksum(self):
        # This is what the checksum actually buys: a string that is a perfectly
        # well-formed CAS RN and still is not one.
        text = "Substance A 7758-99-8 and substance B 7758-99-7"
        assert cas.extract_all(text) == ["7758-99-8"]

    def test_preserves_first_seen_order_and_dedupes(self):
        text = "7758-99-8 then 497-19-8 then 7758-99-8 again"
        assert cas.extract_all(text) == ["7758-99-8", "497-19-8"]

    def test_valid_only_false_keeps_failing_candidates(self):
        assert cas.extract_all("7758-99-7", valid_only=False) == ["7758-99-7"]
        assert cas.extract_all("7758-99-7", valid_only=True) == []
