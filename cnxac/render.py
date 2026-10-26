"""How the commands show their results: in colour on a terminal, plain when piped or with
NO_COLOR set.

Names are printed as Text, never parsed as markup, so a name holding [brackets] or
:colons: prints as it is. Long lines aren't wrapped, so copied lines stay whole.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, TYPE_CHECKING

from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from . import ids as identities, registry
from .desired import Issue
from .files import Staged
from .plan import Action, Plan, count
from .pull import Report, name_key

if TYPE_CHECKING:   # mpsk imports this module
    from . import mpsk

LEVELS = {"error": "bold red", "warning": "yellow"}
VERBS = {"create": ("+", "green"), "update": ("~", "yellow"), "move": ("~", "yellow"), "assign": ("+", "green"),
         "unassign": ("-", "red"), "remove": ("-", "red"), "delete": ("-", "red")}   # in the order totals lists them
ARROW = " → "
DETAIL = " " * 16   # under the action's target


def console(file: IO[str] | None = None, stderr: bool = False) -> Console:
    return Console(file=file, stderr=stderr, markup=False, emoji=False, highlight=False, soft_wrap=True)


OUT = console()
ERR = console(stderr=True)


@contextmanager
def reading(what: str) -> Iterator[None]:
    """A spinner on stderr while Central is read, on a terminal only."""
    if not ERR.is_terminal:
        yield
        return
    with ERR.status(Text(f"Reading {what}…"), spinner="dots"):
        yield


def error(message: str) -> None:
    ERR.print(Text.assemble(("Error: ", "bold red"), message))


def header(con: Console, env_name: str, where: str) -> None:
    con.print(Text.assemble("Environment ", (env_name, "bold"), f" {where}"))


def issues(con: Console, found: list[Issue]) -> None:
    if found:
        con.print()
    for i in found:
        issue(con, i)


def issue(con: Console, i: Issue) -> None:
    con.print(Text.assemble((f"{i.level}:", LEVELS[i.level]), f" {i.where}: {i.message}"))


def check(con: Console, found: list[Issue]) -> None:
    issues(con, found)
    con.print()
    if not found:
        con.print("No problems found.", style="green")
        return
    errors = sum(i.level == "error" for i in found)
    con.print(f"{count(errors, 'error')}, {count(len(found) - errors, 'warning')}.",
              style=LEVELS["error" if errors else "warning"])


def plan(con: Console, result: Plan, title: str = "Changes, in apply order") -> None:
    issues(con, result.issues)
    con.print()
    if result.errors:
        con.print(f"{count(len(result.errors), 'error')}, so no plan. Fix them and run plan again.",
                  style=LEVELS["error"])
        return
    if result.actions:
        con.print(title, style="bold")
        changes(con, result.actions)
        con.print()
    if result.left:
        con.print("Left alone", style="bold")
        for item in result.left:
            con.print(f"  {item}")
        con.print()
    if result.kept:
        con.print("Not unassigned, because something assigned there uses them", style="bold")
        for k in result.kept:
            con.print(f"  {k}")
        con.print()
    if result.local:
        con.print("Local profiles (turn them into library profiles)", style="bold")
        for p in result.local:
            con.print(f"  {p}")
        con.print()
    if result.actions:
        con.print(totals(result.actions))
    else:
        con.print("No changes.", style="green")


def totals(actions: list[Action]) -> Text:
    """The plan's last line: the number of changes, then how many of each kind."""
    kinds = Counter(a.verb for a in actions)
    parts = [Text(f"{kinds[v]} {v}", style=VERBS.get(v, ("~", "yellow"))[1])
             for v in [*VERBS, *sorted(kinds.keys() - VERBS.keys())] if kinds[v]]
    return Text.assemble((count(len(actions), "change"), "bold"), ": ", Text(", ").join(parts), ".")


def confirmation(con: Console, command: str, env_name: str, host: str, n: int) -> None:
    """The line above the apply or destroy prompt, naming where the changes go, since the
    header has scrolled away by then."""
    verb, style, joint = ("Destroy", "bold red", "in") if command == "destroy" else ("Apply", "bold", "to")
    con.print(Text.assemble((verb, style), f" {joint} ", (env_name, "bold"), f" ({host}): {count(n, 'change')}"))


def prompt(word: str) -> Text:
    return Text.assemble("Type ", (word, "bold"), " to go ahead › ")


def declined(con: Console, word: str, answer: str) -> None:
    con.print(f'Nothing done: you typed "{answer}", not {word}.' if answer else "Nothing done.", style="yellow")


def action(a: Action, mark: str = "", mark_style: str = "") -> Text:
    sign, style = VERBS.get(a.verb, ("~", "yellow"))
    return Text.assemble("  ", (mark or sign, mark_style or style), " ", (f"{a.verb:<9}", style), " ", a.target)


def changes(con: Console, actions: list[Action], show_details: bool = True) -> None:
    for a in actions:
        con.print(action(a))
        for line in details(a.details) if show_details else ():
            con.print(line)


def details(lines: list[str], indent: str = DETAIL) -> list[Text]:
    """Diff lines, as diff.lines makes them, in colour: what goes in red, what comes in green,
    with the lines indented under a + or - line taking its colour. Anything else is a heading
    or a note, printed as it is."""
    out: list[Text] = []
    block: tuple[int, str] | None = None   # (depth, style) of the + or - line above
    for line in lines:
        body = line.lstrip(" ")
        depth = len(line) - len(body)
        if block and depth > block[0]:
            out.append(Text(indent + line, style=block[1]))
            continue
        block = None
        if body.startswith(("+ ", "- ")):
            block = (depth, "green" if body[0] == "+" else "red")
            out.append(Text(indent + line, style=block[1]))
            continue
        path, colon, rest = body.partition(": ")
        old, arrow, new = rest.partition(ARROW)
        if colon and arrow:
            out.append(Text.assemble(indent + " " * depth, path, colon, (old, "red"), arrow, (new, "green")))
        else:
            out.append(Text(indent + line))
    return out


def step(con: Console, a: Action, ok: bool, note: str | None = None) -> None:
    """One apply step as it finishes: ✓ done or ✗ failed."""
    line = action(a, "✓" if ok else "✗", "green" if ok else "bold red")
    if note:
        line.append(f"  ({note})", style="dim")
    con.print(line)


def failure(con: Console, message: str) -> None:
    con.print(Text(f"      {message}", style="red"))


# The order a pull lists files in: (mark, style, note) for each status it writes
PULL_STATUSES = {"changed": ("~", "yellow", None), "reformatted": ("~", "yellow", "formatting only"),
                 "new": ("+", "green", "new")}


def pull_changes(con: Console, staged: list[Staged]) -> None:
    """What a pull would write, before it asks: the files whose content Central changes, each
    with its changes, then those it only reformats, then the new ones."""
    con.print()
    shown = sorted((s for s in staged if s.status in PULL_STATUSES), key=lambda s: list(PULL_STATUSES).index(s.status))
    if not shown:
        con.print("No changes to the files.", style="green")
        return
    con.print("Changes to the files, from Central", style="bold")
    for s in shown:
        mark, style, note = PULL_STATUSES[s.status]
        line = Text.assemble("  ", (mark, style), " ", s.rel)
        if note:
            line.append(f"  ({note})", style="dim")
        con.print(line)
        for change in details(s.changes, " " * 6):
            con.print(change)
    con.print()
    kinds = Counter(s.status for s in shown)
    con.print(", ".join(f"{kinds[k]} {k}" for k in PULL_STATUSES if kinds[k]) + ".")


def pull_left(con: Console, report: Report) -> None:
    """What a pull leaves alone that plan or another pull would act on: files whose object
    Central doesn't have, and objects with no file."""
    if report.gone:
        con.print()
        con.print("In the folder but not in Central, so plan would create them "
                  "(if one was deleted in the UI, delete its file)", style="bold")
        for rel in report.gone:
            con.print(Text(f"  {rel}"))
    if any(report.not_held.values()):
        con.print()
        con.print("In Central with no file (pull --add <type> adds them)", style="bold")
        for t in registry.TYPES:
            if report.not_held.get(t.path):
                con.print(Text(f"  {t.path}: {', '.join(sorted(report.not_held[t.path], key=name_key))}"))


def pulled(con: Console, folder: Path, report: Report) -> None:
    con.print()
    if report.nothing_held:
        con.print("Nothing in the folder yet, so nothing to refresh. Run pull --all to seed it, "
                  "or list the profiles to pull in assign.yaml.",
                  style=LEVELS["warning"])
        return
    con.print(f"Pulled into {folder}/")
    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("Path")
    for heading in ("Written", "New", "Changed", "Reformatted", "Unchanged"):
        table.add_column(heading, justify="right")
    for area, counts in report.written.items():
        table.add_row(area, str(sum(counts.values())), number(counts["new"], "green"),
                      number(counts["changed"], "yellow"), number(counts["reformatted"], "yellow"),
                      number(counts["unchanged"]))
    con.print(Padding(table, (0, 0, 0, 2), expand=False))
    for title, items in report.sections():
        con.print()
        con.print(title, style="bold")
        for item in items:
            con.print(f"  {item}")
    summaries = report.summaries()
    if summaries:
        con.print()
    for line in summaries:
        con.print(line, style="dim")


def number(n: int, style: str = "") -> Text:
    return Text(str(n), style=style if n else "dim")


def entries(n: int) -> str:
    return f"{n} {'entry' if n == 1 else 'entries'}"


def mpsk_entry(e: mpsk.Entry, mark: str, style: str, note: str | None = None) -> Text:
    line = Text.assemble("  ", (mark, style), " ", e.name, f"  role {e.role}", "" if e.enabled else ", disabled")
    if note:
        line.append(f"  ({note})", style="dim")
    return line


def mpsk_plan(con: Console, plan: mpsk.Plan) -> None:
    issues(con, plan.issues)
    con.print()
    if plan.errors:
        con.print(f"{count(len(plan.errors), 'error')}, so nothing to add. Fix them and run mpsk add again.",
                  style=LEVELS["error"])
        return
    for wlan, new in plan.add.items():
        con.print(Text.assemble((wlan, "bold"), f" ({plan.where[wlan]})"))
        for e in new:
            con.print(mpsk_entry(e, "+", "green"))
        there = plan.there.get(wlan, [])
        if there:
            left = "left as it is" if len(there) == 1 else "left as they are"
            con.print(f"  {entries(len(there))} already in Central, {left}", style="dim")
            for line in plan.differing(wlan):
                con.print(f"    {line}", style="dim")
        if plan.extra.get(wlan):
            con.print(f"  In Central but not in the file, left as they are: {', '.join(plan.extra[wlan])}",
                      style="dim")
        con.print()
    if plan.count:
        con.print(f"{entries(plan.count)} to add.", style="bold")
    else:
        con.print("Nothing to add.", style="green")


def mpsk_confirmation(con: Console, env_name: str, host: str, n: int) -> None:
    con.print(Text.assemble(("Add", "bold"), " to ", (env_name, "bold"), f" ({host}): {entries(n)}"))


def mpsk_step(con: Console, wlan: str, e: mpsk.Entry, ok: bool | None, note: str | None = None) -> None:
    """One entry as the import finishes: ✓ added, ✗ not as listed, or - left alone."""
    mark, style = {True: ("✓", "green"), False: ("✗", "bold red"), None: ("-", "dim")}[ok]
    con.print(mpsk_entry(e, mark, style, f"{wlan}: {note}" if note else wlan))


def mpsk_pulled(con: Console, folder: Path, pulled: mpsk.Pulled) -> None:
    """After the files are written; the pull's issues come before what it changes."""
    con.print()
    if not pulled.written:
        con.print("The folder holds no MPSK WLAN that Central serves, so nothing was pulled.",
                  style=LEVELS["warning"])
        return
    con.print(f"Pulled into {folder}/, without passphrases")
    for rel, (status, n) in pulled.written.items():
        con.print(Text.assemble(f"  {rel}  {entries(n)}  ", (status, "dim" if status == "unchanged" else "yellow")))
    if pulled.dropped:
        con.print()
        con.print("Not in Central, so no longer in the file", style="bold")
        for rel, names in pulled.dropped.items():
            con.print(f"  {rel}: {', '.join(names)}")


def mpsk_exported(con: Console, out: Path, wlan: str, n: int) -> None:
    con.print()
    con.print(f"Wrote {entries(n)} of {wlan}, with their passphrases, to {out}.")
    con.print("It holds passphrases in clear text: keep it out of the repo, and delete it once it's used.",
              style=LEVELS["warning"])


def markdown(con: Console, text: str) -> None:
    """Markdown as it is, for piping to a file."""
    con.print(Text(text), end="")


def written(con: Console, path: Path, status: str) -> None:
    con.print(Text.assemble(f"Wrote {path} (", (status, "dim" if status == "unchanged" else "yellow"), ")"))


def ids(con: Console, env_name: str, found: identities.Found) -> None:
    con.print()
    con.print("Identity stores", style="bold")
    names = max((len(s.name) for s in found.stores), default=0)
    types = max((len(s.type) for s in found.stores), default=0)
    for s in found.stores:
        line = Text(f"  {s.name:<{names}}  {s.type:<{types}}  {s.id}")
        if s.profiles:
            line.append(f"  ({'auth profile' if len(s.profiles) == 1 else 'auth profiles'} {', '.join(s.profiles)})",
                        style="dim")
        con.print(line)
    if not found.stores:
        con.print("  none", style="dim")
    con.print()
    con.print("Intune extensions", style="bold")
    for name in found.intune:
        con.print(Text(f"  {name}"))
    if not found.intune:
        con.print("  none", style="dim")
    con.print()
    con.print("Certificates", style="bold")
    names = max((len(c.name) for c in found.certificates), default=0)
    types = max((len(c.type) for c in found.certificates), default=0)
    for c in found.certificates:
        expiry = f"valid until {c.expires}" if c.expires else ""
        con.print(Text(f"  {c.name:<{names}}  {c.type:<{types}}  {expiry}".rstrip()))
    if not found.certificates:
        con.print("  none", style="dim")
    con.print()
    con.print("Identity stores the folder uses", style="bold")
    only = found.only_made_in_ui()
    for sid, files in found.uses.items():
        store = found.store(sid)
        folder_use(con, env_name, sid, f"{store.name} ({store.type})" if store else None, files)
        if not store and only:
            con.print(Text(f"      {env_name}'s only identity store made in the UI is {only.name} ({only.type}): "
                           f"{only.id}", style=LEVELS["warning"]))
    if not found.uses:
        con.print("  none", style="dim")
    con.print()
    con.print("Certificates the folder uses", style="bold")
    for name, files in found.cert_uses.items():
        cert = found.certificate(name)
        folder_use(con, env_name, name, cert.type if cert else None, files)
    if not found.cert_uses:
        con.print("  none", style="dim")
    if found.groups:
        con.print()
        con.print("Entra group IDs the folder uses (there's no API to look them up, so check each by hand)",
                  style="bold")
        for where, group in found.groups:
            con.print(Text(f"  {where}: {group}"))


def folder_use(con: Console, env_name: str, key: str, what: str | None, files: list[str]) -> None:
    """An ID or name the folder uses: ✓ and what it is, or ✗ if the environment lacks it, then
    the files that use it."""
    if what:
        con.print(Text.assemble("  ", ("✓", "green"), f" {key}  {what}"))
    else:
        con.print(Text.assemble("  ", ("✗", "bold red"), f" {key}  ", (f"isn't in {env_name}", "red")))
    for f in files:
        con.print(Text(f"      {f}"))
