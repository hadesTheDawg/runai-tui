# Customizing runai-tui

Everything site-specific lives in one configuration, merged in this order
(later wins):

1. built-in defaults (`runai-tui config --defaults` prints them as YAML)
2. `/etc/runai-tui/config.yaml`: the **site's** values, set by administrators
3. `~/.config/runai-tui/config.yaml`: **yours**
4. `$RUNAI_TUI_CONFIG`: one more file, e.g. for testing
5. environment variables `RUNAI_TUI__SECTION__KEY=value` (the value is YAML),
   e.g. `RUNAI_TUI__POLL__IDLE=10`

`runai-tui config` shows every value in use and the file or variable it came
from, the key bindings, presets and plugins, and any problems. The TUI shows
the same problems on the Setup tab ("runai-tui configuration").

## Site values

```yaml
site:
  name: "{group} Researcher"           # header: "▲ <name> · Run:ai"; {group} {user} {email}
  control_plane: https://runai.example.com
  ca_file: /etc/pki/ca-trust/source/anchors/example-ca.pem   # if the control plane uses a private CA
  registry: registry.example.com
  allowed_registries: [docker.io/library]
  offline_nodes: true                  # warn about images from outside the registry (default false)
  home: {host_path: /home, mount_path: /home}   # "Run as me"; host_path "" mounts nothing
  node_pool_label: runai/node-pool
  gpu_fraction_annotation: gpu-fraction
  min_site_uid: 1000
  pull_secrets: [regcred]              # offered as "Image pull secret" in the form
  bind_mounts:                         # offered as "Bind mounts" in the form (see below)
    - {name: passwd, host_path: /etc/passwd, type: File, default: true}
    - {name: group, host_path: /etc/group, type: File, default: true}
  save_dir: ~/runai
  share_dir: /usr/local/share/runai    # kubeconfig skeleton + examples
  example_placeholders: {uid: "1234", gid: "1200", user: jdoe}   # text in the examples to replace
```

The header title is `site.name` with `{group}`, `{user}` and `{email}` filled
in. `{group}` is your primary Unix group if your Run:ai token lists it (or
lists no groups), otherwise the first group in the token. With the default,
a member of `team-a` sees `▲ team-a Researcher · Run:ai`.

There is no built-in control-plane URL. If a config file sets `site.control_plane`, that value wins over the profile
hook (`--cp-url` in `/etc/profile.d/runai-cli.sh`) and `$RUNAI_CP_URL`. Without
one, the order is unchanged: the profile hook, then `$RUNAI_CP_URL`, then the
default. `ca_file` is used for runai-tui's own API reads; the `runai` CLI and
`kubectl` use their own trust settings.

## Run:ai assets in the form

The form reads these from Run:ai, refreshing them every `poll.assets` seconds,
and only offers the ones that belong to the form's project (or are shared
with it):

| Field | Run:ai asset | What it does |
|---|---|---|
| Template | workload templates | fills in environment, compute resource, data sources, command and env vars |
| Environment | environments | image, command, run-as-me |
| Compute resource | compute resources | CPU, memory, GPU (whole or fraction), large /dev/shm |
| Data sources | PVC, NFS, host path, config map | volumes and mounts (Space toggles, Enter accepts) |

Fields with no assets behind them are hidden. Changing CPU, memory or GPU by
hand sets Compute back to `(custom)`. Git and S3 data sources can't be
mounted from a plain manifest; the form warns about them and you can use the
Run:ai UI instead. For CLI workspaces, data sources become `--existing-pvc`,
`--nfs` and `--host-path` flags. Image pull secrets don't apply to CLI
workspaces; use an environment that has credentials.

If your Run:ai version serves these lists from other paths, change them under
`api:`. A list that returns an error is treated as empty, except
environments.

## Bind mounts

These are host paths mounted into the container as `hostPath` volumes. They
are useful for making your users and groups resolve inside images
(`/etc/passwd`, `/etc/group`), and for shared software or datasets.

```yaml
site:
  bind_mounts:
    - {name: passwd, host_path: /etc/passwd, type: File, default: true}
    - {name: group, host_path: /etc/group, type: File, default: true}
    - {name: tools, host_path: /opt/site-tools, mount_path: /opt/tools}
    - {name: datasets, host_path: /srv/datasets, mount_path: /data, read_only: false}
```

| Key | Default | |
|---|---|---|
| `host_path` | (required) | absolute path **on the compute node** |
| `mount_path` | `host_path` | where it appears in the container |
| `read_only` | `true` | `false` mounts it read-write |
| `type` | `""` (no check) | a Kubernetes hostPath type: `File`, `Directory`, `FileOrCreate`, ... |
| `default` | `false` | ticked in every new form |
| `name` | `host_path` | the label in the form |

In the form, **Bind mounts** is a checklist of these (Space toggles).
Researchers add their own under **Extra bind mounts**: `Enter` opens a list
that grows as you add rows: `a` types a new one in place (host path with Tab
completion, then the container path),
`e` edits, `Space` switches read-only and read-write, and `d` deletes. In
presets they are stored as `extra_binds: "/opt/tools:/tools, /scratch2:/s:rw"`.
Both kinds are read-only unless set otherwise. A path mounted twice (for example a bind on `/home` while "Run as
me" also mounts it) gets a warning, because Kubernetes rejects the pod. CLI
workspaces get `--host-path path=...,mount=...[,readwrite]`.

The host path is read on the **node the pod runs on**, not the login node.
`/etc/passwd` only helps if the compute nodes have your users in it; with
SSSD/LDAP-only nodes it will not list them. hostPath volumes also have to be
allowed by the cluster's pod security settings (the "Run as me" home mount
uses one too).

## Form defaults and presets

```yaml
form:
  default_environment: ubuntu
  fallback_image: "{registry}/ubuntu:24.04"   # default ubuntu:24.04
  start_preset: default            # if a preset of this name exists, new forms start from it
  priorities: [very-low, low, medium-low, medium, medium-high, high, very-high]
  defaults: {kind: training, cpu: "0.5", memory: 1Gi, gpu: none, run_as_me: true, ttl: "3600"}
```

**Presets** are saved form values. Press `P` in the form to load one, save
the current form as one, or delete one of yours. Yours are stored in
`~/.config/runai-tui/presets/<name>.yaml`; administrators can publish
read-only ones in `/etc/runai-tui/presets/`, and one of yours with the same
name wins. A preset only stores the fields that differ from an empty form,
and never the workload name. Save one called `default` and every new form
starts from it.

## Refreshing

```yaml
poll:
  idle: 6          # seconds between refreshes
  busy: 2          # while something you submitted is in flight
  logged_out: 15
  scope: project   # project: refresh only the shown project (and projects with
                   #          workloads in flight) every cycle; all: every project, every cycle
  full_every: 60   # with scope project: refresh every project this often
  page_size: 500
  max_workloads: 5000
```

Workloads are fetched a page at a time, so the list isn't cut off at 500.

## Keys and colours

```yaml
keys:
  delete: X        # action: key (one character, "ctrl-x" or "space")
  shell: s
theme:
  brand: 39        # fg
  head: white on 22   # fg on bg; names (black, red, green, yellow, blue, magenta, cyan,
                      # white, default) or 0-255
```

The actions are listed by `runai-tui config` and in `?` help, which always
shows the keys in use. A key bound to two actions that are active at the
same time, or taken from movement keys (`1-4 j k Tab Enter Esc Space`, plus
`h l q` in the form), is reported and that binding keeps its default. Theme
styles: `head ok warn info err sel brand mag dim tab`. Colours the terminal
can't show (256-colour numbers on an 8-colour terminal) keep the built-in
ones.

## Hooks

```yaml
hooks:
  pre_submit:
    - /etc/runai-tui/hooks/add-labels   # manifest on stdin; non-empty stdout replaces it
  post_submit:
    - 'logger -t runai-tui "submitted $RUNAI_TUI_NAMES in $RUNAI_TUI_PROJECT"'
```

Hooks are `sh -c` commands run as you, and they apply to every submission:
from the form, `F`, and `runai-tui submit`.

- `pre_submit` gets the YAML file on stdin. If it prints something, that is
  submitted instead, and the changed copy is kept in
  `~/.local/state/runai-tui/submitted/*-hooked-*.yaml`. A non-zero exit
  rejects the submission and shows its stderr. For CLI workspaces stdin is
  empty and `$RUNAI_TUI_ARGV` holds the command as JSON. Variables:
  `RUNAI_TUI_PROJECT`, `RUNAI_TUI_LABEL`, `RUNAI_TUI_FILE`.
- `post_submit` runs after a successful submit. Variables:
  `RUNAI_TUI_PROJECT`, `RUNAI_TUI_NAMES`, `RUNAI_TUI_FILE`,
  `RUNAI_TUI_METHOD`. If it fails, that is noted in the Activity line.

Each run is written to the action log (`!`).

## Plugins

A plugin is a Python file in `/etc/runai-tui/plugins/` or
`~/.config/runai-tui/plugins/` that defines `register(api)`. A plugin and its
directory must be owned by you or root and must not be writable by group or
others; otherwise it isn't loaded and Setup says why. Set
`plugins: {enabled: false}` to load none.

```python
# ~/.config/runai-tui/plugins/gpu.py
def register(api):
    h = api.app                      # the runai-tui module: RUNAI, KUBECTL, cmd(), CFG, ...

    # a key on the selected workload (Workloads and Activity tabs)
    api.action("smi", "G", "nvidia-smi in the workload", lambda tui, w: tui.external(
        f"nvidia-smi in {w['name']}",
        [h.RUNAI, "workload", "exec", w["name"], "-p", w["projectName"], "--", "nvidia-smi"]))

    # an extra line on the Setup tab
    api.check(lambda sess, state: api.Check(
        "scratch", "Scratch filesystem", "ok" if h.os.path.isdir("/scratch") else "warn", "/scratch"))

    # change every manifest the form builds (it shows in the preview)
    def label(man, values):
        man["metadata"].setdefault("labels", {})["example.com/cost-center"] = "research"
        return man
    api.manifest(label)
```

- `api.action(name, key, label, fn, where="workload")`: `where` is
  `workload` (Workloads and Activity; `fn(tui, workload)`), `workloads`,
  `activity`, `setup` or `global` (`fn(tui, None)`). You can remap it with
  `keys: {plugin.<file>.<name>: K}`. Useful `tui` methods: `flash(msg)`,
  `external(title, argv)`, `pager(title, text)`, `prompt(label)`,
  `confirm(question)`, `pick(title, options)`.
- `api.check(fn)`: `fn(sess, state)` returns a `Check(key, title, "ok"|"warn"|"fail", detail)`
  or a list of them.
- `api.manifest(fn)`: `fn(manifest, form_values)` returns the manifest. It
  only applies to manifests the form builds, not to CLI workspaces or YAML
  files; use a `pre_submit` hook for those.
