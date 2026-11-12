"""Command line: ``python -m bucketcast <command>``.

Commands use the same environment variables as the app, so with
``BUCKETCAST_BUCKET`` set they act on the bucket, and without it on the local
``.local-bucket`` folder.
"""

from __future__ import annotations

import argparse
import sys

from . import load_config
from .importer import FeedImportError, Importer, format_report
from .storage import storage_from_config


def _import(args: argparse.Namespace) -> int:
    config = load_config()
    importer = Importer(storage_from_config(config),
                        base_url=args.base_url or config.get("PUBLIC_BASE_URL", ""))
    where = f"bucket {config['BUCKET']}" if config.get("BUCKET") else (
        f"local folder {config['LOCAL_STORAGE_DIR']}")
    print(f"Importing into {where}" + (" (dry run)" if args.dry_run else ""))
    try:
        report = importer.run(args.show, args.feed, dry_run=args.dry_run, limit=args.limit)
    except FeedImportError as e:
        print(f"Import stopped: {e}", file=sys.stderr)
        return 2
    print(format_report(report))
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bucketcast",
                                     description="Bucketcast command-line tools.")
    commands = parser.add_subparsers(dest="command", required=True)

    imp = commands.add_parser(
        "import", help="copy a show from an existing podcast RSS feed",
        description="Copy a show (settings, artwork, episodes, audio, notes) from an "
                    "existing podcast feed, keeping every episode's GUID. Safe to re-run.")
    imp.add_argument("--show", required=True, help="ID for the show here, e.g. my-podcast")
    imp.add_argument("--feed", required=True, help="address of the existing RSS feed")
    imp.add_argument("--dry-run", action="store_true",
                     help="list what would be imported without downloading or changing anything")
    imp.add_argument("--limit", type=int, default=None,
                     help="import at most this many episodes this run (oldest first)")
    imp.add_argument("--base-url", default="",
                     help="public address of the app, for links in the generated feed "
                          "(local storage only)")
    imp.set_defaults(func=_import)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
