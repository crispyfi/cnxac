"""The type table: one row per managed type, in create and update order.

Adding a type means adding one row. Reference fields are curated from real bodies and the
API reference's x-path notes; fields that aren't listed aren't checked.
"""
from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

IDENTITY_STORES = "identity-stores"   # reference target: UI-made, checked against Central only
CERTIFICATES = "certificate-store"    # reference target, by name: uploaded in the UI, checked against Central only
IDENTITY_FILE = "identity-management.json"   # in nac/: pull's read-only copy of the UI-made identity config


def _mpsk_store(obj: dict) -> None:
    """Central creates each MPSK auth profile's named-MPSK store, so the store ID is generated."""
    if obj.get("auth-type") == "MPSK":
        obj.pop("identity-stores", None)


@dataclass(frozen=True)
class Type:
    path: str                         # API path, also the profile-type in assignments
    list_key: str                     # list key in GET and bulk bodies; for a single object, its key
    id_field: str                     # empty for a single object
    nac: bool = False                 # Central NAC: UUID keyed, assigned at Global
    single: bool = False              # one object per environment, in profiles/<path>.json
    generated: tuple[str, ...] = ()   # top-level keys Central adds
    secrets: tuple[str, ...] = ()     # dotted paths of secret fields
    ui_set: tuple[str, ...] = ()      # dotted paths set in the UI in each environment: never in files, kept on update
    refs: tuple[tuple[str, str], ...] = ()  # (field path, target type); "[]" walks a list
    nac_adds: tuple[str, ...] = ()    # keys Central adds when cloud-auth is on; ignored if the file has none
    ui_created: bool = False          # Central needs a secret on create, so it's made in the UI
    one_per_scope: bool = False       # a settings profile: a scope takes one, which replaces Global's
    drop: Callable[[dict], None] | None = None  # removes conditionally generated fields
    nested_ids: tuple[tuple[str, str], ...] = ()  # (list key, ID key) of IDs inside a NAC body, at any depth

    @property
    def device_function(self) -> str:
        return "SERVICE_PERSONA" if self.nac else "CAMPUS_AP"


_RULE = "security-policy.policy-rule[].condition"

TYPES = (
    # 1. aliases, network groups and network services
    Type("aliases", "alias", "name"),
    Type("net-groups", "net-group", "name"),
    Type("net-services", "net-service", "name"),
    # 2. auth servers, then server groups
    Type("auth-servers", "auth-server", "name", secrets=("shared-secret-config",),
         refs=(("auth-server-address-alias", "aliases"), ("radsec-trusted-cacert-name", CERTIFICATES))),
    Type("server-groups", "server-group", "name", generated=("servers-count",), refs=(   # count: list reads only
        ("servers[].server-name", "auth-servers"),
        ("assignment-rules.assignment-rule[].role", "roles"),
        ("assignment-rules.assignment-rule[].named-vlan", "named-vlan"))),
    # 3. roles, then policies, then the policy order
    Type("roles", "role", "name", refs=(("policies[].name", "policies"),)),
    Type("policies", "policy", "name", refs=tuple(
        (f"{_RULE}.{side}.{field}", target) for side in ("source", "destination") for field, target in (
            ("role", "roles"), ("role-list[]", "roles"), ("net-group", "net-groups"),
            ("host-address.host-address-alias", "aliases"),
            ("network-address.ip-prefix-alias", "aliases"))) + ((f"{_RULE}.services.net-service", "net-services"),)),
    Type("policy-groups", "policy-group", "", single=True),
    # 4. named VLANs
    Type("named-vlan", "profile", "name"),
    # 5. WLANs
    Type("wlan-ssids", "wlan-ssid", "ssid", generated=("uuid", "auto-default-role"),
         secrets=("personal-security.wpa-passphrase",),
         nac_adds=("auth-server-group", "radius-accounting", "radius-interim-accounting-interval"),
         refs=(("essid.alias", "aliases"), ("personal-security.wpa-passphrase-alias", "aliases"),
               ("auth-server-group", "server-groups"), ("acct-server-group", "server-groups"),
               ("primary-auth-server", "auth-servers"), ("backup-auth-server", "auth-servers"),
               ("primary-acct-server", "auth-servers"), ("backup-acct-server", "auth-servers"),
               ("default-role", "roles"), ("pre-auth-role", "roles"), ("mac-auth-only-role", "roles"),
               ("machine-auth.machine-only-role", "roles"), ("machine-auth.user-only-role", "roles"),
               ("vlan-name", "named-vlan"), ("assignment-rules.assignment-rule[].named-vlan", "named-vlan"))),
    # 6. AP settings profiles
    Type("radios", "profile", "name", one_per_scope=True),
    Type("ids", "profile", "name", one_per_scope=True),
    Type("ap-system", "profile", "name", one_per_scope=True),
    Type("ap-uplinks", "profile", "name"),
    Type("ntp", "profile", "name", one_per_scope=True, refs=(("servers-alias", "aliases"),)),
    Type("local-management", "profile", "name", one_per_scope=True),
    Type("management-users", "user", "name", secrets=("password",), ui_created=True),
    Type("arc", "profile", "name", one_per_scope=True),
    Type("certificate-usage", "profile", "name", one_per_scope=True,
         refs=(("cert-usage-assignment[].cert-name", CERTIFICATES),)),
    # 7. Central NAC
    Type("auth-profiles", "profile", "auth-profile-id", nac=True, drop=_mpsk_store,
         ui_set=("eap.uem-providers",),   # UEM onboarding: its ID and SCEP URL are the environment's
         refs=(("networks[]", "wlan-ssids"), ("identity-stores[]", IDENTITY_STORES))),
    Type("authz-policies", "policy", "policy-id", nac=True, refs=(("identity-stores[]", IDENTITY_STORES),),
         nested_ids=(("rule", "rule-id"), ("condition", "condition-id"), ("condition-group", "condition-group-id"),
                     ("enf-profile", "profile-id"))),
)

BY_PATH = {t.path: t for t in TYPES}
AP_TYPES = tuple(t for t in TYPES if not t.nac)
NAC_TYPES = tuple(t for t in TYPES if t.nac)
ORDER = {t.path: i for i, t in enumerate(TYPES)}

# Assigned types that aren't managed, by design. Anything else unknown is reported.
NOT_MANAGED = {
    "role-gpids": "derived",
    "certificate-store": "left to the UI",
    "ap-port-profiles": "out of scope",
    "passpoint": "out of scope",
    "passpoint-identity": "out of scope",
    "layer2-vlan": "out of scope",
}

# Types the tool once managed. A folder's files and assign.yaml entries for them are ignored.
RETIRED = {"layer2-vlan"}

# Alias types a site can give its own value, as a same-name local copy: type -> the value's path
ALIAS_VALUES = {"ALIAS_AUTH_SERVER_ADDRESS": "default-value.auth-server-address-value.auth-server-address"}

# Safety net: string fields whose name contains one of these words are treated as secrets
# even when the registry doesn't list them, and are reported so the registry can be fixed.
SECRET_WORDS = {"secret", "password", "passphrase", "passwd", "psk", "plaintext"}
NOT_SECRET = {"passphrase-format", "password-policy", "secret-type"}
HIDDEN = "(secret, not shown)"


def is_psk_wlan(body: dict) -> bool:
    """A WLAN whose passphrase is a secret set in the UI (personal, not MPSK)."""
    opmode = str(body.get("opmode", ""))
    return ("PERSONAL" in opmode or "SAE" in opmode) and "MPSK" not in opmode


def has_psk(live_wlan: dict) -> bool:
    return bool((live_wlan.get("personal-security") or {}).get("wpa-passphrase"))


def alias_value(body: dict) -> str | None:
    """The value of an alias whose type a site can override, else None."""
    path = ALIAS_VALUES.get(body.get("type"))
    value = get_path(body, path) if path else None
    return None if value is None else str(value)


def with_alias_value(body: dict, value: str) -> dict:
    """The alias with another value: what a site's local copy of it holds."""
    out = copy.deepcopy(body)
    *parents, last = ALIAS_VALUES[body["type"]].split(".")
    node = out
    for key in parents:
        node = node.setdefault(key, {})
    node[last] = value
    return out


def is_value_copy(library: dict, local: dict) -> bool:
    """A local alias that differs from the library alias only in its value."""
    value = alias_value(local)
    return (library.get("type") in ALIAS_VALUES and local.get("type") == library.get("type")
            and value is not None and with_alias_value(library, value) == local)


def values_at(obj: Any, path: str) -> Iterator[str]:
    """Every scalar at a dotted path; a "[]" suffix walks the list at that key."""
    parts = path.split(".")

    def walk(x: Any, i: int) -> Iterator[str]:
        if i == len(parts):
            if isinstance(x, (str, int)) and not isinstance(x, bool) and x != "":
                yield str(x)
            return
        key, many = (parts[i][:-2], True) if parts[i].endswith("[]") else (parts[i], False)
        if not isinstance(x, dict) or key not in x:
            return
        for item in (x[key] if isinstance(x[key], list) else []) if many else [x[key]]:
            yield from walk(item, i + 1)
    yield from walk(obj, 0)


def dicts(obj: Any) -> Iterator[dict]:
    """Every dict in a body, at any depth."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from dicts(v)


def nested_ids(t: Type, body: dict) -> list[tuple[dict, str, str]]:
    """(entry, list key, ID key) for every entry of the type's lists that hold nested IDs."""
    out = []
    for item in dicts(body):
        for list_key, id_key in t.nested_ids:
            entries = item.get(list_key)
            out += [(e, list_key, id_key) for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    return out


def get_path(obj: Any, dotted: str) -> Any:
    for key in dotted.split("."):
        if not isinstance(obj, dict) or key not in obj:
            return None
        obj = obj[key]
    return obj


def pop_path(obj: Any, dotted: str) -> None:
    *parents, last = dotted.split(".")
    for key in parents:
        obj = obj.get(key) if isinstance(obj, dict) else None
    if isinstance(obj, dict):
        obj.pop(last, None)


def _strip_secret_like(obj: Any, path: str = "") -> list[str]:
    found = []
    if isinstance(obj, dict):
        for key in list(obj):
            value, here = obj[key], f"{path}{key}"
            if (isinstance(value, str) and value and key not in NOT_SECRET
                    and SECRET_WORDS & set(key.split("-"))):
                del obj[key]
                found.append(here)
            else:
                found += _strip_secret_like(value, here + ".")
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            found += _strip_secret_like(value, f"{path.rstrip('.')}[{i}].")
    return found


def _mask_secret_like(obj: Any) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and value and key not in NOT_SECRET and SECRET_WORDS & set(key.split("-")):
                obj[key] = HIDDEN
            else:
                _mask_secret_like(value)
    elif isinstance(obj, list):
        for value in obj:
            _mask_secret_like(value)


def masked(t: Type, obj: Any) -> Any:
    """A copy to show, with every secret value, listed or secret-like, replaced by HIDDEN."""
    out = copy.deepcopy(obj)
    for dotted in t.secrets:
        *parents, last = dotted.split(".")
        parent = get_path(out, ".".join(parents)) if parents else out
        if isinstance(parent, dict) and last in parent:
            parent[last] = HIDDEN
    _mask_secret_like(out)
    return out


def strip(t: Type, obj: dict) -> tuple[dict, list[str]]:
    """A copy without generated, secret or UI-set fields, and the paths of any secret-like
    fields the registry doesn't list (also removed)."""
    out = copy.deepcopy(obj)
    for key in t.generated:
        out.pop(key, None)
    for dotted in t.secrets + t.ui_set:
        pop_path(out, dotted)
    if t.drop:
        t.drop(out)
    return out, _strip_secret_like(out)


def carry(t: Type, live: dict, body: dict) -> dict:
    """A PUT body: the file body plus the fields Central owns, copied from the live object,
    because a PUT without them drops them. That covers generated fields, encrypted secrets and
    UI-set fields (into a parent the body already has), conditionally generated fields such as
    an MPSK profile's store, and the keys Central adds for Central NAC when the file doesn't
    set them."""
    out = copy.deepcopy(body)
    for key in t.generated:
        if key in live:
            out[key] = copy.deepcopy(live[key])
    for dotted in t.secrets + t.ui_set:
        value = get_path(live, dotted)
        *parents, last = dotted.split(".")
        parent = get_path(out, ".".join(parents)) if parents else out
        if value is not None and isinstance(parent, dict):
            parent[last] = copy.deepcopy(value)
    if t.drop:
        kept = copy.deepcopy(live)
        t.drop(kept)
        for key in live.keys() - kept.keys() - out.keys():
            out[key] = copy.deepcopy(live[key])
    if out.get("cloud-auth") is True:
        for key in t.nac_adds:
            if key in live and key not in out:
                out[key] = copy.deepcopy(live[key])
    return out


def secrets_in(t: Type, obj: dict) -> list[str]:
    """Paths of secret fields in a body: those the registry lists, then secret-like ones."""
    listed = [p for p in t.secrets if get_path(obj, p) is not None]
    rest = copy.deepcopy(obj)
    for dotted in t.secrets:
        pop_path(rest, dotted)
    return listed + _strip_secret_like(rest)
