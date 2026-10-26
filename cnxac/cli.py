"""Command line: cnxac pull | check | asbuilt | ids | plan | apply | destroy | mpsk, on one config folder."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Annotated, NoReturn
import typer
from rich.text import Text

from . import apply as applying
from . import asbuilt as asbuilts
from . import checks, desired, live, mpsk as mpsks, plan as planning, registry, render
from . import ids as identities
from . import destroy as teardown
from .client import CentralError, Client
from .config import ConfigError, EnvConfig, load_credentials, load_env
from .files import Staged, write, write_staged
from .plan import count
from .pull import stage as staging

# Never show locals in tracebacks: they can hold the client secret and tokens.
app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
    help="Config as code for new Central AP and Central NAC configuration.",
)

mpsk_app = typer.Typer(no_args_is_help=True, help="Named MPSK entries, listed in the folder's mpsk/<WLAN>.csv files.")
app.add_typer(mpsk_app, name="mpsk")

ConfigOpt = Annotated[Path, typer.Option(
    "--config", "-c", help="Config folder holding env.yaml (default: the current folder).", show_default=False)]
YesOpt = Annotated[bool, typer.Option(
    "--yes", "-y", help="Write without asking, even where Central changes what a file says.")]


def main() -> None:
    utf8_output()
    app()


def utf8_output() -> None:
    """On Windows, output to a pipe or a file otherwise uses the ANSI code page, which can't
    print ✓, ✗ or →."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def environment(config: Path) -> EnvConfig:
    try:
        return load_env(config)
    except ConfigError as e:
        fail(str(e))


def fail(message: str) -> NoReturn:
    render.error(message)
    raise typer.Exit(1)


def connect(config: Path, env: EnvConfig) -> Client:
    """Print the header line and return a client for the environment."""
    try:
        creds = load_credentials(config, env.name)
    except ConfigError as e:
        fail(str(e))
    client = Client(creds)
    render.header(render.OUT, env.name, f"on {client.host}")
    return client


def read(config: Path, env: EnvConfig) -> live.Live:
    try:
        with connect(config, env) as client, render.reading(env.name):
            return live.read(client)
    except CentralError as e:
        fail(str(e))


@app.command()
def pull(
    config: ConfigOpt = Path("."),
    everything: Annotated[bool, typer.Option(
        "--all", help="Also add every assigned profile and every NAC object, to seed the folder.")] = False,
    add: Annotated[list[str] | None, typer.Option(
        "--add", help="Also add the objects of this type that have no file, e.g. authz-policies "
                      "(AP profiles: those assigned somewhere). Can be given more than once.")] = None,
    yes: YesOpt = False,
) -> None:
    """Refresh the folder's files from the environment, asking before changing what one says; --all seeds it."""
    env = environment(config)
    types = set(add or ())
    if types and everything:
        fail("--add goes without --all, which adds every type")
    if unknown := sorted(t for t in types if t not in registry.BY_PATH or registry.BY_PATH[t].single):
        fail(f"--add takes a type folder name, not {', '.join(unknown)}: "
             + ", ".join(t.path for t in registry.TYPES if not t.single))
    report = staging(read(config, env), config, env, everything, types)
    if not report.nothing_held:
        render.pull_changes(render.OUT, report.files)
        render.pull_left(render.OUT, report)
        write_pulled("pull", report.files, yes)
    render.pulled(render.OUT, config, report)


@app.command()
def check(config: ConfigOpt = Path(".")) -> None:
    """Check the files offline, with no credentials."""
    env = environment(config)
    render.header(render.OUT, env.name, "(files only)")
    d = desired.load(config)
    lists = mpsks.load(config)
    found = d.issues + checks.run(d, env) + lists.issues + mpsks.offline(lists, d)
    render.check(render.OUT, found)
    raise typer.Exit(1 if any(i.level == "error" for i in found) else 0)


@app.command()
def asbuilt(
    config: ConfigOpt = Path("."),
    out: Annotated[Path | None, typer.Option(
        "--out", "-o", help="Write to this file, replacing it, instead of to the screen.")] = None,
) -> None:
    """Write the files' key settings as a Markdown as-built appendix, by WebUI location. Offline."""
    env = environment(config)
    d = desired.load(config)
    found = d.issues + checks.run(d, env)
    if any(i.level == "error" for i in found):
        render.check(render.ERR, found)
        fail("check found errors; fix them before writing the as-built")
    text = asbuilts.render(d, env, date.today())
    if out is None:
        render.markdown(render.OUT, text)
    else:
        render.written(render.OUT, out, write(out, text))


@app.command()
def ids(config: ConfigOpt = Path(".")) -> None:
    """List the environment's identity stores, Intune extensions and certificates, and those the files use; only reads."""
    env = environment(config)
    d = desired.load(config)
    try:
        with connect(config, env) as client, render.reading(env.name):
            found = identities.find(client, d, env)
    except CentralError as e:
        fail(str(e))
    render.ids(render.OUT, env.name, found)


@app.command()
def plan(
    config: ConfigOpt = Path("."),
    destroying: Annotated[bool, typer.Option(
        "--destroy", help="List what destroy would do instead.")] = False,
) -> None:
    """Check the files and list the changes apply would make; only reads."""
    env = environment(config)
    build, title = (teardown.build, applying.TITLES["destroy"]) if destroying else (planning.build, applying.TITLES["apply"])
    result = build(desired.load(config), env, read(config, env))
    render.plan(render.OUT, result, title)
    raise typer.Exit(result.exit_code)


@app.command()
def apply(config: ConfigOpt = Path(".")) -> None:
    """Show the plan, then make the changes after a typed yes."""
    carry_out("apply", config, applying.main, "run plan to see the changes")


@app.command()
def destroy(config: ConfigOpt = Path(".")) -> None:
    """Remove what the folder describes from the environment, after its name is typed."""
    carry_out("destroy", config, applying.destroy, "run plan --destroy to see the changes")


@mpsk_app.command("add")
def mpsk_add(config: ConfigOpt = Path(".")) -> None:
    """Add the listed entries Central doesn't have, after a typed yes; existing ones are left alone."""
    carry_out("mpsk add", config, mpsks.add, "answer no to the prompt to only see what it would add")


@mpsk_app.command("pull")
def mpsk_pull(
    config: ConfigOpt = Path("."),
    passphrases: Annotated[bool, typer.Option(
        "--passphrases", help="Instead, write one WLAN's entries with their passphrases to a new file outside "
                              "the folder (needs --network and --out).")] = False,
    network: Annotated[str | None, typer.Option("--network", help="The WLAN, with --passphrases.")] = None,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="The new file, with --passphrases.")] = None,
    yes: YesOpt = False,
) -> None:
    """Write Central's entries to the folder's mpsk/ files, without passphrases, asking before changing one."""
    env = environment(config)
    if passphrases and not (network and out):
        fail("--passphrases needs --network <WLAN> and --out <new file>")
    if not passphrases and (network or out):
        fail("--network and --out go with --passphrases")
    if passphrases and yes:
        fail("--yes doesn't go with --passphrases, which never overwrites a file")
    try:
        with connect(config, env) as client, render.reading(env.name):
            central = mpsks.read(client)
    except CentralError as e:
        fail(str(e))
    if not passphrases:
        pulled = mpsks.stage(config, central)
        render.issues(render.OUT, pulled.issues)
        if pulled.files:
            render.pull_changes(render.OUT, pulled.files)
            write_pulled("mpsk pull", pulled.files, yes)
        render.mpsk_pulled(render.OUT, config / mpsks.FOLDER, pulled)
        raise typer.Exit(1 if any(i.level == "error" for i in pulled.issues) else 0)
    try:
        n = mpsks.export(config, central, network, out)
    except ValueError as e:
        fail(str(e))
    render.mpsk_exported(render.OUT, out, network, n)


def write_pulled(command: str, staged: list[Staged], yes: bool) -> None:
    """Write what a pull shows it would. It asks first when Central changes what a file says;
    new files and changes of format alone can't lose anything, so they don't ask."""
    changed = sum(s.status == "changed" for s in staged)
    if changed and not yes:
        if not interactive():
            fail(f"{command} would change {count(changed, 'file')}; confirm in a terminal, or pass --yes")
        answer = ask(render.prompt("yes"))
        if answer != "yes":
            render.declined(render.OUT, "yes", answer)
            raise typer.Exit(1)
    write_staged(staged)


def interactive() -> bool:
    return sys.stdin.isatty()


def carry_out(command: str, config: Path, run, preview: str) -> None:
    env = environment(config)
    if not interactive():
        fail(f"{command} needs a terminal to confirm in; {preview}")
    try:
        with connect(config, env) as client:
            code = run(config, env, client, ask)
    except CentralError as e:
        fail(str(e))
    raise typer.Exit(code)


def ask(prompt: Text) -> str:
    try:
        return render.OUT.input(prompt).strip()
    except EOFError:
        return ""
