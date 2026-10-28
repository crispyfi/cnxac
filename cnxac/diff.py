"""Per-key differences between a live object and its file, as dotted paths."""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

MISSING = object()
# A list of mappings is compared item by item when one of these identifies every item, and each
# item is labelled by the first readable field it has, else its position, else its place
ID_KEYS = ("rule-id", "condition-id", "condition-group-id", "profile-id", "policy-id", "server-name",
           "attr-name", "name", "profile", "radio-type", "ap-model", "address", "type-process-subcategory",
           "position")
LABEL_KEYS = ("rule-name", "name", "server-name", "attr-name", "attr", "profile", "radio-type", "ap-model",
              "address", "type-process-subcategory")


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def diff(old: Any, new: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """(path, old, new) for every difference. Mappings are compared key by key, and lists of
    mappings item by item, matched by an ID (see ID_KEYS); any other changed list, or one
    whose order alone changed, is reported whole. MISSING marks a key or item on one side only."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for key in sorted(set(old) | set(new)):
            out += diff(old.get(key, MISSING), new.get(key, MISSING), f"{path}.{key}" if path else key)
        return out
    if old is not MISSING and new is not MISSING and _same(old, new):
        return []
    key = _id_key(old, new) if isinstance(old, list) and isinstance(new, list) else None
    if key:
        out = _items(old, new, key, path)
        return out or [(path, old, new)]   # same items, another order
    return [(path, old, new)]


def _id_key(old: list, new: list) -> str | None:
    """A key every item of both lists has, unique within each list, if all items are mappings."""
    items = old + new
    if not items or not all(isinstance(i, dict) for i in items):
        return None
    for key in ID_KEYS:
        if all(key in i for i in items) and all(len({_ident(i, key) for i in side}) == len(side) for side in (old, new)):
            return key
    return None


def _ident(item: dict, key: str) -> str:
    return json.dumps(item[key], sort_keys=True)


def _label(item: dict, place: int) -> str:
    for key in LABEL_KEYS:
        if isinstance(item.get(key), str) and item[key]:
            return item[key]
    return f"#{item['position']}" if "position" in item else f"#{place}"


def _items(old: list, new: list, key: str, path: str) -> list[tuple[str, Any, Any]]:
    """Items matched by key, labelled as they are in old; added ones as they are in new."""
    news = {_ident(i, key): i for i in new}
    olds = {_ident(i, key) for i in old}
    out = []
    for place, item in enumerate(old, 1):
        out += diff(item, news.get(_ident(item, key), MISSING), f"{path}[{_label(item, place)}]")
    for place, item in enumerate(new, 1):
        if _ident(item, key) not in olds:
            out.append((f"{path}[{_label(item, place)}]", MISSING, item))
    return out


def _reads_as_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def show(value: Any, limit: int | None = None) -> str:
    """A value as plain text where that's unambiguous, otherwise as JSON (so "100" and 100
    differ), cut to limit characters if one is given."""
    plain = isinstance(value, str) and value and value.strip() == value and not _reads_as_json(value)
    text = value if plain else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if limit is None or len(text) <= limit else text[:limit - 3] + "..."


def brief(changes: list[tuple[str, Any, Any]]) -> list[str]:
    """One line per change, values cut to 120 characters: for a note or a message."""
    out = []
    for path, old, new in changes:
        if old is MISSING:
            out.append(f"+ {path}: {show(new, 120)}")
        elif new is MISSING:
            out.append(f"- {path}: {show(old, 120)}")
        else:
            out.append(f"{path}: {show(old, 120)} → {show(new, 120)}")
    return out


def lines(changes: list[tuple[str, Any, Any]]) -> list[str]:
    """Changes to read, nothing cut off. A plain value takes one line. A list's items and a
    mapping's fields go one per line under its path, indented, with + or - on what comes or
    goes. Items added to or removed from a list of objects go under the list's path, one
    heading for a run of them."""
    out, heading = [], None
    for path, old, new in changes:
        parent, label = _parent(path)
        if label is not None and (old is MISSING or new is MISSING):
            if parent and parent != heading:
                out.append(parent)
            heading = parent
            mark, value = ("+", new) if old is MISSING else ("-", old)
            out += _block(mark, label, value, "  " if parent else "")
            continue
        heading = None
        out += _change(path, old, new)
    return out


def _plain(value: Any) -> bool:
    return not isinstance(value, (dict, list))


def _names(values: list) -> str:
    return ", ".join(show(v) for v in values)


def _parent(path: str) -> tuple[str, str | None]:
    """("a.b", "x") for a path "a.b[x]" naming a list item, else (path, None)."""
    if path.endswith("]") and "[" in path:
        i = path.rindex("[")
        return path[:i], path[i + 1:-1]
    return path, None


def _change(path: str, old: Any, new: Any) -> list[str]:
    if old is MISSING or new is MISSING:
        mark, value = ("+", new) if old is MISSING else ("-", old)
        if isinstance(value, list) and value and all(map(_plain, value)):
            return [path, *(f"  {mark} {show(v)}" for v in value)]
        return _block(mark, path, value, "")
    if isinstance(old, list) and isinstance(new, list):
        return _list_change(path, old, new)
    if _plain(old) and _plain(new):
        return [f"{path}: {show(old)} → {show(new)}"]
    return [path, *_block("-", "was", old, "  "), *_block("+", "now", new, "  ")]


def _list_change(path: str, old: list, new: list) -> list[str]:
    """The items of each list that the other lacks, matched by value."""
    removed, added = _unmatched(old, new), _unmatched(new, old)
    if not removed and not added:
        if all(map(_plain, old + new)):
            return [f"{path}: {_names(old)} → {_names(new)}"]
        return [f"{path}: the same items, in another order"]
    out = [path]
    for mark, items in (("-", removed), ("+", added)):
        for place, item in items:
            if _plain(item):
                out.append(f"  {mark} {show(item)}")
            else:
                out += _block(mark, _label(item, place) if isinstance(item, dict) else f"#{place}", item, "  ")
    return out


def _unmatched(items: list, others: list) -> list[tuple[int, Any]]:
    """(place, item) for each item with no equal one left in others."""
    left = Counter(json.dumps(o, sort_keys=True) for o in others)
    out = []
    for place, item in enumerate(items, 1):
        key = json.dumps(item, sort_keys=True)
        if left[key]:
            left[key] -= 1
        else:
            out.append((place, item))
    return out


def _block(mark: str, label: str, value: Any, indent: str) -> list[str]:
    """A whole value that comes or goes: its label after the mark, then its fields under it."""
    if _plain(value) or not value:
        return [f"{indent}{mark} {label}: {show(value)}"]
    if isinstance(value, list) and all(map(_plain, value)):
        return [f"{indent}{mark} {label}: {_names(value)}"]
    return [f"{indent}{mark} {label}", *_fields(value, indent + "    ", label)]


def _fields(value: Any, indent: str, label: str | None = None) -> list[str]:
    """A mapping's fields, or a list's items, one per line and nested by indent. The field an
    item is labelled by isn't repeated."""
    out = []
    if isinstance(value, list):
        for place, item in enumerate(value, 1):
            if _plain(item):
                out.append(f"{indent}{show(item)}")
                continue
            name = _label(item, place) if isinstance(item, dict) else f"#{place}"
            out += [f"{indent}{name}", *_fields(item, indent + "  ", name)]
        return out
    for key, v in value.items():
        if key in LABEL_KEYS and v == label:
            continue
        if _plain(v) or not v:
            out.append(f"{indent}{key}: {show(v)}")
        elif isinstance(v, list) and all(map(_plain, v)):
            out.append(f"{indent}{key}: {_names(v)}")
        else:
            out += [f"{indent}{key}", *_fields(v, indent + "  ")]
    return out
