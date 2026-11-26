"""Register board-local shell actions and inspect their execution history."""

import argparse
from pathlib import Path

from .conditions import EVENT_FIELDS
from .watchers import Watchers


def add_arguments(parser: argparse.ArgumentParser, common: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="watch_command", required=True)
    add = commands.add_parser("add", parents=[common], help="register a shell command for this board file")
    add.add_argument("name", help="unique watcher name")
    add.add_argument("trigger", help=(
        f"event ({', '.join(EVENT_FIELDS)}) or quoted condition, "
        r'e.g. owner.changed = "codex" /\ task.status = "Backlog". '
        r'Use /\ (and), \/ (or), ~ (not) and parentheses.'
    ))
    add.add_argument("action", help="one quoted shell command, run in the board directory")
    commands.add_parser("list", parents=[common], help="list configured watchers")
    remove = commands.add_parser("remove", parents=[common], help="remove a watcher")
    remove.add_argument("name")
    commands.add_parser("jobs", parents=[common], help="show job history and resume pending work")
    retry = commands.add_parser("retry", parents=[common], help="rerun a failed or interrupted job")
    retry.add_argument("id", type=int, help="job number from 'watch jobs'")


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
        case "jobs":
            jobs = watchers.jobs()
            if not jobs:
                print("No watcher jobs for this board.")
            else:
                print("ID\tWATCHER\tTRIGGER\tSTATUS\tATTEMPT\tDETAIL")
                for job in jobs:
                    print(f"{job['id']}\t{job['name']}\t{job['trigger']}\t{job['status']}\t"
                          f"{job['attempt']}\t{job['detail'] or ''}")
        case "retry":
            watchers.retry(args.id)
            print(f"Queued job {args.id} for retry")
