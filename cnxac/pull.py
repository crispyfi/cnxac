"""pull: export an environment's live config into its config folder.

The folder decides what's managed. A plain pull refreshes the objects that already have a
file: AP profiles in profiles/, NAC objects in nac/, their assignments in assign.yaml and
their policy order entries in profiles/policy-groups.json. A folder with no files yet but
an assign.yaml is seeded from it: the AP profiles it lists, and the auth profiles that serve
its WLANs. (Once there are files, assign.yaml doesn't add any, so a file deleted to stop
managing its object stays deleted.) pull --all also adds every AP
profile assigned anywhere and every NAC object, to seed a folder; what isn't wanted is then
deleted, and later pulls leave it out. pull --add <type> does the same for one type, to adopt
objects made in the UI.

Secret and generated fields are left out, as are system, derived, ignored and factory
default objects. Nothing in Central is changed, and no file is deleted: files the pull
didn't write are listed instead. stage works out the files without writing them, so the
command can show what changes and ask first.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import desired, registry
from .config import EnvConfig
from .files import Staged, dump_json, dump_yaml, file_name, stage as stage_file, write_staged
from .live import Live


@dataclass
class Report:
    written: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    files: list[Staged] = field(default_factory=list)
    local: list[str] = field(default_factory=list)
    unmanaged: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)    # assigned, but not in Central's library
    gone: list[str] = field(default_factory=list)         # files whose object Central doesn't have
    unknown_scopes: list[str] = field(default_factory=list)
    secret_like: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    unassigned: Counter = field(default_factory=Counter)
    not_held: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))   # type -> names with no file
    used_outside: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    nothing_held: bool = False

    def sections(self) -> list[tuple[str, list[str]]]:
        """(title, items) for each non-empty list worth reading through."""
        sections = [
            ("Local profiles, not pulled (turn them into library profiles)", self.local),
            ("Assigned but not managed by cnx-as-code", self.unmanaged),
            ("Assigned, but not in Central's library", self.not_found),
            ("Assigned at a scope that isn't Global or a site (e.g. a site collection)", self.unknown_scopes),
            ("Secret-like fields left out (list each in the registry as secret or not)", self.secret_like),
            ("Duplicate names (the file name has the ID appended)", self.duplicates),
            ("Files this pull didn't write (left in place)", self.stale),
        ]
        return [(title, items) for title, items in sections if items]

    def summaries(self) -> list[str]:
        """One line each for what was left out, counted by type or reason."""
        out = []
        if self.used_outside:
            out.append("Left out of assign.yaml, assigned only where something not in the folder uses them: "
                       + ", ".join(f"{t} {n}" for t, n in self.used_outside.items()))
        if self.unassigned:
            out.append("Not assigned anywhere, not pulled: "
                       + ", ".join(f"{t} {n}" for t, n in self.unassigned.items()))
        if self.skipped:
            out.append("Skipped: " + ", ".join(f"{n} {why}" for why, n in sorted(self.skipped.items())))
        return out


def name_key(name: str) -> tuple:
    return (0, int(name), "") if name.isdigit() else (1, name.casefold(), name)


def block(types: dict[str, set[str] | list[str]]) -> dict[str, list[str]]:
    """One scope's assignments, types in registry order and names sorted."""
    return {t.path: sorted(types[t.path], key=name_key) for t in registry.AP_TYPES if t.path in types}


def pull(live: Live, config_dir: Path, env: EnvConfig, everything: bool = False, add: Collection[str] = ()) -> Report:
    """stage, then write the files without asking."""
    rep = stage(live, config_dir, env, everything, add)
    write_staged(rep.files)
    return rep


def shown_json(t: registry.Type | None) -> Callable[[str], Any]:
    """Parses a JSON file for showing what a pull changes, with any secret value hidden."""
    return lambda text: registry.masked(t, json.loads(text)) if t else json.loads(text)


def stage(live: Live, config_dir: Path, env: EnvConfig, everything: bool = False,
          add: Collection[str] = ()) -> Report:
    """The files a pull would write, and how each differs from the folder's, without writing.
    Objects of the types in add that have no file get one, as with everything."""
    rep = Report()
    held = desired.load(config_dir)   # the objects that already have a file
    listed = held.assigned()
    from_assign = not any(held.profiles.values()) and not any(held.nac.values())   # seeded from assign.yaml
    if from_assign and not (everything or add or listed):
        rep.nothing_held = True
        return rep
    written: set[Path] = set()

    def save(area: str, path: Path, text: str, parse: Callable[[str], Any]) -> None:
        staged = stage_file(path, path.relative_to(config_dir).as_posix(), text, parse)
        rep.written[area][staged.status] += 1
        rep.files.append(staged)
        written.add(path)

    # Assignments, of the profiles this pull writes
    found = live.classify(env)
    rep.unmanaged, rep.unknown_scopes, rep.skipped = found.unmanaged, found.unknown_scopes, found.skipped
    wanted = {t.path: set(held.profiles.get(t.path, {})) for t in registry.AP_TYPES}
    if from_assign:
        for _, _, type_path, name in listed:
            if type_path in wanted:
                wanted[type_path].add(name)
        wanted["aliases"] |= {name for values in held.alias_values.values() for name in values}
    for _, _, type_path, name in found.managed:
        if everything or type_path in add:
            wanted[type_path].add(name)
    # A row that's there only for something outside the folder (e.g. an auth server Central
    # assigned with another folder's server group) is that object's business, unless assign.yaml
    # already lists it.
    users = live.users()
    scopes: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(dict)
    for kind, scope, type_path, name in found.managed:
        row = (kind, scope, type_path, name)
        if name in wanted[type_path]:
            if users.get(row) and row not in listed and not any(n in wanted.get(t, ()) for t, n in users[row]):
                rep.used_outside[type_path] += 1
                continue
            scopes[(kind, scope)].setdefault(type_path, set()).add(name)
        elif name in live.objects[type_path]:
            rep.not_held[type_path].add(name)
    # Sites' own values for the aliases this pull writes: local copies that differ only in value.
    # Other local copies are listed as local profiles.
    values: dict[str, dict[str, str]] = defaultdict(dict)
    for name in wanted["aliases"]:
        library = live.objects["aliases"].get(name) or {}
        for site, local in live.alias_values(name).items():
            if not live.skip("aliases", name, env) and registry.is_value_copy(library, local):
                values[site][name] = registry.alias_value(local)
    assign: dict = {"global": block(scopes[("global", "")])}
    sites = sorted({s for k, s in scopes if k == "site"} | values.keys(), key=name_key)
    if sites:
        assign["sites"] = {s: {**block(scopes[("site", s)]),
                               **({"alias-values": dict(sorted(values[s].items()))} if values[s] else {})}
                           for s in sites}
    save("assign.yaml", config_dir / "assign.yaml", dump_yaml(assign), yaml.safe_load)

    # AP profiles
    for t in registry.AP_TYPES:
        if t.single:
            continue
        for name in sorted(wanted[t.path], key=name_key):
            if live.skip(t.path, name, env):   # a file for one is flagged by check and plan
                continue
            obj = live.objects[t.path].get(name)
            if obj is None:
                if (t.path, name) in held.files:
                    rep.gone.append(held.files[(t.path, name)])
                else:
                    rep.not_found.append(f"{t.path}/{name}")
                continue
            body, flagged = registry.strip(t, obj)
            rep.secret_like += [f"{t.path}/{name}: {p}" for p in flagged]
            save("profiles/", config_dir / "profiles" / t.path / f"{file_name(name)}.json", dump_json(body),
                 shown_json(t))
        if everything or t.path in add:
            unassigned = [n for n in live.objects[t.path] if n not in wanted[t.path] and not live.skip(t.path, n, env)]
            if unassigned:
                rep.unassigned[t.path] = len(unassigned)

    # Policy order: only the entries for policies this pull wrote. System entries and
    # policies used only by other device functions stay out, and apply leaves them as they are.
    if live.policy_group is not None:
        entries = [e for e in live.policy_group.get("policy-group-list", []) if e.get("name") in wanted["policies"]]
        body = {**live.policy_group, "policy-group-list": sorted(entries, key=lambda e: (e.get("position", 0), e["name"]))}
        save("profiles/", config_dir / "profiles" / "policy-groups.json", dump_json(body), shown_json(None))

    rep.local = live.local_profiles(env, None if everything else {t for t, names in wanted.items() if names})

    # Central NAC: a held object keeps its file, even if it was renamed in the UI. Seeding from
    # assign.yaml, one comes along when it names an AP profile this pull writes: an auth profile,
    # its WLANs.
    def follows(t: registry.Type, obj: dict) -> bool:
        return from_assign and any(v in wanted.get(target, ()) for path, target in t.refs
                                   for v in registry.values_at(obj, path))

    nac_dir = config_dir / "nac"
    for t in registry.NAC_TYPES:
        mine = held.nac.get(t.path, {})
        rep.gone += [held.files[(t.path, oid)] for oid in mine if oid not in live.objects[t.path]]
        stems: set[str] = set()
        for oid, obj in live.objects[t.path].items():
            name = str(obj.get("name") or oid)
            reason = live.skip(t.path, name, env)
            if reason:
                rep.skipped[reason] += 1
                continue
            if oid not in mine and not (everything or t.path in add or follows(t, obj)):
                rep.not_held[t.path].add(name)
                continue
            body, flagged = registry.strip(t, obj)
            rep.secret_like += [f"{t.path}/{name}: {p}" for p in flagged]
            if oid in mine:
                path = config_dir / held.files[(t.path, oid)]
            else:
                stem = file_name(name)
                if stem in stems:
                    stem = f"{stem}--{oid[:8]}"
                    rep.duplicates.append(f"{t.path}/{name}")
                stems.add(stem)
                path = nac_dir / t.path / f"{stem}.json"
            save("nac/", path, dump_json(body), shown_json(t))

    held_files = {config_dir / f for f in held.files.values()}
    for area in (config_dir / "profiles", nac_dir):
        for path in sorted(area.rglob("*.json")) if area.is_dir() else ():
            if path not in written and path not in held_files:
                rep.stale.append(path.relative_to(config_dir).as_posix())
    return rep
