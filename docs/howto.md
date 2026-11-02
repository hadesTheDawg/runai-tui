# runai-tui: Run:ai from a terminal UI

A how-to for **researchers**. `runai-tui` is a terminal UI (plus a small
command-line mode) on the login node. It sits on top of the `runai` CLI and
`kubectl`. It does the one-time setup for you, shows your quota, submits
workloads and follows them until they run, shells into them, and saves them
as YAML you can submit again.

It runs **as you, with your own Run:ai login**: it can do nothing you could
not do yourself with `runai` and `kubectl`.
Every change it makes is a `runai`/`kubectl` command, written to a log.

**Placeholders used below** (your administrator can tell you yours):

| What | Here |
|---|---|
| Login node | `ssh <user>@<login-node>` |
| Tool | `/usr/local/bin/runai-tui` (`./install.sh`) |
| Run:ai URL / your login | `https://runai.example.com`, `<you>@example.com` |
| Your manifests | `~/runai/` (examples, saved workloads) |
| Manifests submitted from the form | `~/.local/state/runai-tui/submitted/` |
| Log of every action | `~/.local/state/runai-tui/runai-tui.log` (`!` in the TUI) |

---

## Contents

1. [Starting it](#1-starting-it)
2. [The screen](#2-the-screen)
3. [Setup: logging in and housekeeping](#3-setup-logging-in-and-housekeeping)
4. [Overview: who you are and your quota](#4-overview-who-you-are-and-your-quota)
5. [Workloads: query, shell, logs, delete](#5-workloads-query-shell-logs-delete)
6. [Submitting a new workload (the form)](#6-submitting-a-new-workload-the-form)
7. [Submitting YAML files](#7-submitting-yaml-files)
8. [Activity: progress bars](#8-activity-progress-bars)
9. [Saving a workload as YAML](#9-saving-a-workload-as-yaml)
10. [Keys](#10-keys)
11. [Command-line mode](#11-command-line-mode)
12. [Troubleshooting](#12-troubleshooting)
13. [Notes for administrators](#13-notes-for-administrators)

---

## 1. Starting it

```bash
ssh <user>@<login-node>
runai-tui                    # opens on Overview, or on Setup if you are not logged in
runai-tui -p team-a      # start in a project
runai-tui --setup            # start on the Setup tab
```

It needs a terminal of at least 80×16. A wide one (≥ 110 columns) puts
identity, quota and in-flight workloads on one screen.

---

## 2. The screen

```
 ▲ team-a Researcher · Run:ai   jdoe@example.com          ● runai.example.com  token 14m36s  updated 4s ago
 1 Overview    2 Workloads    3 Activity    4 Setup                                  project: team-a (p)
   You                                    Quota · team-a
  Jane Doe                                team-a  department research          1 GPU guaranteed
  email   jdoe@example.com                pool     GPU used / quota          alloc quota limit  non-pre  CPU  memory
  uid/gid 1234/1200                       h100     ░░░░░░░░┆░░░░░░░░░░░░░░       0   0.5     2        0    0     0Mi
  groups  team-a                      a100     ██████▌┆░░░░░░░░░░░░░░░     0.5   0.5     2        0    1   3.0Gi
  ...
 n new   F submit files   p project   2 workloads   ? help   q quit
```

- **Header**: your group (`<group> Researcher`), your login, the control plane, how long your current token
  has left (it renews itself), and when the data was last refreshed. The
  data refreshes every 6 s, or every 2 s while something is in flight.
- **Tabs** `1`–`4`: Overview, Workloads, Activity, Setup. The Activity tab
  shows `(n)` workloads in flight; Setup shows `(!n)` items that need
  attention.
- **Project** (`p`): what Overview and Workloads show. It starts at your
  CLI default project. Switching offers to make the new one your default
  (`runai project set`).

---

## 3. Setup: logging in and housekeeping

Tab `4` is the one-time CLI and kubectl setup, as a checklist checked live:

| Check | Fix (Enter) |
|---|---|
| runai CLI installed | ask an administrator |
| CLI points at this site | `runai config set --cp-url=<your Run:ai URL> --auto-update=disabled` |
| Logged in to Run:ai | log in (below) |
| Clock in step with Run:ai | tell an administrator (expired-token errors otherwise) |
| Default project | `runai project set <project>` (pick from yours) |
| kubectl config file | copy the site skeleton to `~/.kube/config` (if the site provides one) |
| kubectl uses your Run:ai login | `runai kubeconfig set --bypass-validation` |
| kubectl namespace | `kubectl config set-context --current --namespace=runai-<project>` |
| kubectl access to your project | `kubectl auth whoami` + `can-i create jobs` (informational) |
| Your manifests folder | copy the site's example manifests into `~/runai` (if it provides some) |
| Home directory | free space, permissions, site UID (informational) |
| runai-tui configuration | config files in use and any problems in them (`runai-tui config`) |

`Enter` fixes the selected item. `A` fixes everything that needs no input,
re-checking between rounds, because some fixes only become possible after
others. `r` re-checks.

**Logging in** (`i`, or Enter on *Logged in*): choose

- *user name and password*: runs `runai login user -u <you>@example.com` in
  the terminal. Type the password (same as the web UI), press **Enter**, then
  **Enter** again on `[ Submit ]`. runai-tui never sees the password.
- *single sign-on*: runs `runai login remote-browser`. Open the printed URL
  in a browser on your laptop and paste the code back.

After a successful login it does the housekeeping in one go, with a
progress bar: skeleton `~/.kube/config` if missing, `runai kubeconfig set`,
your default project (asks if you have several), and the kubectl namespace.
`o` logs out.

> Ten wrong passwords lock the account for a while. Ask an administrator
> rather than retrying.

---

## 4. Overview: who you are and your quota

- **You**: name, e-mail, UID/GID and groups (from your Run:ai token),
  token lifetime, site.
- **Your workloads**: counts by phase, and the GPUs, CPUs and memory your
  running workloads hold right now.
- **Quota** for the selected project, one row per node pool (GPU type):

| Column | Meaning |
|---|---|
| bar | GPUs allocated, with `┆` at the guaranteed quota. Green is within quota, yellow is over quota (borrowed idle GPUs, preemptible). |
| alloc / quota / limit | allocated GPUs, guaranteed quota, hard limit (`∞` = none) |
| non-pre | GPUs held by non-preemptible workloads. These must fit in the quota. |
| CPU / memory | allocated across the project's workloads |
| ↳ requested but pending | GPUs asked for that the scheduler has not placed yet |

  Below that: the project's average GPU and GPU-memory utilisation over
  24 h, and its average allocation over 7 days.
- **In flight** (wide terminals): the Activity bars of section 8.

`p` → *(all projects)* lists every project you belong to.

---

## 5. Workloads: query, shell, logs, delete

Tab `2` lists **your** workloads in the project (`★`). `a` toggles to
everyone in the project; `/` filters by name, phase, type or image.
Workloads you created with `kubectl apply` count as yours, even though
Run:ai records no submitter for them. runai-tui remembers what you submitted
through it.

The details pane shows phase and progress bar, why it is pending (the
scheduler's reason), type/category/preemptibility/priority, timings,
requested vs. allocated resources, node pools, images, tool URLs, the
original `runai` command, its pods (node, pool, IP, containers), and the
phases runai-tui has seen with how long each lasted.

| Key | Does |
|---|---|
| `Enter` / `s` | **shell**: `runai workload exec <name> -it -- bash` (falls back to `sh`) in your home directory; asks which pod/container if there are several. `exit` returns. |
| `l` | logs (last 3000 lines) in a pager with `/` search |
| `L` | follow logs live (`runai workload logs -f`); Ctrl-C returns |
| `d` | `runai workload describe` (events, pods, why pending) |
| `y` | save as YAML (section 9) |
| `f` | port-forward a pod port (`kubectl port-forward`); then open `http://localhost:<port>` on the login node, or tunnel it from your laptop with `ssh -L <port>:localhost:<port> <user>@<login-node>` |
| `u` / `c` | copy the tool URL / the name to your clipboard (works over ssh in most terminals) |
| `z` | suspend / resume (Run:ai workspaces and trainings) |
| `x` | delete (`runai workload delete`). Services and Ingresses created by `kubectl apply` stay: `kubectl delete -f <file>` removes those. |

---

## 6. Submitting a new workload (the form)

`n` opens a form on the left with a live preview of the manifest on the
right. Use `↑/↓` to move, `Enter` to edit text or open a list, and `←/→`
(or space) to cycle choices.

| Field | Notes |
|---|---|
| Project, Name | name: lowercase, digits, `-`, ≤ 63 characters, unique in the project |
| Template | a Run:ai workload template: fills environment, compute, data sources, command and env vars (shown when your project has templates) |
| Kind | **batch job**: a `Job` that ends, preemptible (may borrow idle GPUs). **interactive**: a `Job` running `sleep infinity` as a Run:ai *Build* workload, non-preemptible; shell into it with `Enter`. **service**: a `Deployment`. **Run:ai workspace**: `runai workspace submit --environment …`, which keeps the environment's tools and URLs (Jupyter, VS Code, ...). |
| Environment | an environment from the Run:ai catalog; fills image and command |
| Image, Command | command runs as `sh -c "<command>"`; empty = the image's default |
| Compute resource | a Run:ai compute resource: fills CPU, memory and GPU; editing those by hand sets it back to `(custom)` |
| GPU | none / fraction (`gpu-fraction` annotation) / whole (`nvidia.com/gpu`) |
| Node pool | `any`, or a GPU type (`runai/node-pool` label) |
| CPU cores, Memory | requests = limits |
| Run as me + home | your UID/GID, the shared home (`site.home`, default `/home`) mounted, `HOME` and working directory = your home |
| Data sources | Run:ai PVC, NFS, host-path and config-map data sources, mounted where they say (`Enter`, then Space to tick) |
| Bind mounts | host paths from the site's list (e.g. `/etc/passwd`, `/etc/group`), read-only unless the site says otherwise; some may be ticked already |
| Extra bind mounts | your own host paths: `Enter` opens a list. `a` (or `Enter` on *+ add*) opens a new row in the list where you type the host path (Tab completes), then the path in the container, and the list grows as you add more; `e` edits; `Space` switches read-only / read-write; `d` deletes; `q` returns to the form |
| Scratch, /dev/shm | node-local `emptyDir` at `/scratch` (sets `TMPDIR`), bigger shared memory |
| Env | `A=1,B=2` |
| Image pull secret | shown when the site lists some (`site.pull_secrets`) |
| Completions, Keep after end | batch jobs: parallel indexed pods, `ttlSecondsAfterFinished` |
| Priority, Preemptibility | `default` = Run:ai's default for the kind |

Below the fields: GPU used/quota per pool, plus warnings, e.g. the name is
taken, the image is not in the site registry (when `site.offline_nodes` is on), or a
non-preemptible GPU request does not fit your free quota and would stay
pending.

| Key | Does |
|---|---|
| `S` | submit; jumps to Activity (section 8) |
| `W` | write the manifest to `~/runai/<name>.yaml` (or the workspace command as a `.sh` script) without submitting |
| `E` | write it, open it in `$EDITOR`, then submit the edited file |
| `P` | presets: load one, save the form as one, delete one of yours (`~/.config/runai-tui/presets/`); one named `default` fills every new form |
| `Esc` | discard (asks first) |

Priority, preemptibility and category are written into the manifest as
labels. runai-tui passes them to `runai workload submit` as `--priority`,
`--preemptibility` and `--category`, because the CLI does not read them from
the file. A copy of every submitted manifest is kept in
`~/.local/state/runai-tui/submitted/`.

---

## 7. Submitting YAML files

`F` asks for files: names, globs or a directory, with Tab completion, e.g.
`~/runai/0[13]-*.yaml`. It lists what will happen to each file, then
submits them one after the other:

- one object per file: `runai workload submit -f <file> -p <project>`
  (labels → flags, as above);
- several objects (Service, Ingress, ...): `kubectl apply -f <file> -n runai-<project>`.

The project comes from `metadata.namespace: runai-<project>` if the file has
one, otherwise the selected project. Names already taken in the project
(including by an earlier file in the same batch) are reported and skipped.

---

## 8. Activity: progress bars

Tab `3` shows the batch being submitted and every workload in flight:

```
  Submission 2/2  ✔ 2  done
  ████████████████████████████████████████████████████████████████████████ 100%
   ✔ 01-cpu-job.yaml  runai workload submit  Creating Job hello-cpu...
   ✔ 03-fractional-gpu.yaml  runai workload submit  Creating Job half-gpu...
  ▶ ● half-gpu  team-a · Job  Running 2s  GPU 0.5
    ████████████████████████████████████████████████████▓▓█████░░░░░░░░░░░░░  87%
    Submitted          Pending           Starting           Running           Done
    ◐ dev-1101-1336  team-a · Job  Pending 4s  - NonPreemptibleOverQuota
    ███████████████▓▓██░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  37%
    Submitted          Pending           Starting           Ready
```

- Batch jobs go Submitted → Pending → Starting → Running → **Done**.
  Interactive jobs, services and workspaces end at **Ready** (Running).
- The moving block marks a stage still in progress. Yellow means Pending;
  the scheduler's reason is shown on the line. Red means Failed, at the
  stage it reached.
- It lists what you submitted from this session plus any workload of yours
  that is not settled yet. Finished ones you did not submit here drop off
  after 15 minutes. `D` forgets everything finished.
- `Enter` jumps to the workload in tab 2. `s`, `l`, `L`, `d`, `y`, `x` work
  here too.

---

## 9. Saving a workload as YAML

`y` (tab 2 or 3) writes the selected workload to `~/runai/<name>.yaml`
(you can change the path). The file is a manifest you can submit again:

```yaml
# Job dev-1101-1336 in project team-a, saved by runai-tui 2026-11-01 13:36:58
# submit again: runai workload submit -f <this file> -p team-a
apiVersion: batch/v1
kind: Job
metadata:
  name: dev-1101-1336
  labels:
    kai.scheduler/preemptibility: non-preemptible
    priorityClassName: low
    run.ai/category: Build
spec:
  backoffLimit: 0
  template:
    spec:
      securityContext: {runAsUser: 1234, runAsGroup: 1200, supplementalGroups: [1200]}
      containers:
      - name: main
        image: ubuntu:24.04
        command: [sleep, infinity]
        ...
```

It reads the live object with `kubectl get` and strips everything the
cluster added (status, UIDs, owner references, controller labels, injected
`RUNAI_*` variables, scheduler defaults). It keeps what defines the
workload: image, command, resources, GPU annotations, node pool, security
context, volumes, priority/preemptibility/category labels. For workloads
created with `runai workspace submit` or in the UI, the original `runai`
command is added as a comment. If kubectl is not set up (Setup tab), it
falls back to the Run:ai spec of workspaces and trainings: that file is not
a Kubernetes manifest, but carries the command that recreates the workload.

To resubmit: `F` and the file, or `runai workload submit -f ~/runai/<name>.yaml`.
Change `metadata.name` first if the old one still exists.

---

## 10. Keys

```
Everywhere   1-4 tabs · Tab next · n new · F submit files · p project · r refresh · ! log · ? help · q quit
Workloads    ↑↓ move · PgUp/PgDn details · / filter · a mine/everyone
             Enter/s shell · l logs · L follow · d describe · y save YAML · f port-forward
             u copy URL · c copy name · z suspend/resume · x delete
Activity     ↑↓ · Enter go to · D forget finished (and the workload keys)
Setup        ↑↓ · Enter fix · A fix all · i log in · o log out · r re-check
Form         ↑↓ field · Enter/←→ change · S submit · W write · E edit+submit · P presets · Esc cancel
Pagers       ↑↓ PgUp/PgDn g/G · / search · n next · q back
```

Marks: `★` yours · `●` running · `◐` pending/starting · `✔` completed · `✖` failed · `■` stopped.

These are the default keys. They can be remapped (`keys:` in the
configuration), and `?` always shows the ones in use. See
[`customizing.md`](customizing.md).

---

## 11. Command-line mode

The same functions without the UI, for scripts and quick checks:

```bash
runai-tui status                 # identity, quota bars per pool, your workloads
runai-tui ls                     # your workloads in the default project
runai-tui ls -A                  # everyone's, in all your projects
runai-tui doctor                 # setup checklist (exit 1 if anything is not ok)
runai-tui doctor --fix           # ... and apply the fixes that need no input
runai-tui submit ~/runai/0[13]-*.yaml          # batch submit with a progress bar, then follow
runai-tui submit job.yaml -p team-a --no-wait
runai-tui watch [NAME...]        # progress bars until everything has settled (Ctrl-C stops watching)
runai-tui save NAME [-o FILE|-]  # workload -> YAML (default ~/runai/NAME.yaml; --force overwrites)
runai-tui shell NAME             # = runai workload exec NAME -it -- bash
runai-tui config                 # configuration in use and where each value comes from
runai-tui config --defaults      # the built-in defaults as YAML, to start a config file
```

Logging in stays interactive: `runai login user -u <you>@example.com`, or the
TUI.

---

## 12. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `✖ not logged in` in the header | session ended: `i` (or `runai login`) |
| `token is expired` / 401 right after logging in | the login node's clock is off: Setup → *Clock*; tell an administrator |
| Setup: *kubectl access* fails, `whoami` error | `runai kubeconfig set` (Enter on *kubectl uses your Run:ai login*) |
| `runai kubeconfig set`: `open <root's kubeconfig>: permission denied` | your shell inherited root's `KUBECONFIG` (`su <user>` without `-`). Log in as yourself (`ssh`, or `su - <user>`). runai-tui ignores such a `KUBECONFIG` and uses `~/.kube/config`; plain `runai`/`kubectl` need `unset KUBECONFIG`. Never use root's kubeconfig: it is usually the cluster-admin credential. |
| Save gives a Run:ai spec, not a manifest | kubectl not set up yet (Setup tab) |
| Workload stays Pending with `NonPreemptibleOverQuota` | non-preemptible GPU request above your free quota: Overview, *non-pre* column. Use a batch job or preemptible. |
| `ImagePullBackOff` | the nodes cannot pull the image (wrong name, private registry without a pull secret, or offline nodes); set `site.registry` and `site.offline_nodes` and the form warns about this |
| Shell: `I have no name!` prompt | normal: the image has no passwd entry for your site UID; files and `/home` still work as you |
| `kubectl apply` workloads not marked `★` on another machine | ownership of kubectl workloads is remembered per user in `~/.local/state/runai-tui/owned.json` |
| Anything else | `!` shows the log with every command runai-tui ran and its output |

---

## 13. Notes for administrators

- Installed by `./install.sh` from this repo (`install -m 755 runai-tui.py /usr/local/bin/runai-tui`). Python 3.9 + PyYAML (system packages), no
  other dependencies. It honours `RUNAI_CLI_CONFIG_PATH` and `KUBECONFIG`.
- Reads use the Run:ai REST API with the user's token from
  `runai auth get-token` (an ExecCredential; `status.token`, 15-minute JWT,
  refreshed by the CLI): `/api/v1/org-unit/projects` (quota, per-pool
  status: CPU in millicores, memory in MB), `/api/v1/workloads`,
  `/api/v1/workloads/<id>/pods`, `/api/v1/asset/environment`, and for the
  form `/api/v1/asset/compute`, `/api/v1/asset/datasources` and
  `/api/v1/asset/workload-template` (all paths configurable under `api:`). Researchers
  get 403 on `/api/v1/node-pools` and `runai nodepool list`, so cluster-wide
  free capacity is not shown.
- The Run:ai URL comes from `site.control_plane` if a config file sets it, else
  `--cp-url` in `/etc/profile.d/runai-cli.sh`, else `$RUNAI_CP_URL`; the
  optional skeleton kubeconfig and examples from `/usr/local/share/runai`. These and
  the other site values (registry, CA bundle, home mount, labels, form
  defaults, keys, colours, submit hooks, plugins) can be set in
  `/etc/runai-tui/config.yaml`: see [`customizing.md`](customizing.md).
- Run:ai 3.0.20 quirks it works around: `runai workload submit -f` ignores
  `priorityClassName` / `kai.scheduler/preemptibility` / `run.ai/category`
  labels (passed as flags instead); workloads created by `kubectl apply`
  have `submittedBy: null`.
- Test hook: `RUNAI_TUI_SCREEN=<file>` writes a plain-text copy of every
  frame (used to test the UI headlessly through a pty).
