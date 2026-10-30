"""apply: carry out the plan's actions in order, after a typed yes and a backup.

The live reads the plan was made from are saved to backups/<timestamp>/ in the config
folder, one JSON file per endpoint, with a log of every request (method, path and status,
no bodies, and Central's message under a refused one). Each object write is read back and compared with its file. A difference gets
one more write, and a difference after that stops the run, as does any refused write. A
final plan then shows anything that still differs. The UUIDs chosen for empty NAC IDs are
written into their file just before that object's first write, the only file apply edits.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from rich.console import Console
from rich.text import Text

from . import desired, live, plan, registry, render
from . import destroy as teardown
from .client import CentralError, Client, items_of, message_of
from .config import EnvConfig
from .diff import brief, diff
from .files import dump_json, write

BACKUPS = "backups"
TITLES = {"apply": "Changes, in apply order", "destroy": "Destroy, in this order"}


class ApplyError(Exception):
    """A write that Central accepted but that didn't end up as the file says."""


class Recorder:
    """Client response hook: keeps the plan's GET bodies for the backup, and one log line
    per response. Log lines are kept in memory until the backup folder exists, then
    appended to its requests.log as they happen."""

    def __init__(self) -> None:
        self.reads: dict[str, list[Any]] = defaultdict(list)   # path -> bodies, one per page
        self.reading = True
        self.lines: list[str] = []
        self.log: Path | None = None

    def __call__(self, method: str, path: str, params: dict | None, status: int, body: Any) -> None:
        if self.reading and method == "GET" and 200 <= status < 300:
            self.reads[path].append(body)
        query = f"?{urlencode(params)}" if params else ""
        line = f"{datetime.now().isoformat(timespec='seconds')} {method} {path}{query} {status}\n"
        if not 200 <= status < 300:
            line += f"    {' '.join(message_of(body).split())}\n"
        if self.log:
            with self.log.open("a", encoding="utf-8", newline="\n") as f:
                f.write(line)
        else:
            self.lines.append(line)

    def backup(self, config_dir: Path) -> Path:
        """Write the reads to a new backups/<timestamp>/ folder and start its request log."""
        root = config_dir / BACKUPS
        root.mkdir(exist_ok=True)
        ignore = root / ".gitignore"   # never committed, whatever the repo's own .gitignore says
        if not ignore.is_file():
            ignore.write_text("*\n", encoding="utf-8", newline="\n")
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        folder, n = root / stamp, 1
        while folder.exists():
            n += 1
            folder = root / f"{stamp}-{n}"
        folder.mkdir()
        for path, bodies in self.reads.items():
            body = bodies[0] if len(bodies) == 1 else {"items": [i for b in bodies for i in (b or {}).get("items", [])]}
            (folder / f"{path.replace('/', '_')}.json").write_text(dump_json(body), encoding="utf-8", newline="\n")
        self.log = folder / "requests.log"
        self.log.write_text("".join(self.lines), encoding="utf-8", newline="\n")
        self.lines = []
        return folder


def main(config_dir: Path, env: EnvConfig, client: Client, ask: Callable[[Text], str],
         con: Console | None = None) -> int:
    """apply: make the plan's changes after a typed yes."""
    return run(config_dir, env, client, ask, con, plan.build, "apply")


def destroy(config_dir: Path, env: EnvConfig, client: Client, ask: Callable[[Text], str],
            con: Console | None = None) -> int:
    """destroy: remove what the folder describes, after the environment's name is typed."""
    return run(config_dir, env, client, ask, con, teardown.build, "destroy")


def run(config_dir: Path, env: EnvConfig, client: Client, ask: Callable[[Text], str], con: Console | None,
        build: Callable[[desired.Desired, EnvConfig, live.Live], plan.Plan], command: str) -> int:
    con = con or render.OUT
    rec = Recorder()
    client.on_response = rec
    with render.reading(env.name):
        current = live.read(client)
    rec.reading = False
    d = desired.load(config_dir)
    result = build(d, env, current)
    render.plan(con, result, TITLES[command])
    if result.errors:
        return 1
    if not result.actions:
        return 0
    con.print()
    changes = plan.count(len(result.actions), "change")
    word = env.name if command == "destroy" else "yes"
    render.confirmation(con, command, env.name, client.host, len(result.actions))
    answer = ask(render.prompt(word))
    con.print()
    if answer != word:
        render.declined(con, word, answer)
        return 1

    folder = rec.backup(config_dir)
    con.print(f"Backed up to {folder}/")
    con.print()
    con.print("Applying", style="bold")
    applier = _Applier(client, d, current, con)
    try:
        finished = applier.run(result.actions)
    except KeyboardInterrupt:
        con.print("  ✗ interrupted", style="bold red")
        finished = False
    con.print()
    if applier.ids_written:
        con.print(f"Wrote new IDs into {', '.join(applier.ids_written)}. Commit "
                  f"{'it' if len(applier.ids_written) == 1 else 'them'}.", style="yellow")
    if not finished:
        done = len(applier.done)
        con.print(f"Stopped after {done} of {changes}. Not done:", style="bold red")
        render.changes(con, result.actions[done:], show_details=False)
        con.print()
        again = "plan --destroy" if command == "destroy" else "plan"
        con.print(f"Run {again} to see where things stand. The backup and request log are in {folder}/")
        return 1

    con.print(f"Applied {changes}.", style="bold green")
    with render.reading(f"{env.name} again"):
        after = build(d, env, live.read(client))
    if after.errors:
        con.print(f"Plan after {command} found errors:", style="bold red")
        for issue in after.errors:
            render.issue(con, issue)
        return 1
    if after.actions:
        con.print(f"Plan after {command}, still different:", style="yellow")
        render.changes(con, after.actions)
        return 2
    con.print(f"Plan after {command}: no changes left.", style="green")
    return 0


class _Applier:
    def __init__(self, client: Client, d: desired.Desired, current: live.Live, con: Console):
        self.c, self.d, self.live, self.con = client, d, current, con
        self.site_ids = dict(current.site_ids)   # sites created during the run join these
        self.done: list[plan.Action] = []
        self.ids_written: list[str] = []
        self.handlers: dict[str, Callable[[plan.Action], str | None]] = {
            "object": self.object, "move": self.move, "policy-order": self.policy_order,
            "site": self.site, "assignment": self.assignment,
            "order-entry": self.order_entry, "delete": self.delete, "wlan-role": self.wlan_role,
            "site-delete": self.site_delete, "alias-value": self.alias_value,
        }

    def run(self, actions: list[plan.Action]) -> bool:
        """Carry out the actions in order, stopping at the first one that fails."""
        for a in actions:
            try:
                note = self.handlers[a.op[0]](a)
            except (CentralError, ApplyError) as e:
                render.step(self.con, a, ok=False)
                render.failure(self.con, str(e))
                return False
            self.done.append(a)
            render.step(self.con, a, ok=True, note=note)
        return True

    # Objects ----------------------------------------------------------------------------

    def object(self, a: plan.Action) -> str | None:
        _, type_path, key = a.op
        t = registry.BY_PATH[type_path]
        body = (self.d.nac if t.nac else self.d.profiles)[type_path][key]
        path = f"{type_path}/{quote(key, safe='')}"
        if (type_path, key) in self.d.filled:   # first, so a refused write leaves the IDs for the next apply
            write(self.d.filled.pop((type_path, key)), dump_json(body))
            self.ids_written.append(self.d.files[(type_path, key)])
        if a.verb == "create":
            self.c.post(path, plan.wanted(t, body, {}))
        else:
            self.put(t, path, body, self.c.get(path))   # read again just before, to carry fresh secrets
        got = self.c.get(path)
        first = self.differs(t, body, got)
        if not first:
            return None
        self.put(t, path, body, got)
        left = self.differs(t, body, self.c.get(path))
        if left:
            raise ApplyError("still differs from the file after a second write: " + "; ".join(left))
        more = f"; and {len(first) - 3} more" if len(first) > 3 else ""
        return f"read back different, so written again: {'; '.join(first[:3])}{more}"

    def move(self, a: plan.Action) -> str | None:
        """Move a live object to a free position, out of another's way; its update follows."""
        _, type_path, key, position = a.op
        path = f"{type_path}/{quote(key, safe='')}"
        self.c.put(path, {**self.c.get(path), "position": position})
        got = self.c.get(path)
        if not isinstance(got, dict) or got.get("position") != position:
            raise ApplyError(f"read back at position {got.get('position') if isinstance(got, dict) else None}")
        return None

    def put(self, t: registry.Type, path: str, body: dict, current: dict) -> None:
        self.c.put(path, registry.carry(t, current, plan.wanted(t, body, current)))

    def differs(self, t: registry.Type, body: dict, got: Any) -> list[str]:
        return brief(diff(*plan.compared(t, body, got if isinstance(got, dict) else {})))

    def policy_order(self, a: plan.Action) -> str | None:
        body = a.op[1]
        self.c.patch("policy-groups", {"policy-group": body})
        if not self.order_differs(body):
            return None
        self.c.patch("policy-groups", {"policy-group": body})
        left = self.order_differs(body)
        if left:
            raise ApplyError("still differs from the file after a second write: " + "; ".join(left))
        return "read back different, so written again"

    def order_differs(self, body: dict) -> list[str]:
        group = (self.c.get("policy-groups") or {}).get("policy-group") or {}
        entries = {e.get("name"): e for e in group.get("policy-group-list", [])}
        left = []
        for entry in body["policy-group-list"]:
            old = entries.get(entry["name"])
            left += [f"{entry['name']} has no entry"] if old is None else brief(diff(old, entry, entry["name"]))
        rest = {k: v for k, v in body.items() if k != "policy-group-list"}
        return left + brief(diff({k: group.get(k) for k in rest}, rest))

    # Scopes and assignments -------------------------------------------------------------

    def site(self, a: plan.Action) -> str | None:
        """Create a site. Its ID is items[0] of the response, else found by listing."""
        name = a.op[1]
        data = self.c.post("sites", self.d.sites[name])
        items = data.get("items") if isinstance(data, dict) else None
        if items and isinstance(items[0], (str, int)):
            self.site_ids[name] = str(items[0])
            return None
        for s in self.c.get_paged("sites"):
            if s.get("scopeName") == name:
                self.site_ids[name] = str(s["scopeId"])
                return None
        raise ApplyError(f"created, but no ID for {name} was returned or listed")

    def assignment(self, a: plan.Action) -> str | None:
        _, kind, scope, type_path, name = a.op
        sid = self.scope_id(kind, scope)
        function = registry.BY_PATH[type_path].device_function
        if a.verb == "assign":
            self.c.post("config-assignments", {"config-assignment": [
                {"scope-id": sid, "device-function": function, "profile-type": type_path, "profile-instance": name}]})
            return None
        try:
            self.c.delete(f"config-assignments/{sid}/{function}/{type_path}/{quote(name, safe='')}")
        except CentralError:   # fine if Central already removed it, e.g. along with what used it
            row = (sid, function, type_path, name)
            if any((str(r.get("scope-id")), r.get("device-function"), r.get("profile-type"),
                    str(r.get("profile-instance"))) == row
                   for r in items_of(self.c.get("config-assignments"), "config-assignment")):
                raise
            return "Central had already removed it"
        return None

    def alias_value(self, a: plan.Action) -> str | None:
        """A scope's own value for an alias: a same-name local copy there."""
        _, kind, scope, name = a.op
        path = f"aliases/{quote(name, safe='')}"
        sid = self.scope_id(kind, scope)
        local = {"object-type": "LOCAL", "scope-id": sid, "device-function": live.AP}
        want = {}
        if a.verb == "delete":
            self.c.delete(path, local)
        else:
            want = registry.with_alias_value(self.d.profiles["aliases"][name], self.d.alias_values[scope][name])
            (self.c.post if a.verb == "create" else self.c.put)(path, want, local)
        got = self.c.get(path, {"view-type": "LOCAL", "scope-id": sid, "device-function": live.AP}) or {}
        if isinstance(got, dict):
            got.pop(live.META, None)
        if got != want:
            raise ApplyError("still there after the delete" if a.verb == "delete" else
                             "reads back different: " + "; ".join(brief(diff(got, want))))
        return None

    # Destroy ------------------------------------------------------------------------------
    # Central answers a GET of an object that doesn't exist with 200 and an empty body.

    def order_entry(self, a: plan.Action) -> str | None:
        """Remove one policy order entry; Central refuses while the policy is assigned."""
        name = a.op[1]
        try:
            self.c.delete(f"policy-groups/policy-group/policy-group-list/{quote(name, safe='')}")
        except CentralError:
            group = (self.c.get("policy-groups") or {}).get("policy-group") or {}
            if name in {e.get("name") for e in group.get("policy-group-list", [])}:
                raise
            return "Central had already removed it"
        return None

    def delete(self, a: plan.Action) -> str | None:
        _, type_path, key = a.op
        path = f"{type_path}/{quote(key, safe='')}"
        self.c.delete(path)
        if self.c.get(path):
            raise ApplyError("still there after the delete")
        return None

    def wlan_role(self, a: plan.Action) -> str | None:
        """A deleted WLAN's own role: Central sometimes keeps it (e.g. while a sys_allow_all
        rule names it)."""
        path = f"roles/{quote(a.op[1], safe='')}"
        if not self.c.get(path):
            return "Central deleted it with the WLAN"
        self.c.delete(path)
        if self.c.get(path):
            raise ApplyError("still there after the delete")
        return "Central had left it, so it was deleted"

    def site_delete(self, a: plan.Action) -> str | None:
        name = a.op[1]
        self.c.delete("sites", {"scope-id": self.scope_id("site", name)})
        if any(s.get("scopeName") == name for s in self.c.get_paged("sites")):
            raise ApplyError("still there after the delete")
        return None

    def scope_id(self, kind: str, name: str) -> str:
        if kind == "id":   # a scope that's not Global, a site collection or a site, by its ID
            return name
        sid = (self.live.global_id if kind == "global" else self.live.collection_ids.get(name) if kind == "collection"
               else self.site_ids.get(name))
        if sid is None:
            raise ApplyError(f"no scope ID for {name or 'Global'}")
        return sid
