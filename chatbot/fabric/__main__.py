"""CLI entry point for Urban Fabric dataset building: ``uv run python -m chatbot.fabric build``."""

from __future__ import annotations

import argparse
import sys

from chatbot.fabric.build import build


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m chatbot.fabric", description="Urban fabric, heat & 15-min city builder")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="Clip Kontur, composite Landsat LST, read Overture & CyNAP gradients, and write data/fabric_nicosia.json.gz")
    b.add_argument("--skip-landsat", action="store_true", help="Reuse cached Landsat composite if present")
    b.add_argument("--refresh-overture", action="store_true", help="Re-query Overture base layers from S3")
    b.add_argument("--refresh-gradients", action="store_true", help="Re-fetch CyNAP road gradients")
    args = parser.parse_args(argv)
    if args.cmd == "build":
        build(
            skip_landsat=args.skip_landsat,
            refresh_overture=args.refresh_overture,
            refresh_gradients=args.refresh_gradients,
        )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
