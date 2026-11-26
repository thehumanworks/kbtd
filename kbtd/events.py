"""Semantic changes to an existing card at a known board position."""

from .markdown import Card, Document


# A tuple groups simultaneous transitions on one card. A single event name
# remains valid for callers submitting individual events.
type Event = tuple[str | tuple[str, ...], dict]


def _fields(card: Card) -> dict:
    tokens = {match[0] for match in card.metadata}
    return {
        "text": card.text,
        "done": card.done,
        "owners": sorted(token[1:] for token in tokens if token.startswith("@")),
        "tags": sorted(token[1:] for token in tokens if token.startswith("#")),
    }


def edit_events(old: Document, new: Document, section: int, column: int, path: str) -> list[Event]:
    board = old.sections[section]
    lane = board.columns[column]
    before = _fields(lane.resolve(path))
    after = _fields(new.sections[section].columns[column].resolve(path))
    triggers = []
    if before["owners"] != after["owners"]:
        triggers.append("owner.changed")
    if before["tags"] != after["tags"]:
        triggers.append("tags.changed")
    if before["done"] != after["done"]:
        triggers.append("task.completed" if after["done"] else "task.reopened")
    payload = {
        "section": board.name, "section_number": section + 1,
        "column": lane.name, "column_number": column + 1,
        "path": path, "before": before, "after": after,
    }
    return [(tuple(triggers), payload)] if triggers else []
