"""Checks on a config folder. Without Central (check) only the files are checked; with it
(plan), references and UI-made objects are checked against the environment too."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Iterator

from . import registry
from .config import EnvConfig
from .desired import UUID, Desired, Issue
from .files import file_name
from .live import Live, derived_roles_of

ROLE_ATTR = "ATTR_ARUBA_ROLE"
# Without these, Central sends the AP "no dtim-period", "no broadcast-filter" and
# "no dmo-channel-utilization-threshold", which it refuses. The values are a UI-made WLAN's.
PUSHED = {"dtim-period": 1, "broadcast-filter-ipv4": "BCAST_FILTER_ARP", "dmo.channel-utilization-threshold": 90}
STORE_ATTR = re.compile(r"^Authorization:([0-9a-fA-F-]{36}):")


def run(d: Desired, env: EnvConfig, live: Live | None = None) -> list[Issue]:
    checker = _Checker(d, env, live)
    checker.run()
    return checker.issues


def scope_label(kind: str, name: str) -> str:
    return "Global" if kind == "global" else name


class _Checker:
    def __init__(self, d: Desired, env: EnvConfig, live: Live | None):
        self.d, self.env, self.live = d, env, live
        self.issues: list[Issue] = []
        self.derived = derived_roles_of(list(d.profiles.get("wlan-ssids", {}).values()))
        if live:
            self.derived |= live.derived_roles
        self.folded_defaults = {(t, n.casefold()): n for t, n in live.defaults} if live else {}
        # Objects the tool touches. The rest get one warning or error in contents() and no other checks.
        self.profiles = self._managed(d.profiles)
        self.nac = self._managed(d.nac)

    def managed(self, type_path: str, name: str) -> bool:
        """False for objects the tool never touches: system, ignored, derived and factory default ones."""
        return not (name.startswith("sys_") or self.env.ignored(name) or self.default(type_path, name)
                    or (type_path == "roles" and name in self.derived))

    def default(self, type_path: str, name: str) -> bool:
        return self.live is not None and (type_path, name) in self.live.defaults

    def _managed(self, store: dict[str, dict[str, dict]]) -> dict[str, dict[str, dict]]:
        return {type_path: {key: body for key, body in objs.items()
                            if self.managed(type_path, str(body.get("name") or key))}
                for type_path, objs in store.items()}

    def error(self, where: str, message: str) -> None:
        self.issues.append(Issue("error", where, message))

    def warn(self, where: str, message: str) -> None:
        self.issues.append(Issue("warning", where, message))

    def objects(self, everything: bool = False) -> Iterator[tuple[registry.Type, str, dict, str]]:
        """(type, key, body, where) for every managed object file, or every file."""
        for store in (self.d.profiles, self.d.nac) if everything else (self.profiles, self.nac):
            for type_path, objs in store.items():
                t = registry.BY_PATH[type_path]
                for key, body in objs.items():
                    yield t, key, body, self.d.where(type_path, key)

    def run(self) -> None:
        self.contents()
        self.assignments()
        self.used_assignments()
        self.alias_values()
        self.one_per_scope()
        self.one_user_per_role()
        self.references()
        self.authz()
        self.policy_order()
        self.essids()
        self.wlan_servers()
        self.wlan_bodies()
        self.auth_profiles()
        if self.live:
            self.against_central()

    def contents(self) -> None:
        for t, key, body, where in self.objects(everything=True):
            name = str(body.get("name") or key)
            if not t.nac and "/" in key:
                self.error(where, "has a / in its name, which Central's API can't address; rename it")
            for path in registry.secrets_in(t, body):
                self.error(where, f"holds the secret field {path}; secrets are set in the UI")
            for g in t.generated:
                if g in body:
                    self.warn(where, f"{g} is set by Central; leave it out")
            for path in t.ui_set:
                if registry.get_path(body, path) is not None:
                    self.warn(where, f"{path} is set in the UI in each environment; leave it out, since the tool "
                                     "ignores it and keeps Central's")
            if name.startswith("sys_"):
                self.error(where, "system objects are never in the files")
            elif t.path == "roles" and name in self.derived:
                self.warn(where, "is a WLAN's derived role; leave it out")
            elif self.default(t.path, name):
                self.warn(where, "is one of Central's factory defaults, which the tool never touches; remove the file")
            elif self.env.ignored(name):
                self.warn(where, "matches an ignore glob in env.yaml, so the tool never touches it")
            elif default := self.folded_defaults.get((t.path, name.casefold())):
                self.error(where, f"Central refuses this name, since it matches the factory default "
                                  f"{t.path}/{default} ignoring case; rename it")

    def assignments(self) -> None:
        assigned = defaultdict(set)
        for kind, scope, type_path, name in sorted(self.d.assigned()):
            assigned[type_path].add(name)
            where = f"assign.yaml: {scope_label(kind, scope)}"
            if name.startswith("sys_") or (type_path == "roles" and name in self.derived):
                self.error(where, f"{type_path}/{name} is a system or derived object; leave it out")
            elif self.env.ignored(name):
                self.warn(where, f"{type_path}/{name} matches an ignore glob, so it isn't assigned")
            elif name not in self.d.profiles.get(type_path, {}):
                self.error(where, f"no file profiles/{type_path}/{file_name(name)}.json")
        for type_path, objs in self.profiles.items():
            for name in objs:
                if name not in assigned[type_path]:
                    self.warn(self.d.where(type_path, name), "isn't assigned anywhere; no device gets it")

    def used_assignments(self) -> None:
        """What an assigned profile refers to must be assigned at the same scope or at Global,
        when the folder holds it. Central assigns some of these itself (a server group's auth
        servers), so assign.yaml would otherwise not show what's there. Taking both off a
        scope together is fine: plan unassigns the user first."""
        assigned = set(self.d.assigned())
        missing = set()
        for kind, scope, type_path, name in assigned:
            body = self.profiles.get(type_path, {}).get(name)
            for field, target in registry.BY_PATH[type_path].refs if body else ():
                for value in registry.values_at(body, field):
                    if (value in self.profiles.get(target, {}) and (target, value) != (type_path, name)
                            and not {(kind, scope, target, value), ("global", "", target, value)} & assigned):
                        missing.add((kind, scope, type_path, name, target, value))
        for kind, scope, type_path, name, target, value in sorted(missing):
            self.error(f"assign.yaml: {scope_label(kind, scope)}",
                       f"{type_path}/{name} uses {target}/{value}, which isn't assigned there or at Global; "
                       f"list it there, or remove {name} from there too")

    def alias_values(self) -> None:
        """A site's own value for an alias is a same-name local copy of it, so the alias needs a
        file, a type that holds one value, and a Global assignment for the copy to override."""
        top = set(self.d.assign["global"].get("aliases", []))
        types = ", ".join(sorted(registry.ALIAS_VALUES))
        for site, values in sorted(self.d.alias_values.items()):
            for name in sorted(values):
                where, body = f"assign.yaml: {site}", self.d.profiles.get("aliases", {}).get(name)
                if body is None:
                    self.error(where, f"alias-values.{name}: no file profiles/aliases/{file_name(name)}.json")
                elif not self.managed("aliases", name):
                    self.error(where, f"alias-values.{name}: the tool never touches this alias, so it can't "
                                      "give it site values")
                elif body.get("type") not in registry.ALIAS_VALUES:
                    self.error(where, f"alias-values.{name}: only {types} aliases take site values; this one "
                                      f"is {body.get('type')}")
                elif name not in top:
                    self.error(where, f"alias-values.{name}: the alias isn't assigned at Global, so there's "
                                      "nothing for the site's value to override; list it under global")

    def one_per_scope(self) -> None:
        """A scope takes one settings profile of each type, so assigning one where another is
        assigned fails. Unassigns run first, so only Central's profiles not in the files block."""
        want = defaultdict(set)
        for kind, scope, type_path, name in self.d.assigned():
            if registry.BY_PATH[type_path].one_per_scope and self.managed(type_path, name):
                want[(kind, scope, type_path)].add(name)
        for (kind, scope, type_path), names in sorted(want.items()):
            if len(names) > 1:
                self.error(f"assign.yaml: {scope_label(kind, scope)}",
                           f"{' and '.join(sorted(names))} are both {type_path} profiles; a scope takes one")
        if self.live is None:
            return
        for r in self.live.assignments:
            scope = self.live.scope_of(str(r["scope-id"]))
            type_path, name = r.get("profile-type"), str(r.get("profile-instance"))
            if r.get("device-function") != "CAMPUS_AP" or scope is None or (*scope, type_path) not in want:
                continue
            if name not in want[(*scope, type_path)] and name not in self.profiles.get(type_path, {}):
                self.error(f"assign.yaml: {scope_label(*scope)}",
                           f"{type_path}/{name} is assigned there in Central and isn't in the files; a scope takes "
                           f"one {type_path} profile, so unassign it in the UI first")

    def resolve(self, where: str, field: str, target: str, value: str) -> None:
        if target == registry.IDENTITY_STORES:
            if self.live and value not in self.live.identity_stores:
                self.error(where, f"{field}: identity store {value} isn't in {self.env.name}")
            return
        if target == registry.CERTIFICATES:
            if self.live and value not in self.live.certificates:
                self.error(where, f"{field}: certificate {value} isn't in {self.env.name}; upload it in the UI first")
            return
        t = registry.BY_PATH[target]
        if value in (self.nac if t.nac else self.profiles).get(target, {}):
            return
        if value.startswith("sys_") or (target == "roles" and value in self.derived):
            return
        if self.live is None:
            self.warn(where, f"{field}: {target}/{value} isn't in the files; plan checks Central")
        elif value not in self.live.objects.get(target, {}):
            self.error(where, f"{field}: {target}/{value} isn't in the files or in Central")

    def references(self) -> None:
        for t, _, body, where in self.objects():
            for path, target in t.refs:
                for value in registry.values_at(body, path):
                    self.resolve(where, path, target, value)
            if t.path == "authz-policies":
                for item in registry.dicts(body):
                    match = STORE_ATTR.match(str(item.get("attr", "")))
                    if match:
                        self.resolve(where, "condition attr", registry.IDENTITY_STORES, match.group(1))
                    if item.get("attr-name") == ROLE_ATTR and item.get("value"):
                        self.resolve(where, ROLE_ATTR, "roles", str(item["value"]))

    def authz(self) -> None:
        positions: dict[Any, str] = {}
        for key, body in self.nac.get("authz-policies", {}).items():
            where = self.d.where("authz-policies", key)
            pos = body.get("position")
            if pos in positions:
                self.error(where, f"position {pos} is also used by {positions[pos]}")
            positions[pos] = where
            rule_positions: set[Any] = set()
            for rule in body.get("rule") or []:
                if rule.get("position") in rule_positions:
                    self.error(where, f"two rules at position {rule.get('position')}")
                rule_positions.add(rule.get("position"))
            for entry, list_key, id_key in registry.nested_ids(registry.BY_PATH["authz-policies"], body):
                if not UUID.match(str(entry.get(id_key))):   # an empty one was given a UUID on loading
                    self.error(where, f"every {list_key} needs a UUID {id_key}, or an empty one for a new {list_key}")
        if self.live:
            for key, obj in self.live.objects["authz-policies"].items():
                if key not in self.nac.get("authz-policies", {}) and obj.get("position") in positions:
                    self.error(positions[obj["position"]], f"position {obj['position']} is taken by "
                                                           f"{obj.get('name') or key}, which stays as it is")

    def policy_order(self) -> None:
        group = self.d.policy_group
        where = "profiles/policy-groups.json"
        if group is None:
            if any(t == "policies" and self.managed(t, name) for _, _, t, name in self.d.assigned()):
                self.error(where, "not found; every policy assign.yaml assigns needs an entry in it")
            return
        mine: dict[Any, str] = {}
        listed = {e.get("name") for e in group.get("policy-group-list", []) if isinstance(e, dict)}
        for name in sorted({n for _, _, type_path, n in self.d.assigned() if type_path == "policies"} - listed):
            if self.managed("policies", name):
                self.error(where, f"has no entry for {name}, which assign.yaml assigns; Central refuses to "
                                  "assign a policy that isn't in the policy order")
        for entry in group.get("policy-group-list", []):
            name, pos = entry.get("name") if isinstance(entry, dict) else None, None
            if isinstance(entry, dict):
                pos = entry.get("position")
            if not isinstance(name, str) or not isinstance(pos, int) or isinstance(pos, bool):
                self.error(where, "every entry needs a name and a whole-number position")
                continue
            if name not in self.d.profiles.get("policies", {}):
                self.error(where, f"{name} isn't a policy in the files")
            if pos in mine:
                self.error(where, f"{name} and {mine[pos]} are both at position {pos}")
            mine[pos] = name
        if self.live and self.live.policy_group:
            names = set(mine.values())
            for entry in self.live.policy_group.get("policy-group-list", []):
                if entry.get("name") not in names and entry.get("position") in mine:
                    self.error(where, f"position {entry['position']} ({mine[entry['position']]}) is taken by "
                                      f"{entry['name']}, which stays as it is")

    def essids(self) -> None:
        """No scope gets two enabled WLANs that broadcast the same ESSID."""
        wlans = self.profiles.get("wlan-ssids", {})
        top = set(self.d.assign["global"].get("wlan-ssids", []))
        contexts = {"Global": top}
        for site, types in self.d.assign["sites"].items():
            contexts[site] = top | set(types.get("wlan-ssids", []))
        seen = set()
        for context, names in contexts.items():
            by_essid = defaultdict(list)
            for name in sorted(names):
                body = wlans.get(name)
                if body is None or body.get("enable") is False:
                    continue
                essid = body.get("essid") or {}
                key = f"alias {essid['alias']}" if essid.get("alias") else str(essid.get("name") or name)
                by_essid[key].append(name)
            for essid, clash in by_essid.items():
                if len(clash) > 1 and (essid, tuple(clash)) not in seen:
                    seen.add((essid, tuple(clash)))
                    self.error(f"assign.yaml: {context}", f"{' and '.join(clash)} both broadcast ESSID {essid}")

    def wlan_servers(self) -> None:
        """Central refuses a WLAN with both a server group and a primary auth server. A new
        enterprise WLAN with Central NAC gets the sys_central_nac server group from Central,
        so it can't name a primary auth server either (UI-made ones can, so only new ones count)."""
        for name, body in self.profiles.get("wlan-ssids", {}).items():
            where = self.d.where("wlan-ssids", name)
            if body.get("auth-server-group") and body.get("primary-auth-server"):
                self.error(where, "sets both auth-server-group and primary-auth-server; Central refuses a WLAN with both")
            elif (self.live and name not in self.live.objects["wlan-ssids"] and body.get("cloud-auth") is True
                  and "ENTERPRISE" in str(body.get("opmode", "")) and body.get("primary-auth-server")):
                self.error(where, "is new and uses Central NAC, so Central gives it the sys_central_nac server group "
                                  "and refuses primary-auth-server; remove primary-auth-server")

    def wlan_bodies(self) -> None:
        """What a WLAN needs on a device, as the customer lab found: settings Central must send
        the AP, cloud MPSK for an MPSK WLAN with Central NAC, and for a PSK WLAN what the UI
        needs before it can set the passphrase."""
        for name, body in self.profiles.get("wlan-ssids", {}).items():
            where = self.d.where("wlan-ssids", name)
            missing = [f for f in PUSHED if registry.get_path(body, f) is None]
            if missing:
                ui = ", ".join(f"{f}: {json.dumps(PUSHED[f])}" for f in missing)
                self.error(where, f"leaves out {' and '.join(missing)}, so Central sends the AP config lines it "
                                  f"refuses and the push fails; UI-made WLANs set {ui}")
            if ("MPSK" in str(body.get("opmode", "")) and body.get("cloud-auth") is True
                    and registry.get_path(body, "personal-security.mpsk-cloud-auth") is not True):
                self.error(where, "uses Central NAC for MPSK without personal-security.mpsk-cloud-auth: true, so the "
                                  "AP does a plain MAC auth, which fails")
            if registry.is_psk_wlan(body):
                if not registry.get_path(body, "personal-security.passphrase-format"):
                    self.warn(where, "has no personal-security.passphrase-format, so the UI can't show its security "
                                     "section to set the passphrase")
                if not (body.get("vlan-id-range") or body.get("vlan-name")):
                    self.warn(where, "has no VLAN (vlan-id-range or vlan-name), and the UI won't set the passphrase "
                                     "until it has one; UI-made WLANs default to VLAN 1")

    def auth_profiles(self) -> None:
        bound = set()
        for key, body in self.nac.get("auth-profiles", {}).items():
            networks = body.get("networks") or []
            bound |= set(networks)
            if body.get("auth-type") == "CAPTIVE_PORTAL":
                self.error(self.d.where("auth-profiles", key),
                           "captive portals aren't supported; delete it in Central, or add it to ignore in env.yaml")
            if body.get("auth-type") == "MPSK" and len(networks) != 1:
                self.error(self.d.where("auth-profiles", key),
                           f"an MPSK auth profile serves exactly one WLAN; it lists {len(networks)}")
            if body.get("auth-type") == "MPSK" and body.get("identity-stores"):
                self.error(self.d.where("auth-profiles", key),
                           "an MPSK auth profile names no identity stores; Central creates its own store")
            if self.live and body.get("auth-type") == "MPSK":
                current = self.live.objects["auth-profiles"].get(key)
                if current and current.get("networks") != networks:
                    self.error(self.d.where("auth-profiles", key),
                               "an MPSK auth profile's WLAN can't be changed; Central refuses it")
        if self.live:   # auth profiles not in the files count too
            for key, obj in self.live.objects["auth-profiles"].items():
                if key not in self.d.nac.get("auth-profiles", {}):
                    bound |= set(obj.get("networks") or [])
        assigned = {name for _, _, type_path, name in self.d.assigned() if type_path == "wlan-ssids"}
        for name in sorted(assigned):
            body = self.profiles.get("wlan-ssids", {}).get(name)
            if not (body and body.get("cloud-auth") is True and name not in bound):
                continue
            if self.live:
                self.error(self.d.where("wlan-ssids", name), "uses Central NAC but isn't in any auth profile's networks")
            else:
                self.warn(self.d.where("wlan-ssids", name),
                          "uses Central NAC but isn't in the networks of any auth profile in the files; plan checks Central")

    def one_user_per_role(self) -> None:
        """A scope takes one management user of each role (rolename), so assigning one where
        another of its role is assigned fails. As with one_per_scope, unassigns run first, so only
        Central's users not in the files block."""
        users = self.profiles.get("management-users", {})
        want = defaultdict(set)
        for kind, scope, type_path, name in self.d.assigned():
            if type_path == "management-users" and users.get(name, {}).get("rolename"):
                want[(kind, scope, users[name]["rolename"])].add(name)
        for (kind, scope, role), names in sorted(want.items()):
            if len(names) > 1:
                self.error(f"assign.yaml: {scope_label(kind, scope)}",
                           f"management-users/{' and '.join(sorted(names))} both have role {role}; "
                           "Central allows one user per role at a scope")
        if self.live is None:
            return
        for r in self.live.assignments:
            scope = self.live.scope_of(str(r["scope-id"]))
            name = str(r.get("profile-instance"))
            if r.get("profile-type") != "management-users" or r.get("device-function") != "CAMPUS_AP" \
                    or scope is None or name in users:
                continue
            role = (self.live.objects["management-users"].get(name) or {}).get("rolename")
            if blocked := want.get((*scope, role)):
                self.error(f"assign.yaml: {scope_label(*scope)}",
                           f"management-users/{' and '.join(sorted(blocked))} has role {role}, as does {name}, "
                           f"which is assigned there in Central and isn't in the files; Central allows one user "
                           f"per role at a scope, so unassign {name} first")

    def against_central(self) -> None:
        live = self.live
        for type_path, key in sorted(self.d.blank):   # a second object by that name would be made
            body = self.nac.get(type_path, {}).get(key)
            if body is None or key in live.objects[type_path] or not body.get("name"):
                continue
            taken = [oid for oid, obj in live.objects[type_path].items() if obj.get("name") == body["name"]]
            if taken:
                self.error(self.d.where(type_path, key),
                           f"has an empty {registry.BY_PATH[type_path].id_field}, but Central already has "
                           f"{type_path}/{body['name']} with ID {taken[0]}; put that ID in the file to manage it")
        for name in self.profiles.get("management-users", {}):
            if name not in live.objects["management-users"]:
                self.error(self.d.where("management-users", name), "create it in the UI first; Central needs its password")
        for name, body in self.profiles.get("auth-servers", {}).items():
            current = live.objects["auth-servers"].get(name)   # RadSec takes no shared secret
            if current is not None and not body.get("enable-radsec") and not current.get("shared-secret-config"):
                self.warn(self.d.where("auth-servers", name), "has no shared secret in Central; set it in the UI")
        for name, body in self.profiles.get("wlan-ssids", {}).items():
            current = live.objects["wlan-ssids"].get(name)
            if current is not None and registry.is_psk_wlan(body) and not registry.has_psk(current):
                self.warn(self.d.where("wlan-ssids", name),
                          "has no PSK in Central; set it in the UI. The tool keeps it disabled until then.")
        for site in sorted(self.d.assign["sites"]):
            if site not in live.site_ids and site not in (self.d.sites or {}):
                self.error("assign.yaml", f"site {site} isn't in Central or in sites.yaml")
        live_sites = {s["scopeName"]: s for s in live.sites}
        for name, body in (self.d.sites or {}).items():
            current = live_sites.get(name)
            if current is None:
                continue
            fields = [(k, str(current.get(k) or ""), body[k]) for k in ("address", "city", "state", "country", "zipcode")]
            fields.append(("timezone", str((current.get("timezone") or {}).get("timezoneId") or ""),
                           body["timezone"]["timezoneId"]))
            differ = [f"{k}: {old} → {new}" for k, old, new in fields if old != new]
            if differ:
                self.warn(f"sites.yaml: {name}", f"differs from Central ({'; '.join(differ)}); sites aren't updated")
