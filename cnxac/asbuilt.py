"""asbuilt: a folder's config as a Markdown appendix to a design document, regenerated rather
than edited.

It renders offline from the files, which match Central whenever plan shows no changes. Each
type sits under its WebUI location, with only its key settings, and values in words rather
than API enums. Object IDs, secrets and Central's generated fields never appear, and nor do
named MPSK entries.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Any

from . import registry
from .config import EnvConfig
from .desired import Desired
from .registry import get_path, values_at

NONE = "–"
LONG = 30   # a longer channel list is shown as a count and a range

# Where each type is in the WebUI menus, in their order: a heading for each level
WEBUI: list[tuple[str, tuple[str, ...]]] = [
    ("wlan-ssids", ("Profiles", "Wireless", "WLAN")),
    ("radios", ("Profiles", "Wireless", "RF")),
    ("ids", ("Profiles", "Wireless", "Wireless IDS/IPS")),
    ("layer2-vlan", ("Profiles", "VLANs & Networks", "VLAN")),
    ("named-vlan", ("Profiles", "VLANs & Networks", "Named VLANs")),
    ("ap-uplinks", ("Profiles", "Interfaces", "AP Uplink")),
    ("ap-system", ("Profiles", "System", "AP System")),
    ("ntp", ("Profiles", "System", "NTP Server")),
    ("local-management", ("Profiles", "System", "System Administration")),
    ("management-users", ("Profiles", "System", "User Administration")),
    ("auth-servers", ("Profiles", "Security", "Authentication Server")),
    ("server-groups", ("Profiles", "Security", "Authentication Server Group")),
    ("certificate-usage", ("Profiles", "Security", "Certificate Usage")),
    ("arc", ("Profiles", "Application Experience", "App Recognition and Control")),
    ("roles", ("Roles & Policies", "Roles")),
    ("policies", ("Roles & Policies", "Security Policies", "Role-based Policies")),
    ("aliases", ("Named Objects", "Aliases")),
    ("net-services", ("Named Objects", "Services")),
    ("auth-profiles", ("Central NAC", "Authentication Profiles")),
    ("authz-policies", ("Central NAC", "Authorization Policies")),
    ("sites", ("Sites",)),
]
SHOWN_WITH = {"net-groups": "aliases"}   # the WebUI lists network groups as aliases of type Network Destination

# Words for API values ---------------------------------------------------------------------

VALUES = {   # whole values that read better spelled out than split into words
    "WPA2_MPSK_AES": "WPA2 MPSK (AES)", "WPA2_PERSONAL": "WPA2 Personal", "WPA2_ENTERPRISE": "WPA2 Enterprise",
    "WPA3_SAE": "WPA3 Personal (SAE)", "WPA3_ENTERPRISE_CCM_128": "WPA3 Enterprise (CCM-128)",
    "WPA3_ENTERPRISE_GCM_256": "WPA3 Enterprise (GCM-256)", "ENHANCED_OPEN": "Enhanced Open (OWE)",
    "24GHZ_5GHZ": "2.4 and 5 GHz", "5GHZ_6GHZ": "5 and 6 GHz", "24GHZ_5GHZ_6GHZ": "2.4, 5 and 6 GHz",
    "24GHZ_6GHZ": "2.4 and 6 GHz", "EAP_TLS": "EAP-TLS", "ETH0_ETH1": "Eth0 and Eth1",
    "RADIO_2DOT4G": "2.4 GHz", "RADIO_5G": "5 GHz", "RADIO_2ND_5G": "5 GHz (second radio)",
    "RADIO_6G": "6 GHz", "RADIO_2ND_6GHZ": "6 GHz (second radio)",
    "AP1X": "AP uplink 802.1X", "CA_CERT": "CA certificate", "SERVER_CERT": "Server certificate",
}
PREFIXES = ("FORWARD_MODE_", "ACTION_", "ADDRESS_", "RULE_", "BW_", "ANT_", "POLICY_TYPE_", "ALIAS_", "SPEED_", "OP_")
CASED = {"IPV4": "IPv4", "IPV6": "IPv6", "GHZ": "GHz", "MHZ": "MHz"}
ACRONYMS = {"AAA", "AES", "AP", "ARP", "CCM", "DFS", "DHCP", "DNS", "DPI", "EAP", "ESSID", "FQDN", "GCM", "HTTP", "HTTPS", "IP", "LACP",
            "LED", "MAC", "MPSK", "NTP", "OWE", "PSK", "RADIUS", "SAE", "SSH", "SSID", "STP", "TACACS", "TLS", "USB",
            "VLAN", "VRF", "WPA2", "WPA3"}


def enum(value: Any) -> str:
    """An API enum in words: WPA2_MPSK_AES as WPA2 MPSK (AES), IPV4_ONLY as IPv4 only."""
    if value is None or value == "":
        return NONE
    s = str(value)
    if s in VALUES:
        return VALUES[s]
    s = next((s[len(p):] for p in PREFIXES if s.startswith(p) and len(s) > len(p)), s)
    words = []
    for w in s.split("_"):
        if m := re.fullmatch(r"(\d+)(GHZ|MHZ)", w):
            words.append(f"{'2.4' if m[1] == '24' else m[1]} {CASED[m[2]]}")
        elif w in CASED or w in ACRONYMS:
            words.append(CASED.get(w, w))
        else:
            words.append(w.lower())
    out = " ".join(words)
    return out[0].upper() + out[1:]


def number(value: Any) -> str:
    """15.0 as 15; anything else as it is."""
    if value is None or value == "":
        return NONE
    s = str(value)
    return s[:-2] if re.fullmatch(r"-?\d+\.0", s) else s


def text(value: Any) -> str:
    """A setting as it reads: Yes or No, a list joined with commas, names as they are."""
    if value is None or value == "" or value == []:
        return NONE
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(text(v) for v in value)
    return number(value)


def onoff(value: Any) -> str:
    return NONE if value is None else ("On" if value else "Off")


def auto(value: Any) -> str:
    """AUTO in words, anything else (a country code, a time zone) as it is."""
    return "Auto" if value == "AUTO" else text(value)


def span(low: Any, high: Any, unit: str) -> str:
    low, high = number(low), number(high)
    if low == NONE and high == NONE:
        return NONE
    return f"{low} {unit}" if low == high else f"{low}–{high} {unit}"


def channels(values: list | None) -> str:
    nums = [m[1] if (m := re.match(r"CHAN_(\d+)", str(v))) else str(v) for v in values or []]
    if len(nums) > LONG:
        return f"{len(nums)} channels, {nums[0]}–{nums[-1]}"
    return ", ".join(nums) or NONE


def cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """A Markdown table, leaving out columns empty in every row (except the first); none
    without rows."""
    if not rows:
        return []
    keep = [i for i in range(len(headers)) if i == 0 or any(r[i] != NONE for r in rows)]
    lines = ["| " + " | ".join(headers[i] for i in keep) + " |", "|" + "---|" * len(keep)]
    lines += ["| " + " | ".join(cell(r[i]) for i in keep) + " |" for r in rows]
    return lines


# The appendix -----------------------------------------------------------------------------

class Appendix:
    def __init__(self, d: Desired, env: EnvConfig):
        self.d, self.env = d, env
        self.assigned = d.assigned()
        self.order = {e.get("name"): e.get("position") for e in (d.policy_group or {}).get("policy-group-list", [])}

    def objects(self, type_path: str) -> list[dict]:
        """The type's objects as files hold them, without secrets or generated fields, by name."""
        t = registry.BY_PATH[type_path]
        store = self.d.nac if t.nac else self.d.profiles
        bodies = [registry.strip(t, b)[0] for b in store.get(type_path, {}).values()]
        bodies = [b for b in bodies if not self.env.ignored(str(b.get("name") or b.get(t.id_field) or ""))]
        return sorted(bodies, key=lambda b: str(b.get("name") or b.get(t.id_field) or "").casefold())

    def name(self, type_path: str, body: dict) -> str:
        return str(body.get(registry.BY_PATH[type_path].id_field) or body.get("name"))

    def assigned_line(self, type_path: str, name: str) -> str:
        where = self.scopes(type_path, name)
        return "Not assigned." if where == "Not assigned" else f"Assigned: {where}."

    def scopes(self, type_path: str, name: str) -> str:
        """Where the object is assigned: Global and the sites, or Not assigned."""
        if registry.BY_PATH[type_path].nac:
            return "Global"
        found = {(kind, scope) for kind, scope, t, n in self.assigned if (t, n) == (type_path, name)}
        sites = sorted(s for kind, s in found if kind == "site")
        out = (["Global"] if ("global", "") in found else []) + sites
        return ", ".join(out) if out else "Not assigned"

    def render(self, today: date) -> str:
        lines = [f"# As-built configuration: {self.env.name}", "",
                 f"Generated by cnx-as-code from the config folder for {self.env.name} on {today.isoformat()}. "
                 "It lists the key settings of the AP and Central NAC configuration the tool manages, under "
                 "their WebUI locations. Regenerate it with `cnxac asbuilt` rather than editing it."]
        above: tuple[str, ...] = ()   # the menu path headed last
        for t, path in WEBUI:
            if not ((t == "sites" and self.d.sites) or (t != "sites" and any(map(self.objects, self.with_others(t))))):
                continue
            shared = next((i for i, (a, b) in enumerate(zip(above, path)) if a != b), min(len(above), len(path)))
            lines += [line for i in range(shared, len(path)) for line in ("", f"{'#' * (i + 2)} {path[i]}")]
            above = path
            lines += self.section(t, "#" * (len(path) + 2))
        return "\n".join(lines) + "\n"

    def section(self, type_path: str, level: str) -> list[str]:
        if type_path == "sites":
            return ["", *table(["Site", "Address", "City", "Country", "Time zone"], [
                [name, text(b.get("address")), text(b.get("city")), text(b.get("country")),
                 text(get_path(b, "timezone.timezoneId"))] for name, b in sorted(self.d.sites.items())])]
        if type_path in SETTINGS:
            out = []
            for body in self.objects(type_path):
                name = self.name(type_path, body)
                out += ["", f"{level} {name}", "", self.assigned_line(type_path, name)]
                shown = SETTINGS[type_path](self, body)
                out += ["", *shown] if shown else []
            return out
        if type_path == "policies":
            return self.policies(level)
        if type_path == "authz-policies":
            return self.authz_policies(level)
        headers = SUMMARIES.get(type_path, (["Name"],))[0]
        rows = []
        for t in self.with_others(type_path):
            row = SUMMARIES.get(t, (None, lambda a, b: [str(b.get("name"))]))[1]
            rows += [row(self, b) + ([] if t in NO_SCOPE else [self.scopes(t, self.name(t, b))]) for b in self.objects(t)]
        rows.sort(key=lambda r: r[0].casefold())
        return ["", *table(headers + ([] if type_path in NO_SCOPE else ["Assigned"]), rows)]

    def with_others(self, type_path: str) -> list[str]:
        """The type, and those the WebUI lists in its table."""
        return [type_path, *(t for t, shown in SHOWN_WITH.items() if shown == type_path)]

    # Collection types: one row each ---------------------------------------------------------

    def wlan(self, b: dict) -> list[str]:
        ssid = str(b.get("ssid"))
        profiles = [p for p in self.d.nac.get("auth-profiles", {}).values() if ssid in (p.get("networks") or [])]
        nac = ", ".join(f"{p.get('name')} ({enum(p.get('auth-type'))})" for p in profiles)
        servers = [s for s in (b.get("primary-auth-server"), b.get("auth-server-group")) if s and s != "sys_central_nac"]
        roaming = ", ".join(f"802.11{x}" for x in "rkv" if b.get(f"dot11{x}") is True)
        vlan = b.get("vlan-id-range") or b.get("vlan-name")
        return [str(get_path(b, "essid.name") or ssid), enum(b.get("opmode")), text(vlan), enum(b.get("rf-band")),
                enum(b.get("forward-mode")), nac or ("Yes" if b.get("cloud-auth") else "No"), text(servers),
                roaming or NONE, text(b.get("client-isolation")), text(b.get("hide-ssid")), text(b.get("enable"))]

    def auth_profile(self, b: dict) -> list[str]:
        kind = b.get("auth-type")
        store = "Named MPSK" if kind == "MPSK" else ("External" if b.get("identity-stores") else NONE)
        return [str(b.get("name")), enum(kind), text(b.get("networks")), store,
                enum(get_path(b, "mpsk.password-policy"))]

    def alias(self, b: dict) -> list[str]:
        name = str(b.get("name"))
        value = registry.alias_value(b) or next(_scalars(b.get("default-value")), None)
        sites = [f"{site}: {values[name]}" for site, values in sorted(self.d.alias_values.items()) if name in values]
        return [name, enum(b.get("type")), text(value), text(sites)]

    def net_group(self, b: dict) -> list[str]:
        items = sorted(b.get("items") or [], key=lambda i: i.get("index", 0))
        values = [str(v) for i in items for k, v in i.items() if k not in ("type", "index") for v in _scalars(v)]
        return [str(b.get("name")), "Network destination", text(values), NONE]

    # Policies: a rule table each -------------------------------------------------------------

    def policies(self, level: str) -> list[str]:
        out = []
        bodies = sorted(self.objects("policies"), key=lambda b: (self.order.get(b.get("name")) is None,
                                                                 self.order.get(b.get("name")) or 0))
        for b in bodies:
            name = str(b.get("name"))
            position = self.order.get(name)
            where = f"Position {position} in the policy order. " if position is not None else ""
            out += ["", f"{level} {name}", "", where + self.assigned_line("policies", name), ""]
            rules = sorted(get_path(b, "security-policy.policy-rule") or [], key=lambda r: r.get("position", 0))
            out += table(["#", "Source", "Destination", "Service", "Action"], [
                [text(r.get("position")), endpoint(get_path(r, "condition.source")),
                 endpoint(get_path(r, "condition.destination")), service(r.get("condition") or {}),
                 enum(get_path(r, "action.type"))] for r in rules])
        return out

    def authz_policies(self, level: str) -> list[str]:
        out = []
        for b in sorted(self.objects("authz-policies"), key=lambda b: b.get("position", 0)):
            out += ["", f"{level} {b.get('name')}", "", f"Position {text(b.get('position'))}.", ""]
            rules = sorted(b.get("rule") or [], key=lambda r: r.get("position", 0))
            out += table(["#", "Rule", "Conditions", "Role"], [
                [text(r.get("position")), text(r.get("rule-name")), conditions(r.get("conditions") or {}),
                 enforcement(r.get("enf-profile") or [])] for r in rules])
        return out


def _scalars(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _scalars(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _scalars(v)
    elif obj not in (None, "") and not isinstance(obj, bool):
        yield obj


def endpoint(e: dict | None) -> str:
    """A rule's source or destination: Any, or its kind and values (Role CORP-USER)."""
    if not e:
        return NONE
    kind = enum(e.get("type"))
    values = [str(v) for k, v in e.items() if k != "type" for v in _scalars(v)]
    return f"{kind} {', '.join(values)}" if values else kind


def service(condition: dict) -> str:
    kind = enum(condition.get("rule-type"))
    values = [str(v) for k, v in condition.items() if k not in ("rule-type", "source", "destination")
              for v in _scalars(v)]
    return f"{kind} {', '.join(values)}" if values else kind


def attribute(attr: str) -> str:
    """Authorization:<store ID>:GroupIds as Group IDs: the store's ID is left out."""
    part = attr.split(":")[-1]
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", part).replace("Ids", "IDs")


def conditions(c: dict) -> str:
    groups = []
    for cond in sorted(c.get("condition") or [], key=lambda x: x.get("position", 0)):
        parts = [f"{attribute(str(g.get('attr', '')))} {enum(g.get('operator')).lower()} "
                 f"{text(g.get('values') if 'values' in g else g.get('value'))}"
                 for g in sorted(cond.get("condition-group") or [], key=lambda x: x.get("position", 0))]
        groups.append(f" {str(cond.get('combinator-operator') or 'AND').lower()} ".join(parts))
    return f" {str(c.get('combinator-operator') or 'AND').lower()} ".join(groups) or NONE


def enforcement(profiles: list[dict]) -> str:
    roles = [a.get("value") for p in profiles for a in get_path(p, "radius-profile.defined-attr") or []
             if a.get("attr-name") == "ATTR_ARUBA_ROLE"]
    return text(roles) if roles else text([p.get("profile-name") for p in profiles])


# Settings profiles: a table of key settings each ------------------------------------------

def settings(rows: list[tuple[str, str]]) -> list[str]:
    """A table of the settings that have a value."""
    return table(["Setting", "Value"], [[k, v] for k, v in rows if v != NONE])


def radio(a: Appendix, b: dict) -> list[str]:
    rows = []
    for r in b.get("radio") or []:
        arm = r.get("arm-control") or {}
        found = next((v for k, v in arm.items() if k.startswith("channels-for")), None)
        width = [str(arm.get(f"{m}-channel-bandwidth") or "").removeprefix("BW_").removesuffix("MHZ") for m in ("min", "max")]
        rows.append([enum(r.get("profile")), text(r.get("enable")), enum(r.get("mode")), span(*width, "MHz"),
                     span(arm.get("min-tx-power"), arm.get("max-tx-power"), "dBm"), channels(found)])
    return table(["Band", "Enabled", "Mode", "Channel width", "Transmit power", "Channels"], rows)


def ap_system(a: Appendix, b: dict) -> list[str]:
    return settings([
        ("Country code", auto(b.get("country-code"))), ("IP mode", enum(b.get("ip-mode"))),
        ("Time zone", auto(get_path(b, "timezone.name"))), ("LED", onoff(b.get("led"))), ("LACP", enum(b.get("lacp"))),
        ("USB port", NONE if b.get("usb-port-disable") is None else ("Off" if b["usb-port-disable"] else "On")),
    ])


def ap_uplink(a: Appendix, b: dict) -> list[str]:
    rows = [("AP mode", enum(b.get("ap-mode")))]
    for key in sorted(k for k in b if k.startswith("ethernet-")):
        e, port = b[key], f"Ethernet {key.removeprefix('ethernet-')}"
        mgmt = "Native VLAN" if e.get("mgmt-vlan-opt") == "NATIVE_VLAN" else text(e.get("mgmt-vlan-id"))
        allowed = "All" if e.get("trunk-vlan-all") else text(e.get("trunk-vlans") or e.get("allowed-vlans"))
        dot1x = e.get("uplink-dot1xsupp") or {}
        rows += [(f"{port}: ports", enum(e.get("port"))), (f"{port}: mode", enum(e.get("interface-mode"))),
                 (f"{port}: native VLAN", text(e.get("native-vlan"))), (f"{port}: allowed VLANs", allowed),
                 (f"{port}: management VLAN", mgmt), (f"{port}: 802.1X", enum(dot1x.get("eapol-method"))),
                 (f"{port}: 802.1X validates the server certificate", text(dot1x.get("validate-server-cert")))]
    return settings(rows)


def ntp(a: Appendix, b: dict) -> list[str]:
    return settings([("Servers", text([s.get("address") for s in b.get("servers") or []])), ("VRF", text(b.get("vrf")))])


def system_admin(a: Appendix, b: dict) -> list[str]:
    path = "authentication.authentication-group[].access-group[]."
    servers = [*values_at(b, path + "primary-mgmt-auth-server"), *values_at(b, path + "backup-mgmt-auth-server")]
    timeout = b.get("login-session-timeout")
    return settings([
        ("SSH", onoff(get_path(b, "ssh-server-global-configs.enable"))), ("Console", onoff(get_path(b, "console.enable"))),
        ("Web server", onoff(get_path(b, "webservers.enable"))),
        ("Password login", onoff(b.get("mgmt-auth-uname-pwd"))), ("Public key login", onoff(b.get("mgmt-auth-public-key"))),
        ("Login session timeout", NONE if timeout is None else f"{timeout} min"),
        ("Management auth servers", text(servers)),
    ])


def arc(a: Appendix, b: dict) -> list[str]:
    return settings([
        ("Deep packet inspection", onoff(get_path(b, "recog-types.dpi-enable"))),
        ("App performance monitoring", onoff(get_path(b, "recog-types.app-perf-monitoring"))),
        ("Web content classification", onoff(get_path(b, "recog-types.webcc-enable"))),
    ])


def certificate_usage(a: Appendix, b: dict) -> list[str]:
    """What each certificate is used for, then the profile's other settings."""
    uses = sorted(b.get("cert-usage-assignment") or [], key=lambda u: u.get("sequence-id", 0))
    out = table(["Used for", "Certificate", "Type"], [
        [enum(u.get("app-type")), text(u.get("cert-name")), enum(u.get("cert-type"))] for u in uses])
    rest = settings([("RadSec uses the EST certificate", text(b.get("radsec-use-est-certificate")))])
    return out + ([""] if out and rest else []) + rest


def auth_server(a: Appendix, b: dict) -> list[str]:
    alias, port = b.get("auth-server-address-alias"), b.get("radsec-port")
    radsec = f"Yes, port {port}" if b.get("enable-radsec") and port else text(b.get("enable-radsec"))
    return [str(b.get("name")), enum(b.get("type")), text(b.get("auth-server-address") or (f"alias {alias}" if alias else None)),
            radsec, text(b.get("radsec-trusted-cacert-name"))]


def top_level(a: Appendix, b: dict) -> list[str]:
    """A type without a list of key settings: its top-level settings."""
    return settings([(enum(k.replace("-", "_").upper()), text(v)) for k, v in sorted(b.items())
                     if k != "name" and (isinstance(v, (str, int, float, bool)))])


SETTINGS: dict[str, Callable[[Appendix, dict], list[str]]] = {
    "radios": radio, "ap-system": ap_system, "ap-uplinks": ap_uplink, "ntp": ntp, "local-management": system_admin,
    "arc": arc, "ids": top_level, "certificate-usage": certificate_usage,
}

SUMMARIES: dict[str, tuple[list[str], Callable[[Appendix, dict], list[str]]]] = {
    "wlan-ssids": (["ESSID", "Security", "VLAN", "Bands", "Forward mode", "Central NAC", "Auth servers",
                    "Fast roaming", "Client isolation", "Hidden", "Enabled"], Appendix.wlan),
    "auth-profiles": (["Name", "Type", "WLANs", "Identity store", "Passphrases"], Appendix.auth_profile),
    "roles": (["Role", "VLAN"], lambda a, b: [str(b.get("name")), text(get_path(b, "vlan-parameters.access-vlan"))]),
    "layer2-vlan": (["VLAN", "Name"], lambda a, b: [text(b.get("vlan")), text(b.get("name"))]),
    "named-vlan": (["Name", "VLANs"], lambda a, b: [str(b.get("name")), text(get_path(b, "vlan.vlan-id-ranges"))]),
    "management-users": (["User", "Username", "Role", "Group", "Access"], lambda a, b: [
        str(b.get("name")), text(b.get("username")), enum(b.get("rolename")), text(b.get("user-group")),
        text([enum(k.replace("-", "_").upper()) for k, v in sorted((b.get("interface") or {}).items()) if v is True])]),
    "auth-servers": (["Name", "Type", "Address", "RadSec", "RadSec trusted CA"], auth_server),
    "server-groups": (["Name", "Servers, in order"], lambda a, b: [
        str(b.get("name")), text([s.get("server-name") for s in sorted(b.get("servers") or [], key=lambda s: s.get("position", 0))])]),
    "aliases": (["Name", "Type", "Value", "Site values"], Appendix.alias),
    "net-groups": (["Name", "Type", "Value", "Site values"], Appendix.net_group),
}
NO_SCOPE = {"auth-profiles"}   # NAC objects: always at Global, so no column


def render(d: Desired, env: EnvConfig, today: date) -> str:
    return Appendix(d, env).render(today)
