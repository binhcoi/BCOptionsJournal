"""``python3 -m bcoj.web`` -- run the journal."""

import argparse
import os

from .server import serve


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="bcoj.web", description="Serve the options journal locally."
    )
    parser.add_argument(
        "--db", default=os.environ.get("BCOJ_DB", "journal.db"),
        help="SQLite journal file (default: journal.db, or $BCOJ_DB)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="bind address; loopback by default, and there is rarely a reason"
             " to change it",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    serve(args.db, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
