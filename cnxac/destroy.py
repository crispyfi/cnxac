"""destroy: remove what a config folder describes from its environment.

The objects with a file, and the sites in sites.yaml, go in an order Central accepts: the
local copies of aliases going (sites' own values), then every assignment, each before what
it uses, then the policy order entries of the policies
going, then the objects, each before what it uses, then the sites. Last, each deleted WLAN's
own role is deleted too if Central left it behind; Central may refuse, and nothing else waits
on it.

Left alone, and listed: management users (made in the UI), objects that something outside the
folder uses or that are assigned to a device function the tool doesn't manage, and whatever
those use. Never touched: system, derived, factory default and ignored objects. Refused when
env.yaml says protected: true.
"""
from __future__ import annotations

from collections import defaultdict

from . import registry
from .config import EnvConfig
from .desired import Desired, Issue
from .live import AP, NAC, Live
from .plan import KINDS, Action, Plan, users_first

VALUES, UNASSIGN, ENTRIES, DELETE, SITES, ROLES = range(6)
Obj = tuple[str, str]   # (type path, ID)


def build(d: Desired, env: EnvConfig, live: Live) -> Plan:
    plan = Plan(issues=list(d.issues))
    if env.protected:
        plan.issues.append(Issue("error", "env.yaml", "protected: true, so destroy is refused"))
    if not plan.errors:
        _Destroyer(d, env, live, plan).run()
    return plan


class _Destroyer:
    def __init__(self, d: Desired, env: EnvConfig, live: Live, plan: Plan):
        self.d, self.env, self.live, self.plan = d, env, live, plan

    def add(self, step: int, verb: str, target: str, details: list[str] | None = None, op: tuple = ()) -> None:
        self.plan.actions.append(Action(step, verb, target, details or [], op))

    def name(self, o: Obj) -> str:
        if not registry.BY_PATH[o[0]].nac:
            return o[1]
        return str((self.live.objects[o[0]].get(o[1]) or {}).get("name") or o[1])

    def label(self, o: Obj) -> str:
        return f"{o[0]}/{self.name(o)}"

    def run(self) -> None:
        used_by = self.used_by()
        held = self.held()
        going = held - self.left_alone(held, used_by)
        self.alias_values(going)
        self.unassign(going, used_by)
        self.entries(going)
        roles = self.delete(going, used_by)
        self.sites()
        for role in roles:
            self.add(ROLES, "delete", f"roles/{role}, the WLAN's own role, if Central left it", op=("wlan-role", role))

    def held(self) -> set[Obj]:
        """Objects with a file that exist in Central and that the tool may delete."""
        held = set()
        for store in (self.d.profiles, self.d.nac):
            for type_path, objs in store.items():
                t = registry.BY_PATH[type_path]
                for key, body in objs.items():
                    name = str(body.get("name") or key) if t.nac else key
                    if self.live.skip(type_path, name, self.env) or key not in self.live.objects[type_path]:
                        continue
                    if t.ui_created:
                        self.plan.left.append(f"{type_path}/{name}: made in the UI")
                    else:
                        held.add((type_path, key))
        return held

    def used_by(self) -> dict[Obj, set[Obj]]:
        """Every live object's users: the live objects whose reference fields name it."""
        found = defaultdict(set)
        for t in registry.TYPES:
            for key, obj in self.live.objects.get(t.path, {}).items():
                for field, target in t.refs:
                    if target not in registry.BY_PATH:
                        continue
                    for value in registry.values_at(obj, field):
                        if (target, value) != (t.path, key):
                            found[(target, value)].add((t.path, key))
        return found

    def outside(self, u: Obj) -> bool:
        """A user outside the folder that counts: not a system, derived or default object."""
        return self.live.skip(u[0], self.name(u), self.env) in (None, "ignored")

    def left_alone(self, held: set[Obj], used_by: dict[Obj, set[Obj]]) -> set[Obj]:
        functions = defaultdict(set)
        for r in self.live.assignments:
            o = (r.get("profile-type"), str(r.get("profile-instance")))
            if r.get("device-function") not in (AP, NAC) and o in held:
                functions[o].add(r["device-function"])
        why = {}
        for o in held:
            users = sorted(self.label(u) for u in used_by.get(o, ()) if u not in held and self.outside(u))
            if users:
                why[o] = f"used by {', '.join(users)}"
            elif functions[o]:
                why[o] = f"assigned to {', '.join(sorted(functions[o]))} too, which the tool doesn't manage"
        stack = list(why)
        while stack:   # what stays keeps what it uses
            u = stack.pop()
            for o in held - why.keys():
                if u in used_by.get(o, ()):
                    why[o] = f"used by {self.label(u)}, which stays"
                    stack.append(o)
        self.plan.left += [f"{self.label(o)}: {why[o]}" for o in sorted(why, key=self.label)]
        return set(why)

    def alias_values(self, going: set[Obj]) -> None:
        """Local copies of the aliases going, wherever they are."""
        for (sid, name), body in sorted(self.live.local_aliases.items(),
                                        key=lambda item: (item[0][1], self.live.label(item[0][0]))):
            if ("aliases", name) in going:
                kind, scope = self.live.scope_of(sid) or ("id", sid)
                value = registry.alias_value(body)
                self.add(VALUES, "delete", f"aliases/{name} at {self.live.label(sid)} (local value)",
                         [f"was {value}"] if value else [], ("alias-value", kind, scope, name))

    def unassign(self, going: set[Obj], used_by: dict[Obj, set[Obj]]) -> None:
        rows = set()
        for r in self.live.assignments:
            o = (r.get("profile-type"), str(r.get("profile-instance")))
            if o in going and r.get("device-function") == registry.BY_PATH[o[0]].device_function:
                sid = str(r["scope-id"])
                rows.add((*(self.live.scope_of(sid) or ("id", sid)), *o))

        def users_at(a: tuple[str, str, str, str]) -> set[tuple[str, str, str, str]]:
            return {(a[0], a[1], *u) for u in used_by.get(a[2:], ())}

        def key(a: tuple[str, str, str, str]) -> tuple:
            return -registry.ORDER[a[2]], KINDS.get(a[0], 2), a[1], self.name(a[2:])
        for a in users_first(rows, users_at, key):
            where = self.live.label(a[1]) if a[0] == "id" else ("Global" if a[0] == "global" else a[1])
            self.add(UNASSIGN, "unassign", f"{self.label(a[2:])} from {where}", op=("assignment", *a))

    def entries(self, going: set[Obj]) -> None:
        listed = {e.get("name") for e in (self.live.policy_group or {}).get("policy-group-list", [])}
        for type_path, key in sorted(going):
            if type_path == "policies" and key in listed:
                self.add(ENTRIES, "remove", f"{key} from the policy order", op=("order-entry", key))

    def delete(self, going: set[Obj], used_by: dict[Obj, set[Obj]]) -> list[str]:
        """Delete steps; returns the deleted WLANs' own roles."""
        roles = []

        def key(o: Obj) -> tuple:
            return -registry.ORDER[o[0]], self.name(o)
        for o in users_first(going, lambda o: used_by.get(o, set()) & going, key):
            body = self.live.objects[o[0]][o[1]]
            details = []
            if o[0] == "auth-profiles" and body.get("auth-type") == "MPSK":
                details.append("its named-MPSK store goes too, with every passphrase in it")
            self.add(DELETE, "delete", self.label(o), details, ("delete", *o))
            if o[0] == "wlan-ssids":
                roles.append(str(body.get("auto-default-role") or o[1]))
        return roles

    def sites(self) -> None:
        for name in sorted(self.d.sites or {}):
            if name in self.live.site_ids:
                self.add(SITES, "delete", f"site {name}", op=("site-delete", name))
