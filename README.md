# runai-tui (`htd-runai`)

A terminal UI for **researchers** over the Run:ai CLI, run on the login node
as the user with the user's own Run:ai login. It handles setup and quota,
submits workloads and follows them, shells into them, and saves them as YAML.
Everything it changes goes through `runai` or `kubectl`, and every command is
logged, so it can do nothing the user couldn't do by hand.

- **Setup**: a checklist with fixes for the CLI URL, `runai login`, the
  default project, `~/.kube/config`, `runai kubeconfig set`, the kubectl
  namespace, and the example manifests (`htd-runai doctor --fix`).
- **Overview**: identity, per-node-pool GPU quota vs. allocation for each
  project, the user's own usage, and workloads in flight.
- **Submit**: a form (training / interactive / service / workspace) that
  writes a manifest and submits it, plus batch submit of YAML files, with
  progress bars from Submitted to Running/Completed.
- **Workloads**: details, shell (`runai workload exec -it`), logs, describe,
  port-forward, delete, and save as a re-submittable YAML manifest.
- **CLI mode**: `status`, `ls`, `doctor`, `submit`, `watch`, `save`, `shell`.

User guide: [`docs/howto.md`](docs/howto.md).

## Requirements

- Python 3.9+ with PyYAML (`python3-pyyaml`); curses from the stdlib.
- `runai` CLI (tested against Run:ai 3.0.20) and `kubectl` on `PATH`.
- Site setup from [rke2_deploy_ww](https://github.com/hadesTheDawg/wwrke2runai-local):
  `--cp-url` in `/etc/profile.d/runai-cli.sh`, kube-apiserver OIDC, and the
  skeleton kubeconfig and examples in `/usr/local/share/runai`
  (`runai/48-user-access.sh`).

## Install

```sh
./install.sh root@192.168.65.2   # from the workstation: scp + install on ww1
./install.sh                     # or on the login node itself, as root
```

Both put the script at `/usr/local/bin/htd-runai`. `runai/48-user-access.sh`
in rke2_deploy_ww installs its own copy the same way.

## Testing

`HTD_RUNAI_SCREEN=<file>` writes a plain-text copy of every frame, so the UI
can be driven headlessly through a pty. To test as a site user without
touching their real config, point `RUNAI_CLI_CONFIG_PATH`, `KUBECONFIG` and
`HOME` at a temp dir.
