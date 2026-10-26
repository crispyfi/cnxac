"""Named MPSK entries: the devices on an MPSK WLAN, each with a role and a passphrase that
Central generates.

The folder's mpsk/<WLAN>.csv files list them in Central's own import format (Name, Role,
Enabled), so a file also imports as it is in the UI. `mpsk add` adds the entries Central
doesn't have yet and leaves every existing one alone: Central's import updates the role and
enabled flag of an entry it already has, so only new rows are uploaded. `mpsk pull` writes
Central's entries back to the files.

Passphrases never go into the folder, which is committed, and are never printed or logged.
`mpsk pull --passphrases` writes one WLAN's entries with their passphrases to a new file
outside the folder, as the UI's export does.
"""
from __future__ import annotations

import csv
import io
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.text import Text

from . import desired, render
from .client import CentralError, Client
from .config import EnvConfig
from .desired import Issue
from .files import Staged, file_name, name_from_file, stage as stage_file, write_staged

FOLDER = "mpsk"
COLUMNS = ["Name", "Role", "Enabled"]
PASSWORD = "Password"
MODULE = "cnac-named-mpsk-reg"
PAGE = 100
WAIT, POLL = 120, 5   # seconds to wait for an import to show, and between reads


@dataclass(frozen=True)
class Entry:
    name: str
    role: str
    enabled: bool
    line: int = 0   # in its file


@dataclass
class Lists:
    """The folder's entries, by WLAN."""
    entries: dict[str, list[Entry]] = field(default_factory=dict)
    where: dict[str, str] = field(default_factory=dict)   # WLAN -> its file, relative to the folder
    issues: list[Issue] = field(default_factory=list)


@dataclass
class Central:
    profiles: list[dict]   # auth profiles
    roles: set[str]
    entries: list[dict]    # every named MPSK entry, passphrases included: never shown


@dataclass
class Plan:
    issues: list[Issue] = field(default_factory=list)
    where: dict[str, str] = field(default_factory=dict)
    stores: dict[str, str] = field(default_factory=dict)              # WLAN -> its named-MPSK store
    add: dict[str, list[Entry]] = field(default_factory=dict)         # WLAN -> entries Central lacks
    there: dict[str, list[tuple[Entry, dict]]] = field(default_factory=dict)   # in both: (file's, Central's)
    extra: dict[str, list[str]] = field(default_factory=dict)         # WLAN -> names only Central has

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def count(self) -> int:
        return sum(len(entries) for entries in self.add.values())

    def differing(self, wlan: str) -> list[str]:
        """The entries in both that Central holds differently, which stay as Central has them."""
        return [f"{e.name}: {why}" for e, c in self.there.get(wlan, []) if (why := difference(e, c))]


def key(name: str) -> str:
    """Names are matched ignoring case, so an entry differing only in case is never added again
    and so never updated by the import."""
    return name.casefold()


# The folder -----------------------------------------------------------------------------

def load(config_dir: Path) -> Lists:
    lists = Lists()
    folder = config_dir / FOLDER
    if not folder.is_dir():
        return lists
    for path in sorted(folder.iterdir()):
        rel = f"{FOLDER}/{path.name}"
        if path.name.startswith("."):
            continue
        if not path.is_file() or path.suffix.lower() != ".csv":
            lists.issues.append(Issue("error", rel, "unexpected; mpsk/ holds one <WLAN>.csv per MPSK WLAN"))
            continue
        entries = _read(path, rel, lists.issues)
        if entries is not None:
            wlan = name_from_file(path.stem)
            lists.entries[wlan], lists.where[wlan] = entries, rel
    return lists


def _read(path: Path, rel: str, issues: list[Issue]) -> list[Entry] | None:
    def error(where: str, message: str) -> None:
        issues.append(Issue("error", where, message))
    try:
        rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"), newline="")))
    except UnicodeDecodeError:
        error(rel, "not UTF-8 text")
        return None
    except csv.Error as e:
        error(rel, f"isn't valid CSV ({e})")
        return None
    header = [h.strip().casefold() for h in rows[0]] if rows else []
    if PASSWORD.casefold() in header:
        error(rel, "has a Password column; passphrases never go in the folder, which is committed. "
                   "Central generates them, so remove the column")
        return None
    if header != [c.casefold() for c in COLUMNS]:
        error(rel, f"the first line must be {','.join(COLUMNS)}, as in Central's import template")
        return None
    entries: list[Entry] = []
    seen: dict[str, int] = {}
    for n, row in enumerate(rows[1:], start=2):
        cells = [c.strip() for c in row]
        if not any(cells):
            continue
        where = f"{rel}: line {n}"
        if len(cells) != len(COLUMNS):
            error(where, f"has {len(cells)} fields, not {len(COLUMNS)}")
            continue
        name, role, enabled = cells
        if not name or not role:
            error(where, "needs a name and a role")
        elif enabled.upper() not in ("TRUE", "FALSE"):
            error(where, f"Enabled must be TRUE or FALSE, not {enabled!r}")
        elif key(name) in seen:
            error(where, f"{name} is also on line {seen[key(name)]}")
        else:
            seen[key(name)] = n
            entries.append(Entry(name, role, enabled.upper() == "TRUE", n))
    return entries


def offline(lists: Lists, d: desired.Desired) -> list[Issue]:
    """Warnings check can give without Central: a WLAN or role the folder doesn't hold."""
    wlans = {w for p in d.nac.get("auth-profiles", {}).values() if p.get("auth-type") == "MPSK"
             for w in p.get("networks") or []}
    roles = set(d.profiles.get("roles", {}))
    found = []
    for wlan, entries in lists.entries.items():
        rel = lists.where[wlan]
        if wlan not in wlans:
            found.append(Issue("warning", rel, f"no MPSK auth profile in the folder serves {wlan}; "
                                               "mpsk add checks Central"))
        for role in sorted({e.role for e in entries} - roles):
            found.append(Issue("warning", rel, f"role {role} isn't in the folder; mpsk add checks Central"))
    return found


def to_csv(rows: list[list[str]], header: list[str] = COLUMNS) -> str:
    buf = io.StringIO()
    out = csv.writer(buf, lineterminator="\n")
    out.writerow(header)
    out.writerows(rows)
    return buf.getvalue()


def flag(enabled: bool) -> str:
    return "TRUE" if enabled else "FALSE"


# Central ------------------------------------------------------------------------------

def read_entries(client: Client) -> list[dict]:
    """Every named MPSK entry, across all stores. Each holds its passphrase in clear text."""
    items: list[dict] = []
    ids: set[str] = set()
    while True:
        data = client.get(MODULE, {"limit": PAGE, "offset": len(items)}) or {}
        page = data.get("items") or []
        new = [e for e in page if e.get("id") not in ids]
        items += new
        ids |= {e.get("id") for e in new}
        total = data.get("total", len(items))
        if len(items) >= total or not new:   # not a short page: Central may cap a page below PAGE
            break
    if len(items) != total:
        raise CentralError("GET", MODULE, 200, f"read {len(items)} of {total} named MPSK entries; "
                                               "paging didn't work as expected, so nothing was changed")
    return items


def read(client: Client) -> Central:
    return Central(client.get_items("auth-profiles", "profile"),
                   {str(r.get("name")) for r in client.get_items("roles", "role")}, read_entries(client))


def network_of(central: Central, wlan: str) -> dict | None:
    """The MPSK auth profile serving the WLAN."""
    return next((p for p in central.profiles if p.get("auth-type") == "MPSK" and p.get("networks") == [wlan]
                 and p.get("identity-stores")), None)


def build(lists: Lists, central: Central) -> Plan:
    plan = Plan(issues=list(lists.issues), where=dict(lists.where))
    for wlan, entries in lists.entries.items():
        rel = lists.where[wlan]
        profile = network_of(central, wlan)
        if profile is None:
            plan.issues.append(Issue("error", rel, f"no MPSK auth profile in Central serves the WLAN {wlan}"))
            continue
        plan.stores[wlan] = profile["identity-stores"][0]
        for e in entries:
            if e.role not in central.roles:
                plan.issues.append(Issue("error", f"{rel}: line {e.line}", f"role {e.role} isn't in Central"))
        have = {key(str(c.get("name"))): c for c in central.entries if c.get("network") == wlan}
        listed = {key(e.name) for e in entries}
        plan.add[wlan] = [e for e in entries if key(e.name) not in have]
        plan.there[wlan] = [(e, have[key(e.name)]) for e in entries if key(e.name) in have]
        plan.extra[wlan] = sorted((str(c.get("name")) for k, c in have.items() if k not in listed), key=key)
    return plan


def difference(e: Entry, c: dict) -> str | None:
    """How Central's entry differs from the file's, or None."""
    if c.get("userRole") == e.role and c.get("enable") is e.enabled:
        return None
    return f"Central has role {c.get('userRole')}, {'enabled' if c.get('enable') else 'disabled'}"


# mpsk add -------------------------------------------------------------------------------

def add(config_dir: Path, env: EnvConfig, client: Client, ask: Callable[[Text], str], con: Console | None = None,
        sleep: Callable[[float], None] = time.sleep, wait: int = WAIT) -> int:
    """Add the folder's entries Central lacks, after a typed yes."""
    con = con or render.OUT
    lists = load(config_dir)
    with render.reading(env.name):
        central = read(client)
    plan = build(lists, central)
    render.mpsk_plan(con, plan)
    if plan.errors:
        return 1
    if not plan.count:
        return 0
    con.print()
    render.mpsk_confirmation(con, env.name, client.host, plan.count)
    answer = ask(render.prompt("yes"))
    con.print()
    if answer != "yes":
        render.declined(con, "yes", answer)
        return 1
    con.print("Adding", style="bold")
    ok = True
    for wlan, new in plan.add.items():
        if not new:
            continue
        # Read again just before the upload: an entry made since the plan is left alone too
        have = {key(str(c.get("name"))) for c in read_entries(client) if c.get("network") == wlan}
        for e in new:
            if key(e.name) in have:
                render.mpsk_step(con, wlan, e, None, "appeared in Central meanwhile, so left as it is")
        new = [e for e in new if key(e.name) not in have]
        if not new:
            continue
        client.upload(f"{MODULE}/import", f"{wlan}.csv",
                      to_csv([[e.name, e.role, flag(e.enabled)] for e in new]).encode("utf-8"),
                      {"identity-store-id": plan.stores[wlan]})
        found = _wait(client, wlan, new, sleep, wait)
        for e in new:
            c = found.get(key(e.name))
            if c is None:
                ok = False
                render.mpsk_step(con, wlan, e, False, f"not in Central after {wait}s; check the import in the UI")
            elif (why := difference(e, c)) is not None:
                ok = False
                render.mpsk_step(con, wlan, e, False, why)
            else:
                render.mpsk_step(con, wlan, e, True)
    con.print()
    if not ok:
        con.print("Some entries weren't added as listed. Run mpsk add again to see where things stand.",
                  style="bold red")
        return 1
    con.print("Added. Central generated the passphrases; see them in the UI, or with mpsk pull --passphrases.",
              style="bold green")
    return 0


def _wait(client: Client, wlan: str, new: list[Entry], sleep: Callable[[float], None], wait: int) -> dict[str, dict]:
    """Central imports in the background: read until every new entry shows, or time runs out."""
    names = {key(e.name) for e in new}
    waited = 0
    while True:
        found = {key(str(c.get("name"))): c for c in read_entries(client)
                 if c.get("network") == wlan and key(str(c.get("name"))) in names}
        if len(found) == len(names) or waited >= wait:
            return found
        sleep(POLL)
        waited += POLL


# mpsk pull ------------------------------------------------------------------------------

@dataclass
class Pulled:
    written: dict[str, tuple[str, int]] = field(default_factory=dict)   # file -> (status, entries)
    dropped: dict[str, list[str]] = field(default_factory=dict)        # file -> names Central lacks
    issues: list[Issue] = field(default_factory=list)
    files: list[Staged] = field(default_factory=list)


def wlans_held(config_dir: Path, lists: Lists) -> list[str]:
    """The folder's MPSK WLANs: those of its MPSK auth profiles, and those with a file."""
    d = desired.load(config_dir)
    held = {w for p in d.nac.get("auth-profiles", {}).values() if p.get("auth-type") == "MPSK"
            for w in p.get("networks") or []}
    return sorted(held | lists.entries.keys(), key=key)


def entries_of(central: Central, wlan: str) -> list[dict]:
    return sorted((c for c in central.entries if c.get("network") == wlan), key=lambda c: key(str(c.get("name"))))


def shown_entries(text: str) -> dict[str, dict]:
    """A file's entries by name, for showing what a pull changes: role and enabled flag only,
    so a Password column is never shown."""
    return {str(row.get("Name") or "").strip(): {"role": str(row.get("Role") or "").strip(),
                                                 "enabled": str(row.get("Enabled") or "").strip().upper() == "TRUE"}
            for row in csv.DictReader(io.StringIO(text))}


def entry_words(e: dict) -> str:
    return f"role {e['role']}" + ("" if e["enabled"] else ", disabled")


def entry_changes(old: dict[str, dict], new: dict[str, dict]) -> list[str]:
    """What a pull changes in a file's entries, in the style mpsk add shows them."""
    out = []
    for name in sorted(old.keys() | new.keys(), key=key):
        was, now = old.get(name), new.get(name)
        if was is None:
            out.append(f"+ {name}  {entry_words(now)}")
        elif now is None:
            out.append(f"- {name}  {entry_words(was)}")
        elif was != now:
            parts = [f"role {was['role']} → {now['role']}"] if was["role"] != now["role"] else []
            if was["enabled"] != now["enabled"]:
                parts.append("enabled → disabled" if was["enabled"] else "disabled → enabled")
            out.append(f"{name}  {', '.join(parts)}")
    return out


def pull(config_dir: Path, central: Central) -> Pulled:
    """stage, then write the files without asking."""
    pulled = stage(config_dir, central)
    write_staged(pulled.files)
    return pulled


def stage(config_dir: Path, central: Central) -> Pulled:
    """mpsk/<WLAN>.csv from Central for each of the folder's MPSK WLANs, without passphrases,
    and how each differs from the folder's, without writing."""
    lists = load(config_dir)
    pulled = Pulled(issues=list(lists.issues))
    for wlan in wlans_held(config_dir, lists):
        rel = lists.where.get(wlan, f"{FOLDER}/{file_name(wlan)}.csv")
        if network_of(central, wlan) is None:
            pulled.issues.append(Issue("warning", rel, f"no MPSK auth profile in Central serves {wlan}; not pulled"))
            continue
        entries = entries_of(central, wlan)
        names = {key(str(c.get("name"))) for c in entries}
        dropped = [e.name for e in lists.entries.get(wlan, []) if key(e.name) not in names]
        if dropped:
            pulled.dropped[rel] = dropped
        text = to_csv([[str(c.get("name")), str(c.get("userRole")), flag(bool(c.get("enable")))] for c in entries])
        staged = stage_file(config_dir / rel, rel, text, shown_entries, entry_changes)
        pulled.files.append(staged)
        pulled.written[rel] = (staged.status, len(entries))
    return pulled


def export(config_dir: Path, central: Central, wlan: str, out: Path) -> int:
    """Write one WLAN's entries with their passphrases to a new file outside the folder.
    Returns how many."""
    if out.resolve().is_relative_to(config_dir.resolve()):
        raise ValueError(f"{out} is inside the config folder, which is committed; write passphrases outside it")
    if network_of(central, wlan) is None:
        raise ValueError(f"no MPSK auth profile in Central serves {wlan}")
    entries = entries_of(central, wlan)
    text = to_csv([[str(c.get("name")), str(c.get("userRole")), flag(bool(c.get("enable"))), str(c.get("mpsk") or "")]
                   for c in entries], [*COLUMNS, PASSWORD])
    try:   # never overwrites, and only the owner may read it where the OS has permissions
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError(f"{out} exists; give a new file name, since it's never overwritten") from None
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return len(entries)
