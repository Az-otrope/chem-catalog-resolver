"""Command line entry point.

    python -m chemspec run --limit 30
    python -m chemspec run --sources loba chemimpex --no-sds
    python -m chemspec check-targets
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .http import PoliteSession, RobotsDisallowed, SourceNotAllowed
from .match import Thresholds
from .pipeline import SUPPLIER_MODULES, build_session, load_config, run

ROOT = Path(__file__).resolve().parents[2]


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_run(args: argparse.Namespace) -> int:
    summary = run(
        config_dir=args.config,
        out_dir=args.out,
        sources=args.sources,
        limit_per_source=args.limit,
        sds_limit=0 if args.no_sds else args.sds_limit,
        thresholds=Thresholds(accept=args.accept, review=args.review),
    )
    print(json.dumps(summary, indent=2))
    print(f"\nArtefacts written to {args.out}/", file=sys.stderr)
    return 0


def cmd_check_targets(args: argparse.Namespace) -> int:
    """Re-verify every allow-listed host against its live robots.txt.

    Worth running before any re-crawl: permission is a moving target, and a
    config comment recording what was true in September is not evidence about
    today.
    """
    targets, _ = load_config(args.config)
    session = build_session(targets, args.out.parent / "data" / "cache")

    ok = True
    for entry in targets.get("allowed", []):
        host = entry["host"]
        probe = f"https://{host}/"
        try:
            session.check(probe)
            delay = session.crawl_delay_for(f"https://{host}")
            print(f"  ALLOWED  {host:32} crawl-delay={delay:g}s  ({entry['role']})")
        except (RobotsDisallowed, SourceNotAllowed) as exc:
            ok = False
            print(f"  BLOCKED  {host:32} {exc}")

    print("\nDeclined at selection time (not re-probed):")
    for entry in targets.get("declined", []):
        print(f"  {entry['host']:32} {entry['reason']}")

    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chemspec", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--config", type=Path, default=ROOT / "config")
    parser.add_argument("--out", type=Path, default=ROOT / "reports")

    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="harvest, resolve, check and report")
    run_p.add_argument(
        "--sources", nargs="+", choices=list(SUPPLIER_MODULES), default=None
    )
    run_p.add_argument(
        "--limit", type=int, default=40, help="max SKUs per supplier (0 = no cap)"
    )
    run_p.add_argument("--sds-limit", type=int, default=25)
    run_p.add_argument("--no-sds", action="store_true")
    run_p.add_argument("--accept", type=float, default=0.92)
    run_p.add_argument("--review", type=float, default=0.78)
    run_p.set_defaults(func=cmd_run)

    check_p = sub.add_parser(
        "check-targets", help="re-verify robots.txt for every allow-listed host"
    )
    check_p.set_defaults(func=cmd_check_targets)

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    if getattr(args, "limit", None) == 0:
        args.limit = None
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
