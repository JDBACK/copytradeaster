"""CLI entry point. Subcommands are implemented by later plan steps."""

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="copytradeaster", description="AsterDex copy-trade engine"
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    # Placeholder until subcommands land (plan step 4.4).
    print("copytradeaster: no command given", file=sys.stderr)
    parser.print_help(sys.stderr)
    del args
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
