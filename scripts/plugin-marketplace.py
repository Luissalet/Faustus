"""Explicit source acquisition only; never launches or installs plugin code."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import plugin_marketplace as marketplace


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root")
    parser.add_argument("--data-dir")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    for action in ("install", "unlink"):
        commands.add_parser(action).add_argument("id")
    link = commands.add_parser("link")
    link.add_argument("id")
    link.add_argument("path")
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("ids", nargs="*")
    bootstrap.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    options = {"repo_root": args.root, "data_dir": args.data_dir}
    try:
        if args.command == "list":
            result = marketplace.list_marketplace(**options)
        elif args.command == "link":
            result = marketplace.link(args.id, args.path, **options)
        elif args.command == "bootstrap":
            catalog = marketplace.list_marketplace(**options)
            known = {row["id"] for row in catalog["plugins"]}
            if set(args.ids) - known:
                raise marketplace.MarketplaceError("unknown_plugin", "Bootstrap contains an unknown plugin")
            selected = [row for row in catalog["plugins"] if row["id"] in args.ids] if args.ids else [
                row for row in catalog["plugins"] if args.all or row["required"]]
            results = []
            for row in selected:
                if row["can_install"]:
                    results.append(marketplace.install(row["id"], **options))
                elif row["state"] in {"linked", "cloned"}:
                    results.append(row)
                else:
                    raise marketplace.MarketplaceError("source_unavailable", "A selected plugin source needs attention")
            result = {"plugins": results}
        else:
            result = getattr(marketplace, args.command)(args.id, **options)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except marketplace.MarketplaceError as error:
        print(json.dumps({"error": error.message, "code": error.code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
