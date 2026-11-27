"""Semantic changes to an existing card at a known board position."""

from .markdown import Card, Column, Document


# A tuple groups simultaneous transitions on one card. A single event name
# remains valid for callers submitting individual events.
type Event = tuple[str | tuple[str, ...], dict]

STATUS_CHANGED = "task.status.changed"


def _fields(card: Card, column: Column) -> dict:
    tokens = {match[0] for match in card.metadata}
    return {
        "text": card.text,
        "done": card.done,
        "owners": sorted(token[1:] for token in tokens if token.startswith("@")),
        "tags": sorted(token[1:] for token in tokens if token.startswith("#")),
        "status": column.name,
    }


def edit_events(old: Document, new: Document, section: int, column: int, path: str) -> list[Event]:
    """Events for an edit that leaves the card at the same column and path."""
    return _events(old, new, section, (column, path), (column, path))


def move_events(old: Document, new: Document, section: int, column: int, path: str, target: int) -> list[Event]:
    """Events for ``Document.move``, which appends the root task to ``target``."""
    if target == column:
        return []  # a move within one column only changes the order
    roots = new.sections[section].columns[target].cards
    return _events(old, new, section, (column, path), (target, str(len(roots))))


def _events(old: Document, new: Document, section: int, source: tuple[int, str],
            dest: tuple[int, str]) -> list[Event]:
    old_lane = old.sections[section].columns[source[0]]
    lane = new.sections[section].columns[dest[0]]
    before = _fields(old_lane.resolve(source[1]), old_lane)
    after = _fields(lane.resolve(dest[1]), lane)
    triggers = []
    if before["owners"] != after["owners"]:
        triggers.append("owner.changed")
    if before["tags"] != after["tags"]:
        triggers.append("tags.changed")
    if before["done"] != after["done"]:
        triggers.append("task.completed" if after["done"] else "task.reopened")
    # Compare positions, not names: two columns may share a name.
    if source[0] != dest[0]:
        triggers.append(STATUS_CHANGED)
    # The payload names where the task is now, so that a job leads back to it.
    payload = {
        "section": new.sections[section].name, "section_number": section + 1,
        "column": lane.name, "column_number": dest[0] + 1,
        "path": dest[1], "before": before, "after": after,
    }
    return [(tuple(triggers), payload)] if triggers else []
