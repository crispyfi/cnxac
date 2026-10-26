"""Stable file output, so git diffs show only real changes."""
from __future__ import annotations

import csv
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import yaml

from . import diff

UNSAFE = set('/\\:*?"<>|%')
# Windows can't create these as file names, with or without an extension
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"{port}{n}" for port in ("COM", "LPT") for n in range(10))}


def file_name(name: str) -> str:
    """An object name as a file name stem. Unsafe characters are percent-encoded, and so is
    the first character of a hidden or Windows-reserved name; the name inside the file stays
    the source of truth."""
    if not name:
        raise ValueError("empty object name")
    stem = "".join(f"%{ord(c):02X}" if c in UNSAFE or ord(c) < 32 or ord(c) == 127 else c for c in name)
    if stem.startswith(".") or stem.split(".")[0].rstrip(" ").upper() in RESERVED:
        return f"%{ord(stem[0]):02X}" + stem[1:]
    return stem


def name_from_file(stem: str) -> str:
    return unquote(stem)


def dump_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def dump_yaml(obj: Any) -> str:
    """Block mappings, with lists of names on one line."""
    return yaml.safe_dump(obj, sort_keys=False, default_flow_style=None, allow_unicode=True, width=10_000)


def write(path: Path, text: str) -> str:
    """Write text only if it differs from the file. Returns "new", "changed" or "unchanged"."""
    if path.is_file():
        if path.read_text(encoding="utf-8") == text:
            return "unchanged"
        status = "changed"
    else:
        status = "new"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")   # the same bytes on Windows
    return status


@dataclass
class Staged:
    """A file a pull would write, and how it differs from the file there now."""
    path: Path
    rel: str                                          # as shown, relative to the config folder
    text: str
    status: str                                       # new, changed, reformatted or unchanged
    changes: list[str] = field(default_factory=list)  # for changed: diff lines, from the file to Central


def stage(path: Path, rel: str, text: str, parse: Callable[[str], Any],
          describe: Callable[[Any, Any], list[str]] | None = None) -> Staged:
    """How text differs from the file at path. A file whose text differs but that parses to
    the same content is only reformatted. parse must never return a secret value. describe
    turns the parsed file and text into lines to read; by default they're diff lines."""
    if not path.is_file():
        return Staged(path, rel, text, "new")
    try:
        now = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return Staged(path, rel, text, "changed", ["isn't UTF-8 text now"])
    if now == text:
        return Staged(path, rel, text, "unchanged")
    try:
        old = parse(now.removeprefix("\ufeff"))   # some Windows editors add a byte order mark
    except (ValueError, yaml.YAMLError, csv.Error):
        return Staged(path, rel, text, "changed", ["doesn't parse now"])
    new = parse(text)
    if old == new:
        return Staged(path, rel, text, "reformatted")
    changes = describe(old, new) if describe else diff.lines(diff.diff(old, new))
    return Staged(path, rel, text, "changed", changes)


def write_staged(staged: list[Staged]) -> None:
    for s in staged:
        if s.status != "unchanged":
            write(s.path, s.text)
