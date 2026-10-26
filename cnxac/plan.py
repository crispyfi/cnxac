"""plan: compare a config folder with its environment and list the changes in apply order.

It only reads. Errors from the checks stop the plan; warnings don't. Each action carries
what apply needs to carry it out, so what plan shows is what apply runs.
"""
from __future__ import annotations

import copy
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter

from . import checks, registry
from .checks import scope_label
from .config import EnvConfig
from .desired import Desired, Issue
from .diff import MISSING, diff, lines as diff_lines
from .live import Live, derived_roles_of

# Steps after the object types, in apply order. Authorization policies in the way of another's
# new position move just before their updates.
MOVES = registry.ORDER["authz-policies"] - 0.5
SITES, VALUES_GO, UNASSIGN, ASSIGN, VALUES = (len(registry.TYPES) + i for i in range(5))
KINDS = {"global": 0, "site": 1}


@dataclass
class Action:
    step: float
    verb: str
    target: str
    details: list[str] = field(default_factory=list)
    op: tuple = ()   # for apply: (kind, ...), e.g. ("object", type path, ID)


@dataclass
class Plan:
    issues: list[Issue] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    local: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)    # assignments not removed, because something there uses them
    left: list[str] = field(default_factory=list)    # destroy: objects left alone, and why

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def exit_code(self) -> int:
        return 1 if self.errors else 2 if self.actions else 0


def count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def users_first(items: Iterable, users_of: Callable[[Hashable], set], key: Callable) -> list:
    """The items ordered so that each comes after the items that use it, ties broken by key. A
    cycle of uses falls back to key order."""
    items = set(items)
    order = TopologicalSorter({i: users_of(i) & items for i in items})
    try:
        order.prepare()
    except CycleError:
        return sorted(items, key=key)
    out = []
    while order.is_active():
        ready = sorted(order.get_ready(), key=key)
        out += ready
        order.done(*ready)
    return out


def wanted(t: registry.Type, body: dict, current: dict) -> dict:
    """The file body as Central should hold it: no generated or UI-set fields, and a PSK WLAN
    with no PSK in Central kept disabled."""
    want = copy.deepcopy(body)
    for key in t.generated:
        want.pop(key, None)
    for dotted in t.ui_set:
        registry.pop_path(want, dotted)
    if t.path == "wlan-ssids" and registry.is_psk_wlan(want) and not registry.has_psk(current):
        want["enable"] = False
    return want


def compared(t: registry.Type, body: dict, current: dict) -> tuple[dict, dict]:
    """(live, wanted) bodies as they compare: no generated or secret fields, and none of the
    keys Central adds for Central NAC unless the file sets them."""
    want = wanted(t, body, current)
    have, _ = registry.strip(t, current)
    if want.get("cloud-auth") is True:
        for key in t.nac_adds:
            if key not in want:
                have.pop(key, None)
    return have, want


def build(d: Desired, env: EnvConfig, live: Live) -> Plan:
    plan = Plan(issues=d.issues + checks.run(d, env, live))
    if not plan.errors:
        _Planner(d, env, live, plan).run()
        plan.actions.sort(key=lambda a: a.step)
    return plan


class _Planner:
    def __init__(self, d: Desired, env: EnvConfig, live: Live, plan: Plan):
        self.d, self.env, self.live, self.plan = d, env, live, plan
        self.derived = derived_roles_of(list(d.profiles.get("wlan-ssids", {}).values())) | live.derived_roles

    def add(self, step: float, verb: str, target: str, details: list[str] | None = None, op: tuple = ()) -> None:
        self.plan.actions.append(Action(step, verb, target, details or [], op))

    def managed(self, t: registry.Type, name: str) -> bool:
        return not (name.startswith("sys_") or self.env.ignored(name) or (t.path, name) in self.live.defaults
                    or (t.path == "roles" and name in self.derived))

    def run(self) -> None:
        self.objects()
        self.authz_moves()
        self.policy_order()
        self.sites()
        self.assignments()
        self.alias_values()
        self.plan.local = self.live.local_profiles(self.env, {t for t, objs in self.d.profiles.items() if objs})

    def objects(self) -> None:
        for store, types in ((self.d.profiles, registry.AP_TYPES), (self.d.nac, registry.NAC_TYPES)):
            for t in types:
                for key, body in store.get(t.path, {}).items():
                    name = str(body.get("name") or key)
                    if not self.managed(t, name):
                        continue
                    target, op = f"{t.path}/{name}", ("object", t.path, key)
                    filled = ["apply writes the new IDs into its file; commit it"] if (t.path, key) in self.d.filled else []
                    current = self.live.objects[t.path].get(key)
                    if current is None:
                        if not t.ui_created:   # checks report a missing UI-made object
                            self.add(registry.ORDER[t.path], "create", target, self.create_notes(t, body) + filled, op)
                        continue
                    changes = diff(*compared(t, body, current))
                    if changes:
                        self.add(registry.ORDER[t.path], "update", target, diff_lines(changes) + filled, op)

    def authz_moves(self) -> None:
        """Authorization policy positions are unique, so a policy holding the position another
        one moves to (e.g. when two swap) first moves to a free position."""
        t = registry.BY_PATH["authz-policies"]
        files = {key: body for key, body in self.d.nac.get(t.path, {}).items()
                 if self.managed(t, str(body.get("name") or key))}
        wanted = {body.get("position"): key for key, body in files.items()}
        live = self.live.objects[t.path]
        taken = [p for p in [o.get("position") for o in live.values()] + list(wanted)
                 if isinstance(p, int) and not isinstance(p, bool)]
        free = max(taken, default=0)
        for key, current in live.items():
            pos, other = current.get("position"), wanted.get(current.get("position"))
            if key in files and other not in (None, key) and files[key].get("position") != pos:
                free += 1
                self.add(MOVES, "move", f"{t.path}/{current.get('name') or key} to position {free}",
                         [f"frees position {pos} for {t.path}/{files[other].get('name') or other}"],
                         ("move", t.path, key, free))

    @staticmethod
    def create_notes(t: registry.Type, body: dict) -> list[str]:
        if t.path == "wlan-ssids" and registry.is_psk_wlan(body):
            if body.get("enable") is False:
                return ["set its PSK in the UI"]
            return ["created disabled: set its PSK in the UI, then the next apply enables it"]
        if t.path == "auth-servers":
            return ["set its shared secret in the UI after apply"]
        return []

    def policy_order(self) -> None:
        group = self.d.policy_group
        if group is None:
            return
        current = self.live.policy_group or {}
        entries = {e.get("name"): e for e in current.get("policy-group-list", [])}
        details, changed = [], []
        for entry in sorted(group.get("policy-group-list", []), key=lambda e: (e["position"], e["name"])):
            old = entries.get(entry["name"])
            if old is None:
                details.append(f"+ {entry['name']} at position {entry['position']}")
            else:
                details += diff_lines(diff(old, entry, entry["name"]))
            if old is None or diff(old, entry):
                changed.append(entry)
        rest = "policy-group-list"
        details += diff_lines(diff({k: v for k, v in current.items() if k != rest},
                                   {k: v for k, v in group.items() if k != rest}))
        if details:
            # One PATCH, which merges entries by name, so only new and moved ones are sent
            body = {k: v for k, v in group.items() if k != rest and diff(current.get(k, MISSING), v)}
            body[rest] = changed
            self.add(registry.ORDER["policy-groups"], "update", "policy order", details, ("policy-order", body))
        assigned = any(r.get("profile-type") == "policy-groups" and r.get("device-function") == "CAMPUS_AP"
                       and str(r["scope-id"]) == self.live.global_id for r in self.live.assignments)
        if not assigned:
            self.add(ASSIGN, "assign", "policy order at Global", op=("assignment", "global", "", "policy-groups", ""))

    def sites(self) -> None:
        for name, body in (self.d.sites or {}).items():
            if name not in self.live.site_ids:
                self.add(SITES, "create", f"site {name}", [f"timezone {body['timezone']['timezoneId']}"],
                         ("site", name))

    def alias_values(self) -> None:
        """A site's value for one of the folder's aliases is a same-name local copy at the site.
        Copies assign.yaml doesn't list go before the unassigns; new and changed ones follow
        the assignments, once the alias is at Global."""
        t = registry.BY_PATH["aliases"]
        for name, body in sorted(self.d.profiles.get(t.path, {}).items()):
            if not self.managed(t, name) or body.get("type") not in registry.ALIAS_VALUES:
                continue
            want = {site: values[name] for site, values in self.d.alias_values.items() if name in values}
            have = self.live.alias_values(name)
            for site in sorted(have.keys() - want.keys()):
                self.add(VALUES_GO, "delete", f"{t.path}/{name} at {site} (local value)",
                         [f"was {registry.alias_value(have[site])}"], ("alias-value", "site", site, name))
            for site, value in sorted(want.items()):
                target, op = f"{t.path}/{name} at {site} (local value)", ("alias-value", "site", site, name)
                if site not in have:
                    self.add(VALUES, "create", target, [value], op)
                elif changes := diff(have[site], registry.with_alias_value(body, value)):
                    details = ([f"{registry.alias_value(have[site])} → {value}"]
                               if registry.is_value_copy(body, have[site]) else diff_lines(changes))
                    self.add(VALUES, "update", target, details, op)

    def assignments(self) -> None:
        def key(a: tuple[str, str, str, str]) -> tuple:
            kind, scope, type_path, name = a
            return KINDS[kind], scope, registry.ORDER[type_path], name

        # Only the assignments of profiles the folder holds; Central's others are left alone
        live = {a for a in self.live.classify(self.env).managed if a[3] in self.d.profiles.get(a[2], {})}
        want = {a for a in self.d.assigned() if not self.env.ignored(a[3])}
        # Unassigns run first, because a scope takes one settings profile of each type, and each
        # before what it uses. One that something staying at the same scope uses stays too:
        # Central may have assigned it with that object, and would refuse or break without it.
        users = self.live.users()

        def users_at(a: tuple[str, str, str, str]) -> set[tuple[str, str, str, str]]:
            return {(a[0], a[1], t, n) for t, n in users.get(a, ())}
        going, kept = live - want, {}
        while True:
            more = {a: staying for a in going if (staying := users_at(a) - going)}
            if not more:
                break
            kept |= more
            going -= more.keys()
        for a in sorted(kept, key=key):
            using = ", ".join(f"{t}/{n}" for _, _, t, n in sorted(kept[a]))
            self.plan.kept.append(f"{a[2]}/{a[3]} at {scope_label(a[0], a[1])}, used by {using}")
        for a in users_first(going, users_at, key):
            self.add(UNASSIGN, "unassign", f"{a[2]}/{a[3]} from {scope_label(a[0], a[1])}", op=("assignment", *a))
        for a in sorted(want - live, key=key):
            self.add(ASSIGN, "assign", f"{a[2]}/{a[3]} at {scope_label(a[0], a[1])}", op=("assignment", *a))
        nac_live = self.live.nac_assigned()
        for t in registry.NAC_TYPES:   # as the UI does, every NAC object is assigned at Global
            for key_, body in self.d.nac.get(t.path, {}).items():
                name = str(body.get("name") or key_)
                if self.managed(t, name) and (t.path, key_) not in nac_live:
                    self.add(ASSIGN, "assign", f"{t.path}/{name} at Global (Central NAC)",
                             op=("assignment", "global", "", t.path, key_))
