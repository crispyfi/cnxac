"""Reads an environment's live config from Central, once, for pull and plan."""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Collection
from dataclasses import dataclass, field
from urllib.parse import quote

from . import registry
from .client import Client, items_of
from .config import EnvConfig

AP = "CAMPUS_AP"
NAC = "SERVICE_PERSONA"
META, IS_DEFAULT = "@", "aruba-annotation:is_default"   # metadata a detailed=true read adds to each object


def skip_reason(type_path: str, name: str, env: EnvConfig, derived_roles: set[str] = frozenset()) -> str | None:
    """Why an object is never in the files, or None."""
    if name.startswith("sys_"):
        return "system"
    if type_path == "roles" and name in derived_roles:
        return "derived"
    if env.ignored(name):
        return "ignored"
    return None


def derived_roles_of(wlans: list[dict]) -> set[str]:
    """A WLAN's auto-default role is derived. Older WLANs lack the field but still have a
    same-named role."""
    return {w.get("auto-default-role") or w["ssid"] for w in wlans}


@dataclass
class Classified:
    """Live CAMPUS_AP assignments, split into the managed ones and the rest."""
    managed: set[tuple[str, str, str, str]] = field(default_factory=set)  # (scope kind, scope name, type, name)
    library: set[tuple[str, str]] = field(default_factory=set)            # (scope ID, "type/name")
    unmanaged: list[str] = field(default_factory=list)
    unknown_scopes: list[str] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)


@dataclass
class Live:
    assignments: list[dict]
    scope_maps: list[dict]
    sites: list[dict]
    objects: dict[str, dict[str, dict]]      # type path -> ID -> object, for every type but single ones
    policy_group: dict | None
    identity_stores: dict[str, dict]
    certificates: set[str] = field(default_factory=set)   # names in the certificate store
    defaults: set[tuple[str, str]] = field(default_factory=set)   # (type, name) of Central's factory defaults
    local_aliases: dict[tuple[str, str], dict] = field(default_factory=dict)   # (scope ID, name) -> local copy

    def __post_init__(self) -> None:
        self.global_id = next((str(r["scope-id"]) for r in self.assignments if r.get("scope-type") == "GLOBAL"), None)
        self.site_names = {str(s["scopeId"]): s["scopeName"] for s in self.sites}
        self.site_ids = {name: sid for sid, name in self.site_names.items()}
        self.derived_roles = derived_roles_of(list(self.objects.get("wlan-ssids", {}).values()))

    def label(self, sid: str) -> str:
        if sid == self.global_id:
            return "Global"
        return self.site_names.get(sid, f"scope {sid}")

    def scope_of(self, sid: str) -> tuple[str, str] | None:
        if sid == self.global_id:
            return ("global", "")
        if sid in self.site_names:
            return ("site", self.site_names[sid])
        return None

    def skip(self, type_path: str, name: str, env: EnvConfig) -> str | None:
        """Why an object is never in the files, or None. Factory defaults are Central's own:
        never pulled or touched, though they can still be referred to."""
        if (type_path, name) in self.defaults:
            return "default"
        return skip_reason(type_path, name, env, self.derived_roles)

    def classify(self, env: EnvConfig) -> Classified:
        out = Classified()
        for r in self.assignments:
            if r.get("device-function") != AP:
                continue
            type_path, name, sid = r["profile-type"], str(r["profile-instance"]), str(r["scope-id"])
            out.library.add((sid, f"{type_path}/{name}"))
            t = registry.BY_PATH.get(type_path)
            if t is None or t.nac:
                if type_path in registry.NOT_MANAGED:
                    out.skipped[registry.NOT_MANAGED[type_path]] += 1
                else:
                    out.unmanaged.append(f"{type_path}/{name} ({self.label(sid)})" if name else
                                         f"{type_path} ({self.label(sid)})")
                continue
            if t.single:
                continue
            reason = self.skip(type_path, name, env)
            if reason:
                out.skipped[reason] += 1
                continue
            scope = self.scope_of(sid)
            if scope is None:
                out.unknown_scopes.append(f"{type_path}/{name} (scope {sid})")
                continue
            out.managed.add((*scope, type_path, name))
        return out

    def local_profiles(self, env: EnvConfig, types: Collection[str] | None = None) -> list[str]:
        """Profiles in scope-maps that aren't library assignments: local copies, e.g. from
        editing at a site in the UI. A local copy of a factory default is listed too, since it
        overrides what's assigned above it. Optionally only for some types."""
        library = self.classify(env).library
        found = []
        for m in self.scope_maps:
            type_path, _, name = m["resource"].partition("/")
            t = registry.BY_PATH.get(type_path)
            sid = str(m["scope-id"])
            if m.get("persona") != AP or t is None or t.nac or t.single or not name:
                continue
            if types is not None and type_path not in types:
                continue
            if (sid, m["resource"]) in library or skip_reason(type_path, name, env, self.derived_roles):
                continue
            if type_path == "aliases" and registry.is_value_copy(self.objects["aliases"].get(name) or {},
                                                                 self.local_aliases.get((sid, name)) or {}):
                continue   # a site's own value for the alias, which assign.yaml holds
            found.append(f"{self.label(sid)}: {m['resource']}")
        return found

    def alias_values(self, name: str) -> dict[str, dict]:
        """Site name -> the local copy of an alias at that site."""
        return {scope[1]: body for (sid, n), body in self.local_aliases.items()
                if n == name and (scope := self.scope_of(sid)) and scope[0] == "site"}

    def users(self) -> dict[tuple[str, str, str, str], set[tuple[str, str]]]:
        """What uses each profile at a scope: (scope kind, scope name, type, name) to the
        (type, name) of objects assigned at that scope whose reference fields name it. Central
        assigns some of these along with their user, e.g. a server group's auth servers."""
        found = defaultdict(set)
        for r in self.assignments:
            t = registry.BY_PATH.get(r.get("profile-type"))
            scope = self.scope_of(str(r["scope-id"]))
            if r.get("device-function") != AP or t is None or t.nac or t.single or scope is None:
                continue
            name = str(r["profile-instance"])
            for field, target in t.refs:
                for value in registry.values_at(self.objects[t.path].get(name) or {}, field):
                    if (target, value) != (t.path, name):
                        found[(*scope, target, value)].add((t.path, name))
        return found

    def nac_assigned(self) -> set[tuple[str, str]]:
        """(type, ID) of NAC objects assigned at Global."""
        return {(r["profile-type"], str(r["profile-instance"])) for r in self.assignments
                if r.get("device-function") == NAC and str(r["scope-id"]) == self.global_id}


def read(client: Client) -> Live:
    objects, defaults = {}, set()
    for t in registry.TYPES:
        if t.single:
            continue
        objects[t.path] = {}
        for o in items_of(client.get(t.path, {"detailed": "true"}), t.list_key):
            if t.id_field not in o:
                continue
            key = str(o[t.id_field])
            if (o.pop(META, None) or {}).get(IS_DEFAULT):
                defaults.add((t.path, str(o.get("name") or key) if t.nac else key))
            objects[t.path][key] = o
    group = (client.get("policy-groups") or {}).get("policy-group")
    assignments = items_of(client.get("config-assignments"), "config-assignment")
    scope_maps = items_of(client.get("scope-maps"), "scope-map")
    return Live(
        assignments=assignments,
        scope_maps=scope_maps,
        sites=client.get_paged("sites"),
        objects=objects,
        policy_group=group if isinstance(group, dict) else None,
        identity_stores={str(s["id"]): s for s in client.get_items("identity-stores", "store") if "id" in s},
        certificates={str(c["name"]) for c in client.get_items("certificate-store", "certificate") if "name" in c},
        defaults=defaults,
        local_aliases=_local_aliases(client, assignments, scope_maps),
    )


def _local_aliases(client: Client, assignments: list[dict], scope_maps: list[dict]) -> dict[tuple[str, str], dict]:
    """Local copies of aliases: in scope-maps but not assigned from the library at that scope.
    Only aliases are read, since the tool writes no other local profiles."""
    library = {(str(r["scope-id"]), f'{r["profile-type"]}/{r["profile-instance"]}') for r in assignments
               if r.get("device-function") == AP}
    found = {}
    for m in scope_maps:
        type_path, _, name = str(m.get("resource", "")).partition("/")
        sid = str(m["scope-id"])
        if m.get("persona") != AP or type_path != "aliases" or not name or (sid, m["resource"]) in library:
            continue
        body = client.get(f"aliases/{quote(name, safe='')}", {"view-type": "LOCAL", "scope-id": sid,
                                                                "device-function": AP})
        if isinstance(body, dict) and body:
            body.pop(META, None)
            found[(sid, name)] = body
    return found
