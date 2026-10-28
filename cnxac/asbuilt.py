"""asbuilt: a folder's config as a Markdown appendix to a design document, regenerated rather
than edited.

It renders offline from the files, which match Central whenever plan shows no changes. Each
type sits under its WebUI location, with the settings the WebUI shows, under its labels, and
values in words rather than API enums. Everything is assigned at Global unless noted. Object
IDs, secrets and Central's generated fields never appear, and nor do named MPSK entries.
Identity stores, Intune extensions (under their own Extensions heading, as in the WebUI) and UEM
onboarding come from nac/identity-management.json, which pull writes.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Any

from . import ids as identities
from . import registry
from .config import EnvConfig
from .desired import Desired, Issue
from .registry import get_path, values_at

NONE = "–"
GROUP = object()   # marks a group's heading row in a settings table
CENTRAL_NAC = "sys_central_nac"

# Where each type is in the WebUI menus, in their order: a heading for each level
WEBUI: list[tuple[str, tuple[str, ...]]] = [
    ("wlan-ssids", ("Profiles", "Wireless", "WLAN")),
    ("radios", ("Profiles", "Wireless", "RF")),
    ("ids", ("Profiles", "Wireless", "Wireless IDS/IPS")),
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
    ("identity", ("Central NAC", "Identity Management")),
    ("extensions", ("Extensions",)),
    ("sites", ("Sites",)),
]
SHOWN_WITH = {"net-groups": "aliases"}   # the WebUI lists network groups as aliases of type Network Destination
LEFT_OUT_EMPTY = {"certificate-usage"}    # a profile with nothing to show isn't listed

# Words for API values ---------------------------------------------------------------------

PREFIXES = ("FORWARD_MODE_", "ACTION_", "ADDRESS_", "RULE_", "BW_", "ANT_", "POLICY_TYPE_", "ALIAS_", "SPEED_",
            "OP_", "ATTR_")
CASED = {"IPV4": "IPv4", "IPV6": "IPv6", "GHZ": "GHz", "MHZ": "MHz"}
ACRONYMS = {"AAA", "AES", "AP", "ARP", "CCM", "DFS", "DHCP", "DNS", "DPI", "EAP", "ESSID", "FQDN", "GCM", "HTTP", "HTTPS", "ID",
            "IP", "LACP", "LED", "MAC", "MPSK", "NTP", "OWE", "PSK", "RADIUS", "SAE", "SSH", "SSID", "STP", "TACACS", "TLS",
            "USB", "VLAN", "VRF", "WPA2", "WPA3"}


def enum(value: Any) -> str:
    """An API enum in words: IPV4_ONLY as IPv4 only, ALIAS_DOMAIN_NAME as Domain name."""
    if value is None or value == "":
        return NONE
    s = str(value)
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


def named(table: dict[str, str]) -> Callable[[Any], str]:
    """A value's WebUI words from a table, else the enum in words."""
    return lambda value: table.get(str(value), enum(value)) if value not in (None, "") else NONE


def number(value: Any) -> str:
    """15.0 as 15; anything else as it is."""
    if value is None or value == "":
        return NONE
    s = str(value)
    return s[:-2] if re.fullmatch(r"-?\d+\.0", s) else s


def text(value: Any) -> str:
    """A setting as it reads: Enabled or Disabled, a list joined with commas, names as they are."""
    if value is None or value == "" or value == []:
        return NONE
    if isinstance(value, bool):
        return "Enabled" if value else "Disabled"
    if isinstance(value, list):
        return ", ".join(text(v) for v in value)
    return number(value)


def unit(value: Any, word: str) -> str:
    return NONE if value in (None, "") else f"{number(value)} {word}"


def span(low: Any, high: Any, word: str) -> str:
    """A minimum and maximum: 9 dBm - 15 dBm, or one value when they're the same."""
    low, high = number(low), number(high)
    if low == NONE and high == NONE:
        return NONE
    return f"{low} {word}" if low == high else f"{low} {word} - {high} {word}"


def rates(values: list | None) -> str:
    """Data rates in Mbps, slowest first: RATE_12MB, RATE_24MB as 12, 24 Mbps."""
    nums = []
    for v in values or []:
        m = re.fullmatch(r"RATE_(\d+)(?:_|DOT)?(\d*)MB", str(v))
        nums.append(f"{m[1]}.{m[2]}" if m and m[2] else m[1] if m else str(v))
    nums.sort(key=lambda n: (0, float(n)) if re.fullmatch(r"[\d.]+", n) else (1, 0))
    return f"{', '.join(nums)} Mbps" if nums else NONE


def channels(values: list | None, step: int) -> str:
    """Channels with unbroken runs as ranges: 36-64,100-140,149-165. A run is three or more
    channels each step apart."""
    nums = sorted(int(m[1]) for v in values or [] if (m := re.fullmatch(r"(?:CHAN_)?(\d+)(?:_6GHZ)?", str(v))))
    runs: list[list[int]] = []
    for n in nums:
        if runs and n - runs[-1][-1] == step:
            runs[-1].append(n)
        else:
            runs.append([n])
    parts = [f"{r[0]}-{r[-1]}" if len(r) > 2 else ",".join(map(str, r)) for r in runs]
    return ",".join(parts) or NONE


def cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """A Markdown table, leaving out columns empty in every row (except the first); none
    without rows."""
    if not rows:
        return []
    keep = [i for i in range(len(headers)) if i == 0 or any(r[i] not in (NONE, "") for r in rows)]
    lines = ["| " + " | ".join(headers[i] for i in keep) + " |", "|" + "---|" * len(keep)]
    lines += ["| " + " | ".join(cell(r[i]) for i in keep) + " |" for r in rows]
    return lines


def group(name: str) -> tuple[str, object]:
    return (f"**{name}**", GROUP)


def settings(rows: list[tuple[str, Any]]) -> list[str]:
    """A Setting/Value table of the settings that have a value, under bold group rows. A group
    with no setting left is left out."""
    kept = [(k, v) for k, v in rows if v is GROUP or v != NONE]
    kept = [(k, v) for i, (k, v) in enumerate(kept)
            if v is not GROUP or (i + 1 < len(kept) and kept[i + 1][1] is not GROUP)]
    if all(v is GROUP for _, v in kept):
        return []
    return table(["Setting", "Value"], [[k, "" if v is GROUP else v] for k, v in kept])


# WebUI words ----------------------------------------------------------------------------

KEY_MANAGEMENT = named({
    "WPA3_ENTERPRISE_CCM_128": "WPA3-Enterprise (CCM 128)", "WPA3_ENTERPRISE_GCM_256": "WPA3-Enterprise (GCM 256)",
    "WPA2_ENTERPRISE": "WPA2-Enterprise", "WPA3_SAE": "WPA3-Personal", "WPA2_PERSONAL": "WPA2-Personal",
    "WPA2_MPSK_AES": "MPSK AES", "ENHANCED_OPEN": "Enhanced Open", "OPEN": "Open"})
BROADCAST_FILTER = named({"BCAST_FILTER_ARP": "ARP", "BCAST_FILTER_ALL": "All",
                          "BCAST_FILTER_ARP_ONLY": "Unicast ARP Only", "BCAST_FILTER_NONE": "Disabled"})
LAN_PORTS = named({"ETH_0": "Ethernet 0/0", "ETH_1": "Ethernet 0/1", "ETH0_ETH1": "Ethernet 0/0 + Ethernet 0/1 (Dual)"})
IP_MODE = named({"IPV4_ONLY": "IP Version 4", "IPV4_PREFER": "IP Version 4 & IP Version 6 (Dual Stack)",
                 "IPV6_ONLY": "IP Version 6"})
USER_ROLE = named({"ROOT": "Admin", "READ_ONLY": "Read only", "GUEST_PROVISIONING": "Guest"})
SERVER_MODE = named({"AUTH_ONLY": "RADIUS", "AUTH_AND_COA": "RADIUS with CoA", "COA_ONLY": "CoA only"})
AUTH_TYPE = named({"EAP": "EAP-TLS", "MPSK": "MPSK", "CAPTIVE_PORTAL": "Captive Portal", "MAB": "MAC Authentication"})
PSK_FORMAT = named({"WORDS": "Passphrase", "ALPHANUMERIC": "Random Password"})
UEM_TYPE = named({"MSINTUNE_UEM": "Microsoft Intune"})
STORE_TYPE = named({"MICROSOFT": "Microsoft Entra ID"})
POLICY_TYPE = named({"USER": "User", "CUSTOM": "Custom", "DEVICE": "Device", "VISITOR": "Visitor", "DPP": "DPP",
                     "NAMED_MPSK": "Named MPSK"})
OPERATOR = {"OP_EQUALS": "is equal to", "OP_CONTAINS_ELEM": "contains", "OP_CONTAINS_ANY": "contains any"}
AUTHENTICATION_TYPE = {"EAP": "EAP-TLS", "Captive-Portal": "Captive Portal"}
ACTION = {"Accept": "Allow", "Reject": "Deny"}
PROTOCOLS = {"IP_TCP": "TCP", "IP_UDP": "UDP"}
PROTOCOL_NUMBERS = {"IP_ICMP": 1, "IP_IGMP": 2, "IP_IN_IP": 4, "IP_RSVP": 46, "IP_GRE": 47, "IP_ESP": 50, "IP_AUTH": 51,
                    "IPV6_ICMP": 58, "IP_PIM": 103, "IP_L2TP": 115}
IDS_LEVELS = {"OFF", "CUSTOM", "LOW", "MEDIUM", "HIGH"}

# RF bands, as the radio profile names them; second radios aren't shown
BANDS = (("RADIO_2DOT4G", "2.4 GHz", 1), ("RADIO_5G", "5 GHz", 4), ("RADIO_6G", "6 GHz", 4))

# Certificate usage entries shown, by (application, certificate type)
CERT_USES = (
    (("RADIUS", "CA_CERT"), "Authentication Survivability Client CA"),
    (("RADIUS", "SERVER_CERT"), "Authentication Survivability Server Certificate"),
    (("RADSEC", "CA_CERT"), "RadSec Server CA"),
    (("RADSEC", "CLIENT_CERT"), "RadSec Client Certificate"),
    (("AP1X", "CA_CERT"), "AP 802.1X Server CA (Uplink)"),
    (("AP1X", "CLIENT_CERT"), "AP 802.1X Client Certificate (Uplink)"),
)
DEFAULT_CERTS = {"default", "aruba_default"}

# Alias types under their WebUI names
ALIAS_TYPES = {
    "ALIAS_AP_EFFECTIVE_GAIN": "AP Effective Gain", "ALIAS_AP_INSTALLATION_TYPE": "AP Installation Type",
    "ALIAS_AP_HEIGHT": "AP Location Altitude", "ALIAS_AP_PORT_PROFILE_SWITCHPORT": "AP Switchport",
    "ALIAS_AUTH_SERVER_ADDRESS": "Auth Server Address", "ALIAS_SECRET_VALUE": "Auth Server Shared Secret",
    "ALIAS_DNS_RESOLVER": "DNS Server List", "ALIAS_DOMAIN_NAME": "Domain Name", "ALIAS_EAP_IDENTITY": "EAP Identity",
    "ALIAS_EAP_IDENTITY_PASSWORD": "EAP Identity password", "ALIAS_ECHO_SOURCE_ADDRESS": "Echo Source IP Address",
    "ALIAS_ESSID": "ESSID", "ALIAS_HOST": "Host", "ALIAS_GW_PORT_PROFILE_SWITCHPORT": "Gateway Switchport",
    "ALIAS_IDENTITY_PROFILE": "Identity Profile Control", "ALIAS_IP_ADDRESS": "IP Address",
    "ALIAS_IPV4_ADDRESS": "IPv4 Address", "ALIAS_IPV4_ADDRESSES_WITH_VRF": "IPv4 Helper Addresses",
    "ALIAS_IPV4_SYSTEM_VLAN": "IPv4 System VLAN", "ALIAS_IPV6_ADDRESS": "IPv6 Address",
    "ALIAS_IPV6_ADDRESSES": "IPv6 Helper Addresses", "ALIAS_IPV6_SYSTEM_VLAN": "IPv6 System VLAN",
    "ALIAS_LOCATION": "Location", "ALIAS_MPSK_LOCAL_PASSPHRASE": "MPSK-Local Passphrase", "ALIAS_NETWORK": "Network",
    "ALIAS_NETWORK_DESTINATION": "Network Destination", "ALIAS_NTP_SERVER_LIST": "NTP Server List",
    "ALIAS_NEXT_HOP": "Nexthop IPv4 Address", "ALIAS_NEXT_HOP_V6": "Nexthop IPv6 Address",
    "ALIAS_SW_PORT_PROFILE_SWITCHPORT": "Switchport", "ALIAS_VLAN": "VLAN", "ALIAS_VLAN_ACTIVE_GATEWAY": "VLAN Active Gateway",
    "ALIAS_VLAN_DESCRIPTION": "VLAN Description", "ALIAS_IPV4_ADDRESS_TYPE": "VLAN IPv4 Address",
    "ALIAS_IPV6_ADDRESS_TYPE": "VLAN IPv6 Address", "ALIAS_VLAN_VOICE_ENABLE": "VLAN Voice Enable",
    "ALIAS_VRRP_PRIORITY": "VRRP Priority", "ALIAS_FEATURE_CONTROL": "WLAN Enable", "ALIAS_WPA_PASSPHRASE": "WPA Passphrase",
}
SECRET_ALIASES = {"ALIAS_SECRET_VALUE", "ALIAS_EAP_IDENTITY_PASSWORD", "ALIAS_MPSK_LOCAL_PASSPHRASE",
                  "ALIAS_WPA_PASSPHRASE", "ALIAS_ADMIN_PASSWORD", "ALIAS_NTP_AUTH_LIST"}
REDACTED = "redacted"
# Alias values nested in lists: only these fields of them are shown
ALIAS_LISTS = {"dns-resolver-value": "resolver[].name-server[].ip", "ntp-server-list-value": "servers[].address",
               "ipv4-relay-value": "server[].ip-vrf", "ipv6-address-type-value": "ipv6-addresses[].address"}


def security_level(opmode: Any) -> str:
    s = str(opmode or "")
    if not s:
        return NONE
    if s in ("OPEN", "ENHANCED_OPEN"):
        return "Open"
    return "Enterprise" if "ENTERPRISE" in s or "DOT1X" in s or s == "DYNAMIC_WEP" else "Personal"


# The appendix -----------------------------------------------------------------------------

class Appendix:
    def __init__(self, d: Desired, env: EnvConfig):
        self.d, self.env = d, env
        self.assigned = d.assigned()
        self.order = {e.get("name"): e.get("position") for e in (d.policy_group or {}).get("policy-group-list", [])}
        identity = d.identity or {}
        self.stores = {str(s.get("id")): s for s in identity.get("identity-stores") or [] if s.get("id")}
        self.extensions = identity.get("intune-extensions") or []
        self.uem = {str(e.get("auth-profile-id")): e.get("uem") or [] for e in identity.get("uem-onboarding") or []}

    def objects(self, type_path: str) -> list[dict]:
        """The type's objects as files hold them, without secrets or generated fields, by name."""
        t = registry.BY_PATH[type_path]
        store = self.d.nac if t.nac else self.d.profiles
        bodies = [registry.strip(t, b)[0] for b in store.get(type_path, {}).values()]
        bodies = [b for b in bodies if not self.env.ignored(str(b.get("name") or b.get(t.id_field) or ""))]
        return sorted(bodies, key=lambda b: str(b.get("name") or b.get(t.id_field) or "").casefold())

    def name(self, type_path: str, body: dict) -> str:
        t = registry.BY_PATH[type_path]
        return str(body.get("name") if t.nac else body.get(t.id_field) or body.get("name"))

    def scopes(self, type_path: str, name: str) -> str:
        """Where the object is assigned: Global and the sites, or Not assigned."""
        if registry.BY_PATH[type_path].nac:
            return "Global"
        found = {(kind, scope) for kind, scope, t, n in self.assigned if (t, n) == (type_path, name)}
        sites = sorted(s for kind, s in found if kind == "site")
        out = (["Global"] if ("global", "") in found else []) + sites
        return ", ".join(out) if out else "Not assigned"

    def assigned_line(self, type_path: str, name: str) -> str:
        """Where the object is assigned, unless that's Global alone."""
        where = self.scopes(type_path, name)
        return "" if where == "Global" else "Not assigned." if where == "Not assigned" else f"Assigned: {where}."

    def store_name(self, sid: str) -> str:
        return str(self.stores.get(sid, {}).get("name") or "unknown store")

    def render(self, today: date) -> str:
        lines = [f"# As-built configuration: {self.env.name}", "",
                 f"Generated by cnx-as-code from the config folder for {self.env.name} on {today.isoformat()}. "
                 "It lists the key settings of the AP and Central NAC configuration the tool manages, under "
                 "their WebUI locations. Everything is assigned at Global unless noted. Regenerate it with "
                 "`cnxac asbuilt` rather than editing it."]
        above: tuple[str, ...] = ()   # the menu path headed last
        for t, path in WEBUI:
            body = self.section(t, "#" * (len(path) + 2))
            if not body:
                continue
            shared = next((i for i, (a, b) in enumerate(zip(above, path)) if a != b), min(len(above), len(path)))
            lines += [line for i in range(shared, len(path)) for line in ("", f"{'#' * (i + 2)} {path[i]}")]
            above = path
            lines += body
        return "\n".join(lines) + "\n"

    def section(self, type_path: str, level: str) -> list[str]:
        if type_path in SPECIAL:
            return SPECIAL[type_path](self, level)
        if type_path in SETTINGS:
            out = []
            for body in self.objects(type_path):
                shown = SETTINGS[type_path](self, body)
                if not shown and type_path in LEFT_OUT_EMPTY:
                    continue
                name = self.name(type_path, body)
                line = self.assigned_line(type_path, name)
                out += ["", f"{level} {name}", *(["", line] if line else []), *(["", *shown] if shown else [])]
            return out
        headers = SUMMARIES[type_path][0]
        rows = [(SUMMARIES[t][1](self, b), self.scopes(t, self.name(t, b)))
                for t in self.with_others(type_path) for b in self.objects(t)]
        rows.sort(key=lambda r: r[0][0].casefold())
        if not rows:
            return []
        if any(where != "Global" for _, where in rows):
            return ["", *table(headers + ["Assigned"], [r + [where] for r, where in rows])]
        return ["", *table(headers, [r for r, _ in rows])]

    def with_others(self, type_path: str) -> list[str]:
        """The type, and those the WebUI lists in its table."""
        return [type_path, *(t for t, shown in SHOWN_WITH.items() if shown == type_path)]

    # Sections with a layout of their own ----------------------------------------------------

    def policies(self, level: str) -> list[str]:
        out = []
        bodies = sorted(self.objects("policies"), key=lambda b: (self.order.get(b.get("name")) is None,
                                                                 self.order.get(b.get("name")) or 0))
        for b in bodies:
            name = str(b.get("name"))
            position = self.order.get(name)
            line = " ".join(s for s in (f"Position {position} in the policy order." if position is not None else "",
                                        self.assigned_line("policies", name)) if s)
            out += ["", f"{level} {name}", *(["", line] if line else []), ""]
            rules = sorted(get_path(b, "security-policy.policy-rule") or [], key=lambda r: r.get("position", 0))
            out += table(["Source", "Destination", "Service/Application", "Action"], [
                [endpoint(get_path(r, "condition.source")), endpoint(get_path(r, "condition.destination")),
                 service(r.get("condition") or {}), enum(get_path(r, "action.type"))] for r in rules])
        return out

    def authz_policies(self, level: str) -> list[str]:
        bodies = sorted(self.objects("authz-policies"), key=lambda b: b.get("position", 0))
        if not bodies:
            return []
        out = ["", *table(["#", "Name", "Rules", "Identity Store", "Policy Type", "Status"], [
            [text(b.get("position")), str(b.get("name")), str(len(b.get("rule") or [])),
             text([self.store_name(str(s)) for s in b.get("identity-stores") or []]), POLICY_TYPE(b.get("policy-type")),
             "Inactive" if b.get("enable") is False else "Active"] for b in bodies])]
        for b in bodies:
            out += ["", f"{level} {text(b.get('position'))}. {b.get('name')}", ""]
            if b.get("description"):
                out += [str(b["description"]), ""]
            if pre := self.conditions(b.get("conditions") or {}):
                out += [f"Pre-conditions: {pre}.", ""]
            rules = sorted(b.get("rule") or [], key=lambda r: r.get("position", 0))
            out += table(["#", "Name", "Description", "Conditions", "Action", "Role", "Attributes"], [
                [text(r.get("position")), text(r.get("rule-name")), text(r.get("description")),
                 self.conditions(r.get("conditions") or {}) or NONE, *enforcement(r.get("enf-profile") or [])]
                for r in rules]) or ["No rules."]
        return out

    def conditions(self, c: dict) -> str:
        """Conditions in words: each condition's groups joined by its operator, in brackets when
        there are several, and the conditions joined by the outer operator."""
        parts = []
        for cond in sorted(c.get("condition") or [], key=lambda x: x.get("position", 0)):
            groups = [self.condition(g) for g in sorted(cond.get("condition-group") or [], key=lambda x: x.get("position", 0))]
            joined = f" {combinator(cond)} ".join(groups)
            parts.append(f"({joined})" if len(groups) > 1 else joined)
        return f" {combinator(c)} ".join(p for p in parts if p)

    def condition(self, g: dict) -> str:
        attr = str(g.get("attr") or "")
        values = g.get("values") if "values" in g else [g.get("value")]
        values = [str(v) for v in values or [] if v not in (None, "")]
        if attr == "Authentication:Type":
            label, values = "Authentication Type", [AUTHENTICATION_TYPE.get(v, v) for v in values]
        elif attr.startswith("Authorization:") and attr.endswith(":GroupIds"):
            sid = attr.split(":")[1]
            label = f"User Group ({self.store_name(sid)})" if sid in self.stores else "User Group"
            values = [self.env.group_names.get(v, v) for v in values]
        elif attr == "Endpoint:Tags":
            label = "Client Tags"
        else:
            label = attribute(attr)
        operator = g.get("operator")
        words = OPERATOR.get(str(operator), enum(operator).lower())
        return f"{label} {words} {', '.join(values) or NONE}"

    def identity(self, level: str) -> list[str]:
        out = []
        stores = sorted((s for s in self.stores.values() if s.get("id-store-type") == "MICROSOFT"),
                        key=lambda s: str(s.get("name")).casefold())
        if stores:
            out += ["", f"{level} Identity Stores", "", *table(["Name", "Provider", "Tenant ID", "Client ID"], [
                [str(s.get("name")), STORE_TYPE(s.get("id-store-type")), text(get_path(s, "microsoft.tenant-id")),
                 text(get_path(s, "microsoft.client-id"))] for s in stores])]
        return out

    def extensions_table(self, level: str) -> list[str]:
        if not self.extensions:
            return []
        return ["", *table(["Name", "Type", "URL", "Client ID", "Token Server URL"], [
            [str(e.get("name")), UEM_TYPE(e.get("type")), text(e.get("base-url")),
             text(get_path(e, "oauth2-cc.client-id")), text(get_path(e, "oauth2-cc.token-server-url"))]
            for e in sorted(self.extensions, key=lambda e: str(e.get("name")).casefold())])]

    def sites(self, level: str) -> list[str]:
        if not self.d.sites:
            return []
        return ["", *table(["Name", "Address", "City", "State", "Country", "Postal Code", "Time Zone"], [
            [name, text(b.get("address")), text(b.get("city")), text(b.get("state")), text(b.get("country")),
             text(b.get("zipcode")), text(get_path(b, "timezone.timezoneId"))]
            for name, b in sorted((self.d.sites or {}).items())])]

    # Summary rows ----------------------------------------------------------------------------

    def alias(self, b: dict) -> list[str]:
        name, kind = str(b.get("name")), b.get("type")
        sites = [f"{site}: {values[name]}" for site, values in sorted(self.d.alias_values.items()) if name in values]
        return [name, ALIAS_TYPES.get(str(kind), enum(kind)), REDACTED if kind in SECRET_ALIASES else alias_value(b),
                text(sites)]

    def net_group(self, b: dict) -> list[str]:
        items = sorted(b.get("items") or [], key=lambda i: i.get("index", 0))
        values = [str(v) for i in items for k, v in i.items() if k not in ("type", "index") for v in _scalars(v)]
        return [str(b.get("name")), "Network Destination", text(values), NONE]

    def auth_server(self, b: dict) -> list[str]:
        kind = str(b.get("type") or "")
        radius, tacacs = kind == "RADIUS", kind == "TACACS"
        radsec = radius and b.get("enable-radsec") is True
        alias = b.get("auth-server-address-alias")
        address = b.get("auth-server-address")
        address = (", ".join(map(str, _scalars(address))) if isinstance(address, (dict, list)) else address) or (
            f"alias {alias}" if alias else None)
        certificate = ("EST Profile" if b.get("radsec-est-cert-profile") else
                       "Custom" if b.get("radsec-client-cert") else "Internal")
        return [str(b.get("name")), enum(kind) if not (radius or tacacs) else kind,
                text(bool(b.get("enable-radsec"))) if radius else "",
                SERVER_MODE(b.get("radius-server-mode") or "AUTH_ONLY") if radius else "", text(address),
                text(bool(b.get("session-authorization"))) if tacacs else "",
                certificate if radsec else "", text(b.get("radsec-trusted-cacert-name")) if radsec else ""]


def _scalars(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _scalars(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _scalars(v)
    elif obj not in (None, "") and not isinstance(obj, bool):
        yield obj


def _leaves(obj: Any, path: str = "") -> list[tuple[str, Any]]:
    """(field name, value) for each value in a mapping, a list of plain values counting as one."""
    if isinstance(obj, dict):
        return [leaf for k, v in obj.items() for leaf in _leaves(v, k)]
    if isinstance(obj, list) and any(isinstance(v, (dict, list)) for v in obj):
        return [leaf for v in obj for leaf in _leaves(v, path)]
    return [] if obj in (None, "", []) else [(path, obj)]


def alias_value(b: dict) -> str:
    """What an alias holds: one value as it is; a list joined with commas; several fields as
    "Field: value"; a nested list (DNS or NTP servers) by its key field only."""
    default = b.get("default-value") or {}
    found = []
    for key, value in default.items():
        if key in ALIAS_LISTS:
            found.append((key, list(values_at(value, ALIAS_LISTS[key]))))
        else:
            found += _leaves(value, key)
    found = [(k, v) for k, v in found if v not in (None, "", [])]
    if len(found) == 1:
        return text(found[0][1])
    return "; ".join(f"{enum(k.replace('-', '_').upper())}: {text(v)}" for k, v in found) or NONE


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


def combinator(c: dict) -> str:
    return "or" if str(c.get("combinator-operator") or "").endswith("OR") else "and"


def enforcement(profiles: list[dict]) -> list[str]:
    """A rule's Action, Role and other attributes: Allow or Deny, the Aruba role, then the rest
    as "Name: value"."""
    attrs = [(str(a.get("attr-name")), a.get("value")) for p in profiles for a in get_path(p, "radius-profile.defined-attr") or []]
    custom = [(str(a.get("custom-attr-name")), a.get("value")) for p in profiles
              for a in get_path(p, "radius-profile.custom-attr") or []]
    action = next((v for k, v in attrs if k == "ATTR_POLICY_ACTION"), None)
    roles = [v for k, v in attrs if k == "ATTR_ARUBA_ROLE"]
    rest = [f"{enum(k)}: {text(v)}" for k, v in attrs if k not in ("ATTR_POLICY_ACTION", "ATTR_ARUBA_ROLE")]
    rest += [f"{k}: {text(v)}" for k, v in custom]
    return [ACTION.get(str(action), text(action)), text(roles), text(rest)]


# Settings profiles: a table of settings each ----------------------------------------------

def wlan(a: Appendix, b: dict) -> list[str]:
    opmode = b.get("opmode")
    level = security_level(opmode)
    servers = {b.get("auth-server-group"), b.get("primary-auth-server")}
    server = ("Central NAC" if b.get("cloud-auth") is True or CENTRAL_NAC in servers else
              b.get("auth-server-group") or b.get("primary-auth-server"))
    server = None if level == "Open" else server
    bands = re.findall(r"(24|5|6)GHZ", str(b.get("rf-band") or ""))
    return settings([
        group("General"),
        ("ESSID Name", text(get_path(b, "essid.name"))),
        ("Bands", ", ".join(f"{'2.4' if n == '24' else n} GHz" for n in bands) or enum(b.get("rf-band"))),
        ("Broadcast Filtering", BROADCAST_FILTER(b.get("broadcast-filter-ipv4"))),
        ("2.4 GHz Basic Rates", rates(get_path(b, "g-legacy-rates.basic-rates"))),
        ("2.4 GHz Transmit Rates", rates(get_path(b, "g-legacy-rates.tx-rates"))),
        ("5 GHz Basic Rates", rates(get_path(b, "a-legacy-rates.basic-rates"))),
        ("5 GHz Transmit Rates", rates(get_path(b, "a-legacy-rates.tx-rates"))),
        ("Video DSCP Mapping", text(get_path(b, "wmm-cfg.video-dscp"))),
        ("Voice DSCP Mapping", text(get_path(b, "wmm-cfg.voice-dscp"))),
        ("Advertise Access Point Name", text(b.get("advertise-apname"))),
        group("VLAN"),
        ("Default VLAN", text(b.get("vlan-id-range") or b.get("vlan-name"))),
        group("Security"),
        ("Security Level", level),
        ("Key Management", KEY_MANAGEMENT(opmode)),
        ("Client Isolation", text(b.get("client-isolation"))),
        ("Enforce DHCP", text(b.get("enforce-dhcp"))),
        ("WPA3 Transition", NONE if opmode == "WPA2_MPSK_AES" else text(b.get("wpa3-transition-mode-enable"))),
        group("Authentication"),
        ("Server Group", text(server)),
        ("Personal Wireless Network", text(b.get("pan")) if level == "Personal" else NONE),
        ("Reauthentication Interval", unit(b.get("radius-reauth-interval"), "Minutes") if server else NONE),
        group("Fast Roaming"),
        ("Opportunistic Key Caching (OKC)", text(b.get("opp-key-caching") is True) if level == "Enterprise" else NONE),
        ("802.11r", NONE if level == "Open" else text(b.get("dot11r"))),
        ("802.11k", text(b.get("dot11k"))),
    ])


def radio(a: Appendix, b: dict) -> list[str]:
    """A row per setting and a column per band. A disabled band shows only that."""
    radios = {r.get("profile"): r for r in b.get("radio") or []}
    rows: list[list[str]] = []

    def row(label: str, value: Callable[[dict, dict, int], str], six_only: bool = False) -> None:
        cells = []
        for profile, _, step in BANDS:
            r = radios.get(profile)
            if r is None or (six_only and profile != "RADIO_6G") or (r.get("enable") is False and label != "Enable"):
                cells.append("")
            else:
                cells.append(value(r, r.get("arm-control") or {}, step))
        if any(c not in ("", NONE) for c in cells):
            rows.append([label, *cells])

    width = lambda arm: [str(arm.get(f"{m}-channel-bandwidth") or "").removeprefix("BW_").removesuffix("MHZ") or None
                         for m in ("min", "max")]
    row("Enable", lambda r, arm, step: text(r.get("enable")))
    row("Channel Bandwidth", lambda r, arm, step: span(*width(arm), "MHz"))
    row("Transmit Power", lambda r, arm, step: span(arm.get("min-tx-power"), arm.get("max-tx-power"), "dBm"))
    row("Allowed Channels", lambda r, arm, step: channels(
        next((v for k, v in arm.items() if k.startswith("channels-for")), None), step))
    row("Advertise 802.11d & 802.11h", lambda r, arm, step: text(r.get("ieee802dot11h")))
    row("802.11k", lambda r, arm, step: text(r.get("dot11k")), six_only=True)
    row("Basic Rates", lambda r, arm, step: rates(r.get("basic-rates")), six_only=True)
    row("Transmit Rates", lambda r, arm, step: rates(r.get("tx-rates")), six_only=True)
    row("Advertise Access Point Name", lambda r, arm, step: text(r.get("advertise-ap-name")), six_only=True)
    return table(["Setting", *(name for _, name, _ in BANDS)], rows)


def ids(a: Appendix, b: dict) -> list[str]:
    policies = b.get("detection-protection") or {}

    def level(key: str) -> tuple[str | None, list[str]]:
        values = [str(v) for v in policies.get(key) or []]
        found = next((v for v in values if v in IDS_LEVELS), None)
        return found, [v.lower() for v in values if v not in IDS_LEVELS]

    infra, infra_threats = level("infrastructure-detection")
    client, client_threats = level("client-detection")
    detection = any(lvl not in (None, "OFF") for lvl in (infra, client))
    protection = any(level(k)[0] not in (None, "OFF") for k in ("infrastructure-protection", "client-protection"))
    shown = lambda lvl: enum(lvl or "OFF") if detection else NONE
    threats = lambda lvl, found: text(found) if detection and lvl == "CUSTOM" else NONE
    return settings([
        group("Detection"),
        ("Detection", text(detection)),
        ("Infrastructure Security Level", shown(infra)),
        ("Threats", threats(infra, infra_threats)),
        ("Client Security Level", shown(client)),
        ("Threats", threats(client, client_threats)),
        group("Protection"),
        ("Protection", text(protection)),
    ])


def ap_uplink(a: Appendix, b: dict) -> list[str]:
    ports = sorted(k for k in b if k.startswith("ethernet-"))
    rows: list[tuple[str, Any]] = []
    for key in ports:
        e = b[key]
        dot1x = e.get("uplink-dot1xsupp") or {}
        on = bool(dot1x.get("eapol-method"))
        access = e.get("interface-mode") == "ACCESS"
        mgmt = "Native VLAN" if e.get("mgmt-vlan-opt") == "NATIVE_VLAN" else text(e.get("mgmt-vlan-id"))
        allowed = "All" if e.get("trunk-vlan-all") else text(e.get("trunk-vlan-ranges"))
        rows += [
            *([group(f"Ethernet {key.removeprefix('ethernet-')}")] if len(ports) > 1 else []),
            ("LAN Port(s)", LAN_PORTS(e.get("port"))),
            group("VLAN"),
            ("Port Mode", enum(e.get("interface-mode"))),
            ("Access VLAN", text(e.get("access-vlan")) if access else NONE),
            ("Native VLAN", NONE if access else text(e.get("native-vlan"))),
            ("Allowed VLANs", NONE if access else allowed),
            ("Management VLAN", mgmt),
            group("Authentication"),
            ("IEEE 802.1X Authentication", text(on)),
            ("EAP Method", str(dot1x["eapol-method"]).removeprefix("EAP_") if on else NONE),
            ("Certificate Type", enum(dot1x.get("eap-tls-cert-type")) if on else NONE),
        ]
    return settings(rows)


def ap_system(a: Appendix, b: dict) -> list[str]:
    from_site = lambda v: "Automatic (from site)" if v == "AUTO" else text(v)
    usb = b.get("usb-port-disable")
    return settings([
        group("General Settings"),
        ("Country Code", from_site(b.get("country-code"))),
        ("Timezone", from_site(get_path(b, "timezone.name"))),
        ("LED Display", text(b.get("led"))),
        ("USB Port", NONE if usb is None else text(not usb)),
        ("Advertise AP Health IE", text(b.get("advertise-ap-health-ie"))),
        group("IP Version 6"),
        ("AP Management Mode", IP_MODE(b.get("ip-mode"))),
        group("Security"),
        ("PMK Cache Timeout", unit(get_path(b, "aaa.pmk-cache-timeout"), "Hours")),
        ("Authentication Survivability Timeout", unit(get_path(b, "aaa.auth-survivability-cachetime"), "Hours")),
    ])


def ntp(a: Appendix, b: dict) -> list[str]:
    """A row per server, in the order the AP tries them."""
    iburst = lambda s: NONE if s.get("tx-mode") is None else text(s.get("tx-mode") == "IBURST")
    return table(["FQDN", "iBurst Mode", "Minimum Poll Interval", "Maximum Poll Interval"], [
        [text(s.get("address")), iburst(s), text(s.get("min-poll")), text(s.get("max-poll"))]
        for s in b.get("servers") or []])


def system_admin(a: Appendix, b: dict) -> list[str]:
    return settings([
        group("General"),
        ("Console Access", text(get_path(b, "console.enable"))),
        ("SSH Access", text(get_path(b, "ssh-server-global-configs.enable"))),
        ("Web Access", text(get_path(b, "webservers.enable"))),
        ("Login Session Timeout", unit(b.get("login-session-timeout"), "Minutes")),
    ])


def arc(a: Appendix, b: dict) -> list[str]:
    return settings([
        ("App Performance Monitoring", text(get_path(b, "recog-types.app-perf-monitoring"))),
        ("Enable Application Recognition", text(get_path(b, "recog-types.dpi-enable"))),
        ("Web Content Classification", text(get_path(b, "recog-types.webcc-enable"))),
    ])


def certificate_usage(a: Appendix, b: dict) -> list[str]:
    """The custom certificates the profile assigns; none means the profile isn't shown."""
    uses = {(u.get("app-type"), u.get("cert-type")): u.get("cert-name") for u in b.get("cert-usage-assignment") or []}
    return settings([(label, text(uses.get(use))) for use, label in CERT_USES
                     if uses.get(use) and str(uses[use]) not in DEFAULT_CERTS])


def auth_profile(a: Appendix, b: dict) -> list[str]:
    kind = b.get("auth-type")
    if kind == "MPSK":
        store = "Named MPSK"
    else:
        store = text([a.store_name(str(s)) for s in b.get("identity-stores") or []])
    rows: list[tuple[str, Any]] = [
        ("Name", text(b.get("name"))),
        ("Authentication Type", AUTH_TYPE(kind)),
        ("Network", text(b.get("networks"))),
    ]
    if kind in ("EAP", "MPSK"):
        rows += [("Use for wired connection", text(b.get("wired") is True)), ("Identity Store", store)]
    if kind == "EAP":
        days = get_path(b, "eap.cert-valid-days")
        months = round(int(days) * 12 / 365) if str(days or "").isdigit() else None
        rows += [
            group("EAP-TLS Settings"),
            ("Organization Name", text(b.get("organization-name"))),
            ("Certificate", "Custom Certificate" if get_path(b, "eap.use-custom-server") is True else "Default Certificate"),
            ("EAP-TLS Client Valid Days",
             NONE if months is None else f"{days} days ({months} month{'' if months == 1 else 's'})"),
        ]
        for u in a.uem.get(str(b.get("auth-profile-id")), []):
            if u.get("enable") is False:
                continue
            rows += [group("UEM Onboarding"), ("Name", text(u.get("name"))), ("Type", UEM_TYPE(u.get("uem-type"))),
                     ("Extension", text(u.get("intune-extension"))), ("SCEP URL", text(u.get("scep-url")))]
    if kind == "MPSK":
        rows += [
            group("MPSK Settings"),
            ("Organization Name", text(b.get("organization-name"))),
            ("Pre-Shared Key (PSK) Format", PSK_FORMAT(get_path(b, "mpsk.password-policy"))),
        ]
    return settings(rows)


def net_service(a: Appendix, b: dict) -> list[str]:
    protocol = b.get("protocol")
    if str(protocol) in PROTOCOLS:
        word = PROTOCOLS[str(protocol)]
    elif protocol in (None, ""):
        word = NONE
    elif protocol == "PROTOCOL_ANY":
        word = "Any"
    else:
        word = f"Protocol Number {PROTOCOL_NUMBERS.get(str(protocol), protocol)}"
    ports = NONE
    if str(protocol) in PROTOCOLS:
        r = b.get("port-range") or {}
        if b.get("ports"):
            ports = text(b.get("ports"))
        elif r.get("operator") == "COMPARISON_RANGE":
            ports = f"{text(r.get('min'))}-{text(r.get('max'))}"
        elif r:
            ports = text(r.get("min")) if r.get("operator") in (None, "COMPARISON_EQ") else f"{enum(r['operator'])} {text(r.get('min'))}"
    return [str(b.get("name")), word, ports]


SETTINGS: dict[str, Callable[[Appendix, dict], list[str]]] = {
    "wlan-ssids": wlan, "radios": radio, "ids": ids, "ap-uplinks": ap_uplink, "ap-system": ap_system, "ntp": ntp,
    "local-management": system_admin, "arc": arc, "certificate-usage": certificate_usage, "auth-profiles": auth_profile,
}

SPECIAL: dict[str, Callable[[Appendix, str], list[str]]] = {
    "policies": Appendix.policies, "authz-policies": Appendix.authz_policies, "identity": Appendix.identity,
    "extensions": Appendix.extensions_table,
    "sites": Appendix.sites,
}

SUMMARIES: dict[str, tuple[list[str], Callable[[Appendix, dict], list[str]]]] = {
    "named-vlan": (["Name", "VLANs"], lambda a, b: [str(b.get("name")), text(get_path(b, "vlan.vlan-id-ranges"))]),
    "management-users": (["Name", "Username", "Role"], lambda a, b: [
        str(b.get("name")), text(b.get("username")), USER_ROLE(b.get("rolename"))]),
    "auth-servers": (["Name", "Server Type", "Secure RADIUS", "Auth Server Mode", "IP Address/FQDN",
                      "Session Authorization", "Certificate Type", "Secure RADIUS Trusted CA"], Appendix.auth_server),
    "server-groups": (["Name", "Servers, in order"], lambda a, b: [
        str(b.get("name")), text([s.get("server-name") for s in sorted(b.get("servers") or [], key=lambda s: s.get("position", 0))])]),
    "roles": (["Name", "VLAN ID"], lambda a, b: [str(b.get("name")), text(get_path(b, "vlan-parameters.access-vlan"))]),
    "aliases": (["Name", "Type", "Value", "Site values"], Appendix.alias),
    "net-groups": (["Name", "Type", "Value", "Site values"], Appendix.net_group),
    "net-services": (["Name", "Protocol", "Ports"], net_service),
}


def render(d: Desired, env: EnvConfig, today: date) -> str:
    return Appendix(d, env).render(today)


def notes(d: Desired, env: EnvConfig) -> list[Issue]:
    """What the as-built can't name: identity stores missing from the identity snapshot (or the
    whole snapshot), and Entra groups with no name under group_names in env.yaml."""
    stores, groups = identities.used(d, env)
    snapshot = f"nac/{registry.IDENTITY_FILE}"
    out = []
    if stores and d.identity is None:
        out.append(Issue("warning", snapshot, "not found, so the as-built names no identity store and leaves out "
                                              "Identity Management and UEM onboarding; pull writes it"))
    elif stores:
        known = {str(s.get("id")) for s in d.identity.get("identity-stores") or []}
        out += [Issue("warning", files[0], f"identity store {sid} isn't in {snapshot}, so the as-built can't name it; "
                                           "pull to refresh it")
                for sid, files in stores.items() if sid not in known]
    seen = set()
    for where, gid in groups:
        if str(gid) not in env.group_names and str(gid) not in seen:
            seen.add(str(gid))
            out.append(Issue("warning", where, f"Entra group {gid} has no name under group_names in env.yaml, "
                                               "so the as-built shows its ID"))
    return out
