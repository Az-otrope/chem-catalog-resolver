"""SDS parsing and the cross-source data-quality checks."""

from chemspec import quality
from chemspec.match import match_products
from chemspec.sds import SDSRecord, parse_text, sniff_format, split_sections
from chemspec.sources.base import ReferenceSubstance, SupplierProduct

SDS_TEXT = """
SAFETY DATA SHEET
according to Regulation (EC) No. 1907/2006

SECTION 1: Identification of the substance/mixture
Product form : Substance
Product name : Ammonium acetate
Product code : 01081

SECTION 3: Composition/information on ingredients
Name            Product identifier      %
Ammonium acetate  (CAS No) 631-61-8      100
EC-No. 211-162-9
UN-No. 3077

SECTION 9: Physical and chemical properties
Melting point : 114 °C
pH : 7.0
Molecular weight : 77.08 g/mol
H302 H315
"""


def product(**kw):
    defaults = dict(source="loba", sku="1", name="Thing")
    return SupplierProduct(**{**defaults, **kw}).enrich()


class TestFormatSniffing:
    def test_pdf_is_detected_from_magic_bytes(self):
        # Loba serves PDFs from .aspx URLs, so the extension cannot be trusted.
        assert sniff_format(b"%PDF-1.7\nstuff") == "pdf"
        assert sniff_format(b"   \n%PDF-1.4") == "pdf"

    def test_html_is_the_fallback(self):
        assert sniff_format(b"<!DOCTYPE html><html>") == "html"


class TestParsing:
    def test_sections_are_split(self):
        sections = split_sections(SDS_TEXT)
        assert any(k.startswith("1.") or k.startswith("1 ") for k in sections) or sections
        assert len(sections) >= 2

    def test_cas_comes_from_composition_and_lookalikes_are_dropped(self):
        record = parse_text(SDS_TEXT, "http://x/sds", "pdf")
        assert record.primary_cas == "631-61-8"
        # The EC and UN numbers sit right beside it and must not be picked up.
        assert "211-162-9" not in record.cas_numbers
        assert record.parse_errors == []

    def test_product_form_is_not_mistaken_for_product_name(self):
        record = parse_text(SDS_TEXT, "http://x/sds", "pdf")
        assert record.product_name == "Ammonium acetate"

    def test_physical_properties_and_hazards(self):
        record = parse_text(SDS_TEXT, "http://x/sds", "pdf")
        assert record.molar_mass == 77.08
        assert "H302" in record.hazard_codes

    def test_empty_document_is_an_error_not_a_crash(self):
        record = parse_text("", "http://x/sds", "pdf")
        assert "empty_text" in record.parse_errors


class TestQualityChecks:
    def test_checksum_failure_is_an_error_missing_is_a_warning(self):
        findings = quality.check_cas_integrity([
            product(sku="a", cas_raw="7758-99-7"),
            product(sku="b", cas_raw=None),
            product(sku="c", cas_raw="not-a-cas"),
        ])
        by_check = {f.check: f.severity for f in findings}
        assert by_check["cas_checksum_failed"] == "error"
        assert by_check["cas_missing"] == "warning"
        assert by_check["cas_malformed"] == "error"

    def test_catalogue_disagreeing_with_its_own_sds(self):
        p = product(sku="a", cas_raw="7758-99-8")
        p.sds_urls = ["http://x/sds"]
        sds = {"http://x/sds": SDSRecord(
            url="http://x/sds", fmt="pdf",
            cas_numbers=["7758-98-7"], primary_cas="7758-98-7",
        )}
        [finding] = quality.check_catalogue_vs_sds([p], sds)
        assert finding.check == "catalogue_sds_cas_conflict"
        assert finding.severity == "error"

    def test_agreement_with_its_own_sds_produces_nothing(self):
        p = product(sku="a", cas_raw="7758-99-8")
        p.sds_urls = ["http://x/sds"]
        sds = {"http://x/sds": SDSRecord(
            url="http://x/sds", fmt="pdf",
            cas_numbers=["7758-99-8"], primary_cas="7758-99-8",
        )}
        assert quality.check_catalogue_vs_sds([p], sds) == []

    def test_cross_supplier_conflict_needs_two_suppliers(self):
        same_supplier = [
            product(source="loba", sku="a", name="Sodium chloride", cas_raw="7647-14-5"),
            product(source="loba", sku="b", name="Sodium chloride", cas_raw="7758-99-8"),
        ]
        assert quality.check_cross_supplier_conflicts(same_supplier) == []

        two_suppliers = [
            product(source="loba", sku="a", name="Sodium chloride", cas_raw="7647-14-5"),
            product(source="carlroth", sku="b", name="Sodium chloride", cas_raw="7758-99-8"),
        ]
        [finding] = quality.check_cross_supplier_conflicts(two_suppliers)
        assert finding.check == "cross_supplier_cas_conflict"

    def test_anhydrous_name_with_water_in_formula(self):
        p = product(sku="a", name="Copper(II) sulfate anhydrous", formula="CuSO4.5H2O")
        findings = quality.check_hydration_consistency([p])
        assert findings[0].check == "anhydrous_with_water_in_formula"
        assert findings[0].severity == "error"

    def test_hydrate_name_without_water_in_formula(self):
        p = product(sku="a", name="Copper(II) sulfate pentahydrate", formula="CuSO4")
        findings = quality.check_hydration_consistency([p])
        assert findings[0].check == "hydration_name_formula_mismatch"

    def test_mass_disagreement_with_reference_is_reported(self):
        p = product(sku="a", name="Copper(II) sulfate pentahydrate",
                    cas_raw="7758-99-8", molar_mass=159.6)
        ref = ReferenceSubstance(
            source="pubchem", ref_id="24463", name="Copper sulfate pentahydrate",
            cas="7758-99-8", all_cas=["7758-99-8"], molar_mass=249.69,
        ).enrich()
        results = match_products([p], [ref])
        findings = quality.check_against_reference([p], {"24463": ref}, results)
        assert any(f.check == "molar_mass_vs_reference" for f in findings)

    def test_summarise_counts_by_check_and_severity(self):
        findings = quality.check_cas_integrity([
            product(sku="a", cas_raw="7758-99-7"),
            product(sku="b", cas_raw="7758-99-7"),
        ])
        summary = quality.summarise(findings)
        assert summary["cas_checksum_failed"]["total"] == 2
        assert summary["cas_checksum_failed"]["error"] == 2
