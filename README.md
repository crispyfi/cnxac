# cnx-as-code

Config as code for HPE Aruba Networking Central (new Central): AP configuration and Central NAC, kept as plain API JSON in a folder per environment, with `pull`, `check`, `plan`, `apply` and `destroy`.

## What it manages

| Area | Types |
|---|---|
| Security | auth servers, server groups, aliases, network groups and services, roles, policies and their order |
| Network | named VLANs |
| WLAN and radio | WLANs, radio, wireless IDS/IPS |
| AP | AP system, AP uplink, NTP, system administration, user administration |
| Other | App Recognition and Control, certificate usage |
| Central NAC | auth profiles, authorization policies |
| Scopes | sites, only in an environment that lists them in `sites.yaml` |

AP profiles are library profiles assigned at Global or at sites for the `CAMPUS_AP` device function. Central NAC objects are assigned at Global, as the UI does.

Not managed: what's set in the UI (see below), certificates, device groups, local profiles (other than sites' alias values), captive portals, custom EAP server certificates, management auth servers (`check` warns), layer 2 VLANs (APs don't need them), site collections, AP port profiles, Passpoint and device-level config.

## Install

Needs Python 3.11 or later and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/crispyfi/cnxac.git   # puts cnxac on your PATH
cnxac --help
```

Or clone it and run it in place:

```sh
git clone https://github.com/crispyfi/cnxac.git
cd cnxac
uv run cnxac --help
```

It runs on macOS, Linux and Windows. Save any file you write by hand as UTF-8; Windows PowerShell 5.1's `echo … > file` writes UTF-16, which the tool refuses.

## Credentials

Create an API client in HPE GreenLake (client credentials) with access to the Central workspace. Then make a `credentials.yaml` in the config folder, or in the folder above it so one file serves every environment. [credentials.example.yaml](credentials.example.yaml) shows its shape: one section per environment, each with `base_url` (the regional API gateway), `client_id` and `client_secret`.

Never commit `credentials.yaml`. The tool never prints, logs or writes the values in it.

## A config folder

One folder per Central environment, usually in a private repo:

```
credentials.yaml                      git-ignored; one section per environment
lab/
  env.yaml                            which environment, protected flag, ignore list, group names
  assign.yaml                         what's assigned at Global and at each site
  sites.yaml                          only where the tool creates the sites
  profiles/<type>/<name>.json         AP library profiles, as the API takes them
  profiles/policy-groups.json         the policy order, for the folder's policies only
  nac/<type>/<name>.json              Central NAC objects, with their IDs
  nac/identity-management.json        written by pull for the as-built: the UI-made identity config, no secrets
  mpsk/<WLAN>.csv                     named MPSK entries, for cnxac mpsk (no passphrases)
prod/
  …
```

```yaml
# env.yaml
environment: prod          # the credentials.yaml section to use
protected: true            # destroy is refused
ignore: ["test-*"]         # name globs the tool never touches
group_names:               # Entra group IDs and their names, for the as-built
  "00000000-0000-0000-0000-000000000001": SG-Staff
```

```yaml
# assign.yaml
global:
  wlan-ssids: [STAFF, IOT, GUEST]
  radios: [rf-default]
sites:
  SITE014:
    radios: [rf-high-density]            # replaces Global's at this site
    alias-values:
      acct-server: 10.14.0.1             # this site's own value for the alias
```

```yaml
# sites.yaml
default_timezone: Europe/London
sites:
  SITE001: { address: 1 Example Street, city: Town, state: County, country: GB, zipcode: AB1 2CD }
```

- **The folder decides what's managed.** An object is managed when it has a file. Central's other objects are left alone and aren't listed.
- **Files hold exactly what the API takes and returns.** There's no templating and no variables. File names are object names, with unsafe characters percent-encoded.
- **A settings profile at a site replaces Global's;** WLANs add to Global's. NAC objects aren't listed in `assign.yaml`.
- **A site can have its own value for an Auth Server Address alias,** under `alias-values`. The tool writes it as a same-name local copy of the alias at the site: the only local profile it writes.
- **Central NAC objects are keyed by a UUID.** A new object leaves it empty (`""`), and `apply` chooses one and writes it into the file. Commit the file afterwards.

## Getting started

Make a folder for the environment, holding an `env.yaml` with the line `environment: lab`, and add a `lab` section to `credentials.yaml`. Then, in that folder:

```sh
cnxac pull --all        # seed the folder with everything assigned
git init
git add .
git commit -m "Seed from Central"
```

Delete the files of anything the tool shouldn't manage, and commit. From then on:

```sh
cnxac pull      # bring the folder's files up to date with changes made in the UI
cnxac check     # check the files, offline
cnxac plan      # show what apply would change
cnxac apply     # make the changes, after a typed yes
```

### Starting from someone else's assign.yaml

To pull the same set of profiles as an existing folder, from the same environment, a new folder needs only that folder's `env.yaml` and `assign.yaml`:

1. Make the folder and put the two files in it. Add the environment's section to `credentials.yaml`, under the name `env.yaml` gives.
2. In the folder, run `cnxac pull --add authz-policies`. It fetches the profiles `assign.yaml` lists, the auth profiles that serve its WLANs, the policy order entries of its policies, and every authorization policy, since those belong to no WLAN. It rewrites `assign.yaml` with where Central has each profile assigned.
3. Run `cnxac mpsk pull` for the named MPSK lists of its MPSK WLANs.
4. `cnxac plan` should show no changes. Commit the folder.

This only happens in a folder with no profile or NAC files yet. Once there are files, `assign.yaml` adds nothing, so a file deleted to stop managing its object stays deleted.

## Commands

Each command acts on the current folder, or the one `-c <folder>` names.

| Command | Does |
|---|---|
| `pull` | Rewrites the folder's files from Central. It shows what would change, and asks for `yes` before changing what a file says (`--yes` skips that). It also lists files whose object Central doesn't have, which it never deletes, and objects Central has with no file. A folder with no files yet is seeded from its `assign.yaml` (see below); with neither, it does nothing. |
| `pull --all` | Seeds the folder: every AP profile assigned anywhere and every NAC object, skipping factory defaults and system objects. |
| `pull --add <type>` | Adds the objects of one type that have no file, such as an authorization policy made in the UI (`--add authz-policies`). Can be given more than once. |
| `check` | Checks the files offline, with no credentials. Suits CI and an LLM. |
| `plan` | Runs the checks, compares the files with Central and lists the changes in the order `apply` makes them. Only reads. |
| `plan --destroy` | Lists what `destroy` would do. Only reads. |
| `apply` | Shows the plan, asks for `yes`, backs up, makes the changes, then plans again. |
| `asbuilt` | Writes the files' key settings as a Markdown as-built appendix, under their WebUI locations and labels, to the screen or to the file `-o` names. Offline; refused while `check` finds errors. Identity store names, Intune extensions and UEM onboarding come from `nac/identity-management.json`, and Entra group names from `group_names` in `env.yaml`; it warns on the terminal about any it can't name. |
| `ids` | Lists the environment's identity stores, Intune extensions and certificates, and the store IDs and certificate names the folder uses, marking any the environment lacks. Also lists the Entra group IDs in authorization conditions, which nothing can look up. Only reads, and never shows a secret. |
| `destroy` | Removes what the folder describes, sites in `sites.yaml` included, after the environment's name is typed. Refused when `env.yaml` says `protected: true`. |

Exit codes for `plan`, `apply` and `destroy`: 0 for nothing to do or nothing left, 2 for changes (or changes still left after `apply`), 1 for errors, a refused write or no confirmation. `check` exits 0 or 1.

## Named MPSK entries

The devices on an MPSK WLAN, each with the role it gets, are listed in `mpsk/<WLAN>.csv`, in the same format as Central's own CSV import. So a file works with the tool, or as it is with the UI's import. Central generates each passphrase.

```csv
Name,Role,Enabled
printer-01@site001,PRINTER,TRUE
scanner-07@site001,SCANNER,FALSE
```

| Command | Does |
|---|---|
| `mpsk add` | Shows the entries Central doesn't have yet, asks for `yes`, then adds them and waits for Central to show them. Entries already there are left as they are, even if the file differs. |
| `mpsk pull` | Rewrites each MPSK WLAN's file from Central, without passphrases. Like `pull`, it shows what would change and asks first (`--yes` skips that). |
| `mpsk pull --passphrases --network <WLAN> -o <file>` | Writes one WLAN's entries with their passphrases to a new file outside the folder, as the UI's export does. It never overwrites a file. |

- Passphrases never go in the folder, and are never printed or logged. `check` refuses a `Password` column in `mpsk/`.
- Central's import updates an existing entry's role and enabled flag, so `mpsk add` uploads only the new rows. Importing a whole file in the UI would update the rest too.
- Names are matched ignoring case.

## Copying a folder to another environment

A folder proven in one environment (a lab, say) seeds the next (production):

1. **Prove the source:** `plan` shows no changes, `mpsk add` has nothing to add, and the folder is committed.
2. **Copy** it, leaving out `env.yaml`, `backups/`, `nac/identity-management.json` and any as-built (both describe the source), and `sites.yaml` if the target's sites already exist. Write the target's `env.yaml` (`protected: true` for production, and `group_names` for the target's Entra groups) and add its section to `credentials.yaml`.
3. **Rename** anything named differently in the target, such as ESSIDs or a profile prefix: the file name, the name inside it and every reference to it, including `assign.yaml`, the policy order, auth profiles' `networks` and `mpsk/` file names. `check` reports references to a name the folder no longer holds. Do this before the first apply, since an MPSK WLAN can't be renamed later without registering every device again.
4. **Replace what belongs to the environment:**
   - the NAC objects' own IDs: empty `auth-profile-id` and `policy-id`, and the `rule-id`, `condition-id`, `condition-group-id` and enforcement `profile-id` inside each policy, so `apply` gives the target its own
   - identity store IDs: `cnxac ids` lists the target's stores and marks those the folder uses that the target lacks
   - Entra group IDs in authorization conditions, if the target has its own tenant, and their names under `group_names` in `env.yaml`. Nothing can check these, so check each by hand.
   - auth profiles' `organization-name`
   - certificate names, if the target's were uploaded under other names (`cnxac ids` lists the target's certificates)
   - the `mpsk/` device lists
5. **In the target's UI,** create the management users the folder lists.
6. **`check`, then `plan`.** Expect creates and assigns. An update, or a name Central refuses, means the target already holds something by that name; resolve it first.
7. **`apply`,** then set what's set in the UI (see below) and run `mpsk add`. `plan` should then show no changes. Run `pull` to write the target's `nac/identity-management.json` for `asbuilt`. Commit, including the IDs `apply` wrote in.

## Safety

- Only objects that have a file are touched. Deleting a file leaves its object in Central as it is.
- System (`sys_*`), derived, factory default and ignored objects are never touched, even with a file.
- Only the `CAMPUS_AP` assignments of the folder's profiles, the Global assignments of its NAC objects and the site values of its aliases are added or removed. Roles, policies, VLANs and servers shared with switches and gateways keep their other assignments.
- An assignment that something staying at the same scope still uses isn't removed, because Central may have added it along with that object.
- `apply` and `destroy` need a terminal and a typed confirmation, with no flag to skip it.
- Before writing anything, `apply` and `destroy` save the live config they planned from to `backups/<timestamp>/` in the folder, with a log of every request. Backups hold encrypted secrets and are never committed.
- The first refused write stops the run, and the tool lists what was and wasn't done.

## Set in the UI

Some things are set in the UI in each environment, never in files. On update the tool sends back what Central returned, so what was set in the UI survives.

| What | Notes |
|---|---|
| WLAN PSKs | A PSK WLAN is created disabled. Once its PSK is set, the next `apply` enables it as the file says. |
| MPSK passphrases | Central generates them for the entries `mpsk add` or the UI adds (see Named MPSK entries). An MPSK auth profile owns the store that holds them. `destroy` deletes it, and the plan warns that the passphrases go too. |
| Auth server shared secrets | Auth servers are created without one. `plan` warns about any still missing, except on RadSec servers, which take none. |
| Management users | Created in the UI, because Central needs the password (user administration). The tool then manages their other settings. |
| UEM onboarding on an EAP auth profile | Central NAC's PKI for Intune devices (`eap.uem-providers`). Its ID and SCEP URL belong to the environment. |
| Identity stores and the Intune extension | Referred to by ID from NAC objects; `plan` checks they exist, and `ids` lists them. |

When `pull` writes NAC objects, it also writes `nac/identity-management.json`: the identity stores, the Intune extensions and each EAP auth profile's UEM onboarding, without their secrets. It's there for `asbuilt` only. `plan`, `apply` and `destroy` ignore it, so editing it changes nothing in Central.
| Certificates | Uploading one in the UI assigns it at Global. Certificate usage profiles and RadSec auth servers refer to it by name, and `plan` checks it exists. |

## Licence

Apache-2.0. See [LICENSE](LICENSE).
