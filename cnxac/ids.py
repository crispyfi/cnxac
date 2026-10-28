"""ids: an environment's identity stores, Intune extensions and certificates, and the store IDs
and certificate names a folder uses. These are made or uploaded in the UI, so a folder copied
to another environment needs the target's in place of the source's.

It only reads. A Microsoft store and an Intune extension each hold a client secret, so only
names, types and IDs are kept, and for certificates only their names, types and expiry dates.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import registry
from .checks import STORE_ATTR
from .client import Client
from .config import EnvConfig
from .desired import Desired

# Store types Central makes itself: system stores, and one per MPSK auth profile
CENTRAL_MADE = {"REGISTERED_DPP", "REGISTERED_MACS", "NAMED_MPSK"}
TYPES = {"MICROSOFT": "Microsoft Entra ID", "REGISTERED_DPP": "Wi-Fi Easy Connect",
         "REGISTERED_MACS": "Registered MACs", "NAMED_MPSK": "Named MPSK"}
CERT_TYPES = {"CA_CERT": "CA certificate", "SERVER_CERT": "Server certificate"}
GROUP_IDS = ":GroupIds"


@dataclass
class Store:
    id: str
    name: str
    type: str                                           # in words
    made_in_ui: bool
    profiles: list[str] = field(default_factory=list)   # the environment's auth profiles that list it


@dataclass
class Certificate:
    name: str
    type: str       # in words
    expires: str    # the date it's valid to, or empty


@dataclass
class Found:
    stores: list[Store]
    intune: list[str]                                    # Intune extensions, by name
    certificates: list[Certificate]
    uses: dict[str, list[str]]                           # store ID -> the files that use it
    groups: list[tuple[str, str]]                        # (file and rule, Entra group ID)
    cert_uses: dict[str, list[str]]                      # certificate name -> the files that use it

    def store(self, sid: str) -> Store | None:
        return next((s for s in self.stores if s.id == sid), None)

    def certificate(self, name: str) -> Certificate | None:
        return next((c for c in self.certificates if c.name == name), None)

    def only_made_in_ui(self) -> Store | None:
        """The environment's one store made in the UI, if it has exactly one."""
        made = [s for s in self.stores if s.made_in_ui]
        return made[0] if len(made) == 1 else None


def find(client: Client, d: Desired, env: EnvConfig) -> Found:
    profiles = client.get_items("auth-profiles", "profile")
    stores = []
    for s in client.get_items("identity-stores", "store"):
        if "id" not in s:
            continue
        sid, kind = str(s["id"]), str(s.get("id-store-type") or "")
        users = sorted(str(p.get("name")) for p in profiles if sid in (p.get("identity-stores") or []))
        stores.append(Store(sid, str(s.get("name") or sid), TYPES.get(kind, kind), kind not in CENTRAL_MADE, users))
    intune = [str(i["name"]) for i in client.get_items("uem-msintune-ext-instances", "uem-msintune-instance")
              if "name" in i]
    certificates = [Certificate(str(c["name"]), CERT_TYPES.get(str(c.get("type") or ""), str(c.get("type") or "")),
                                str(c.get("valid-to") or "")[:10])
                    for c in client.get_items("certificate-store", "certificate") if "name" in c]
    uses, groups = used(d, env)
    return Found(sorted(stores, key=lambda s: s.name.casefold()), sorted(intune),
                 sorted(certificates, key=lambda c: c.name.casefold()), uses, groups, certificates_used(d, env))


def _add(uses: dict[str, list[str]], key: str, where: str) -> None:
    if where not in uses.setdefault(key, []):
        uses[key].append(where)


def used(d: Desired, env: EnvConfig) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """The store IDs the folder's NAC files use, with the files using each, and the Entra group
    IDs in authorization conditions, with the file and rule (or the policy's pre-conditions) of
    each."""
    uses: dict[str, list[str]] = {}
    groups = []
    for t in registry.NAC_TYPES:
        for key, body in d.nac.get(t.path, {}).items():
            if env.ignored(str(body.get("name") or key)):
                continue
            body, where = registry.strip(t, body)[0], d.where(t.path, key)
            found = [v for path, target in t.refs if target == registry.IDENTITY_STORES
                     for v in registry.values_at(body, path)]
            rules = sorted(body.get("rule") or [], key=lambda r: r.get("position", 0))
            for label, part in [("pre-conditions", body), *((f"rule {r.get('rule-name')}", r) for r in rules)]:
                for c in (part.get("conditions") or {}).get("condition") or []:
                    for g in c.get("condition-group") or []:
                        attr = str(g.get("attr", ""))
                        match = STORE_ATTR.match(attr)
                        if not match:
                            continue
                        found.append(match.group(1))
                        if attr.endswith(GROUP_IDS):
                            groups += [(f"{where}, {label}", v)
                                       for path in ("value", "values[]") for v in registry.values_at(g, path)]
            for sid in found:
                _add(uses, sid, where)
    return uses, groups


def certificates_used(d: Desired, env: EnvConfig) -> dict[str, list[str]]:
    """The certificate names the folder's AP profiles use, with the files using each."""
    uses: dict[str, list[str]] = {}
    for t in registry.AP_TYPES:
        paths = [path for path, target in t.refs if target == registry.CERTIFICATES]
        for key, body in d.profiles.get(t.path, {}).items() if paths else ():
            if not env.ignored(key):
                for name in (v for path in paths for v in registry.values_at(body, path)):
                    _add(uses, name, d.where(t.path, key))
    return uses
