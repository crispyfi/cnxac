"""Loads a config folder: the desired state of its environment.

Problems are collected as issues rather than raised, so check and plan can report them all
at once.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import registry
from .config import ConfigError, load_yaml
from .files import file_name

UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SITE_FIELDS = ("address", "city", "state", "country", "zipcode")


@dataclass
class Issue:
    level: str        # "error" or "warning"
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.where}: {self.message}"


@dataclass
class Desired:
    assign: dict = field(default_factory=lambda: {"global": {}, "sites": {}})
    profiles: dict[str, dict[str, dict]] = field(default_factory=dict)   # type -> name -> body
    policy_group: dict | None = None
    nac: dict[str, dict[str, dict]] = field(default_factory=dict)        # type -> ID -> body
    files: dict[tuple[str, str], str] = field(default_factory=dict)      # (type, name or ID) -> file
    filled: dict[tuple[str, str], Path] = field(default_factory=dict)    # (type, ID) -> NAC file with empty IDs, now chosen; apply writes them in
    blank: set[tuple[str, str]] = field(default_factory=set)             # (type, ID) of NAC objects whose own ID was empty
    sites: dict[str, dict] | None = None                                 # name -> body for POST sites
    alias_values: dict[str, dict[str, str]] = field(default_factory=dict) # site -> alias name -> its value there
    identity: dict | None = None                                         # nac/identity-management.json, for the as-built
    issues: list[Issue] = field(default_factory=list)

    def error(self, where: str, message: str) -> None:
        self.issues.append(Issue("error", where, message))

    def warn(self, where: str, message: str) -> None:
        self.issues.append(Issue("warning", where, message))

    def where(self, type_path: str, key: str) -> str:
        return self.files.get((type_path, key), f"{type_path}/{key}")

    def assigned(self) -> set[tuple[str, str, str, str]]:
        """(scope kind, scope name, type, name) for every assignment in assign.yaml."""
        out = set()
        for type_path, names in self.assign["global"].items():
            out |= {("global", "", type_path, n) for n in names}
        for site, types in self.assign["sites"].items():
            for type_path, names in types.items():
                out |= {("site", site, type_path, n) for n in names}
        return out


def _read_json(d: Desired, path: Path, rel: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))   # some Windows editors add a byte order mark
    except json.JSONDecodeError as e:
        d.error(rel, f"invalid JSON (line {e.lineno})")
    except UnicodeDecodeError:
        d.error(rel, "not UTF-8 text")
    return None


def _names(d: Desired, where: str, value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(n, (str, int)) and not isinstance(n, bool) for n in value):
        d.error(where, "must be a list of names")
        return None
    return [str(n) for n in value]


def _types(d: Desired, where: str, value: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if value is None:
        return out
    if not isinstance(value, dict):
        d.error(where, "must map profile types to lists of names")
        return out
    for type_path, names in value.items():
        here = f"{where}.{type_path}"
        t = registry.BY_PATH.get(type_path)
        if type_path in registry.RETIRED:
            continue
        if t is None:
            d.error(here, "unknown profile type")
            continue
        if t.nac or t.single:
            d.error(here, "isn't listed in assign.yaml; the tool assigns it at Global")
            continue
        listed = _names(d, here, names)
        if listed is not None:
            out[type_path] = listed
    return out


def _alias_values(d: Desired, where: str, value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                              for k, v in value.items()):
        d.error(where, "must map alias names to their values at the site")
        return {}
    return dict(value)


def _load_assign(d: Desired, config_dir: Path) -> None:
    path = config_dir / "assign.yaml"
    if not path.is_file():
        d.error("assign.yaml", "not found")
        return
    try:
        data = load_yaml(path) or {}
    except ConfigError as e:
        d.error("assign.yaml", str(e))
        return
    if not isinstance(data, dict):
        d.error("assign.yaml", "must be a mapping")
        return
    for key in sorted(set(data) - {"global", "sites"}):
        d.error(f"assign.yaml: {key}", "unknown section; use global or sites")
    top = data.get("global")
    if isinstance(top, dict) and "alias-values" in top:
        d.error("assign.yaml: global.alias-values", "only sites take alias values; Global gets the alias's own value")
        top = {k: v for k, v in top.items() if k != "alias-values"}
    d.assign["global"] = _types(d, "assign.yaml: global", top)
    sites = data.get("sites") or {}
    if not isinstance(sites, dict):
        d.error("assign.yaml: sites", "must map site names to their profiles")
        return
    for site, types in sites.items():
        here = f"assign.yaml: sites.{site}"
        if isinstance(types, dict) and "alias-values" in types:
            values = _alias_values(d, f"{here}.alias-values", types["alias-values"])
            if values:
                d.alias_values[str(site)] = values
            types = {k: v for k, v in types.items() if k != "alias-values"}
        d.assign["sites"][str(site)] = _types(d, here, types)


def _load_objects(d: Desired, folder: Path, rel: str, types: tuple[registry.Type, ...],
                  into: dict[str, dict[str, dict]]) -> None:
    if not folder.is_dir():
        return
    by_path = {t.path: t for t in types}
    for sub in sorted(folder.iterdir()):
        if sub.name.startswith("."):
            continue
        if sub.is_file():
            if not ((rel, sub.name) in {("profiles", "policy-groups.json"), ("nac", registry.IDENTITY_FILE)}):
                d.error(f"{rel}/{sub.name}", "unexpected file")
            continue
        t = by_path.get(sub.name)
        if sub.name in registry.RETIRED:
            continue
        if t is None or t.single:
            d.error(f"{rel}/{sub.name}/", "unknown type folder")
            continue
        objs = into.setdefault(t.path, {})
        for path in sorted(sub.glob("*.json")):
            here = f"{rel}/{t.path}/{path.name}"
            body = _read_json(d, path, here)
            if body is None:
                continue
            if not isinstance(body, dict):
                d.error(here, "must be a JSON object")
                continue
            blank = t.nac and _empty(body, t.id_field)
            filled = t.nac and _fill_ids(t, body)
            if t.id_field not in body or body[t.id_field] in (None, ""):
                d.error(here, f"has no {t.id_field}")
                continue
            key = str(body[t.id_field])
            if t.nac and not UUID.match(key):
                d.error(here, f"{t.id_field} must be a UUID, or empty for a new object")
                continue
            if key in objs:
                d.error(here, f"{t.id_field} {key} is also in {d.files[(t.path, key)]}")
                continue
            name = str(body.get("name") or key) if t.nac else key
            if path.stem != file_name(name) and not path.stem.startswith(file_name(name) + "--"):
                d.warn(here, f"file name doesn't match its name {name!r}")
            objs[key] = body
            d.files[(t.path, key)] = here
            if filled:
                d.filled[(t.path, key)] = path
            if blank:
                d.blank.add((t.path, key))


def _empty(obj: dict, key: str) -> bool:
    return obj.get(key) in (None, "")


def _fill_ids(t: registry.Type, body: dict) -> bool:
    """Give a NAC object's empty IDs, its own and nested ones, new UUIDs. Only apply writes
    them into the file, so a plan made from it chooses again next time."""
    empty = [(body, t.id_field)] if _empty(body, t.id_field) else []
    empty += [(entry, id_key) for entry, _, id_key in registry.nested_ids(t, body) if _empty(entry, id_key)]
    for obj, id_key in empty:
        obj[id_key] = str(uuid.uuid4())
    return bool(empty)


def _load_sites(d: Desired, config_dir: Path) -> None:
    path = config_dir / "sites.yaml"
    if not path.is_file():
        return
    try:
        data = load_yaml(path) or {}
    except ConfigError as e:
        d.error("sites.yaml", str(e))
        return
    if not isinstance(data, dict) or not isinstance(data.get("sites") or {}, dict):
        d.error("sites.yaml", "must have a sites mapping")
        return
    default_tz = data.get("default_timezone")
    d.sites = {}
    for name, site in (data.get("sites") or {}).items():
        here = f"sites.yaml: {name}"
        if not isinstance(site, dict):
            d.error(here, "must be a mapping of address fields")
            continue
        missing = [k for k in SITE_FIELDS if site.get(k) in (None, "")]
        tz = site.get("timezone") or default_tz
        if not tz:
            missing.append("timezone (or default_timezone)")
        if missing:
            d.error(here, f"missing {', '.join(missing)}")
            continue
        d.sites[str(name)] = {"name": str(name), **{k: str(site[k]) for k in SITE_FIELDS},
                              "timezone": {"timezoneId": str(tz)}}


def load(config_dir: Path) -> Desired:
    d = Desired()
    _load_assign(d, config_dir)
    _load_objects(d, config_dir / "profiles", "profiles", registry.AP_TYPES, d.profiles)
    group_path = config_dir / "profiles" / "policy-groups.json"
    if group_path.is_file():
        group = _read_json(d, group_path, "profiles/policy-groups.json")
        if isinstance(group, dict) and isinstance(group.get("policy-group-list", []), list):
            d.policy_group = group
        elif group is not None:
            d.error("profiles/policy-groups.json", "must be an object with a policy-group-list")
    _load_objects(d, config_dir / "nac", "nac", registry.NAC_TYPES, d.nac)
    identity_path = config_dir / "nac" / registry.IDENTITY_FILE
    if identity_path.is_file():
        identity = _read_json(d, identity_path, f"nac/{registry.IDENTITY_FILE}")
        if isinstance(identity, dict):
            d.identity = identity
        elif identity is not None:
            d.error(f"nac/{registry.IDENTITY_FILE}", "must be a JSON object")
    _load_sites(d, config_dir)
    return d
