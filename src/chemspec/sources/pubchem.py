"""PubChem PUG-REST — the canonical spine.

PubChem is the reference everything else is matched *to*. Without a spine you
have three catalogues and no ground truth, so agreement between suppliers
would be the only signal and two suppliers copying the same upstream error
would look like confirmation.

CAS numbers are not a first-class PubChem field; they appear in the synonym
list. That list is long, unordered and contains supplier codes, so the CAS is
recovered by validating every candidate against its check digit and keeping
those that pass. PUG-REST is a documented public API and ``/rest/`` is
explicitly allowed by PubChem's robots.txt.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from .. import cas as cas_mod
from ..http import PoliteSession
from ..normalize import parse_number
from .base import ReferenceSubstance

log = logging.getLogger(__name__)

BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
HOST = "pubchem.ncbi.nlm.nih.gov"

PROPERTIES = "MolecularFormula,MolecularWeight,IUPACName,InChIKey,Title"


def _props_url(cid: int) -> str:
    return f"{BASE}/compound/cid/{cid}/property/{PROPERTIES}/JSON"


def cids_for_name(session: PoliteSession, name: str) -> list[int]:
    url = f"{BASE}/compound/name/{quote(name)}/cids/JSON"
    res = session.get(url)
    if res.status != 200:
        return []
    try:
        return res.json()["IdentifierList"]["CID"]
    except (KeyError, ValueError):
        return []


def cids_for_cas(session: PoliteSession, cas: str) -> list[int]:
    """CAS lookup goes through the same name endpoint; PubChem indexes it as a synonym."""
    return cids_for_name(session, cas)


def fetch_cid(session: PoliteSession, cid: int) -> ReferenceSubstance | None:
    res = session.get(_props_url(cid))
    if res.status != 200:
        return None
    try:
        props = res.json()["PropertyTable"]["Properties"][0]
    except (KeyError, IndexError, ValueError):
        return None

    syn_res = session.get(f"{BASE}/compound/cid/{cid}/synonyms/JSON")
    synonyms: list[str] = []
    if syn_res.status == 200:
        try:
            synonyms = syn_res.json()["InformationList"]["Information"][0]["Synonym"]
        except (KeyError, IndexError, ValueError):
            synonyms = []

    # Every synonym that survives the check digit is a registry number for this
    # substance. The first is treated as primary, but all are kept: suppliers
    # legitimately cite deprecated or alternate registry numbers.
    all_cas = [s for s in synonyms if cas_mod.is_valid(s)]

    name = props.get("Title") or props.get("IUPACName") or (synonyms[0] if synonyms else str(cid))

    return ReferenceSubstance(
        source="pubchem",
        ref_id=str(cid),
        name=name,
        cas=all_cas[0] if all_cas else None,
        all_cas=all_cas,
        formula=props.get("MolecularFormula"),
        molar_mass=parse_number(props.get("MolecularWeight")),
        inchikey=props.get("InChIKey"),
        # Trim: the full synonym list runs to hundreds of entries, most of them
        # vendor catalogue codes that add noise to name matching.
        synonyms=[s for s in synonyms if not cas_mod.is_valid(s)][:40],
        extra={"iupac_name": props.get("IUPACName")},
    ).enrich()


def resolve(session: PoliteSession, query: str) -> ReferenceSubstance | None:
    """Resolve a name or CAS to a single canonical substance.

    Takes the lowest CID on an ambiguous hit. PubChem assigns CIDs roughly in
    deposition order, so the lowest is usually the parent record rather than a
    later salt or mixture entry — a heuristic, and flagged as one in the
    output rather than presented as a resolution.
    """
    cids = cids_for_name(session, query)
    if not cids:
        return None
    substance = fetch_cid(session, min(cids))
    if substance is not None and len(cids) > 1:
        substance.extra["ambiguous_cids"] = len(cids)
    return substance
