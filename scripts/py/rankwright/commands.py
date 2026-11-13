"""The schema, index, rank and publish subcommands.

Kept apart from cli.py so the audit, geo and fix commands stay exactly as they
were. Every command here that can change something outside this machine follows
one rule: the default run is a preview, and the write happens only behind an
explicit flag (`--submit`, `--apply`, `--publish`).
"""
from __future__ import annotations

import argparse
import json
import sys

from . import indexing, publish_webflow, rank, schema_gen, social
from .config import Config, ConfigError
from .fetch import make_client

COMMANDS = {"schema", "index", "rank", "publish"}


def _site_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("url", nargs="?", help="Site root. Omit when using --config.")
    parser.add_argument("--config", help="Path to rankwright.config.json")


def register(sub) -> None:
    schema = sub.add_parser(
        "schema", help="Generate JSON-LD from each page's real content")
    _site_args(schema)
    schema.add_argument("--out", default=".rankwright/schema",
                        help="Output directory (default: .rankwright/schema)")
    schema.add_argument("--page", action="append", default=None, metavar="URL",
                        help="Generate for this URL only. Repeatable. "
                             "Default: every sitemap URL.")

    index = sub.add_parser(
        "index", help="Submit new and changed URLs through IndexNow. "
                      "Previews unless --submit is given.")
    _site_args(index)
    index.add_argument("--key", help="IndexNow key (default: the indexnow secret "
                                     "or indexing.indexnowKeyFile)")
    index.add_argument("--key-location", help="URL of the key file "
                                              "(default: <site>/<key>.txt)")
    index.add_argument("--state", default=indexing.DEFAULT_STATE,
                       help=f"Hash state file (default: {indexing.DEFAULT_STATE})")
    index.add_argument("--all", action="store_true",
                       help="Include unchanged URLs too")
    index.add_argument("--submit", action="store_true",
                       help="Actually send the URLs. Without this nothing is sent.")
    index.add_argument("--keygen", metavar="DIR",
                       help="Create a new key file in DIR and exit")
    index.add_argument("--json", action="store_true", help="Print the result as JSON")

    track = sub.add_parser("rank", help="Record keyword positions and show movement")
    _site_args(track)
    track.add_argument("--provider", choices=rank.PROVIDERS,
                       help="gsc (Search Console) or serpapi. "
                            "Default: rank.provider in config, else gsc.")
    track.add_argument("--days", type=int, default=None,
                       help="Search Console window in days (default 28)")
    track.add_argument("--top", type=int, default=50,
                       help="Search Console: rows to keep when no keywords are "
                            "configured (default 50)")
    track.add_argument("--history", default=rank.DEFAULT_HISTORY,
                       help=f"History file (default: {rank.DEFAULT_HISTORY})")
    track.add_argument("--no-save", action="store_true",
                       help="Compare with history but do not append this run")
    track.add_argument("--json", action="store_true", help="Print the result as JSON")

    publish = sub.add_parser(
        "publish", help="Publish approved changes. Previews unless told to write.")
    targets = publish.add_subparsers(dest="target", required=True)

    webflow = targets.add_parser("webflow", help="SEO fields and JSON-LD on Webflow")
    actions = webflow.add_subparsers(dest="action", required=True)
    plan = actions.add_parser("plan", help="Write a plan file. Changes nothing.")
    apply = actions.add_parser(
        "apply", help="Preview approved plan entries; write them with --apply")
    for parser in (plan, apply):
        parser.add_argument("--config", required=True,
                            help="Path to rankwright.config.json")
        parser.add_argument("--plan", default=publish_webflow.DEFAULT_PLAN,
                            help=f"Plan file (default: {publish_webflow.DEFAULT_PLAN})")
    plan.add_argument("--force", action="store_true",
                      help="Overwrite an existing plan file")
    apply.add_argument("--apply", action="store_true",
                       help="Write approved entries to Webflow. Saved, not live.")
    apply.add_argument("--publish", action="store_true",
                       help="With --apply: also make the changed pages live")
    apply.add_argument("--json", action="store_true", help="Print the result as JSON")

    soc = targets.add_parser("social", help="Facebook, Instagram and LinkedIn posts")
    soc_actions = soc.add_subparsers(dest="action", required=True)
    draft = soc_actions.add_parser("draft", help="Write draft files from a page")
    draft.add_argument("page", help="URL of the published page to draft from")
    send = soc_actions.add_parser(
        "send", help="Preview approved drafts; post them with --apply")
    send.add_argument("--apply", action="store_true",
                      help="Actually post approved drafts")
    send.add_argument("--json", action="store_true", help="Print the result as JSON")
    for parser in (draft, send):
        parser.add_argument("--config", help="Path to rankwright.config.json")
        parser.add_argument("--queue", help="Queue directory "
                                            f"(default: {social.DEFAULT_QUEUE})")
    draft.add_argument("--platform", action="append", default=None,
                       choices=["facebook", "instagram", "linkedin", "x", "youtube"],
                       help="Repeatable. Default: smm.platforms in config.")


def _err(message: str) -> None:
    print(f"rankwright: {message}", file=sys.stderr)


def _schema(args, config: Config) -> int:
    results, unreachable = schema_gen.generate(config, args.out, urls=args.page)
    for result in results:
        print(f"  {result.url}")
        print(f"      {', '.join(result.types) or 'nothing generated'}")
        for note in result.notes:
            print(f"      note: {note}")
    for url in unreachable:
        print(f"  UNREACHABLE {url}")
    print(f"  wrote {len(results)} page(s) to {args.out} (.jsonld, .html, manifest.json)")
    print("  Nothing was published. Add each .html snippet to its page's <head>.")
    return 1 if unreachable else 0


def _index(args, config: Config) -> int:
    if args.keygen:
        path = indexing.write_key_file(indexing.new_key(), args.keygen)
        print(f"  wrote {path}")
        print(f"  Serve it at {config.site_url}/{path.name}, then set the key as the "
              "indexnow secret or pass --key.")
        return 0

    key = indexing.resolve_key(config, args.key) if args.submit else None
    state = indexing.load_state(args.state)
    client = make_client()
    try:
        found = indexing.plan(config, client, state)
        urls = list(found.hashes) if args.all else found.to_submit
        output: dict = {"site": config.site_url, **found.to_dict(),
                        "selected": len(urls), "submitted": False}

        results = []
        blocked = None
        if args.submit and urls:
            location = indexing.key_location(config, key, args.key_location)
            blocked = indexing.verify_key_file(client, key, location)
            if blocked is None:
                results = indexing.submit(client, config, key, location, urls)
                indexing.save_state(args.state, indexing.record(state, found, results))
                output["submitted"] = any(r.accepted for r in results)
                output["batches"] = [r.to_dict() for r in results]
            else:
                output["blocked"] = blocked
    finally:
        client.close()

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print(f"IndexNow — {config.site_url}")
        print(f"  {len(found.new)} new · {len(found.changed)} changed · "
              f"{len(found.unchanged)} unchanged · {len(found.unreachable)} unreachable")
        for label, items in (("new", found.new), ("changed", found.changed)):
            for url in items[:20]:
                print(f"    {label:<8}{url}")
            if len(items) > 20:
                print(f"    …and {len(items) - 20} more {label}")
        for url in found.unreachable:
            print(f"    UNREACHABLE {url}")
        for url in found.foreign:
            print(f"    SKIPPED (other host) {url}")
        if blocked:
            print(f"  NOT SUBMITTED: {blocked}")
        elif results:
            for r in results:
                print(f"  {len(r.urls)} URL(s): HTTP {r.status} — {r.detail}")
            print("  Received is not indexed: each engine decides what to crawl.")
        elif not urls:
            print("  Nothing to submit.")
        else:
            print(f"  Preview only: {len(urls)} URL(s) would be submitted. "
                  "Re-run with --submit to send them.")
    if blocked or any(not r.accepted for r in results):
        return 1
    return 0


def _rank(args, config: Config) -> int:
    provider = args.provider or rank.rank_config(config).get("provider", "gsc")
    days = args.days or rank.rank_config(config).get("days", 28)
    client = make_client(timeout=30.0)
    try:
        if provider == "gsc":
            snapshot = rank.fetch_gsc(client, config, rank.gsc_token(config),
                                      days=days, top=args.top)
        else:
            snapshot = rank.fetch_serpapi(client, config, config.secret("serpapi"))
    finally:
        client.close()
    history = rank.load_history(args.history)
    rows = rank.compare(snapshot, rank.previous_snapshot(history, provider,
                                                         config.site_url))
    if not args.no_save:
        rank.append_history(args.history, snapshot)
    if args.json:
        print(json.dumps({**snapshot.to_dict(), "rows": rows}, indent=2))
    else:
        print(rank.format_table(snapshot, rows))
        if not args.no_save:
            print(f"  saved to {args.history}")
    return 0


def _print_actions(results: list[dict]) -> None:
    for r in results:
        print(f"  [{r['status']}] {r.get('title') or r['pageId']}")
        for key, change in r.get("changes", {}).items():
            print(f"      {key}: {change['from']!r} -> {change['to']!r}")
        if r.get("jsonld"):
            print(f"      JSON-LD: {r['jsonld']['file']} "
                  f"({r['jsonld']['scriptChars']} chars)")
        for note in r.get("notes", []):
            print(f"      note: {note}")
        if r.get("error"):
            print(f"      error: {r['error']}")


def _webflow(args, config: Config) -> int:
    if args.action == "plan":
        from pathlib import Path
        if Path(args.plan).exists() and not args.force:
            _err(f"{args.plan} already exists and may hold approvals. "
                 "Use --force to overwrite it.")
            return 2
        client = publish_webflow.make_client(config)
        try:
            plan = publish_webflow.build_plan(config, client)
        finally:
            client.close()
        path = publish_webflow.write_plan(plan, args.plan)
        flagged = sum(1 for e in plan["entries"] if e["issues"])
        print(f"  wrote {path}: {len(plan['entries'])} page(s), {flagged} with issues, "
              f"{len(plan['skipped'])} skipped")
        print('  Fill in "proposed", set "approved": true, then run '
              "`rankwright publish webflow apply`.")
        return 0

    if args.publish and not args.apply:
        _err("--publish only makes sense with --apply.")
        return 2
    plan = publish_webflow.load_plan(args.plan)
    client = publish_webflow.make_client(config) if args.apply else None
    try:
        results = publish_webflow.apply_plan(plan, client, write=args.apply)
        published = []
        if args.apply:
            applied = [r["pageId"] for r in results if r["status"] == "applied"]
            if args.publish and applied:
                published = publish_webflow.publish_pages(
                    client, plan["siteId"], applied)
            publish_webflow.append_log(publish_webflow.DEFAULT_LOG,
                                       [*results, *published])
    finally:
        if client is not None:
            client.close()

    if args.json:
        print(json.dumps({"wrote": args.apply, "results": results,
                          "published": published}, indent=2, ensure_ascii=False))
    else:
        print("Webflow — " + ("applying" if args.apply else "preview (writes nothing)"))
        _print_actions(results)
        if not results:
            print("  No entry has a proposed change.")
        for p in published:
            print(f"  publish {p['pageId']}: {p['status']} {p.get('error', '')}".rstrip())
        if args.apply and not args.publish and any(
                r["status"] == "applied" for r in results):
            print("  Saved in Webflow, not live. Publish from the Designer, or re-run "
                  "with --apply --publish.")
        elif not args.apply:
            print("  Re-run with --apply to write the approved entries.")
    failed = any(r["status"] in {"failed", "stale"} for r in [*results, *published])
    return 1 if failed else 0


def _social(args, config: Config) -> int:
    client = make_client(timeout=30.0)
    try:
        if args.action == "draft":
            try:
                written, skipped = social.draft(config, client, args.page,
                                                only=args.platform, out=args.queue)
            except social.SocialError as exc:
                _err(str(exc))
                return 1
            for path in written:
                print(f"  wrote {path}")
            for reason in skipped:
                print(f"  skipped {reason}")
            if written:
                print('  Edit the text, set "approved": true, then run '
                      "`rankwright publish social send`.")
            return 0
        results = social.publish(config, client, out=args.queue, write=args.apply)
    finally:
        client.close()

    if args.json:
        print(json.dumps({"wrote": args.apply, "results": results}, indent=2))
    else:
        print("Social — " + ("posting" if args.apply else "preview (posts nothing)"))
        for r in results:
            line = f"  [{r['status']}] {r['platform']}  {r['file']}"
            if r.get("postId"):
                line += f"  id {r['postId']}"
            print(line)
            if r.get("error"):
                print(f"      {r['error']}")
        if not results:
            print("  The queue is empty.")
        elif not args.apply:
            print("  Re-run with --apply to post the approved drafts.")
    return 1 if any(r["status"] in {"failed", "blocked"} for r in results) else 0


def run(args, config: Config) -> int:
    try:
        if args.command == "schema":
            return _schema(args, config)
        if args.command == "index":
            return _index(args, config)
        if args.command == "rank":
            return _rank(args, config)
        if args.target == "webflow":
            return _webflow(args, config)
        return _social(args, config)
    except ConfigError as exc:
        _err(str(exc))
        return 2
    except publish_webflow.WebflowError as exc:
        _err(str(exc))
        return 1
