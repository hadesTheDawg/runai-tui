# runai-tui (`runai-tui`)

A terminal UI for **researchers** over the Run:ai CLI, run on the login node
as the user with the user's own Run:ai login. It handles setup and quota,
submits workloads and follows them, shells into them, and saves them as YAML.
Everything it changes goes through `runai` or `kubectl`, and every command is
logged, so it can do nothing the user couldn't do by hand.

- **Setup**: a checklist with fixes for the CLI URL, `runai login`, the
  default project, `~/.kube/config`, `runai kubeconfig set`, the kubectl
  namespace, and the example manifests (`runai-tui doctor --fix`).
- **Overview**: identity, per-node-pool GPU quota vs. allocation for each
  project, the user's own usage, and workloads in flight.
- **Submit**: a form (training / interactive / service / workspace) that
  writes a manifest and submits it, plus batch submit of YAML files, with
  progress bars from Submitted to Running/Completed.
- **Workloads**: details, shell (`runai workload exec -it`), logs, describe,
  port-forward, delete, and save as a re-submittable YAML manifest.
- **CLI mode**: `status`, `ls`, `doctor`, `submit`, `watch`, `save`, `shell`, `config`, `theme`, `quota`.
- **Attach** to a workload's main program (`t`), or submit and attach/shell in once it runs (`G` in the form).
- **PVCs**: browse the project's claims with sizes (`v`) and mount existing ones from the form.
- **Save to Run:ai** (`M` in the form): the form as a workload template, environment, compute resource or
  data source, visible in the Run:ai UI.
- **Quota** by department and project (tab 4) and **AI applications** (tab 5: list, describe, submit, delete).
- **Colour themes**: Dracula, One Dark Pro, Monokai, Solarized Dark built in, with live
  preview (`T`); import your own VS Code theme `.json`.
- **Customizable**: site values, Run:ai assets in the form (templates, compute
  resources, data sources), form presets, key bindings, colours, submit hooks
  and plugins, from `/etc/runai-tui/config.yaml` and
  `~/.config/runai-tui/config.yaml` (`runai-tui config` shows what's in use).

User guide: [`docs/howto.md`](docs/howto.md). Customizing:
[`docs/customizing.md`](docs/customizing.md).

## Requirements

- Python 3.9+ with PyYAML (`python3-pyyaml`); curses from the stdlib.
- `runai` CLI (tested against Run:ai 3.0.20) and `kubectl` on `PATH`.
- A Run:ai control plane the CLI can log in to. The URL comes from the CLI
  config (`runai config set --cp-url=...`), `--cp-url` in a profile hook
  (`/etc/profile.d/runai-cli.sh`), `$RUNAI_CP_URL`, or `site.control_plane` in
  the configuration.
- Optional: kube-apiserver OIDC for `kubectl` with the Run:ai login, and a
  kubeconfig skeleton and example manifests in `/usr/local/share/runai`.

## Install

```sh
./install.sh root@<login-node>   # from a workstation: scp + install over ssh
./install.sh                     # or on the login node itself, as root
```

Both put the script at `/usr/local/bin/runai-tui`. Site-wide settings go in
`/etc/runai-tui/config.yaml` ([`docs/customizing.md`](docs/customizing.md)).

## Testing

`RUNAI_TUI_SCREEN=<file>` writes a plain-text copy of every frame, so the UI
can be driven headlessly through a pty. To test as a site user without
touching their real config, point `RUNAI_CLI_CONFIG_PATH`, `KUBECONFIG` and
`HOME` at a temp dir.
