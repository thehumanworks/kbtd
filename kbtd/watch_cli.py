"""Register board-local shell actions and inspect their execution history."""

import argparse
import json
from pathlib import Path

import sys

from .conditions import EVENTS, FIELDS, compile_trigger
from .watchers import Watchers


def add_arguments(parser: argparse.ArgumentParser, common: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="watch_command", required=True)
    add = commands.add_parser("add", parents=[common], help="register a shell command for this board file")
    add.add_argument("name", help="unique watcher name")
    add.add_argument("trigger", help=(
        f"event ({', '.join(EVENTS)}) or a quoted Python condition, "
        "e.g. 'owner.changed and \"codex\" in task.owners and task.status == \"Backlog\"'. "
        f"Names: {', '.join(name for name in FIELDS if name not in EVENTS)}; "
        "operators: and, or, not, ==, !=, in, not in."
    ))
    add.add_argument("action", help="one quoted shell command, run in the board directory")
    commands.add_parser("list", parents=[common], help="list configured watchers")
    remove = commands.add_parser("remove", parents=[common], help="remove a watcher")
    remove.add_argument("name")
    commands.add_parser("jobs", parents=[common], help="show job history and resume pending work")
    show = commands.add_parser("show", parents=[common], help="print one job, its event and output, as JSON")
    show.add_argument("id", type=int, help="job number from 'watch jobs'")
    retry = commands.add_parser("retry", parents=[common], help="rerun a failed, interrupted or cancelled job")
    retry.add_argument("id", type=int, help="job number from 'watch jobs'")
    cancel = commands.add_parser("cancel", parents=[common], help="cancel a queued job before it starts")
    cancel.add_argument("id", type=int, help="job number from 'watch jobs'")


def manage(path: Path, args: argparse.Namespace) -> None:
    watchers = Watchers(path)
    match args.watch_command:
        case "add":
            watchers.add(args.name, args.trigger, args.action)
            print(f"Registered {args.name} for {args.trigger}")
        case "remove":
            watchers.remove(args.name)
            print(f"Removed {args.name}")
        case "list":
            entries = watchers.list()
            if not entries:
                print("No watchers configured for this board.")
            for entry in entries:
                print(f"{entry['name']}\t{entry['trigger']}\t{entry['command']}")
                try:
                    compile_trigger(entry["trigger"])
                except ValueError as exc:
                    print(f"kbtd: watcher {entry['name']!r} never runs: {exc}. "
                          "Remove it and register it again.", file=sys.stderr)
        case "jobs":
            jobs = watchers.jobs()
            if not jobs:
                print("No watcher jobs for this board.")
            else:
                print("ID\tWATCHER\tTRIGGER\tSTATUS\tATTEMPT\tDETAIL")
                for job in jobs:
                    print(f"{job['id']}\t{job['name']}\t{job['trigger']}\t{job['status']}\t"
                          f"{job['attempt']}\t{job['detail'] or ''}")
        case "show":
            print(json.dumps(watchers.job(args.id), indent=2, ensure_ascii=False))
        case "retry":
            watchers.retry(args.id)
            print(f"Queued job {args.id} for retry")
        case "cancel":
            watchers.cancel(args.id)
            print(f"Cancelled job {args.id}")
