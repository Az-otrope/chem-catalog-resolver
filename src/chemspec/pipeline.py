"""End-to-end orchestration: harvest, resolve, check, report."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import evaluate, quality
from .http import DEFAULT_UA, PoliteSession
from .match import Decision, Thresholds, cluster_by_substance, match_products
from .sds import SDSRecord, fetch_and_parse
from .sources import carlroth, chemimpex, loba, pubchem
from .sources.base import ReferenceSubstance, SupplierProduct

log = logging.getLogger(__name__)

SUPPLIER_MODULES = {"loba": loba, "carlroth": carlroth, "chemimpex": chemimpex}


def load_config(config_dir: Path) -> tuple[dict, dict]:
    targets = yaml.safe_load((config_dir / "targets.yaml").read_text())
    seeds = yaml.safe_load((config_dir / "seeds.yaml").read_text())
    return targets, seeds


def build_session(targets: dict, cache_dir: Path) -> PoliteSession:
    allowed = {entry["host"].lower() for entry in targets.get("allowed", [])}
    authority = {
        entry["host"]: entry["robots_authority"]
        for entry in targets.get("allowed", [])
        if entry.get("robots_authority")
    }
    return PoliteSession(
        cache_dir=cache_dir,
        user_agent=(targets.get("user_agent") or "").strip() or DEFAULT_UA,
        default_delay=float(targets.get("default_delay_seconds", 2.0)),
        allowed_hosts=allowed,
        robots_authority=authority,
        robots_overrides=targets.get("robots_overrides", []),
        host_delays={
            e["host"]: e["min_delay_seconds"]
            for e in targets.get("allowed", [])
            if e.get("min_delay_seconds") is not None
        },
    )


def harvest_suppliers(
    session: PoliteSession,
    seeds: dict,
    sources: list[str],
    limit_per_source: int | None,
) -> list[SupplierProduct]:
    terms = seeds.get("search_terms", [])
    products: list[SupplierProduct] = []

    for name in sources:
        module = SUPPLIER_MODULES[name]
        log.info("harvesting %s", name)
        try:
            if name == "loba":
                # Loba is discovered by category, not by search, so the seed
                # substances have to be pushed down as a filter or the three
                # catalogues barely intersect.
                wanted_cas = set()
                for pair in seeds.get("pairs", []):
                    wanted_cas.add(pair["anhydrous"])
                    wanted_cas.add(pair["hydrate"])
                batch = module.scrape(
                    session,
                    categories=seeds.get("loba_categories"),
                    limit=limit_per_source,
                    wanted_cas=wanted_cas,
                    wanted_terms=terms,
                )
            else:
                batch = module.scrape(session, terms, limit=limit_per_source)
        except Exception as exc:  # noqa: BLE001 - one dead source must not kill the run
            log.error("%s harvest failed: %s", name, exc)
            continue
        log.info("  %s: %d SKUs", name, len(batch))
        products.extend(batch)

    return products


def build_reference(
    session: PoliteSession, products: list[SupplierProduct], seeds: dict
) -> list[ReferenceSubstance]:
    """Resolve a canonical substance for every distinct CAS and seed term.

    Seeded from the CAS numbers the suppliers published plus the seed search
    terms, so the spine covers what was actually harvested rather than a fixed
    list decided in advance.
    """
    queries: dict[str, None] = {}
    for p in products:
        if p.cas:
            queries.setdefault(p.cas, None)
    for term in seeds.get("search_terms", []):
        queries.setdefault(term, None)
    for pair in seeds.get("pairs", []):
        queries.setdefault(pair["anhydrous"], None)
        queries.setdefault(pair["hydrate"], None)

    references: dict[str, ReferenceSubstance] = {}
    for query in queries:
        try:
            substance = pubchem.resolve(session, query)
        except Exception as exc:  # noqa: BLE001
            log.warning("pubchem: %r failed: %s", query, exc)
            continue
        if substance:
            references.setdefault(substance.ref_id, substance)

    # Pull in every CID a supplier published as a cross-reference. These are
    # the evaluation's labels: if the spine does not contain the record a
    # label points at, that label cannot be scored either way, and the matcher
    # would be marked wrong for an answer nobody could check.
    published_cids = {
        str(p.extra["pubchem_cid"]).strip()
        for p in products
        if (p.extra or {}).get("pubchem_cid")
        and str(p.extra["pubchem_cid"]).strip().isdigit()
    }
    for cid in sorted(published_cids - set(references)):
        try:
            substance = pubchem.fetch_cid(session, int(cid))
        except Exception as exc:  # noqa: BLE001
            log.warning("pubchem: CID %s failed: %s", cid, exc)
            continue
        if substance:
            references.setdefault(substance.ref_id, substance)

    log.info(
        "reference spine: %d substances from %d queries + %d published CIDs",
        len(references), len(queries), len(published_cids),
    )
    return list(references.values())


def harvest_sds(
    session: PoliteSession, products: list[SupplierProduct], limit: int | None
) -> dict[str, SDSRecord]:
    urls: list[str] = []
    for p in products:
        urls.extend(p.sds_urls[:1])  # one language per product is enough
    seen: dict[str, SDSRecord] = {}
    for url in urls:
        if url in seen:
            continue
        if limit is not None and len(seen) >= limit:
            break
        record = fetch_and_parse(session, url)
        if record:
            seen[url] = record
    log.info("parsed %d safety data sheets", len(seen))
    return seen


def run(
    config_dir: Path,
    out_dir: Path,
    sources: list[str] | None = None,
    limit_per_source: int | None = 40,
    sds_limit: int | None = 25,
    thresholds: Thresholds | None = None,
) -> dict:
    sources = sources or list(SUPPLIER_MODULES)
    thresholds = thresholds or Thresholds()
    targets, seeds = load_config(config_dir)

    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir = out_dir.parent / "data"
    session = build_session(targets, data_dir / "cache")

    products = harvest_suppliers(session, seeds, sources, limit_per_source)
    if not products:
        raise RuntimeError("no products harvested; check network and target config")

    references = build_reference(session, products, seeds)
    sds_records = harvest_sds(session, products, sds_limit)

    results = match_products(products, references, thresholds)
    ref_by_id = {r.ref_id: r for r in references}
    findings = quality.run_all(products, ref_by_id, results, sds_records)
    clusters = cluster_by_substance(products, results)
    metrics = evaluate.run_evaluation(products, references, thresholds)

    decisions = {d.value: 0 for d in Decision}
    for res in results:
        decisions[res.decision.value] += 1

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
        "counts": {
            "supplier_skus": len(products),
            "by_source": {
                s: sum(1 for p in products if p.source == s) for s in sources
            },
            "reference_substances": len(references),
            "sds_parsed": len(sds_records),
            "substance_clusters": len(clusters),
            "multi_supplier_clusters": sum(
                1
                for keys in clusters.values()
                if len({k.split(":")[0] for k in keys}) > 1
            ),
        },
        "match_decisions": decisions,
        "quality_findings": quality.summarise(findings),
        "evaluation": metrics,
        "http": session.stats,
    }

    _write(out_dir / "products.json", [p.to_dict() for p in products])
    _write(out_dir / "reference.json", [r.to_dict() for r in references])
    _write(out_dir / "matches.json", [r.to_dict() for r in results])
    _write(out_dir / "findings.json", [f.to_dict() for f in findings])
    _write(out_dir / "clusters.json", clusters)
    _write(out_dir / "sds.json", {k: v.to_dict() for k, v in sds_records.items()})
    _write(out_dir / "summary.json", summary)
    _write_review_queue(out_dir / "review_queue.json", results)

    return summary


def _write(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    log.info("wrote %s", path)


def _write_review_queue(path: Path, results) -> None:
    """The ambiguous tail, ordered so a human starts with the closest calls."""
    queue = [r.to_dict() for r in results if r.decision is Decision.REVIEW]
    queue.sort(key=lambda r: -r["score"])
    _write(path, queue)
