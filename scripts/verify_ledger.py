"""Validate the evidence hash chain. ``make verify-ledger``.

Exits non-zero on the first break, naming the tenant and the sequence number, because a
chain that does not verify is an audit finding and CI should treat it as one.

Also exports a tenant's chain as verified JSONL (``--export``), which is the form an
auditor receives: verified during the walk, not afterwards, so a broken chain is reported
rather than handed over as if it were sound.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent_forge.events.evidence import ChainError, EvidenceLedger, JsonlSink, verify_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path", default="./var/ledger", help="ledger root directory (default: ./var/ledger)"
    )
    parser.add_argument("--tenant", default="", help="verify or export a single tenant")
    parser.add_argument(
        "--export",
        default="",
        help="write the verified chain of --tenant to this file as JSONL",
    )
    args = parser.parse_args(argv)

    root = Path(args.path)
    if not root.exists():
        # An absent ledger is not a broken one: a cell that has answered nothing yet has
        # nothing to verify. Say so and succeed.
        print(f"no ledger at {root}: nothing to verify")
        return 0

    try:
        if args.export:
            return _export(root, args.tenant, Path(args.export))
        results = (
            {args.tenant: EvidenceLedger(JsonlSink(root)).verify(args.tenant)}
            if args.tenant
            else verify_path(root)
        )
    except ChainError as exc:
        location = f" (seq {exc.seq})" if exc.seq >= 0 else ""
        print(f"ledger chain BROKEN{location}: {exc}", file=sys.stderr)
        return 1

    if not results:
        print(f"no tenant chains under {root}")
        return 0

    for tenant, count in sorted(results.items()):
        print(f"  [OK ] {tenant}: {count} records")
    print(f"\nledger OK: {sum(results.values())} records across {len(results)} tenant(s)")
    return 0


def _export(root: Path, tenant: str, destination: Path) -> int:
    if not tenant:
        print("--export needs --tenant: a chain is exported per tenant", file=sys.stderr)
        return 2
    ledger = EvidenceLedger(JsonlSink(root))
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with destination.open("w", encoding="utf-8") as handle:
        for line in ledger.export(tenant):
            handle.write(line + "\n")
            written += 1
    print(f"exported {written} verified records for {tenant} -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
