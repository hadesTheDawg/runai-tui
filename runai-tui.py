#!/usr/bin/env python3
"""runai-tui: a terminal UI for researchers over the Run:ai CLI (login node).

Everything runs as you, with your own Run:ai login. Reads come from the Run:ai API with your CLI token
(`runai auth get-token`); every change goes through `runai` or `kubectl`, so
the TUI never does anything you could not do (or repeat) by hand:

  setup    checklist + fixes: CLI URL, `runai login`, default project,
           ~/.kube/config, `runai kubeconfig set`, kubectl namespace, examples
  overview who you are, your projects' GPU quota vs. allocation per node pool,
           your own usage, workloads in flight
  submit   a form that writes a Kubernetes manifest (or YAML files you pick)
           -> `runai workload submit -f` (one object) / `kubectl apply -f`
  follow   progress bars from submission to Running/Completed, per workload
  shell    `runai workload exec -it` into a running pod; logs, describe,
           port-forward, delete
  save     a workload back to a re-submittable YAML manifest

Usage (on the login node, as yourself):

  runai-tui                         TUI
  runai-tui status                  identity, quota bars, your workloads
  runai-tui ls [-A]                 workloads (all users of the project with -A)
  runai-tui doctor [--fix]          setup checklist (and apply the safe fixes)
  runai-tui submit FILE... [-p P]   submit manifests, then follow them
  runai-tui watch [NAME...]         progress bars until everything settles
  runai-tui save NAME [-o FILE]     workload -> YAML manifest
  runai-tui shell NAME              shell into a workload
  runai-tui config [--defaults]     configuration in use (keys, site values, hooks, plugins)

Configuration: /etc/runai-tui/config.yaml (site), ~/.config/runai-tui/config.yaml (yours),
RUNAI_TUI__SECTION__KEY=value; form presets in ~/.config/runai-tui/presets/; plugins in
/etc/runai-tui/plugins and ~/.config/runai-tui/plugins.

Manifests submitted from the form are kept in ~/.local/state/runai-tui/
submitted/; actions are logged to ~/.local/state/runai-tui/runai-tui.log.
"""
import argparse
import base64
import calendar
import curses
import datetime
import getpass
import glob
import grp
import json
import os
import re
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

# --- configuration: built-in defaults < /etc/runai-tui < ~/.config/runai-tui < environment ---------
# `runai-tui config` shows the merged result and where each value came from;
# `runai-tui config --defaults` prints this table as a starting point.
DEFAULTS = {
    "site": {
        "name": "{group} Researcher",                # header: "▲ <name> · Run:ai"; {group} {user} {email}
        "control_plane": "",                         # used when the CLI config and the profile hook have none
        "ca_file": "",                               # PEM bundle for the control plane; "" = system trust store
        "share_dir": "/usr/local/share/runai",       # optional: kubeconfig skeleton + examples/*.yaml
        "profile_hook": "/etc/profile.d/runai-cli.sh",
        "registry": "",                              # your registry host, e.g. registry.example.com
        "allowed_registries": ["docker.io/library"],  # pulled without a warning, besides the registry
        "offline_nodes": False,                      # warn about images outside the registry (air-gapped nodes)
        "home": {"host_path": "/home", "mount_path": "/home"},   # "Run as me": host_path "" mounts nothing
        "node_pool_label": "runai/node-pool",
        "gpu_fraction_annotation": "gpu-fraction",
        "min_site_uid": 1000,                        # lower UIDs are local accounts, not site users
        "pull_secrets": [],                          # imagePullSecrets offered in the form
        # host paths offered as "Bind mounts" in the form, e.g.
        #   - {name: passwd, host_path: /etc/passwd, type: File, default: true}
        #   - {name: datasets, host_path: /srv/datasets, mount_path: /data, read_only: false}
        # mount_path defaults to host_path, read_only to true; type is a hostPath type (File,
        # Directory, ...; "" = no check); default: true ticks it in every new form
        "bind_mounts": [],
        "save_dir": "~/runai",
        "example_placeholders": {},                  # text in the examples to replace: {uid: "1234", gid: .., user: ..}
    },
    "api": {
        "projects": "/api/v1/org-unit/projects",
        "workloads": "/api/v1/workloads",
        "environments": "/api/v1/asset/environment",
        "computes": "/api/v1/asset/compute",
        "datasources": "/api/v1/asset/datasource",      # create: <this>/host-path, /pvc
        "templates": "/api/v1/workload-templates",       # create: <this>/trainings, /workspaces
        "departments": "/api/v1/org-unit/departments",
        "applications": "/api/v1/ai-applications",
    },
    "poll": {
        "idle": 6, "busy": 2, "logged_out": 15,      # seconds between refreshes
        "assets": 300,                               # environments, computes, data sources, templates
        "scope": "project",                          # project: fast refresh of the shown project only; all
        "full_every": 60,                            # with scope project: every project this often
        "page_size": 500, "max_workloads": 5000,
    },
    "form": {
        "default_environment": "ubuntu",
        "fallback_image": "ubuntu:24.04",            # {registry} = site.registry
        "start_preset": "default",                   # a preset of this name, if any, fills a new form
        "priorities": ["very-low", "low", "medium-low", "medium", "medium-high", "high", "very-high"],
        "defaults": {"kind": "training", "command": "echo hello from $(hostname); nproc; sleep 30",
                     "gpu": "none", "gpu_amount": "0.5", "pool": "any", "cpu": "0.5", "memory": "1Gi",
                     "run_as_me": True, "scratch": "", "shm": "", "env_vars": "", "completions": "1",
                     "ttl": "3600", "priority": "default", "preempt": "default"},
    },
    "hooks": {
        "pre_submit": [],    # sh -c commands: manifest on stdin, non-empty stdout replaces it, exit != 0 rejects
        "post_submit": [],   # sh -c commands after a successful submit (RUNAI_TUI_* variables, no stdin)
    },
    "plugins": {"enabled": True, "dirs": ["/etc/runai-tui/plugins", "~/.config/runai-tui/plugins"]},
    "keys": {},              # action -> key, e.g. {delete: X, shell: s}; see `?` in the TUI for actions
    "theme": {},             # name: a theme (T in the TUI); style -> "fg" or "fg on bg" (names, 0-255, #rrggbb)
}
CONFIG_FILES = ["/etc/runai-tui/config.yaml", "~/.config/runai-tui/config.yaml"]
PRESET_DIRS = ["/etc/runai-tui/presets", "~/.config/runai-tui/presets"]     # the last one is writable


def _merge(base, over, src, sources, path=""):
    for k, v in over.items():
        key = f"{path}{k}"
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v, src, sources, key + ".")
        else:
            base[k] = v
            sources[key] = src


def load_config():
    """-> (config, {dotted key: source}, [errors]). Env: RUNAI_TUI_CONFIG=file adds one more file,
    RUNAI_TUI__SECTION__KEY=value (YAML) sets a single value."""
    cfg, sources, errors = json.loads(json.dumps(DEFAULTS)), {}, []
    files = CONFIG_FILES + ([os.environ["RUNAI_TUI_CONFIG"]] if os.environ.get("RUNAI_TUI_CONFIG") else [])
    for path in files:
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                raise ValueError("not a mapping")
            _merge(cfg, data, path, sources)
        except (OSError, ValueError, yaml.YAMLError) as e:
            errors.append(f"{path}: {str(e).splitlines()[0]}")
    for name, raw in sorted(os.environ.items()):
        if name.startswith("RUNAI_TUI__"):
            parts = [p.lower() for p in name[len("RUNAI_TUI__"):].split("__") if p]
            try:
                val = yaml.safe_load(raw)
            except yaml.YAMLError:
                val = raw
            for p in reversed(parts):
                val = {p: val}
            if parts:
                _merge(cfg, val, f"${name}", sources)
    for k in ("pre_submit", "post_submit"):
        if isinstance(cfg["hooks"].get(k), str):
            cfg["hooks"][k] = [cfg["hooks"][k]] if cfg["hooks"][k].strip() else []
    return cfg, sources, errors


CFG, CFG_SOURCES, CONFIG_ERRORS = load_config()
SITE = CFG["site"]
SHARE = SITE["share_dir"]
SKEL_KUBECONFIG = f"{SHARE}/kubeconfig"
EXAMPLES = f"{SHARE}/examples"
PROFILE_HOOK = SITE["profile_hook"]
DEFAULT_CP = SITE["control_plane"]
STATE = os.path.expanduser("~/.local/state/runai-tui")
LOG = os.path.join(STATE, "runai-tui.log")
SUBMITTED = os.path.join(STATE, "submitted")
SAVE_DIR = os.path.expanduser(SITE["save_dir"])
# by name when on PATH: Run:ai records the command line (shown as "cli" in the UI)
RUNAI = "runai" if shutil.which("runai") else "/usr/local/bin/runai"
KUBECTL = "kubectl" if shutil.which("kubectl") else "/usr/local/bin/kubectl"
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
MEM_RE = re.compile(r"^\d+(\.\d+)?(Mi|Gi|Ti|M|G|T)$")
WORKLOAD_KINDS = {"Job", "Deployment", "StatefulSet", "Pod", "ReplicaSet", "CronJob"}
PRIORITIES = CFG["form"]["priorities"]
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# --- key bindings: (action, default key, where, help) -----------------------------------------
# where: global (all tabs), workload (Workloads + Activity), workloads, activity, setup, form
ACTIONS = [
    ("new", "n", "global", "new workload (form -> YAML -> submit)"),
    ("submit_files", "F", "global", "submit YAML file(s)"),
    ("project", "p", "global", "switch project"),
    ("refresh", "r", "global", "refresh now (re-check on Setup)"),
    ("log", "!", "global", "action log"),
    ("help", "?", "global", "this help"),
    ("quit", "q", "global", "quit"),
    ("login", "i", "global", "log in"),
    ("theme", "T", "global", "colour theme (preview, switch, import)"),
    ("pvcs", "v", "global", "PVCs in the project, with sizes"),
    ("shell", "s", "workload", "shell into it (also Enter on Workloads)"),
    ("attach", "t", "workload", "attach to its main process (the program's own terminal)"),
    ("logs", "l", "workload", "logs"),
    ("follow_logs", "L", "workload", "follow logs (Ctrl-C returns)"),
    ("describe", "d", "workload", "describe (events)"),
    ("save", "y", "workload", "save as YAML"),
    ("port_forward", "f", "workload", "port-forward"),
    ("copy_url", "u", "workload", "copy URL"),
    ("copy_name", "c", "workload", "copy name"),
    ("suspend", "z", "workload", "suspend / resume"),
    ("delete", "x", "workload", "delete"),
    ("toggle_mine", "a", "workloads", "mine / everyone in the project"),
    ("filter", "/", "workloads", "filter"),
    ("forget_done", "D", "activity", "forget finished entries"),
    ("fix_all", "A", "setup", "fix everything safe"),
    ("logout", "o", "setup", "log out"),
    ("app_describe", "d", "apps", "describe the application (also Enter)"),
    ("app_delete", "x", "apps", "delete the application"),
    ("app_submit", "S", "apps", "submit an AI application (Helm chart)"),
    ("form_submit", "S", "form", "submit"),
    ("form_write", "W", "form", "write YAML (or a script for workspaces)"),
    ("form_edit", "E", "form", "edit in $EDITOR, then submit"),
    ("form_presets", "P", "form", "presets: load / save / delete"),
    ("form_go", "G", "form", "submit, then attach or shell in once it runs"),
    ("form_runai", "M", "form", "save to Run:ai: environment, compute resource, data source or template"),
]
# keys a binding may not take: tab numbers, movement, Enter/Tab/Esc/space
RESERVED = {"main": set("123456jk\t\n\r\x1b "), "form": set("jkhl\t\n\r\x1b q")}
SCOPE_TABS = {"global": {0, 1, 2, 3, 4, 5}, "workload": {1, 2}, "workloads": {1}, "activity": {2}, "apps": {4},
              "setup": {5}}                     # tab numbers: Overview Workloads Activity Quota Apps Setup


def parse_key(spec):
    """'x', 'ctrl-x', 'space' -> the character getch/get_wch returns (or None)."""
    s = str(spec)
    if s.lower().startswith("ctrl-") and len(s) == 6:
        return chr(ord(s[-1].lower()) & 0x1f)
    return {"space": " "}.get(s.lower(), s if len(s) == 1 else None)


def key_name(ch):
    if ch and ord(ch) < 32:
        return f"^{chr(ord(ch) + 64)}"
    return {" ": "Space"}.get(ch, ch or "")


def resolve_keys(user, extra=()):
    """-> ({action: char}, [errors]). A binding that is invalid, reserved or clashes keeps its default."""
    acts = list(ACTIONS) + list(extra)
    default = {a: k for a, k, _, _ in acts}
    keys, errors = dict(default), []
    where = {a: w for a, _, w, _ in acts}
    for a, spec in (user or {}).items():
        ch = parse_key(spec)
        if a not in keys:
            errors.append(f"keys.{a}: no such action")
        elif ch is None:
            errors.append(f"keys.{a}: '{spec}' is not a key (one character or ctrl-x)")
        elif ch in RESERVED["form" if where[a] == "form" else "main"]:
            errors.append(f"keys.{a}: '{spec}' is reserved for movement")
        else:
            keys[a] = ch
    def clash(a, b):
        return a != b and keys[a] == keys[b] and (where[a] == where[b] == "form" or (
            "form" not in (where[a], where[b]) and SCOPE_TABS[where[a]] & SCOPE_TABS[where[b]]))

    for a in [a for a in keys if keys[a] != default[a]]:       # a remap that clashes is undone
        other = next((b for b in keys if clash(a, b)), None)
        if other:
            errors.append(f"keys.{a}: '{key_name(keys[a])}' is already {other}; kept '{key_name(default[a])}'")
            keys[a] = default[a]
    for a in keys:                                              # still clashing: a remap took a default
        other = next((b for b in keys if clash(a, b)), None)
        if other and keys[a] != default[a]:
            errors.append(f"keys.{a}: clashes with {other}; kept '{key_name(default[a])}'")
            keys[a] = default[a]
    return keys, errors


KEYS, _key_errors = resolve_keys(CFG.get("keys"))
CONFIG_ERRORS += _key_errors
SHELL_CMD = ('cd "${HOME:-/}" 2>/dev/null; '
             'if command -v bash >/dev/null 2>&1; then exec bash -l; else exec sh -l; fi')

# workload phases -> progress stages
JOB_STAGES = ["Submitted", "Pending", "Starting", "Running", "Done"]
SVC_STAGES = ["Submitted", "Pending", "Starting", "Ready"]
PHASE_STAGE = {"Creating": 0, "Submitted": 0, "Pending": 1, "Initializing": 2, "Resuming": 2,
               "Updating": 2, "Running": 3, "Degraded": 3, "Completed": 4, "Succeeded": 4}
DONE_OK = {"Completed", "Succeeded"}
FAILED = {"Failed", "Deleted", "Error"}
STOPPED = {"Stopped", "Stopping", "Deleting", "Terminating", "Suspended"}
TERMINAL = DONE_OK | FAILED | {"Stopped", "Suspended"}


def settled(w):
    """Nothing more will happen by itself: finished, failed, stopped, or a service that is up."""
    return w.get("phase") in TERMINAL or (w.get("phase") == "Running" and is_service(w))


# --- small helpers ------------------------------------------------------------
def cmd(argv, timeout=60, env=None, stdin=None):
    """-> (rc, stdout, stderr); never raises for a missing binary or timeout."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, **(env or {})), input=stdin)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(argv[:3])}: timed out after {timeout}s"


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-Z]|\x1b[=>]")


def plain_lines(*texts):
    """CLI output without colours/spinners: the CLI redraws spinners with \r and escapes."""
    out = []
    for t in texts:
        for l in ANSI_RE.sub("", t or "").replace("\r", "\n").splitlines():
            l = l.strip().lstrip("|/-\\ ").strip() if l.strip()[:2] in ("| ", "/ ", "- ", "\\ ") else l.strip()
            if l and not l.endswith(":"):
                out.append(l)
    return out


def last_line(*texts):
    lines = plain_lines(*texts)
    return lines[-1][:200] if lines else ""


def first_error(*texts):
    lines = plain_lines(*texts)
    for i, l in enumerate(lines):
        if l.startswith("Error"):
            if i + 1 < len(lines) and len(l) < 60:      # "Error: failed to submit." + the reason
                l = f"{l} {lines[i + 1]}"
            return l[:200]
    return (lines[0] if lines else "")[:200]


def log_action(title, argv=None, rc=None, output=""):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"\n=== {datetime.datetime.now():%F %T} {title}\n")
            if argv:
                f.write("$ " + " ".join(shlex.quote(a) for a in argv) + "\n")
            if output:
                f.write(output.rstrip() + "\n")
            if rc is not None:
                f.write(f"=== exit {rc}\n")
    except OSError:
        pass


def parse_ts(s):
    """RFC 3339 (any fraction length, Z or offset) -> epoch seconds."""
    if not s:
        return None
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?$", str(s))
    if not m:
        return None
    t = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
    t += float("0." + m.group(2)) if m.group(2) else 0
    tz = m.group(3)
    if tz and tz != "Z":
        t -= (1 if tz[0] == "+" else -1) * (int(tz[1:3]) * 3600 + int(tz[-2:]) * 60)
    return t


def dur(sec):
    if sec is None:
        return "-"
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    if sec < 86400:
        return f"{sec // 3600}h{sec % 3600 // 60:02d}m"
    return f"{sec // 86400}d{sec % 86400 // 3600:02d}h"


def num(x, nd=2):
    if x in (None, -1):
        return "∞" if x == -1 else "-"
    return f"{x:.{nd}f}".rstrip("0").rstrip(".") if isinstance(x, float) else str(x)


MEM_UNITS = {"": 1, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
             "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}


def mem_bytes(s):
    if s in (None, ""):
        return 0
    if isinstance(s, (int, float)):
        return s
    m = re.match(r"^([\d.]+)\s*([kKMGT]i?)?B?$", str(s).strip())
    return float(m.group(1)) * MEM_UNITS.get(m.group(2) or "", 1) if m else 0


def human_mem(b):
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.1f}Gi"
    return f"{b / 1024 ** 2:.0f}Mi"


def jwt_claims(tok):
    try:
        part = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def osc52(text):
    """Copy to the local clipboard through the terminal (works over ssh)."""
    sys.stdout.write(f"\033]52;c;{base64.b64encode(text.encode()).decode()}\a")
    sys.stdout.flush()


OWNED = os.path.join(STATE, "owned.json")


def load_owned():
    try:
        with open(OWNED) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def remember_owned(project, name):
    owned = load_owned()
    owned.add(f"{project}/{name}")
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(OWNED + ".tmp", "w") as f:
            json.dump(sorted(owned)[-500:], f)
        os.replace(OWNED + ".tmp", OWNED)
    except OSError:
        pass


def is_service(w):
    return w.get("category") in ("Build", "Deploy") or w.get("type") in (
        "Workspace", "Inference", "Deployment", "StatefulSet")


def wl_gpu(w):
    return (w.get("allocatedResources") or {}).get("gpu") or 0


def wl_gpu_req(w):
    r = w.get("workloadRequestedResources") or {}
    g = r.get("gpu") or {}
    return g.get("request") or g.get("limit") or 0


def pending_reason(w):
    for c in w.get("conditions") or []:
        msg = c.get("message") or c.get("reason")
        if msg and c.get("status", "True") != "False":
            return msg.replace("\n", " ")
    return ""


# --- site and CLI configuration --------------------------------------------------
def cli_config_dir():
    return os.environ.get("RUNAI_CLI_CONFIG_PATH") or os.path.expanduser("~/.runai")


def read_cli_config():
    try:
        with open(os.path.join(cli_config_dir(), os.environ.get("RUNAI_CLI_CONFIG_FILE", "config.json"))) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def cli_cp_url(cfg=None):
    cfg = cfg if cfg is not None else read_cli_config()
    return ((cfg.get("control_plane") or {}).get("url") or "").rstrip("/")


def cli_project(cfg=None):
    cfg = cfg if cfg is not None else read_cli_config()
    return ((cfg.get("cluster") or {}).get("project") or {}).get("name") or ""


def site_url():
    """The control plane: site.control_plane when a config file sets it, else what this site's
    profile hook points the CLI at, else $RUNAI_CP_URL ("" when none of them says)."""
    if "site.control_plane" in CFG_SOURCES:
        return DEFAULT_CP.rstrip("/")
    try:
        m = re.search(r"--cp-url=(\S+)", open(PROFILE_HOOK).read())
        if m:
            return m.group(1).rstrip("/")
    except OSError:
        pass
    return os.environ.get("RUNAI_CP_URL", DEFAULT_CP).rstrip("/")


ENV_NOTE = ""      # set by sanitize_env(), shown in Setup


def sanitize_env():
    """Drop an inherited KUBECONFIG you cannot read. Typical case: `su <user>` (no '-')
    from root keeps root's KUBECONFIG (a cluster-admin file, root-only); `runai kubeconfig set` then fails with 'permission denied'. Without
    it, runai and kubectl use your own ~/.kube/config, as after a real login."""
    global ENV_NOTE
    kc = os.environ.get("KUBECONFIG", "")
    bad = [p for p in kc.split(":") if p and os.path.exists(p) and not os.access(p, os.R_OK)]
    if bad:
        os.environ.pop("KUBECONFIG")
        ENV_NOTE = (f"ignored KUBECONFIG={kc}: {', '.join(bad)} is not readable by you (inherited from "
                    f"another user, e.g. `su` without '-'); using ~/.kube/config")
    return ENV_NOTE


def kubeconfig_path():
    kc = os.environ.get("KUBECONFIG", "")
    return kc.split(":")[0] if kc else os.path.expanduser("~/.kube/config")


def read_kubeconfig():
    try:
        with open(kubeconfig_path()) as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


def kube_current(kc):
    """-> (context dict, user dict) of the current context."""
    name = kc.get("current-context")
    ctx = next((c.get("context") or {} for c in kc.get("contexts") or [] if c.get("name") == name), {})
    user = next((u.get("user") or {} for u in kc.get("users") or [] if u.get("name") == ctx.get("user")), {})
    return ctx, user


# --- Run:ai session: token + REST reads ---------------------------------------------
class NotLoggedIn(Exception):
    pass


class ApiError(Exception):
    pass


class Session:
    def __init__(self):
        self.lock = threading.Lock()
        self.tok, self.exp, self.claims = None, 0, {}
        ca = os.path.expanduser(SITE.get("ca_file") or "")
        try:
            self.ssl = ssl.create_default_context(cafile=ca or None)
        except (OSError, ssl.SSLError) as e:
            CONFIG_ERRORS.append(f"site.ca_file {ca}: {e}; using the system trust store")
            self.ssl = ssl.create_default_context()
        self.server_skew = None            # seconds, server minus local
        self.base = cli_cp_url() or site_url()

    def reset(self):
        with self.lock:
            self.tok, self.exp, self.claims = None, 0, {}
        self.base = cli_cp_url() or site_url()

    def token(self, force=False):
        with self.lock:
            if not force and self.tok and self.exp - time.time() > 60:
                return self.tok
            rc, out, err = cmd([RUNAI, "auth", "get-token"], timeout=30)
            if rc != 0:
                self.tok, self.claims = None, {}
                raise NotLoggedIn(first_error(err, out) or "not logged in")
            try:
                tok = json.loads(out)["status"]["token"]       # ExecCredential
            except (ValueError, KeyError, TypeError):
                tok = out.strip().splitlines()[-1] if out.strip() else ""
            self.tok, self.claims = tok, jwt_claims(tok)
            self.exp = self.claims.get("exp", time.time() + 300)
            return tok

    @property
    def email(self):
        return self.claims.get("email") or self.claims.get("preferred_username") or ""

    def api(self, path, method="GET", body=None, retry=True):
        if not self.base:
            raise ApiError("no Run:ai URL: `runai config set --cp-url=<url>` or site.control_plane (Setup)")
        tok = self.token()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {tok}", "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, context=self.ssl, timeout=20) as r:
                raw = r.read()
                self._note_date(r.headers.get("Date"))
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry:
                with self.lock:
                    self.tok = None
                return self.api(path, method, body, False)
            text = e.read().decode(errors="replace")
            try:
                text = json.loads(text).get("message", text)
            except ValueError:
                pass
            raise ApiError(f"{e.code} {text.strip()[:160]}") from None
        except urllib.error.URLError as e:
            raise ApiError(f"{self.base}: {e.reason}") from None

    def _note_date(self, date):
        if date:
            try:
                srv = calendar.timegm(time.strptime(date, "%a, %d %b %Y %H:%M:%S GMT"))
                self.server_skew = srv - time.time()
            except ValueError:
                pass

    def probe_clock(self):
        try:
            req = urllib.request.Request(self.base + "/", method="HEAD")
            with urllib.request.urlopen(req, context=self.ssl, timeout=10) as r:
                self._note_date(r.headers.get("Date"))
        except urllib.error.HTTPError as e:
            self._note_date(e.headers.get("Date"))
        except Exception:
            pass
        return self.server_skew


# --- quota ----------------------------------------------------------------------------
def project_pools(p):
    """Per node pool: GPU quota (deserved), limit, allocation; CPU cores, memory bytes."""
    st = {s.get("nodePoolName"): s for s in (p.get("status") or {}).get("nodePoolQuotaStatuses") or []}
    out = []
    for r in p.get("resources") or []:
        name = (r.get("nodePool") or {}).get("name") or "-"
        g, s = r.get("gpu") or {}, st.get(name, {})
        alloc, nonp, req = s.get("allocated") or {}, s.get("allocatedNonPreemptible") or {}, s.get("requested") or {}
        cpu_q = (r.get("cpu") or {}).get("deserved")
        mem_q = (r.get("memory") or {}).get("deserved")
        out.append({
            "pool": name, "quota": g.get("deserved") or 0, "limit": g.get("limit"), "weight": g.get("overQuotaWeight"),
            "gpu": alloc.get("gpu") or 0, "gpu_np": nonp.get("gpu") or 0, "gpu_req": req.get("gpu") or 0,
            "cpu": (alloc.get("cpu") or 0) / 1000, "mem": (alloc.get("memory") or 0) * 1e6,      # millicores, MB
            "cpu_quota": cpu_q / 1000 if cpu_q not in (None, -1) else None,
            "mem_quota": mem_q * 1e6 if mem_q not in (None, -1) else None})
    return out


def pool_visible(q):
    return q["quota"] > 0 or q["gpu"] > 0 or q["gpu_req"] > 0 or (q["limit"] not in (None, -1, 0))


# --- shared state, filled by the poller thread ------------------------------------------
class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.version = 0
        self.logged_in = None
        self.error = ""
        self.projects, self.workloads = [], []
        self.envs, self.computes, self.datasources, self.templates = [], [], [], []
        self.want_quota = self.want_apps = False   # set while those tabs are shown: fetched only then
        self.departments, self.dept_error = None, ""
        self.apps, self.apps_error = None, ""
        self.scope = ""                  # project the TUI shows ("" = all): polled fast with poll.scope project
        self.pods = {}                   # workload id -> (time, [pods] | error str)
        self.focus_id = None
        self.history = {}                # workload id -> [(phase, first seen)]
        self.watch = {}                  # (project, name) -> {"added", "source", "done_at"}
        self.batch = None
        self.updated = 0
        self.me = ""
        self.owned = load_owned()        # "project/name" submitted from here (kubectl ones have no submitter)

    def mine(self, w):
        by = w.get("submittedBy")
        return by == self.me if by else f"{w.get('projectName')}/{w.get('name')}" in self.owned

    def project(self, name):
        return next((p for p in self.projects if p.get("name") == name), None)

    def find(self, project, name):
        return next((w for w in self.workloads if w.get("projectName") == project and w.get("name") == name), None)

    def add_watch(self, project, name, source="submitted"):
        with self.lock:
            self.watch[(project, name)] = {"added": time.time(), "source": source, "done_at": None}
            if source == "submitted":
                self.owned.add(f"{project}/{name}")
                remember_owned(project, name)

    def in_flight(self):
        with self.lock:
            return any(not settled(w) for w in self.watched()) or bool(
                self.batch and not self.batch.done)

    def watched(self):
        """Workloads shown in Activity: watched ones, in watch order (newest first)."""
        out = []
        for (proj, name), info in sorted(self.watch.items(), key=lambda kv: -kv[1]["added"]):
            w = self.find(proj, name)
            out.append(w if w else {"name": name, "projectName": proj, "phase": "Submitted"
                                    if time.time() - info["added"] < 60 else "Gone", "_ghost": True})
        return out


def fetch_workloads(sess, project=None):
    """All workloads (of one project), a page at a time, up to poll.max_workloads."""
    pc = CFG["poll"]
    size, cap = max(1, int(pc["page_size"])), int(pc["max_workloads"])
    out = []
    while len(out) < cap:
        q = {"limit": size, "offset": len(out)}
        if project:
            q["filterBy"] = f"projectName=={project}"
        page = (sess.api(f"{CFG['api']['workloads']}?{urllib.parse.urlencode(q)}") or {}).get("workloads") or []
        out += page
        if len(page) < size:
            break
    return out


def fetch_assets(sess, key):
    """An asset list (environments, computes, ...); [] when this Run:ai has no such API."""
    try:
        return (sess.api(CFG["api"][key]) or {}).get("entries") or []
    except ApiError:
        if key == "environments":
            raise
        return []


class Poller(threading.Thread):
    def __init__(self, sess, state):
        super().__init__(daemon=True)
        self.sess, self.state = sess, state
        self.ev = threading.Event()
        self.assets_at = 0
        self.full_at = 0

    def wake(self):
        self.ev.set()

    def run(self):
        pc = CFG["poll"]
        while True:
            self.poll()
            if self.state.logged_in is False:
                delay = pc["logged_out"]
            else:
                delay = pc["busy"] if self.state.in_flight() else pc["idle"]
            self.ev.wait(delay)
            self.ev.clear()

    def scoped(self):
        """Projects for a fast refresh, or None for all: the shown project plus those with
        watched workloads still in flight."""
        st = self.state
        if CFG["poll"]["scope"] != "project" or not st.scope or not self.full_at \
                or time.time() - self.full_at > CFG["poll"]["full_every"]:
            return None
        with st.lock:
            return {st.scope} | {p for (p, _), info in st.watch.items() if not info["done_at"]}

    def poll(self):
        st, sess = self.state, self.sess
        try:
            projects = (sess.api(CFG["api"]["projects"]) or {}).get("projects") or []
            scope = self.scoped()
            if scope is None:
                wls = fetch_workloads(sess)
                self.full_at = time.time()
            else:
                fresh = [w for p in sorted(scope) for w in fetch_workloads(sess, p)]
                with st.lock:
                    wls = [w for w in st.workloads if w.get("projectName") not in scope] + fresh
            assets = None
            if time.time() - self.assets_at > CFG["poll"]["assets"]:
                assets = {k: fetch_assets(sess, k) for k in ("environments", "computes", "datasources", "templates")}
                self.assets_at = time.time()
            deps = apps = None
            if st.want_quota:
                try:
                    deps = (sess.api(f"{CFG['api']['departments']}?limit=500&verbosity=verbose") or {}) \
                        .get("departments") or []
                except ApiError as e:
                    deps = str(e)
            if st.want_apps:
                try:
                    apps = (sess.api(f"{CFG['api']['applications']}?limit=500&verbosity=full") or {}) \
                        .get("applications") or []
                except ApiError as e:
                    apps = str(e)
            pods = None
            fid = st.focus_id
            if fid:
                try:
                    pods = (sess.api(f"{CFG['api']['workloads']}/{fid}/pods") or {}).get("pods") or []
                except ApiError as e:
                    pods = str(e)
            now = time.time()
            by_name = lambda e: (e.get("meta") or {}).get("name", "")
            with st.lock:
                st.me = sess.email
                st.projects = sorted(projects, key=lambda p: p.get("name", ""))
                st.workloads = sorted(wls, key=lambda w: w.get("createdAt") or "", reverse=True)
                if assets is not None:
                    st.envs = sorted(assets["environments"], key=by_name)
                    st.computes = sorted(assets["computes"], key=by_name)
                    st.datasources = sorted(assets["datasources"], key=by_name)
                    st.templates = sorted(assets["templates"], key=by_name)
                if fid:
                    st.pods[fid] = (now, pods)
                if deps is not None:
                    st.departments, st.dept_error = (None, deps) if isinstance(deps, str) else (deps, "")
                if apps is not None:
                    st.apps, st.apps_error = (None, apps) if isinstance(apps, str) else (
                        sorted(apps, key=lambda a: app_info(a)["name"]), "")
                for w in wls:
                    h = st.history.setdefault(w["id"], [])
                    if not h or h[-1][0] != w.get("phase"):
                        h.append((w.get("phase"), now))
                    if st.mine(w) and not settled(w):
                        key = (w.get("projectName"), w.get("name"))
                        if key not in st.watch:
                            st.watch[key] = {"added": now, "source": "seen", "done_at": None}
                for key, info in list(st.watch.items()):
                    w = st.find(*key)
                    if (w is None or settled(w)) and not info["done_at"] \
                            and now - info["added"] > 60:
                        info["done_at"] = now
                    if info["source"] == "seen" and info["done_at"] and now - info["done_at"] > 900:
                        del st.watch[key]
                st.logged_in, st.error, st.updated = True, "", now
        except NotLoggedIn as e:
            with st.lock:
                st.logged_in, st.error = False, str(e)
        except Exception as e:                 # API down, clock skew, ...
            with st.lock:
                st.error = str(e).split("\n")[0][:160]
        with st.lock:
            st.version += 1


# --- progress ---------------------------------------------------------------------------
def progress(w, history=None):
    """-> (fraction 0..1, colour, stages, current stage index, animate)."""
    phase = w.get("phase") or "Unknown"
    stages = SVC_STAGES if is_service(w) else JOB_STAGES
    n = len(stages) - 1
    if phase in DONE_OK:
        return 1.0, "ok", stages, n, False
    if phase in FAILED or phase == "Gone":
        last = 1
        for ph, _ in history or []:
            if ph in PHASE_STAGE:
                last = PHASE_STAGE[ph]
        return min(last, n) / n, "err", stages, min(last, n), False
    if phase in STOPPED:
        return 0.0, "dim", stages, 0, False
    st = min(PHASE_STAGE.get(phase, 0), n)
    if phase == "Running" and is_service(w):
        return 1.0, "ok", stages, n, False
    colour = {"Pending": "warn", "Degraded": "warn", "Running": "ok"}.get(phase, "info")
    return (st + 0.5) / n if st < n else 1.0, colour, stages, st, True


def bar_cells(width, frac, animate, tick):
    """-> list of (char, kind) where kind is 'fill', 'pulse' or 'empty'."""
    filled = int(round(frac * width))
    cells = [("█", "fill") if i < filled else ("░", "empty") for i in range(width)]
    if animate and 0 < filled <= width:
        span = max(3, width // 6)
        pos = filled - span + (tick % span)
        for i in range(max(0, pos), min(filled, pos + 2)):
            cells[i] = ("▓", "pulse")
    return cells


def quota_cells(width, value, quota, limit):
    """GPU bar: fill up to the quota in green, over-quota in yellow, ┆ at the quota."""
    full = limit if limit not in (None, -1, 0) else max(quota, value) * 1.25
    full = max(full, value, quota, 1e-9)
    filled = int(round(min(value, full) / full * width))
    qpos = int(round(quota / full * width)) if quota else -1
    cells = []
    for i in range(width):
        if i < filled:
            cells.append(("█", "ok" if i < qpos or quota <= 0 and value <= 0 else "warn"))
        elif i == qpos:
            cells.append(("┆", "info"))
        else:
            cells.append(("░", "dim"))
    if 0 <= qpos < width and qpos < filled:      # quota mark inside the fill
        cells[qpos] = ("▌", "ok")
    return cells


# --- manifests --------------------------------------------------------------------------
DROP_LABELS = {"controller-uid", "job-name", "batch.kubernetes.io/controller-uid", "batch.kubernetes.io/job-name",
               "run.ai/control-plane-managed", "run.ai/converted-from-native", "run.ai/project", "run.ai/workload",
               "project", "release", "createdBy", "workloadKind", "workloadName", "pod-template-hash",
               "runai/queue"}
DROP_ANNOTATIONS = {"workloadId", "run.ai/submitted-by", "runai-submit-command", "user",
                    "kubectl.kubernetes.io/last-applied-configuration", "deployment.kubernetes.io/revision",
                    "batch.kubernetes.io/job-tracking", "runai-cli-command"}
INJECTED_ENV = {"RUNAI_PROJECT", "RUNAI_JOB_NAME", "RUNAI_JOB_UUID", "RUNAI_NUM_OF_GPUS", "RUNAI_JOB_TYPE",
                "LDAP_UID", "LDAP_GID", "JOB_NAME", "JOB_UUID", "POD_INDEX", "POD_UUID", "NVIDIA_VISIBLE_DEVICES",
                "RUNAI_GPU_MEMORY_LIMIT", "REPORTER_NAME", "WORKLOAD_NAME", "WORKLOAD_UUID"}
POD_DEFAULTS = {"dnsPolicy": "ClusterFirst", "terminationGracePeriodSeconds": 30, "schedulerName": "default-scheduler",
                "serviceAccountName": "default", "serviceAccount": "default", "enableServiceLinks": True}


def _clean_meta(meta, keep_name=True):
    out = {}
    if keep_name and meta.get("name"):
        out["name"] = meta["name"]
    labels = {k: v for k, v in (meta.get("labels") or {}).items()
              if k not in DROP_LABELS and not (k == "app" and v == "job")}
    ann = {k: v for k, v in (meta.get("annotations") or {}).items() if k not in DROP_ANNOTATIONS}
    if labels:
        out["labels"] = labels
    if ann:
        out["annotations"] = ann
    return out


def _clean_pod(spec):
    spec = dict(spec)
    for k in ("nodeName", "priority", "preemptionPolicy", "schedulerName", "serviceAccount"):
        spec.pop(k, None)
    for k, v in POD_DEFAULTS.items():
        if spec.get(k) == v:
            spec.pop(k)
    if spec.get("securityContext") == {}:
        spec.pop("securityContext")
    tol = [t for t in spec.get("tolerations") or []
           if t.get("key") not in ("node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable")]
    if tol:
        spec["tolerations"] = tol
    else:
        spec.pop("tolerations", None)
    conts = []
    for c in spec.get("containers") or []:
        c = dict(c)
        for k, v in (("terminationMessagePath", "/dev/termination-log"), ("terminationMessagePolicy", "File")):
            if c.get(k) == v:
                c.pop(k)
        if c.get("resources") == {}:
            c.pop("resources")
        env = [e for e in c.get("env") or [] if e.get("name") not in INJECTED_ENV]
        if env:
            c["env"] = env
        else:
            c.pop("env", None)
        conts.append(c)
    spec["containers"] = conts
    return spec


def clean_manifest(obj):
    """A live Kubernetes object -> a manifest you can submit again."""
    kind = obj.get("kind")
    out = {"apiVersion": obj.get("apiVersion"), "kind": kind, "metadata": _clean_meta(obj.get("metadata") or {})}
    spec = dict(obj.get("spec") or {})
    if kind == "Pod":
        out["spec"] = _clean_pod(spec)
        return out
    for k in ("selector",) if kind == "Job" else ():
        spec.pop(k, None)
    for k, v in (("manualSelector", False), ("suspend", False), ("completionMode", "NonIndexed"),
                 ("podReplacementPolicy", "TerminatingOrFailed"), ("revisionHistoryLimit", 10),
                 ("progressDeadlineSeconds", 600)):
        if spec.get(k) == v:
            spec.pop(k)
    if kind == "Deployment" and spec.get("strategy", {}).get("type") == "RollingUpdate":
        spec.pop("strategy")
    tmpl = spec.get("template")
    if kind == "CronJob":
        tmpl = (spec.get("jobTemplate") or {}).get("spec", {}).get("template")
    if tmpl:
        tmpl["metadata"] = _clean_meta(tmpl.get("metadata") or {}, keep_name=False)
        if not tmpl["metadata"]:
            tmpl.pop("metadata")
        tmpl["spec"] = _clean_pod(tmpl.get("spec") or {})
    out["spec"] = spec
    return out


def kind_candidates(w):
    t = w.get("type") or ""
    m = {"Deployment": ["deployment"], "StatefulSet": ["statefulset"], "Pod": ["pod"], "ReplicaSet": ["replicaset"],
         "CronJob": ["cronjob"], "Job": ["job"], "Workspace": ["job"], "Training": ["job"]}
    return m.get(t, ["job", "deployment", "statefulset", "pod"])


def export_workload(sess, w):
    """-> (yaml text, how). kubectl manifest if possible, else the Run:ai spec."""
    ns = w.get("namespace") or f"runai-{w.get('projectName')}"
    name = w["name"]
    submit_cmd, errors = "", []
    for kind in kind_candidates(w):
        rc, out, err = cmd([KUBECTL, "get", kind, name, "-n", ns, "-o", "json"], timeout=30)
        if rc == 0:
            obj = json.loads(out)
            submit_cmd = (obj.get("metadata", {}).get("annotations") or {}).get("runai-submit-command", "")
            man = clean_manifest(obj)
            head = [f"# {w.get('type')} {name} in project {w.get('projectName')}, saved by runai-tui "
                    f"{datetime.datetime.now():%F %T}",
                    (f"# submit again: kubectl apply -f <this file> -n {ns}   (Services/Ingresses of the "
                     f"original file are not included)" if w.get("source") == "Other" else
                     f"# submit again: runai workload submit -f <this file> -p {w.get('projectName')}")]
            if submit_cmd:
                head.append("# originally created with:")
                head += ["#   " + l for l in textwrap.wrap(submit_cmd, 96, break_on_hyphens=False)]
            body = yaml.safe_dump(man, sort_keys=False, default_flow_style=False, width=100, allow_unicode=True)
            return "\n".join(head) + "\n" + body, f"kubectl get {kind}"
        errors.append(first_error(err, out))
    # no kubectl access: the Run:ai spec (as the UI would resubmit it)
    kind = {"Workspace": "workspaces", "Training": "trainings", "Inference": "inferences"}.get(w.get("type"))
    if not kind:
        raise RuntimeError("; ".join(e for e in errors if e) or "cannot read the workload with kubectl")
    spec = sess.api(f"{CFG['api']['workloads']}/{kind}/{w['id']}") or {}
    s = spec.get("spec") or {}
    cli = s.get("runaiCliCommand") or next((a.get("value") for a in s.get("annotations") or []
                                            if a.get("name") == "runai-submit-command"), "")
    head = [f"# Run:ai {w.get('type')} spec of {name} (not a Kubernetes manifest: kubectl was not usable:",
            f"#   {'; '.join(e for e in errors if e)[:150]})"]
    if cli:
        head.append("# recreate with:")
        head += ["#   " + l for l in textwrap.wrap(cli, 96, break_on_hyphens=False)]
    return "\n".join(head) + "\n" + yaml.safe_dump(s, sort_keys=False, width=100), f"Run:ai API ({kind})"


def load_docs(path):
    with open(os.path.expanduser(path)) as f:
        return [d for d in yaml.safe_load_all(f) if d]


def workload_names(docs):
    return [d["metadata"]["name"] for d in docs
            if isinstance(d, dict) and d.get("kind") in WORKLOAD_KINDS and (d.get("metadata") or {}).get("name")]


def submit_flags(doc):
    """`runai workload submit` ignores these labels in the file; pass them as flags so a
    manifest (ours, or one saved from a workload) behaves the same on every submit."""
    labels = (doc.get("metadata") or {}).get("labels") or {}
    flags = []
    for label, flag in (("priorityClassName", "--priority"), ("kai.scheduler/preemptibility", "--preemptibility"),
                        ("run.ai/category", "--category")):
        if labels.get(label):
            flags += [flag, str(labels[label])]
    return flags


def doc_project(docs):
    ns = next(((d.get("metadata") or {}).get("namespace") for d in docs if isinstance(d, dict)
               and (d.get("metadata") or {}).get("namespace")), "")
    return ns[len("runai-"):] if ns.startswith("runai-") else ""


# --- the new-workload form -> manifest --------------------------------------------------------
KINDS = [("training", "batch job (Job, preemptible, ends)"),
         ("interactive", "interactive (Job, sleep infinity, shell in)"),
         ("service", "service (Deployment, non-preemptible)"),
         ("workspace", "Run:ai workspace (CLI, tool URLs)")]


def identity():
    """uid, gid, user name: from the token if logged in, else from the OS."""
    return os.getuid(), os.getgid(), getpass.getuser()


_GROUP_NAMES = {}


def group_name(gid):
    """gid -> name, cached: the header asks every frame and the lookup may go to LDAP/SSSD."""
    if gid not in _GROUP_NAMES:
        try:
            _GROUP_NAMES[gid] = grp.getgrgid(int(gid)).gr_name
        except (KeyError, ValueError, OverflowError):
            _GROUP_NAMES[gid] = ""
    return _GROUP_NAMES[gid]


def user_group(claims):
    """The user's research group: their primary group if the Run:ai token lists it (or lists
    none), else the token's first group."""
    primary = group_name(claims.get("gid") or os.getgid())
    groups = claims.get("groups") or []
    return primary if primary in groups or not groups else groups[0]


def site_title(claims):
    """site.name with {group}, {user} and {email} filled in; "{group} Researcher" by default."""
    vals = {"group": user_group(claims), "user": getpass.getuser(),
            "email": claims.get("email") or claims.get("preferred_username") or ""}
    class Keep(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    try:
        title = SITE["name"].format_map(Keep(vals))
    except (IndexError, ValueError, AttributeError):
        title = SITE["name"]
    return " ".join(title.split()) or "Researcher"


# --- Run:ai assets in the form: environments, compute resources, data sources, templates ----------
def asset_name(a):
    return (a.get("meta") or {}).get("name", "")


def asset_in_scope(a, project):
    """Usable from this project: project-scoped assets of other projects (and department-scoped
    ones of other departments) are not."""
    m = a.get("meta") or {}
    if not project:
        return True
    if m.get("projectId") not in (None, ""):
        return str(m["projectId"]) == str(project.get("id"))
    dep = project.get("parentId") or (project.get("parent") or {}).get("id")
    if m.get("departmentId") not in (None, "") and dep not in (None, ""):
        return str(m["departmentId"]) == str(dep)
    return True


def compute_values(c):
    """A compute resource asset -> (form values, note)."""
    s = c.get("spec") or {}
    out, note = {}, ""
    if s.get("cpuCoreRequest") is not None:
        out["cpu"] = num(float(s["cpuCoreRequest"]))
    mem = s.get("cpuMemoryRequest")
    if mem:
        out["memory"] = str(mem) if MEM_RE.match(str(mem)) else human_mem(mem_bytes(mem))
    n, typ = s.get("gpuDevicesRequest") or 0, s.get("gpuRequestType") or "portion"
    portion = s.get("gpuPortionRequest")
    if not n:
        out["gpu"] = "none"
    elif typ != "portion":
        note = f"GPU request type '{typ}' is not in the form: set GPU by hand"
    elif portion not in (None, 1, 1.0) and float(portion) < 1:
        out.update(gpu="fraction", gpu_amount=num(float(portion)))
        if n > 1:
            note = f"{n} devices x {num(float(portion))}: the form requests one fraction"
    else:
        out.update(gpu="whole", gpu_amount=str(int(n)))
    if s.get("largeShmRequest"):
        out["shm"] = out.get("memory") or "1Gi"
    return out, note


DS_KINDS = {"pvc", "nfs", "hostPath", "configMap"}


def ds_kind(d):
    return (d.get("meta") or {}).get("kind") or d.get("kind") or ""


def datasource_mount(d, workspace=False):
    """A data source asset -> (volume, volumeMount) for a manifest, or CLI flags for a workspace.
    Raises ValueError for kinds the form cannot mount."""
    kind, s, name = ds_kind(d), d.get("spec") or {}, asset_name(d)
    if len(s) == 1 and isinstance(next(iter(s.values())), dict):     # the list nests it: {hostPath: {...}}
        s = next(iter(s.values()))
    vol = "ds-" + re.sub(r"[^a-z0-9-]", "-", name.lower())[:58].strip("-")
    ro = bool(s.get("readOnly"))
    if kind == "pvc":
        claim, path = s.get("claimName"), s.get("path")
        if not claim or not path:
            raise ValueError(f"data source {name}: PVC without claimName/path")
        if workspace:
            return ["--existing-pvc", f"claimname={claim},path={path}"]
        return ({"name": vol, "persistentVolumeClaim": {"claimName": claim, **({"readOnly": True} if ro else {})}},
                {"name": vol, "mountPath": path, **({"readOnly": True} if ro else {})})
    if kind == "nfs":
        server, path, mount = s.get("server"), s.get("path"), s.get("mountPath")
        if not (server and path and mount):
            raise ValueError(f"data source {name}: NFS without server/path/mountPath")
        if workspace:
            return ["--nfs", f"server={server},path={path},mountpath={mount}" + (",readonly" if ro else "")]
        return ({"name": vol, "nfs": {"server": server, "path": path, **({"readOnly": True} if ro else {})}},
                {"name": vol, "mountPath": mount, **({"readOnly": True} if ro else {})})
    if kind == "hostPath":
        path, mount = s.get("path"), s.get("mountPath")
        if not (path and mount):
            raise ValueError(f"data source {name}: host path without path/mountPath")
        if workspace:
            return ["--host-path", f"path={path},mount={mount}" + ("" if ro else ",readwrite")]
        return ({"name": vol, "hostPath": {"path": path}},
                {"name": vol, "mountPath": mount, **({"readOnly": True} if ro else {})})
    if kind == "configMap" and not workspace:
        cm, mount = s.get("configMap"), s.get("mountPath")
        if not (cm and mount):
            raise ValueError(f"data source {name}: config map without configMap/mountPath")
        return {"name": vol, "configMap": {"name": cm}}, {"name": vol, "mountPath": mount, "readOnly": True}
    raise ValueError(f"data source {name}: kind '{kind}' is not supported here (use the Run:ai UI)")


HOSTPATH_TYPES = {"", "File", "Directory", "FileOrCreate", "DirectoryOrCreate", "Socket", "CharDevice",
                  "BlockDevice"}


def site_binds():
    """site.bind_mounts -> ({name: bind}, [problems]); a bind is {host, path, ro, type}."""
    out, errs = {}, []
    for i, b in enumerate(SITE.get("bind_mounts") or []):
        if isinstance(b, str):
            b = {"host_path": b}
        host = str((b or {}).get("host_path") or "")
        path = str(b.get("mount_path") or host)
        name = str(b.get("name") or host)
        typ = str(b.get("type") or "")
        if not host.startswith("/") or not path.startswith("/"):
            errs.append(f"site.bind_mounts[{i}]: host_path and mount_path must be absolute")
        elif typ not in HOSTPATH_TYPES:
            errs.append(f"site.bind_mounts[{i}]: type '{typ}' is not one of {', '.join(sorted(HOSTPATH_TYPES - {''}))}")
        else:
            out[name] = {"host": host, "path": path, "ro": b.get("read_only", True) is not False, "type": typ,
                         "default": bool(b.get("default"))}
    return out, errs


def parse_binds(text):
    """'/host[:/container][:ro|rw], ...' -> ([bind], [problems]). Read-only unless :rw."""
    out, errs = [], []
    for item in [x.strip() for x in (text or "").split(",") if x.strip()]:
        parts = item.split(":")
        mode = "ro"
        if parts[-1] in ("ro", "rw"):
            mode = parts.pop()
        if len(parts) not in (1, 2) or not all(p.startswith("/") for p in parts):
            errs.append(f"bind '{item}': use /host/path[:/container/path][:ro|rw]")
            continue
        out.append({"host": parts[0], "path": parts[-1], "ro": mode == "ro", "type": ""})
    return out, errs


def format_binds(binds):
    """[bind] -> the '/host[:/container][:rw], ...' text kept in the form and in presets."""
    return ", ".join(b["host"] + (f":{b['path']}" if b["path"] != b["host"] else "") + ("" if b["ro"] else ":rw")
                     for b in binds)


PVC_RE = re.compile(r"^[a-z0-9]([-.a-z0-9]*[a-z0-9])?$")
ACCESS_MODES = {"ReadWriteOnce": "RWO", "ReadOnlyMany": "ROX", "ReadWriteMany": "RWX", "ReadWriteOncePod": "RWOP"}


def parse_pvcs(text):
    """'claim:/path[:ro|rw], ...' -> ([{host: claim, path, ro}], [problems]). Read-write unless :ro."""
    out, errs = [], []
    for item in [x.strip() for x in (text or "").split(",") if x.strip()]:
        parts = item.split(":")
        mode = "rw"
        if parts[-1] in ("ro", "rw"):
            mode = parts.pop()
        if len(parts) != 2 or not PVC_RE.match(parts[0]) or not parts[1].startswith("/"):
            errs.append(f"PVC '{item}': use claim-name:/mount/path[:ro]")
            continue
        out.append({"host": parts[0], "path": parts[1], "ro": mode == "ro", "type": ""})
    return out, errs


def format_pvcs(items):
    return ", ".join(f"{p['host']}:{p['path']}" + (":ro" if p["ro"] else "") for p in items)


def project_namespace(state, project):
    p = state.project(project) or {}
    return (p.get("status") or {}).get("namespace") or f"runai-{project}"


def list_pvcs(project, namespace):
    """-> ([{name, managed, size, phase, sc, modes}], note). Names from `runai pvc list`; size, status,
    access modes and storage class from kubectl, if you may read PVCs in the namespace."""
    out, notes = {}, []
    rc, so, se = cmd([RUNAI, "pvc", "list", "-p", project, "--json"], timeout=60)
    if rc == 0:
        try:
            for x in (json.loads(so or "{}") or {}).get("pvcs") or []:
                if x.get("name"):
                    out[x["name"]] = {"name": x["name"], "managed": x.get("managed")}
        except (ValueError, AttributeError):
            notes.append("runai pvc list: unreadable output")
    else:
        notes.append("runai pvc list: " + first_error(se, so))
    rc, ko, ke = cmd([KUBECTL, "get", "pvc", "-n", namespace, "-o", "json"], timeout=30)
    if rc == 0:
        try:
            for x in json.loads(ko).get("items") or []:
                n, sp, st = x["metadata"]["name"], x.get("spec") or {}, x.get("status") or {}
                d = out.setdefault(n, {"name": n, "managed": None})
                d.update(size=(st.get("capacity") or {}).get("storage")
                         or ((sp.get("resources") or {}).get("requests") or {}).get("storage") or "",
                         phase=st.get("phase") or "", sc=sp.get("storageClassName") or "",
                         modes=",".join(ACCESS_MODES.get(m, m) for m in sp.get("accessModes") or []))
        except (ValueError, KeyError, TypeError, AttributeError):
            notes.append("kubectl get pvc: unreadable output")
    else:
        notes.append(f"no sizes: kubectl cannot list PVCs in {namespace} ({first_error(ke, ko)})")
    return sorted(out.values(), key=lambda d: d["name"]), "; ".join(notes)


def pvc_line(d):
    managed = {True: "Run:ai", False: "", None: ""}[d.get("managed")]
    return (f"{d['name']:<28} {d.get('size') or '?':>7}  {d.get('phase') or '':<8} {d.get('modes') or '':<8} "
            f"{d.get('sc') or '':<14} {managed}").rstrip()


def form_binds(v):
    """The form's bind mounts (ticked site ones, then extra ones) -> ([bind], [warnings])."""
    known, errs = site_binds()
    binds = [known[n] for n in v.get("binds") or [] if n in known]
    gone = [n for n in v.get("binds") or [] if n not in known]
    if gone:
        errs.append(f"bind mount(s) not in site.bind_mounts: {', '.join(gone)}")
    extra, bad = parse_binds(v.get("extra_binds"))
    return binds + extra, errs + bad


def join_command(command, args):
    """An environment's or template's command + args -> the form's sh -c text (unwraps sh -c '...')."""
    if command in ("sh", "/bin/sh", "bash", "/bin/bash") and str(args or "").startswith("-c "):
        try:
            rest = shlex.split(args)
            if len(rest) == 2:
                return rest[1]
        except ValueError:
            pass
    return " ".join(x for x in (command, args) if x)


def template_values(t, state):
    """A workload template -> (form values, notes). Assets are referenced by id."""
    if "workloadType" in (t.get("meta") or {}) or "assets" in t:
        return template_values_v2(t, state)
    s = t.get("spec") or {}
    a = s.get("assets") or {}
    out, notes = {}, []

    def by_id(items, ref):
        ref = ref.get("id") if isinstance(ref, dict) else ref
        return next((x for x in items if str((x.get("meta") or {}).get("id")) == str(ref)), None) if ref else None

    env = by_id(state.envs, a.get("environment"))
    if env:
        out["env"] = asset_name(env)
    elif a.get("environment"):
        notes.append("its environment is not visible to you")
    comp = by_id(state.computes, a.get("compute"))
    if comp:
        out["compute"] = asset_name(comp)
    ds = [by_id(state.datasources, r) for r in a.get("datasources") or []]
    if a.get("datasources"):
        out["datasources"] = [asset_name(d) for d in ds if d]
        if None in ds:
            notes.append(f"{ds.count(None)} data source(s) not visible to you")
    se = s.get("specificEnv") or {}
    command = join_command(se.get("command"), se.get("args"))
    if command:
        out["command"] = command
    envs = [f"{e['name']}={e.get('value', '')}" for e in se.get("environmentVariables") or []
            if e.get("name") and "value" in e]
    if envs:
        out["env_vars"] = ",".join(envs)
    return out, notes


def _by_id(items, ref):
    ref = ref.get("id") if isinstance(ref, dict) else ref
    return next((x for x in items if str((x.get("meta") or {}).get("id")) == str(ref)), None) if ref else None


def template_values_v2(t, state):
    """A template from /api/v1/workload-templates (spec inline, assets by id) -> (form values, notes)."""
    s, a, m = t.get("spec") or {}, t.get("assets") or {}, t.get("meta") or {}
    out, notes = {}, []
    wt = m.get("workloadType") or ""
    if wt in ("Workspace", "Training"):
        out["kind"] = wt.lower()
    elif wt:
        notes.append(f"a {wt} template: filled in as a batch job")
    env = _by_id(state.envs, a.get("environment"))
    if env:
        out["env"] = asset_name(env)
    elif a.get("environment"):
        notes.append("its environment is not visible to you")
    if s.get("image"):
        out["image"] = s["image"]
        if not env:
            out["env"] = "(custom image)"
    comp = _by_id(state.computes, a.get("compute"))
    if comp:
        out["compute"] = asset_name(comp)
    elif a.get("compute"):
        notes.append("its compute resource is not visible to you")
    elif s.get("compute"):
        vals, note = compute_values({"spec": s["compute"]})
        out.update(vals, compute="(custom)")
        if note:
            notes.append(note)
    if a.get("datasources"):
        ds = [_by_id(state.datasources, r) for r in a["datasources"]]
        out["datasources"] = [asset_name(d) for d in ds if d]
        if None in ds:
            notes.append(f"{ds.count(None)} data source(s) not visible to you")
    command = join_command(s.get("command"), s.get("args"))
    if command and not (s.get("command") == "sleep" and s.get("args") == "infinity"):
        out["command"] = command
    envs = [f"{e['name']}={e.get('value', '')}" for e in s.get("environmentVariables") or []
            if e.get("name") and "value" in e]
    if envs:
        out["env_vars"] = ",".join(envs)
    hp, hm = SITE["home"].get("host_path") or "", SITE["home"].get("mount_path") or "/home"
    st = s.get("storage") or {}
    binds = [{"host": x.get("path"), "path": x.get("mountPath") or x.get("path"), "ro": bool(x.get("readOnly")),
              "type": ""} for x in st.get("hostPath") or []
             if x.get("path") and not (x.get("path") == hp and x.get("mountPath") == hm)]
    if binds:
        out["extra_binds"] = format_binds(binds)
    pvcs = [{"host": x["claimName"], "path": x.get("path") or f"/data/{x['claimName']}",
             "ro": bool(x.get("readOnly")), "type": ""} for x in st.get("pvc") or [] if x.get("claimName")]
    if pvcs:
        out["pvcs"] = format_pvcs(pvcs)
    for e in st.get("emptyDirVolume") or []:
        if e.get("path") == "/scratch" and e.get("sizeLimit"):
            out["scratch"] = str(e["sizeLimit"])
    if (s.get("security") or {}).get("uidGidSource") == "fromIdpToken":
        out["run_as_me"] = True
    if s.get("nodePools"):
        out["pool"] = s["nodePools"][0]
    if s.get("priorityClass") in PRIORITIES:
        out["priority"] = s["priorityClass"]
    if s.get("preemptibility") in ("preemptible", "non-preemptible"):
        out["preempt"] = s["preemptibility"]
    if s.get("completions"):
        out["completions"] = str(s["completions"])
    if s.get("autoDeletionTimeAfterCompletionSeconds"):
        out["ttl"] = str(s["autoDeletionTimeAfterCompletionSeconds"])
    return out, notes


def api_mem(mem):
    """Form memory -> what the Run:ai CLI and API take (M/G): binary units in whole MB, so 1Gi stays 1Gi."""
    return mem if not mem.endswith("i") else f"{-(-int(mem_bytes(mem)) // 10 ** 6)}M"


def build_workload(v, sess, state):
    """Form values -> (manifest dict or None, argv or None, warnings)."""
    warn = []
    uid, gid, user = identity()
    if sess.claims.get("uid"):
        uid, gid = int(sess.claims["uid"]), int(sess.claims.get("gid", gid))
    name, proj, kind = v["name"].strip(), v["project"], v["kind"]
    if not NAME_RE.match(name) or len(name) > 63:
        warn.append("name: lowercase letters, digits and '-', at most 63 characters")
    if state.find(proj, name):
        warn.append(f"a workload '{name}' already exists in {proj}: pick another name or delete it")
    image = v["image"].strip()
    if not image:
        warn.append("image is empty")
    reg = registry_host(state)
    ok_regs = ([reg] if reg else []) + list(SITE.get("allowed_registries") or [])
    if image and reg and SITE.get("offline_nodes") and not any(image.startswith(r + "/") for r in ok_regs):
        warn.append(f"image is not in the local registry ({reg}/...): compute nodes are offline")
    cpu, mem = v["cpu"].strip(), v["memory"].strip()
    try:
        float(cpu or "0")
    except ValueError:
        warn.append("CPU: a number of cores, e.g. 0.5 or 2")
    if mem and not MEM_RE.match(mem):
        warn.append("memory: e.g. 512Mi, 2Gi")
    gpu, amount = v["gpu"], v["gpu_amount"].strip()
    g = 0.0
    if gpu != "none":
        try:
            g = float(amount)
            if gpu == "fraction" and not 0 < g < 1:
                warn.append("GPU fraction must be between 0 and 1 (e.g. 0.5)")
            if gpu == "whole" and (g < 1 or g != int(g)):
                warn.append("whole GPUs: 1, 2, ...")
        except ValueError:
            warn.append("GPU amount must be a number")
    preempt = v["preempt"]
    if preempt == "default":
        preempt = "preemptible" if kind == "training" else "non-preemptible"
    p = state.project(proj)
    if p and g and preempt == "non-preemptible":
        pools = [q for q in project_pools(p) if v["pool"] in ("any", q["pool"])]
        free = max([q["quota"] - q["gpu_np"] for q in pools] or [0])
        if g > free + 1e-9:
            warn.append(f"non-preemptible GPU workloads must fit the guaranteed quota: {num(free)} GPU free "
                        f"in {v['pool'] if v['pool'] != 'any' else 'any pool'} -> it would stay pending")
    env_vars = []
    for kv in [x.strip() for x in v["env_vars"].split(",") if x.strip()]:
        if "=" not in kv:
            warn.append(f"env '{kv}': use NAME=value")
        else:
            k, val = kv.split("=", 1)
            env_vars.append({"name": k.strip(), "value": val})
    hp, hm = SITE["home"].get("host_path") or "", SITE["home"].get("mount_path") or "/home"
    home = f"{hm.rstrip('/')}/{user}"
    p_obj = state.project(proj)
    ds_by = {asset_name(d): d for d in state.datasources}
    chosen = [ds_by[n] for n in v.get("datasources") or [] if n in ds_by]
    missing = [n for n in v.get("datasources") or [] if n not in ds_by]
    if missing:
        warn.append(f"data source(s) not found: {', '.join(missing)}")
    chosen_bad = [asset_name(d) for d in chosen if not asset_in_scope(d, p_obj)]
    if chosen_bad:
        warn.append(f"not usable from {proj}: {', '.join(chosen_bad)}")
    pull = v.get("pull_secret") or "(none)"
    binds, bind_warn = form_binds(v)
    warn += bind_warn
    pvcs, pvc_warn = parse_pvcs(v.get("pvcs"))
    warn += pvc_warn

    if kind == "workspace":                      # Run:ai CLI, keeps the environment's tools/URLs
        argv = [RUNAI, "workspace", "submit", name, "-p", proj]
        if v["env"] != "(custom image)":
            argv += ["--environment", v["env"]]
        else:
            argv += ["-i", image]
        if cpu:
            argv += ["--cpu-core-request", cpu]
        if mem:
            argv += ["--cpu-memory-request", api_mem(mem)]
        if gpu == "whole":
            argv += ["-g", str(int(g or 1))]
        elif gpu == "fraction":
            argv += ["--gpu-portion-request", amount]
        if v["pool"] != "any":
            argv += ["--node-pools", v["pool"]]
        if v["run_as_me"]:
            argv += ["--run-as-user"]
            if hp:
                argv += ["--host-path", f"path={hp},mount={hm},readwrite", "--working-dir", home, "-e", f"HOME={home}"]
        for d in chosen:
            try:
                argv += datasource_mount(d, workspace=True)
            except ValueError as e:
                warn.append(str(e))
        for b in binds:
            argv += ["--host-path", f"path={b['host']},mount={b['path']}" + ("" if b["ro"] else ",readwrite")]
        for c in pvcs:
            argv += ["--existing-pvc", f"claimname={c['host']},path={c['path']}"]
        if any(c["ro"] for c in pvcs):
            warn.append("PVCs: read-only is not available for CLI workspaces (mounted read-write)")
        if pull != "(none)":
            warn.append("pull secret: not available for CLI workspaces (use an environment with credentials)")
        for e in env_vars:
            argv += ["-e", f"{e['name']}={e['value']}"]
        if v["priority"] != "default":
            argv += ["--priority", v["priority"]]
        if v["preempt"] != "default":
            argv += ["--preemptibility", v["preempt"]]
        if v["command"].strip() and v["env"] == "(custom image)":
            argv += ["--command", "--", "sh", "-c", v["command"].strip()]
        return None, argv, warn

    container = {"name": "main", "image": image}
    if v["command"].strip():
        container["command"] = ["sh", "-c", v["command"].strip()]
    elif kind == "interactive":
        container["command"] = ["sleep", "infinity"]
    res = {}
    if cpu:
        res.setdefault("requests", {})["cpu"] = cpu
        res.setdefault("limits", {})["cpu"] = cpu
    if mem:
        res.setdefault("requests", {})["memory"] = mem
        res.setdefault("limits", {})["memory"] = mem
    if gpu == "whole":
        res.setdefault("limits", {})["nvidia.com/gpu"] = int(g or 1)
    if res:
        container["resources"] = res
    pod_meta = {}
    if v["pool"] != "any":
        pod_meta.setdefault("labels", {})[SITE["node_pool_label"]] = v["pool"]
    if gpu == "fraction":
        pod_meta.setdefault("annotations", {})[SITE["gpu_fraction_annotation"]] = amount
    if kind == "service":
        pod_meta.setdefault("labels", {})["app"] = name
    env = list(env_vars)
    mounts, vols = [], []
    spec = {}
    if v["run_as_me"]:
        spec["securityContext"] = {"runAsUser": uid, "runAsGroup": gid, "supplementalGroups": [gid]}
        if hp:
            container["workingDir"] = home
            env.insert(0, {"name": "HOME", "value": home})
            mounts.append({"name": "home", "mountPath": hm})
            vols.append({"name": "home", "hostPath": {"path": hp, "type": "Directory"}})
    for d in chosen:
        try:
            vol, mnt = datasource_mount(d)
            vols.append(vol)
            mounts.append(mnt)
        except ValueError as e:
            warn.append(str(e))
    for i, c in enumerate(pvcs):
        vols.append({"name": f"pvc-{i}", "persistentVolumeClaim": {"claimName": c["host"],
                                                                   **({"readOnly": True} if c["ro"] else {})}})
        mounts.append({"name": f"pvc-{i}", "mountPath": c["path"], **({"readOnly": True} if c["ro"] else {})})
    for i, b in enumerate(binds):
        vol = f"bind-{i}"
        vols.append({"name": vol, "hostPath": {"path": b["host"], **({"type": b["type"]} if b["type"] else {})}})
        mounts.append({"name": vol, "mountPath": b["path"], **({"readOnly": True} if b["ro"] else {})})
    if pull != "(none)":
        spec["imagePullSecrets"] = [{"name": pull}]
    if v["scratch"].strip():
        if not MEM_RE.match(v["scratch"].strip()):
            warn.append("scratch size: e.g. 10Gi")
        mounts.append({"name": "scratch", "mountPath": "/scratch"})
        vols.append({"name": "scratch", "emptyDir": {"sizeLimit": v["scratch"].strip()}})
        env.append({"name": "TMPDIR", "value": "/scratch"})
    if v["shm"].strip():
        if not MEM_RE.match(v["shm"].strip()):
            warn.append("/dev/shm size: e.g. 1Gi")
        mounts.append({"name": "dshm", "mountPath": "/dev/shm"})
        vols.append({"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": v["shm"].strip()}})
    if env:
        container["env"] = env
    seen = {}
    for m in mounts:
        if m["mountPath"] in seen:
            warn.append(f"{m['mountPath']} is mounted twice ({seen[m['mountPath']]} and {m['name']}): "
                        "the pod will be rejected")
        seen.setdefault(m["mountPath"], m["name"])
    if mounts:
        container["volumeMounts"] = mounts
    if kind != "service":
        spec["restartPolicy"] = "Never"
    spec["containers"] = [container]
    if vols:
        spec["volumes"] = vols
    template = ({"metadata": pod_meta} if pod_meta else {})
    template["spec"] = spec
    labels = {}
    if v["priority"] != "default":
        labels["priorityClassName"] = v["priority"]
    if v["preempt"] != "default" or kind == "interactive":
        labels["kai.scheduler/preemptibility"] = preempt
    if kind == "interactive":
        labels["run.ai/category"] = "Build"
    meta = {"name": name}
    if labels:
        meta["labels"] = labels
    if kind == "service":
        man = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta,
               "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}}, "template": template}}
    else:
        jspec = {"backoffLimit": 0}
        if kind == "training":
            try:
                n = int(v["completions"] or 1)
            except ValueError:
                n = 1
                warn.append("completions must be a whole number")
            if n > 1:
                jspec.update(completions=n, parallelism=n, completionMode="Indexed")
            if v["ttl"].strip():
                try:
                    jspec["ttlSecondsAfterFinished"] = int(v["ttl"])
                except ValueError:
                    warn.append("TTL: seconds, e.g. 3600")
        jspec["template"] = template
        man = {"apiVersion": "batch/v1", "kind": "Job", "metadata": meta, "spec": jspec}
    for name_, fn in PLUGINS.manifest_hooks:
        try:
            man = fn(man, dict(v)) or man
        except Exception as e:
            warn.append(f"plugin {name_}: {e}")
    return man, None, warn


# --- saving to Run:ai: assets and workload templates (REST API; the CLI cannot create them) ------
def asset_meta(name, project, desc):
    pid = project.get("id")
    return {"name": name, "scope": "project", "projectId": int(pid) if str(pid).isdigit() else pid,
            "clusterId": project.get("clusterId"), "description": desc}


def sh_args(command):
    """The form's sh -c text -> (command, args) for an environment or template."""
    return ("sh", "-c " + shlex.quote(command)) if command else (None, None)


def form_compute(v):
    """The form's CPU, memory, GPU and /dev/shm -> a Run:ai compute spec."""
    c = {}
    if v["cpu"].strip():
        c["cpuCoreRequest"] = float(v["cpu"])
    if v["memory"].strip():
        c["cpuMemoryRequest"] = api_mem(v["memory"].strip())
    c["gpuRequestType"] = "portion"
    if v["gpu"] == "whole":
        c.update(gpuDevicesRequest=int(float(v["gpu_amount"] or 1)), gpuPortionRequest=1.0)
    elif v["gpu"] == "fraction":
        c.update(gpuDevicesRequest=1, gpuPortionRequest=float(v["gpu_amount"]))
    else:
        c["gpuDevicesRequest"] = 0
    if v["shm"].strip():
        c["largeShmRequest"] = True
    return c


def environment_body(name, v, project, desc):
    spec = {"image": v["image"].strip(), "imagePullPolicy": "IfNotPresent",
            "uidGidSource": "fromIdpToken" if v["run_as_me"] else "fromTheImage"}
    command, args = sh_args(v["command"].strip())
    if command:
        spec.update(command=command, args=args)
    meta = dict(asset_meta(name, project, desc), workloadSupportedTypes={
        "workspace": True, "training": True, "inference": False, "distributed": False})
    return {"meta": meta, "spec": spec}


def template_body(name, v, state, desc):
    """The form -> (templates sub-path, request body, notes). Everything is stored in the template
    itself (a template that links an environment asset may not set its command, env or user)."""
    tkind = {"training": "trainings", "interactive": "trainings", "workspace": "workspaces"}.get(v["kind"])
    if not tkind:
        raise ValueError("services (Deployments) have no Run:ai template type: change Kind, or save a preset (P)")
    p = state.project(v["project"]) or {}
    notes, spec = [], {"image": v["image"].strip()}
    command, args = sh_args(v["command"].strip())
    if command:
        spec.update(command=command, args=args)
    elif v["kind"] == "interactive":
        spec.update(command="sleep", args="infinity")
    envs = [x.split("=", 1) for x in v["env_vars"].split(",") if "=" in x]
    if envs:
        spec["environmentVariables"] = [{"name": k.strip(), "value": val} for k, val in envs]
    spec["compute"] = form_compute(v)
    hp, hm = SITE["home"].get("host_path") or "", SITE["home"].get("mount_path") or "/home"
    binds, _ = form_binds(v)
    host = ([{"name": "home", "path": hp, "mountPath": hm, "readOnly": False}] if v["run_as_me"] and hp else [])
    host += [{"name": f"bind-{i}", "path": b["host"], "mountPath": b["path"], "readOnly": b["ro"]}
             for i, b in enumerate(binds)]
    storage = {}
    if host:
        storage["hostPath"] = host
    pvcs = parse_pvcs(v.get("pvcs"))[0]
    if pvcs:
        storage["pvc"] = [{"name": f"pvc-{i}", "claimName": c["host"], "path": c["path"], "existingPvc": True,
                           "readOnly": c["ro"]} for i, c in enumerate(pvcs)]
    if v["scratch"].strip():
        storage["emptyDirVolume"] = [{"name": "scratch", "path": "/scratch", "sizeLimit": v["scratch"].strip()}]
    if storage:
        spec["storage"] = storage
    if v["run_as_me"]:
        spec["security"] = {"uidGidSource": "fromIdpToken"}
    if v["pool"] != "any":
        spec["nodePools"] = [v["pool"]]
    if v["priority"] != "default":
        spec["priorityClass"] = v["priority"]
    if v["preempt"] != "default":
        spec["preemptibility"] = v["preempt"]
    if tkind == "trainings":
        n = int(v["completions"] or 1) if str(v["completions"] or "1").isdigit() else 1
        if n > 1:
            spec.update(completions=n, parallelism=n)
        if v["ttl"].strip().isdigit():
            spec["autoDeletionTimeAfterCompletionSeconds"] = int(v["ttl"])
    if (v.get("pull_secret") or "(none)") != "(none)":
        notes.append("the image pull secret is not part of templates")
    ds_by = {asset_name(d): d for d in state.datasources}
    ids = [str((ds_by[n].get("meta") or {}).get("id")) for n in v.get("datasources") or [] if n in ds_by]
    body = {"meta": {"name": name, "scopeType": "project", "scopeId": str(p.get("id")), "description": desc},
            "spec": spec}
    if ids:
        body["assets"] = {"datasources": ids}
    return tkind, body, notes


# --- quota by department ----------------------------------------------------------------------
def project_totals(p):
    """GPU totals of a project over its node pools: alloc, quota, limit (None = no limit), pending, weight."""
    qs = project_pools(p)
    total = (p.get("totalResources") or {}).get("gpuQuota")
    limits = [q["limit"] for q in qs if pool_visible(q)]      # pools without quota or use don't count
    return {"alloc": sum(q["gpu"] for q in qs), "quota": total if total not in (None, -1) else sum(q["quota"] for q in qs),
            "limit": None if not limits or any(x in (None, -1) for x in limits) else sum(limits),
            "pending": max(0, sum(q["gpu_req"] for q in qs) - sum(q["gpu"] for q in qs)),
            "nonpre": sum(q["gpu_np"] for q in qs), "weight": max([q["weight"] or 0 for q in qs] or [0])}


def org_rows(projects, departments):
    """-> [(dept name, dept totals, [(project, totals)])], departments in name order. Departments the
    API did not return (no permission) are made up from their projects."""
    by = {}
    for p in projects:
        by.setdefault((p.get("parent") or {}).get("name") or "-", []).append(p)
    deps = {d.get("name"): d for d in departments or []}
    out = []
    for name in sorted(set(by) | set(deps)):
        kids = [(p, project_totals(p)) for p in sorted(by.get(name, []), key=lambda p: p.get("name", ""))]
        d = deps.get(name) or {}
        alloc = ((d.get("status") or {}).get("quotaStatus") or {}).get("allocated") or {}
        quota = (d.get("totalResources") or {}).get("gpuQuota")
        tot = {"alloc": alloc.get("gpu") if alloc.get("gpu") is not None else sum(t["alloc"] for _, t in kids),
               "quota": None if quota in (None, -1) else quota, "known": bool(d),
               "projects_quota": sum(t["quota"] for _, t in kids)}
        out.append((name, tot, kids))
    return out


# --- AI applications --------------------------------------------------------------------------
def app_info(a):
    """An AI application from the API -> {name, project, status, chart, created, workloads}."""
    m = a.get("meta") if isinstance(a.get("meta"), dict) else {}
    sp = a.get("spec") if isinstance(a.get("spec"), dict) else {}
    st = a.get("status")

    def first(*vals):
        return next((x for x in vals if x not in (None, "", [], {})), "")
    status = st if isinstance(st, str) else first(*(((st or {}).get(k)) for k in ("phase", "status", "state")))
    wls = first((st or {}).get("workloads") if isinstance(st, dict) else None, a.get("workloads"), a.get("components"))
    return {"name": first(a.get("name"), m.get("name")), "id": first(a.get("id"), m.get("id")),
            "project": first(a.get("projectName"), m.get("projectName"), a.get("project"), sp.get("projectName")),
            "status": str(first(status, a.get("phase"))), "chart": first(sp.get("chartUrl"), a.get("chartUrl"),
                                                                         sp.get("chart"), a.get("chart")),
            "created": str(first(a.get("createdAt"), m.get("createdAt")))[:19].replace("T", " "),
            "workloads": len(wls) if isinstance(wls, list) else (wls or "")}


_KUBECTL_DETACH = []


def kubectl_can_detach():
    """kubectl attach with --detach-keys (Ctrl-P Ctrl-Q leaves the program running); runai attach has none."""
    if not _KUBECTL_DETACH:
        rc, out, err = cmd([KUBECTL, "attach", "--help"], timeout=15)
        _KUBECTL_DETACH.append(rc == 0 and "--detach-keys" in out + err)
    return _KUBECTL_DETACH[0]


def attach_words(w):
    """-> the runai sub-command for attach (['workspace'], ['training', 'standard'], ...), or None:
    then kubectl attach is used."""
    t = w.get("type") or ""
    if t == "Workspace":
        return ["workspace"]
    if t == "Training":
        return ["training", "standard"]
    fw = str(w.get("distributedFramework") or w.get("framework") or "").lower()
    if t == "Distributed" and fw in ("pytorch", "mpi", "tf", "tensorflow", "xgboost", "jax"):
        return ["training", {"tensorflow": "tf"}.get(fw, fw)]
    return None


def registry_host(state):
    return (SITE.get("registry") or "").rstrip("/")


def pretty_argv(argv, indent="    "):
    """A command line with one flag (and its value) per line."""
    parts, cur = [], []
    for i, a in enumerate(argv):
        if a.startswith("-") and cur and not (cur[-1] == "--" or "--" in cur):
            parts.append(" ".join(cur))
            cur = []
        cur.append(shlex.quote(a))
    parts.append(" ".join(cur))
    return (" \\\n" + indent).join(parts)


def manifest_text(man, project):
    head = (f"# Created with runai-tui {datetime.datetime.now():%F %T}\n"
            f"# submit: runai workload submit -f <this file> -p {project}\n")
    return head + yaml.safe_dump(man, sort_keys=False, default_flow_style=False, width=100, allow_unicode=True)


# --- form presets: ~/.config/runai-tui/presets/<name>.yaml (site ones in /etc/runai-tui/presets) ---------
FORM_EMPTY = {"project": "", "template": "(none)", "kind": "training", "name": "", "env": "(custom image)",
              "image": "", "command": "", "compute": "(custom)", "gpu": "none", "gpu_amount": "0.5", "pool": "any",
              "cpu": "", "memory": "", "run_as_me": True, "datasources": [], "pvcs": "", "binds": [], "extra_binds": "", "scratch": "", "shm": "",
              "env_vars": "", "pull_secret": "(none)", "completions": "1", "ttl": "", "priority": "default",
              "preempt": "default"}


def user_preset_dir():
    return os.path.expanduser(PRESET_DIRS[-1])


def list_presets():
    """-> {name: path}; your presets override site ones of the same name."""
    out = {}
    for d in PRESET_DIRS:
        for path in sorted(glob.glob(os.path.join(os.path.expanduser(d), "*.yaml"))):
            out[os.path.basename(path)[:-5]] = path
    return dict(sorted(out.items()))


def read_preset(name):
    with open(list_presets()[name]) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("not a mapping of form fields")
    return data


def write_preset(name, v):
    os.makedirs(user_preset_dir(), exist_ok=True)
    path = os.path.join(user_preset_dir(), f"{name}.yaml")
    keep = {k: v[k] for k in FORM_EMPTY if k != "name" and v.get(k) != FORM_EMPTY[k]}
    with open(path, "w") as f:
        f.write(f"# runai-tui form preset, saved {datetime.datetime.now():%F %T}\n")
        yaml.safe_dump(keep, f, sort_keys=False, default_flow_style=False)
    return path


# --- plugins and hooks ---------------------------------------------------------------------
class Plugins:
    def __init__(self):
        self.loaded, self.errors = [], []
        self.actions = []                # (action id, key, where, label, fn(tui, workload))
        self.checks = []                 # (plugin, fn(sess, state) -> Check | [Check])
        self.manifest_hooks = []         # (plugin, fn(manifest, form values) -> manifest)

    def action(self, aid):
        return next((a for a in self.actions if a[0] == aid), None)


PLUGINS = Plugins()


class PluginAPI:
    """Passed to a plugin's register(api). A plugin is a .py file in one of plugins.dirs:

        def register(api):
            api.action("gpus", "G", "nvidia-smi", lambda tui, w: tui.external(
                "nvidia-smi", [api.app.RUNAI, "workload", "exec", w["name"], "-p", w["projectName"],
                               "--", "nvidia-smi"]))
            api.check(lambda sess, state: api.Check("vpn", "VPN up", "ok", "..."))
            api.manifest(lambda man, values: man)

    Actions bind a key (remappable as keys.plugin.<file>.<name>); where is workload (Workloads and
    Activity, fn gets the selected workload), workloads, activity, setup or global (fn gets None)."""

    def __init__(self, name):
        self.name = name
        self.app = sys.modules[__name__]
        self.config = CFG
        self.Check = Check

    def action(self, name, key, label, fn, where="workload"):
        if where not in SCOPE_TABS:
            raise ValueError(f"action {name}: where must be one of {', '.join(SCOPE_TABS)}")
        PLUGINS.actions.append((f"plugin.{self.name}.{name}", key, where, label, fn))

    def check(self, fn):
        PLUGINS.checks.append((self.name, fn))

    def manifest(self, fn):
        PLUGINS.manifest_hooks.append((self.name, fn))


def safe_owner(path):
    """Code we run must be yours or root's, and not writable by anyone else."""
    st_ = os.stat(path)
    return st_.st_uid in (0, os.getuid()) and not st_.st_mode & 0o022


def load_plugins():
    global KEYS
    if not CFG["plugins"].get("enabled"):
        return
    import importlib.util
    for d in CFG["plugins"].get("dirs") or []:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        for path in sorted(glob.glob(os.path.join(d, "*.py"))):
            name = os.path.basename(path)[:-3]
            try:
                if not (safe_owner(d) and safe_owner(path)):
                    raise PermissionError("must be owned by you or root and not group/world-writable")
                spec = importlib.util.spec_from_file_location(f"runai_tui_plugin_{name}", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.register(PluginAPI(name))
                PLUGINS.loaded.append(path)
            except Exception as e:
                PLUGINS.errors.append(f"plugin {path}: {type(e).__name__}: {e}")
    KEYS, errs = resolve_keys(CFG.get("keys"), [a[:4] for a in PLUGINS.actions])
    PLUGINS.errors += [e for e in errs if e not in CONFIG_ERRORS]


def run_hooks(kind, text="", env=None, label=""):
    """hooks.<kind> commands in order. pre_submit: text on stdin, non-empty stdout replaces it.
    -> (ok, text, message)."""
    for c in CFG["hooks"].get(kind) or []:
        rc, out, err = cmd(["sh", "-c", c], timeout=120, env=env, stdin=text)
        log_action(f"hook {kind} {label}", ["sh", "-c", c], rc, err)
        if rc != 0:
            return False, text, f"{kind} hook: " + (first_error(err, out) or f"exit {rc}")
        if kind == "pre_submit" and out.strip():
            text = out
    return True, text, ""


# --- batch submission ---------------------------------------------------------------------
class Batch(threading.Thread):
    """Submit items one after the other; the UI draws self.items as it goes."""

    def __init__(self, items, state, poller=None, sess=None):
        super().__init__(daemon=True)
        self.items, self.state, self.poller, self.sess = items, state, poller, sess
        self.started, self.done, self.current = time.time(), False, -1
        self.claimed = set()             # (project, name) taken by earlier items of this batch

    def plan(self, it):
        """-> argv; fills it['names'] and it['method'] (raises ValueError when invalid)."""
        env = {"RUNAI_TUI_PROJECT": it.get("project") or "", "RUNAI_TUI_LABEL": it.get("label") or ""}
        if it.get("argv"):
            it["method"] = "runai workspace submit"
            ok, _, msg = run_hooks("pre_submit", "", dict(env, RUNAI_TUI_ARGV=json.dumps(it["argv"])),
                                   it.get("label"))
            if not ok:
                raise ValueError(msg)
            return it["argv"]
        if CFG["hooks"].get("pre_submit"):
            path = os.path.expanduser(it["file"])
            try:
                text = open(path).read()
            except OSError as e:
                raise ValueError(f"cannot read: {e}")
            ok, new, msg = run_hooks("pre_submit", text, dict(env, RUNAI_TUI_FILE=path), it.get("label"))
            if not ok:
                raise ValueError(msg)
            if new != text:              # submit the hook's version, keep it next to the form's manifests
                os.makedirs(SUBMITTED, exist_ok=True)
                base = os.path.splitext(os.path.basename(path))[0]
                it["file"] = os.path.join(SUBMITTED, f"{base}-hooked-{datetime.datetime.now():%Y%m%d-%H%M%S}.yaml")
                with open(it["file"], "w") as f:
                    f.write(new)
        try:
            docs = load_docs(it["file"])
        except (OSError, yaml.YAMLError) as e:
            raise ValueError(f"cannot read: {str(e).splitlines()[0]}")
        if not docs:
            raise ValueError("no YAML documents")
        it["names"] = workload_names(docs)
        proj = it.get("project") or doc_project(docs)
        if not proj:
            raise ValueError("no project (pick one with p, or set metadata.namespace: runai-<project>)")
        it["project"] = proj
        for n in it["names"]:
            if self.state.find(proj, n) or (proj, n) in self.claimed:
                raise ValueError(f"workload '{n}' already exists in {proj}")
        self.claimed.update((proj, n) for n in it["names"])
        if len(docs) > 1:
            it["method"] = "kubectl apply"
            return [KUBECTL, "apply", "-f", os.path.expanduser(it["file"]), "-n", f"runai-{proj}"]
        it["method"] = "runai workload submit"
        return [RUNAI, "workload", "submit", "-f", os.path.expanduser(it["file"]), "-p", proj] + submit_flags(docs[0])

    def run(self):
        for i, it in enumerate(self.items):
            self.current = i
            it["state"], it["t0"] = "running", time.time()
            try:
                argv = self.plan(it)
            except ValueError as e:
                it["state"], it["msg"] = "fail", str(e)
                log_action(f"submit {it.get('label')}: {e}")
                continue
            rc, out, err = cmd(argv, timeout=180)
            log_action(f"submit {it.get('label')}", argv, rc, out + err)
            it["t1"] = time.time()
            if rc == 0:
                it["state"] = "ok"
                it["msg"] = first_error(out, err) or "submitted"
                for n in it.get("names") or []:
                    self.state.add_watch(it["project"], n)
                ok, _, msg = run_hooks("post_submit", "", {
                    "RUNAI_TUI_PROJECT": it["project"], "RUNAI_TUI_NAMES": " ".join(it.get("names") or []),
                    "RUNAI_TUI_FILE": os.path.expanduser(it.get("file") or ""), "RUNAI_TUI_METHOD": it["method"]},
                    it.get("label"))
                if not ok:
                    it["msg"] += f" ({msg})"
            else:
                it["state"], it["msg"] = "fail", first_error(err, out) or f"exit {rc}"
            if self.poller:
                self.poller.wake()
        self.done, self.current = True, len(self.items)
        if self.poller:
            self.poller.wake()


# --- setup checklist ---------------------------------------------------------------------
class Check:
    def __init__(self, key, title, state, detail, fix=None, help=""):
        self.key, self.title, self.state, self.detail, self.fix, self.help = key, title, state, detail, fix, help
        # fix: (label, kind, payload): kind "cmd" (argv, captured), "py" (callable), "login", "project"


def copy_skeleton():
    path = kubeconfig_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    shutil.copy(SKEL_KUBECONFIG, path)
    os.chmod(path, 0o600)
    return f"copied {SKEL_KUBECONFIG} -> {path}"


def copy_examples():
    uid, gid, user = identity()
    os.makedirs(SAVE_DIR, exist_ok=True)
    done = []
    for src in sorted(glob.glob(os.path.join(EXAMPLES, "*.yaml"))):
        dst = os.path.join(SAVE_DIR, os.path.basename(src))
        if os.path.exists(dst):
            continue
        text = open(src).read()
        mine = {"uid": str(uid), "gid": str(gid), "user": user}
        for k, placeholder in (SITE.get("example_placeholders") or {}).items():
            if k in mine and placeholder:
                text = text.replace(str(placeholder), mine[k])
        with open(dst, "w") as f:
            f.write(text)
        done.append(os.path.basename(dst))
    return f"{len(done)} examples personalised into {SAVE_DIR}" if done else "examples already there"


def run_checks(sess, state, cb=None):
    checks, total = [], 12 + len(PLUGINS.checks)

    def add(c):
        checks.append(c)
        if cb:
            cb(len(checks), total, c)

    uid, gid, user = identity()
    # 1 CLI
    if shutil.which("runai"):
        rc, out, _ = cmd([RUNAI, "version"], timeout=15)
        add(Check("cli", "runai CLI installed", "ok", (out.splitlines() or ["?"])[0] + f"  ({shutil.which('runai')})"))
    else:
        add(Check("cli", "runai CLI installed", "fail", "runai is not on your PATH",
                  help="Ask an administrator to install the Run:ai CLI (Run:ai UI: Help > Researcher "
                       "Command Line Interface)."))
    # 2 control plane URL
    cfg, site = read_cli_config(), site_url()
    cp = cli_cp_url(cfg)
    fix = ("point the CLI at " + site, "cmd", [RUNAI, "config", "set", f"--cp-url={site}", "--auto-update=disabled", "-q"])
    if cp and (cp == site or not site):
        add(Check("cpurl", "CLI points at this site", "ok", cp))
    elif not cp and not site:
        add(Check("cpurl", "CLI points at this site", "fail", "no control plane URL configured",
                  help="Run `runai config set --cp-url=https://<your Run:ai URL>`, or set site.control_plane "
                       "in /etc/runai-tui/config.yaml (administrators) or ~/.config/runai-tui/config.yaml."))
    else:
        add(Check("cpurl", "CLI points at this site", "fail" if not cp else "warn",
                  f"configured: {cp or '(nothing)'}; this site: {site}", fix,
                  "The profile hook does this on first login; it is missing if ~/.runai was removed or copied."))
    sess.base = cp or site
    # 3 login
    try:
        sess.token(force=True)
        left = sess.exp - time.time()
        add(Check("login", "Logged in to Run:ai", "ok",
                  f"{sess.email} ({sess.claims.get('name', '')}), groups {', '.join(sess.claims.get('groups') or [])}; "
                  f"token valid {dur(left)}, renews automatically", ("log in again / switch user", "login", None)))
        logged = True
    except NotLoggedIn as e:
        add(Check("login", "Logged in to Run:ai", "fail", str(e), ("log in", "login", None),
                  "Uses `runai login user -u <you>@<site>` (password prompt) or SSO in a browser. "
                  "Ten wrong passwords lock the account for a while."))
        logged = False
    # 4 clock
    skew = sess.probe_clock()
    if skew is None:
        add(Check("clock", "Clock in step with Run:ai", "warn", f"{sess.base} did not answer"))
    elif abs(skew) <= 30:
        add(Check("clock", "Clock in step with Run:ai", "ok", f"offset {skew:+.0f}s"))
    else:
        add(Check("clock", "Clock in step with Run:ai", "warn", f"this node is {abs(skew):.0f}s "
                  f"{'behind' if skew > 0 else 'ahead of'} the control plane",
                  help="Expect 'token is expired' errors. Tell an administrator (time sync on this node)."))
    # 5 default project
    projects = []
    if logged:
        try:
            projects = [p["name"] for p in (sess.api(CFG["api"]["projects"]) or {}).get("projects") or []]
        except Exception:
            pass
    proj = cli_project(cfg)
    if proj and (not projects or proj in projects):
        add(Check("project", "Default project", "ok", f"{proj}" + (f"  (you can use: {', '.join(projects)})"
                                                                 if len(projects) > 1 else ""),
                  ("choose another", "project", projects) if len(projects) > 1 else None))
    elif not logged:
        add(Check("project", "Default project", "warn", proj or "(none) - log in first"))
    elif not projects:
        add(Check("project", "Default project", "fail", "you have no projects: ask an administrator to add your group"))
    else:
        fix = (f"set {projects[0]}", "cmd", [RUNAI, "project", "set", projects[0]]) if len(projects) == 1 \
            else ("choose", "project", projects)
        add(Check("project", "Default project", "fail", f"{proj or '(none)'}; yours: {', '.join(projects)}", fix))
        proj = projects[0] if len(projects) == 1 else proj
    # 6 kubeconfig file
    kpath = kubeconfig_path()
    kc = read_kubeconfig()
    if kc.get("clusters"):
        add(Check("kubeconfig", "kubectl config file", "warn" if ENV_NOTE else "ok",
                  kpath + (f"  ({ENV_NOTE})" if ENV_NOTE else ""),
                  help="Log in as yourself (`ssh <user>@<login node>` or `su - <user>`) so your shell "
                       "does not carry someone else's KUBECONFIG." if ENV_NOTE else ""))
    else:
        add(Check("kubeconfig", "kubectl config file", "fail", f"{kpath} missing or empty",
                  ("copy the site skeleton", "py", copy_skeleton) if os.path.exists(SKEL_KUBECONFIG) else None))
    # 7 kubeconfig credentials
    kctx, kuser = kube_current(kc)
    if (kuser.get("exec") or {}).get("command", "").endswith("runai"):
        add(Check("kubecreds", "kubectl uses your Run:ai login", "ok", "exec plugin: runai auth get-token"))
    else:
        add(Check("kubecreds", "kubectl uses your Run:ai login", "fail" if logged else "warn",
                  "no runai credential plugin in the current context",
                  ("runai kubeconfig set", "cmd", [RUNAI, "kubeconfig", "set", "--bypass-validation"])
                  if logged else None, "Needed for kubectl and for saving workloads as Kubernetes YAML."))
    # 8 namespace
    want = f"runai-{proj}" if proj else ""
    if want and kctx.get("namespace") == want:
        add(Check("namespace", "kubectl namespace", "ok", want))
    else:
        add(Check("namespace", "kubectl namespace", "warn", f"{kctx.get('namespace') or '(default)'}"
                  + (f"; your project is {want}" if want else ""),
                  (f"use {want}", "cmd", [KUBECTL, "config", "set-context", "--current", f"--namespace={want}"])
                  if want and kc.get("contexts") else None))
    # 9 kubectl works
    if logged and (kuser.get("exec") or {}):
        rc, out, err = cmd([KUBECTL, "auth", "whoami", "-o", "json"], timeout=30)
        who = ""
        try:
            who = json.loads(out)["status"]["userInfo"]["username"]
        except Exception:
            pass
        rc2, out2, _ = cmd([KUBECTL, "auth", "can-i", "create", "jobs", "-n", want], timeout=30) if want else (1, "", "")
        if who == sess.email and out2.strip() == "yes":
            add(Check("kubectl", "kubectl access to your project", "ok", f"{who}: can create jobs in {want}"))
        else:
            add(Check("kubectl", "kubectl access to your project", "fail",
                      first_error(err, out) or f"whoami={who or '?'}, create jobs in {want}: {out2.strip() or 'no'}",
                      help="If whoami fails: run `runai kubeconfig set` (fix above). If access is 'no', "
                           "your group has no access rule on the project."))
    else:
        add(Check("kubectl", "kubectl access to your project", "warn", "skipped (log in and set up kubectl first)"))
    # 10 examples
    have = glob.glob(os.path.join(SAVE_DIR, "*.yaml"))
    if have:
        add(Check("examples", "Your manifests folder", "ok", f"{SAVE_DIR}: {len(have)} YAML files"))
    else:
        add(Check("examples", "Your manifests folder", "warn", f"{SAVE_DIR} has no manifests",
                  ("copy the guide's examples", "py", copy_examples) if os.path.isdir(EXAMPLES) else None,
                  "Personalised copies (your UID/GID/user) of the cli-howto.md examples."))
    # 11 home
    home = os.path.expanduser("~")
    try:
        sv = os.statvfs(home)
        free = sv.f_bavail * sv.f_frsize
        mode = oct(os.stat(home).st_mode & 0o777)
        state_ = "ok" if free > 2 * 1024 ** 3 else "warn"
        add(Check("home", "Home directory", state_, f"{home}: {human_mem(free)} free, mode {mode}, uid {uid}, gid {gid}"
                  + ("" if uid >= SITE["min_site_uid"] else "  (not a site user: /home is not mounted into pods as you)")))
    except OSError as e:
        add(Check("home", "Home directory", "warn", str(e)))
    # 12 configuration
    files = sorted({v for v in CFG_SOURCES.values() if not v.startswith("$")})
    envs = sorted({v for v in CFG_SOURCES.values() if v.startswith("$")})
    errs = CONFIG_ERRORS + PLUGINS.errors + site_binds()[1]
    detail = (", ".join(files + envs) or "built-in defaults") + (
        f"; {len(PLUGINS.loaded)} plugin(s)" if PLUGINS.loaded else "")
    add(Check("config", "runai-tui configuration", "warn" if errs else "ok",
              detail + (f"; {len(errs)} problem(s): {errs[0]}" if errs else ""),
              help="`runai-tui config` shows every value and where it comes from. Problems:\n  "
                   + "\n  ".join(errs) if errs else "Files: " + ", ".join(CONFIG_FILES)))
    # 13 theme
    tname, tsrc = theme_choice()
    themes = load_themes()
    add(Check("theme", "Colour theme", "ok" if tname in themes else "warn",
              f"{(themes.get(tname) or {}).get('name') or tname}" + (f" (from {tsrc})" if tsrc else "")
              + f"; {len(themes)} themes", fix=("choose a colour theme (also T anywhere)", "theme"),
              help="Moving through the list previews each theme; Enter keeps it. i imports a VS Code\n"
                   "colour theme (the .json in an extension's themes/ folder) or a runai-tui .yaml theme\n"
                   f"into {THEME_DIRS[-1]}/."))
    for name, fn in PLUGINS.checks:
        try:
            got = fn(sess, state)
            for c in got if isinstance(got, list) else [got] if got else []:
                add(c)
        except Exception as e:
            add(Check(f"plugin-{name}", f"plugin {name}", "warn", f"{type(e).__name__}: {e}"))
    return checks


def apply_fix(check):
    """Non-interactive fixes. -> (ok, message)."""
    label, kind, payload = check.fix
    if kind == "cmd":
        rc, out, err = cmd(payload, timeout=60)
        log_action(f"setup: {label}", payload, rc, out + err)
        return rc == 0, (first_error(out, err) if rc == 0 else first_error(err, out)) or f"exit {rc}"
    if kind == "py":
        try:
            msg = payload()
            log_action(f"setup: {label}", output=msg)
            return True, msg
        except Exception as e:
            return False, str(e)
    return False, "interactive"


# --- TUI ---------------------------------------------------------------------------------
def help_text():
    """The help screen, from the key bindings in use (keys: in the config, plugins)."""
    acts = list(ACTIONS) + [a[:4] for a in PLUGINS.actions]

    def section(where):
        return [f"   {key_name(KEYS[a]):<6} {label}" for a, _, w, label in acts if w == where]

    out = [" runai-tui: Run:ai for researchers", "",
           " Everywhere", "   1-6    Overview · Workloads · Activity · Quota · Apps · Setup     Tab  next view"]
    out += section("global")
    out += ["", " Workloads and Activity (the selected workload)",
            "   ↑/↓ j/k move   PgUp/PgDn scroll details   Enter shell (Workloads) / go to (Activity)"]
    out += section("workload")
    out += ["", " Workloads"] + section("workloads")
    out += ["", " Activity       progress of everything you submitted or have in flight"] + section("activity")
    out += ["", " Setup          the one-time checklist", "   Enter  fix the selected item"] + section("setup")
    out += ["", " Quota          GPUs by department and project (allocated, guaranteed, over quota, limit)",
            "   Enter  show that project"]
    out += ["", " Apps           AI applications (Helm charts run as a group of workloads)"] + section("apps")
    out += ["", " Form (n)", "   ↑/↓    field     Enter / ←→  edit or change     Esc  cancel"] + section("form")
    out += ["", " Marks  ★ yours   ● running   ◐ pending/starting   ✔ completed   ✖ failed", "",
            " Submitting: one object per file -> `runai workload submit -f`; files with several",
            " objects (Service, Ingress, ...) -> `kubectl apply -f`. Everything is logged to",
            f" {LOG}.", "",
            " Customize: keys, colours, site values, form defaults, hooks and plugins in",
            f" {' and '.join(CONFIG_FILES)}; `runai-tui config` shows them."]
    return "\n".join(out)


TABS = ["Overview", "Workloads", "Activity", "Quota", "Apps", "Setup"]
T_QUOTA, T_APPS, T_SETUP = TABS.index("Quota"), TABS.index("Apps"), TABS.index("Setup")
THEME_STYLES = ["head", "ok", "warn", "info", "err", "sel", "brand", "mag", "dim", "tab"]
THEME_KEYS = ["base"] + THEME_STYLES      # base: normal text and the background behind everything
COLOR_NAMES = {"default": -1, "black": 0, "red": 1, "green": 2, "yellow": 3, "blue": 4, "magenta": 5,
               "cyan": 6, "white": 7}
THEME_DIRS = ["/etc/runai-tui/themes", "~/.config/runai-tui/themes"]     # the last one is writable
THEME_CHOICE = "~/.config/runai-tui/current-theme"                       # written by the theme picker
# Popular VS Code colour themes, as "fg" or "fg on bg" per style (hex is mapped to the 256-colour palette).
BUILTIN_THEMES = {
    "default": {"name": "Default (terminal colours)"},
    "dracula": {"name": "Dracula", "base": "#f8f8f2 on #282a36", "head": "#f8f8f2 on #191a21",
                "tab": "#f8f8f2 on #44475a", "sel": "#282a36 on #bd93f9", "ok": "#50fa7b", "warn": "#f1fa8c",
                "info": "#8be9fd", "err": "#ff5555", "brand": "#ffb86c", "mag": "#ff79c6", "dim": "#6272a4"},
    "one-dark-pro": {"name": "One Dark Pro", "base": "#abb2bf on #282c34", "head": "#abb2bf on #21252b",
                     "tab": "#abb2bf on #3e4451", "sel": "#282c34 on #61afef", "ok": "#98c379", "warn": "#e5c07b",
                     "info": "#56b6c2", "err": "#e06c75", "brand": "#d19a66", "mag": "#c678dd", "dim": "#5c6370"},
    "monokai": {"name": "Monokai", "base": "#f8f8f2 on #272822", "head": "#f8f8f2 on #414339",
                "tab": "#f8f8f2 on #1e1f1c", "sel": "#272822 on #66d9ef", "ok": "#a6e22e", "warn": "#e6db74",
                "info": "#66d9ef", "err": "#f92672", "brand": "#fd971f", "mag": "#ae81ff", "dim": "#75715e"},
    "solarized-dark": {"name": "Solarized Dark", "base": "#839496 on #002b36", "head": "#93a1a1 on #00212b",
                       "tab": "#93a1a1 on #073642", "sel": "#002b36 on #268bd2", "ok": "#859900",
                       "warn": "#b58900", "info": "#2aa198", "err": "#dc322f", "brand": "#cb4b16",
                       "mag": "#d33682", "dim": "#586e75"},
}
HEX_RE = re.compile(r"^#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def hex_rgb(h):
    h = h.lstrip("#")
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def hex256(h):
    """'#rrggbb' -> the nearest xterm-256 colour (the 6x6x6 cube or the grey ramp, never 0-15)."""
    rgb = hex_rgb(h)
    lv = [0, 95, 135, 175, 215, 255]
    ci = [min(range(6), key=lambda i: abs(lv[i] - v)) for v in rgb]
    gi = max(0, min(23, round((sum(rgb) / 3 - 8) / 10)))

    def dist(c):
        return sum((a - b) ** 2 for a, b in zip(c, rgb))
    if dist([lv[i] for i in ci]) <= dist([8 + 10 * gi] * 3):
        return 16 + 36 * ci[0] + 6 * ci[1] + ci[2]
    return 232 + gi


def parse_color(spec):
    """'green', '208', '#ff79c6', 'white on 22' -> (fg, bg or None)."""
    def one(x):
        x = str(x).strip().lower()
        if x in COLOR_NAMES:
            return COLOR_NAMES[x]
        if HEX_RE.match(x):
            return hex256(x)
        if x.lstrip("-").isdigit() and -1 <= int(x) <= 255:
            return int(x)
        raise ValueError(f"'{x}' is not a colour (a name, 0-255 or #rrggbb)")
    fg, _, bg = str(spec).partition(" on ")
    return one(fg), (one(bg) if bg else None)


def load_themes():
    """-> {name: {"name": title, style: spec, ..., "_src": where}}: built-ins, then theme files
    (/etc/runai-tui/themes, ~/.config/runai-tui/themes; *.yaml, one of yours wins)."""
    out = {k: dict(v, _src="built-in") for k, v in BUILTIN_THEMES.items()}
    for d in THEME_DIRS:
        d = os.path.expanduser(d)
        for path in sorted(glob.glob(os.path.join(d, "*.yaml")) + glob.glob(os.path.join(d, "*.yml"))):
            name = os.path.splitext(os.path.basename(path))[0]
            try:
                with open(path) as f:
                    data = yaml.safe_load(f) or {}
                if not isinstance(data, dict):
                    raise ValueError("not a mapping")
                out[name] = dict({str(k): v for k, v in data.items()}, _src=path)
            except (OSError, ValueError, yaml.YAMLError) as e:
                msg = f"{path}: {str(e).splitlines()[0]}"
                if msg not in CONFIG_ERRORS:
                    CONFIG_ERRORS.append(msg)
    return out


def theme_choice():
    """-> (theme name, where the choice comes from). An env override wins, then the picker's choice
    (~/.config/runai-tui/current-theme), then theme.name in the config files."""
    src = CFG_SOURCES.get("theme.name", "")
    name = str((CFG.get("theme") or {}).get("name") or "")
    if not src.startswith("$"):
        try:
            with open(os.path.expanduser(THEME_CHOICE)) as f:
                saved = f.read().strip()
            if saved:
                return saved, THEME_CHOICE
        except OSError:
            pass
    return (name, src) if name else ("default", "")


def save_theme_choice(name):
    path = os.path.expanduser(THEME_CHOICE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(name + "\n")


def theme(name=None):
    """-> {style: (fg, bg or None)}: the chosen theme, then the theme.<style> overrides in the config.
    Bad entries are reported in Setup."""
    themes = load_themes()
    name = name or theme_choice()[0]
    if name not in themes:
        msg = f"theme '{name}': no such theme (themes: {', '.join(themes)})"
        if msg not in CONFIG_ERRORS:
            CONFIG_ERRORS.append(msg)
        name = "default"
    specs = [(k, v, f"theme {name}") for k, v in themes[name].items() if k not in ("name", "_src")]
    specs += [(k, v, "theme") for k, v in (CFG.get("theme") or {}).items() if k != "name"]
    out = {}
    for style, spec, where in specs:
        try:
            if style not in THEME_KEYS:
                raise ValueError(f"no such style (styles: {', '.join(THEME_KEYS)})")
            out[style] = parse_color(spec)
        except ValueError as e:
            msg = f"{where}.{style}: {e}" if where == "theme" else f"{where}: {style}: {e}"
            if msg not in CONFIG_ERRORS:
                CONFIG_ERRORS.append(msg)
    return out


def strip_jsonc(text):
    """VS Code theme files are JSON with comments and trailing commas."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def vscode_theme(data):
    """A VS Code colour theme (the JSON in an extension's themes/ folder) -> our styles."""
    colors = data.get("colors") or {}
    tokens = {}
    for t in data.get("tokenColors") or []:
        fg = (t.get("settings") or {}).get("foreground") if isinstance(t, dict) else None
        scope = t.get("scope") if isinstance(t, dict) else None
        for sc in scope if isinstance(scope, list) else str(scope or "").split(","):
            if fg and sc.strip():
                tokens.setdefault(sc.strip(), fg)
    bg = colors.get("editor.background")
    bg = bg if isinstance(bg, str) and HEX_RE.match(bg) else None

    def col(*keys):
        for k in keys:
            v = tokens.get(k[6:]) if k.startswith("token:") else colors.get(k)
            if isinstance(v, str) and HEX_RE.match(v):
                rgb, h = hex_rgb(v), v.lstrip("#")
                if len(h) in (4, 8):                      # alpha: blend over the editor background
                    a = int(h[-2:] if len(h) == 8 else h[-1] * 2, 16) / 255
                    under = hex_rgb(bg) if bg else (0, 0, 0)
                    rgb = tuple(round(x * a + u * (1 - a)) for x, u in zip(rgb, under))
                return "#%02x%02x%02x" % rgb
        return None

    def pair(fg, back):
        return f"{fg or 'default'} on {back}" if back else fg
    out = {
        "base": pair(col("editor.foreground", "foreground"), col("editor.background")),
        "head": pair(col("statusBar.foreground", "titleBar.activeForeground", "editor.foreground"),
                     col("statusBar.background", "titleBar.activeBackground", "activityBar.background")),
        "tab": pair(col("tab.inactiveForeground", "tab.activeForeground", "editor.foreground"),
                    col("editorGroupHeader.tabsBackground", "tab.inactiveBackground", "sideBar.background")),
        "sel": pair(col("list.activeSelectionForeground", "editor.foreground"),
                    col("list.activeSelectionBackground", "editor.selectionBackground")),
        "ok": col("terminal.ansiGreen", "token:string", "gitDecoration.addedResourceForeground"),
        "warn": col("terminal.ansiYellow", "editorWarning.foreground", "list.warningForeground"),
        "info": col("terminal.ansiCyan", "token:support.function", "terminal.ansiBlue", "textLink.foreground"),
        "err": col("terminal.ansiRed", "editorError.foreground", "errorForeground"),
        "brand": col("token:constant.numeric", "token:constant", "focusBorder", "button.background"),
        "mag": col("terminal.ansiMagenta", "token:keyword", "token:storage"),
        "dim": col("token:comment", "editorLineNumber.foreground", "descriptionForeground",
                   "terminal.ansiBrightBlack"),
    }
    return {k: v for k, v in out.items() if v}


def import_theme(path):
    """A VS Code theme (.json) or a runai-tui theme (.yaml) -> ~/.config/runai-tui/themes/<name>.yaml.
    -> the theme's name."""
    path = os.path.expanduser(path)
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    stem = os.path.splitext(os.path.basename(path))[0]
    if path.endswith((".yaml", ".yml")):
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError("not a mapping of style: colour")
        styles = {str(k): v for k, v in data.items() if k != "name"}
    else:
        try:
            data = json.loads(strip_jsonc(text))
        except ValueError as e:
            raise ValueError(f"not a VS Code theme (.json) or a .yaml theme: {e}")
        if not isinstance(data, dict):
            raise ValueError("not a VS Code colour theme")
        styles = vscode_theme(data)
        if not styles:
            raise ValueError("no colours found" + (f" (it includes {data['include']}: import that one)"
                                                   if data.get("include") else ""))
    for k, v in styles.items():
        if k not in THEME_KEYS:
            raise ValueError(f"{k}: no such style (styles: {', '.join(THEME_KEYS)})")
        parse_color(v)
    title = str(data.get("name") or stem)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "theme"
    d = os.path.expanduser(THEME_DIRS[-1])
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, slug + ".yaml"), "w") as f:
        f.write(f"# imported from {path}\n")
        yaml.safe_dump(dict({"name": title}, **styles), f, sort_keys=False, allow_unicode=True)
    return slug


PHASE_STYLE = {"Running": ("●", "ok"), "Pending": ("◐", "warn"), "Initializing": ("◐", "info"),
               "Creating": ("◐", "info"), "Resuming": ("◐", "info"), "Completed": ("✔", "dim"),
               "Succeeded": ("✔", "dim"), "Failed": ("✖", "err"), "Stopped": ("■", "dim"),
               "Suspended": ("■", "dim"), "Degraded": ("●", "warn"), "Deleting": ("✖", "dim"),
               "Stopping": ("■", "dim"), "Terminating": ("✖", "dim"), "Gone": ("✖", "dim"),
               "Submitted": ("◌", "info")}


class Tui:
    def __init__(self, scr, sess, state, poller, project=None, tab=0):
        self.scr, self.sess, self.st, self.poller = scr, sess, state, poller
        self.tab = tab
        self.project = project or cli_project() or ""
        self.mine, self.filter = True, ""
        self.sel = {"wl": 0, "act": 0, "setup": 0, "quota": 0, "apps": 0}
        self.top = {"wl": 0, "act": 0, "setup": 0, "quota": 0, "apps": 0}
        self.after_start = None          # {project, name, attach, t0}: G in the form, attach/shell once it runs
        self.dscroll = 0
        self.status, self.status_at = "", 0
        self.checks, self.check_progress = [], None
        self.tick = 0
        self.dump = os.environ.get("RUNAI_TUI_SCREEN")       # test hook: plain-text copy of each frame
        self.shadow = {}
        curses.curs_set(0)
        curses.use_default_colors()
        self.apply_theme()

    def apply_theme(self, name=None):
        """(Re)define the colour pairs from a theme; what is on screen changes with them."""
        c256 = curses.COLORS >= 256
        pairs = dict(zip(THEME_STYLES, [
            (curses.COLOR_WHITE, 17 if c256 else curses.COLOR_BLUE), (curses.COLOR_GREEN, -1),
            (curses.COLOR_YELLOW, -1), (curses.COLOR_CYAN, -1), (curses.COLOR_RED, -1),
            (curses.COLOR_BLACK, curses.COLOR_CYAN), (208 if c256 else curses.COLOR_YELLOW, -1),
            (curses.COLOR_MAGENTA, -1), (244 if c256 else curses.COLOR_WHITE, -1),
            (curses.COLOR_WHITE, 24 if c256 else curses.COLOR_BLUE)]))
        base = (-1, -1)
        for style, (fg, bg) in theme(name).items():     # colours this terminal lacks keep the built-in ones
            if max(fg, bg if bg is not None else -1) >= curses.COLORS:
                continue
            if style == "base":
                base = (fg, -1 if bg is None else bg)
            else:
                pairs[style] = (fg, pairs[style][1] if bg is None else bg)
        for i, style in enumerate(THEME_STYLES, start=1):   # "terminal default" means the theme's base
            fg, bg = pairs[style]
            curses.init_pair(i, base[0] if fg == -1 else fg, base[1] if bg == -1 else bg)
        nbase = len(THEME_STYLES) + 1
        curses.init_pair(nbase, *base)
        self.scr.bkgd(" ", curses.color_pair(nbase))
        self.C = {k: curses.color_pair(i) for i, k in enumerate(THEME_STYLES, start=1)}
        self.C["dim"] |= curses.A_DIM if not c256 else 0
        self.C[None] = 0
        self.C["bold"] = curses.A_BOLD
        self.C["pulse"] = self.C["brand"] | curses.A_BOLD

    # --- helpers -----------------------------------------------------------------------
    def flash(self, msg):
        self.status, self.status_at = msg, time.time()

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w or x < 0:
            return
        width = min(width if width is not None else w - x, w - x)
        if y == h - 1:
            width = min(width, w - x - 1)          # never write the last cell
        if width <= 0:
            return
        if self.dump:
            row = self.shadow.setdefault(y, [" "] * w)
            for i, ch in enumerate(text[:width]):
                if x + i < len(row):
                    row[x + i] = ch
        try:
            self.scr.addnstr(y, x, text, width, attr if isinstance(attr, int) else self.C.get(attr, 0))
        except curses.error:
            pass

    def segs(self, y, x, segments, width):
        """Draw [(text, style)] left to right, clipped to width."""
        end = x + width
        for text, style in segments:
            if x >= end:
                break
            attr = style if isinstance(style, int) else self.C.get(style, 0)
            self.put(y, x, text, attr, end - x)
            x += len(text)
        return x

    def box(self, y, x, h, w, title, focused=False):
        attr = self.C["brand"] | curses.A_BOLD if focused else self.C["dim"]
        try:
            self.scr.attron(attr)
            self.scr.addch(y, x, curses.ACS_ULCORNER)
            self.scr.hline(y, x + 1, curses.ACS_HLINE, w - 2)
            self.scr.addch(y, x + w - 1, curses.ACS_URCORNER)
            self.scr.vline(y + 1, x, curses.ACS_VLINE, h - 2)
            self.scr.vline(y + 1, x + w - 1, curses.ACS_VLINE, h - 2)
            self.scr.addch(y + h - 1, x, curses.ACS_LLCORNER)
            self.scr.hline(y + h - 1, x + 1, curses.ACS_HLINE, w - 2)
            self.scr.insch(y + h - 1, x + w - 1, curses.ACS_LRCORNER)
        except curses.error:
            pass
        finally:
            self.scr.attroff(attr)
        if title:
            self.put(y, x + 2, f" {title} ", attr, w - 4)

    def cells(self, y, x, cells, attr_map):
        for i, (ch, kind) in enumerate(cells):
            self.put(y, x + i, ch, self.C.get(attr_map.get(kind, kind), 0), 1)

    def scroll_list(self, key, n, rows):
        sel = self.sel[key] = max(0, min(self.sel[key], n - 1))
        if sel < self.top[key]:
            self.top[key] = sel
        elif sel >= self.top[key] + rows:
            self.top[key] = sel - rows + 1
        self.top[key] = max(0, min(self.top[key], max(0, n - rows)))
        return self.top[key], sel

    # --- data views ----------------------------------------------------------------------
    def visible_workloads(self):
        f = self.filter.lower()
        with self.st.lock:
            out = [w for w in self.st.workloads
                   if (not self.project or w.get("projectName") == self.project)
                   and (not self.mine or self.st.mine(w))
                   and (not f or f in w.get("name", "").lower() or f in (w.get("phase") or "").lower()
                        or f in (w.get("type") or "").lower() or f in " ".join(w.get("images") or []).lower())]
        return out

    def selected(self):
        ws = self.visible_workloads()
        return ws[min(self.sel["wl"], len(ws) - 1)] if ws else None

    def projects(self):
        with self.st.lock:
            return [p["name"] for p in self.st.projects]

    # --- drawing ----------------------------------------------------------------------------
    def draw(self):
        self.tick += 1
        self.scr.erase()
        self.shadow = {}
        h, w = self.scr.getmaxyx()
        if h < 16 or w < 80:
            self.put(0, 0, "terminal too small for runai-tui (need 80x16)", self.C["err"])
            self.scr.refresh()
            return
        st = self.st
        who = self.sess.email or "not logged in"
        head = f" ▲ {site_title(self.sess.claims)} · Run:ai   {who}"
        self.put(0, 0, head.ljust(w), self.C["head"] | curses.A_BOLD)
        if st.logged_in is False:
            right = f"✖ not logged in ({T_SETUP + 1} → Setup)"
        elif st.error:
            right = f"✖ {st.error[:50]}"
        elif st.updated:
            left = self.sess.exp - time.time()
            spin = SPIN[self.tick % len(SPIN)] if st.in_flight() else "●"
            right = f"{spin} {self.sess.base.replace('https://', '')}  token {dur(left)}  updated {dur(time.time() - st.updated)} ago"
        else:
            right = f"{SPIN[self.tick % len(SPIN)]} connecting to {self.sess.base} ..."
        self.put(0, max(len(head) + 2, w - len(right) - 1), right, self.C["head"])
        # tabs
        x = 0
        for i, name in enumerate(TABS):
            label = f" {i + 1} {name} "
            badge = ""
            if name == "Activity":
                n = sum(1 for wl in st.watched() if not settled(wl))
                badge = f"({n})" if n else ""
            if name == "Setup" and self.checks:
                bad = sum(1 for c in self.checks if c.state == "fail")
                badge = f"(!{bad})" if bad else ""
            self.put(1, x, label + badge + " ", (self.C["sel"] | curses.A_BOLD) if i == self.tab else self.C["tab"])
            x += len(label + badge) + 2
        proj = f" project: {self.project or 'all'} (p) "
        self.put(1, x, " " * max(0, w - x), self.C["tab"])
        self.put(1, w - len(proj) - 1, proj, self.C["tab"] | curses.A_BOLD)
        body = (2, h - 4)                      # top row, height
        [self.draw_overview, self.draw_workloads, self.draw_activity, self.draw_org, self.draw_apps,
         self.draw_setup][self.tab](*body, w)
        # status + keys
        if self.status and time.time() - self.status_at > 12:
            self.status = ""
        msg = self.status or (st.error and f"⚠ {st.error}") or ""
        self.put(h - 2, 0, msg.ljust(w), self.C["warn"] if msg else 0)
        K = lambda a: key_name(KEYS[a])
        keys = {0: [(K("new"), "new"), (K("submit_files"), "submit files"), (K("project"), "project"),
                    ("2", "workloads")],
                1: [("⏎", "shell"), (K("attach"), "attach"), (K("logs"), "logs"), (K("describe"), "describe"),
                    (K("save"), "save YAML"),
                    (K("delete"), "delete"), (K("toggle_mine"), "mine/all"), (K("filter"), "filter"),
                    (K("new"), "new")],
                2: [("⏎", "go to"), (K("forget_done"), "forget done"), (K("new"), "new"),
                    (K("submit_files"), "submit files")],
                T_SETUP: [("⏎", "fix"), (K("fix_all"), "fix all"), (K("login"), "log in"),
                          (K("logout"), "log out"), (K("refresh"), "re-check")],
                T_QUOTA: [("⏎", "use project"), (K("project"), "project"), (K("refresh"), "refresh")],
                T_APPS: [("⏎", "describe"), (K("app_submit"), "submit"), (K("app_delete"), "delete"),
                    (K("refresh"), "refresh")]}[self.tab]
        keys += [(K("help"), "help"), (K("quit"), "quit")]
        x = 0
        for k, label in keys:
            if x + len(k) + len(label) + 4 > w:
                break
            self.put(h - 1, x, f" {k} ", self.C["sel"] | curses.A_BOLD)
            self.put(h - 1, x + len(k) + 2, f"{label} ", 0)
            x += len(k) + len(label) + 4
        self.scr.refresh()
        if self.dump:
            self.dump_screen()

    def dump_screen(self):
        h, w = self.scr.getmaxyx()
        try:
            rows = ["".join(self.shadow.get(y, [])).rstrip() for y in range(h)]
            with open(self.dump + ".tmp", "w") as f:
                f.write("\n".join(rows) + "\n")
            os.replace(self.dump + ".tmp", self.dump)          # readers never see a half-written frame
        except OSError:
            pass

    # Overview ---------------------------------------------------------------------
    def draw_overview(self, top, height, w):
        st, s = self.st, self.sess
        cl = s.claims
        left_w = max(38, w * 38 // 100) if w >= 110 else w
        lines = []
        if cl:
            lines += [[(cl.get("name") or cl.get("email", ""), "bold")],
                      [("email   ", "dim"), (cl.get("email", ""), None)],
                      [("uid/gid ", "dim"), (f"{cl.get('uid', '?')}/{cl.get('gid', '?')}", None)],
                      [("groups  ", "dim"), (", ".join(cl.get("groups") or []) or "-", None)],
                      [("token   ", "dim"), (f"valid {dur(s.exp - time.time())} (renews itself)", None)]]
        else:
            lines += [[("Not logged in.", "err")], [(f"Press {T_SETUP + 1} (Setup) and i to log in.", None)]]
        lines += [[("site    ", "dim"), (s.base, None)], [("", None)]]
        with st.lock:
            mine = [x for x in st.workloads if st.mine(x)]
        by = {}
        for x in mine:
            by[x.get("phase")] = by.get(x.get("phase"), 0) + 1
        gpu = sum(wl_gpu(x) for x in mine if x.get("phase") == "Running")
        cpu = sum((x.get("allocatedResources") or {}).get("cpu") or 0 for x in mine if x.get("phase") == "Running")
        mem = sum(mem_bytes((x.get("allocatedResources") or {}).get("cpuMemory")) for x in mine
                  if x.get("phase") == "Running")
        lines.append([("Your workloads", "brand")])
        if mine:
            lines.append([("  " + "  ".join(f"{PHASE_STYLE.get(k, ('·',))[0]} {v} {k.lower()}"
                                            for k, v in sorted(by.items(), key=lambda kv: -kv[1])), None)])
        else:
            lines.append([("  none yet: n creates one, F submits a YAML file", "dim")])
        lines.append([("  in use  ", "dim"), (f"{num(gpu)} GPU · {num(cpu)} CPU · {human_mem(mem)} memory", None)])
        if self.checks:
            bad = [c for c in self.checks if c.state == "fail"]
            if bad:
                lines += [[("", None)], [(f"⚠ {len(bad)} setup item(s) need attention: ", "warn"),
                                         (", ".join(c.title for c in bad)[:left_w], None)],
                          [(f"  press {T_SETUP + 1} (Setup), then A to fix", "dim")]]
        box_h = len(lines) + 2 if w >= 110 else min(len(lines) + 2, height // 2)
        if w >= 110:
            box_h = height - min(height // 3, 12) if st.watch else height
        self.box(top, 0, box_h, left_w, "You")
        for i, segs in enumerate(lines[:box_h - 2]):
            self.segs(top + 1 + i, 2, segs, left_w - 4)
        if w >= 110:
            qx, qw, qy, qh = left_w, w - left_w, top, box_h
        else:
            qx, qw, qy, qh = 0, w, top + box_h, height - box_h
        self.draw_quota(qy, qx, qh, qw)
        if w >= 110 and st.watch:
            ay = top + box_h
            self.draw_tracker(ay, 0, height - box_h, w, compact=True)

    def draw_quota(self, y, x, h, w):
        if h < 4:
            return
        with self.st.lock:
            projects = [p for p in self.st.projects if not self.project or p["name"] == self.project]
        self.box(y, x, h, w, f"Quota · {self.project or 'all projects'}")
        row, inner = y + 1, w - 4
        if not projects:
            self.put(row, x + 2, "no project data yet" if self.st.logged_in is not False else "log in to see quotas",
                     self.C["dim"])
            return
        bw = max(10, min(30, inner - 52))
        for p in projects:
            if row >= y + h - 1:
                break
            dept = (p.get("parent") or {}).get("name", "")
            total = (p.get("totalResources") or {}).get("gpuQuota")
            self.segs(row, x + 2, [(p["name"], "brand"), (f"  department {dept}" if dept else "", "dim"),
                                   (f"   {num(total)} GPU guaranteed" if total is not None else "", None)], inner)
            row += 1
            self.segs(row, x + 2, [(f"{'pool':<9}{'GPU used / quota':<{bw + 2}}", "dim"),
                                   ("alloc quota limit  non-pre  CPU     memory", "dim")], inner)
            row += 1
            for q in project_pools(p):
                if not pool_visible(q):
                    continue
                if row >= y + h - 1:
                    break
                self.put(row, x + 2, f"{q['pool']:<9}", self.C["bold"])
                self.cells(row, x + 11, quota_cells(bw, q["gpu"], q["quota"], q["limit"]), {})
                over = q["gpu"] > q["quota"] + 1e-9
                txt = (f"  {num(q['gpu']):>5} {num(q['quota']):>5} {num(q['limit']):>5}  {num(q['gpu_np']):>7}  "
                       f"{num(q['cpu']):>5}  {human_mem(q['mem']):>7}")
                self.put(row, x + 11 + bw, txt, self.C["warn"] if over else 0, inner - bw - 9)
                row += 1
                if q["gpu_req"] > q["gpu"] + 1e-9 and row < y + h - 1:
                    self.put(row, x + 11, f"↳ {num(q['gpu_req'] - q['gpu'])} GPU requested but pending",
                             self.C["warn"], inner - 9)
                    row += 1
            ot = p.get("overtimeData") or {}
            d24, d7 = ot.get("range24hData") or {}, ot.get("range7dData") or {}
            if (d24 or d7) and row < y + h - 1:
                self.segs(row, x + 2, [("avg GPU utilisation ", "dim"),
                                       (f"24h {num(d24.get('averageGpuUtilization'), 0)}%", None),
                                       ("  ·  GPU memory ", "dim"),
                                       (f"{num(d24.get('averageGpuMemoryUtilization'), 0)}%", None),
                                       ("  ·  7d avg allocation ", "dim"),
                                       (f"{num(d7.get('averageGpuAllocation'))} GPU", None)], inner)
                row += 1
            row += 1
        notes = ["┆ = guaranteed quota.  Green: within quota.  Yellow: over quota, preemptible only.",
                 "Non-preemptible (Deployments, workspaces) must fit in quota - 'non-pre' column.",
                 "Batch Jobs may borrow idle GPUs beyond quota and are preempted when the owner needs them."]
        for n in notes:
            if row < y + h - 1:
                self.put(row, x + 2, n, self.C["dim"], inner)
                row += 1

    # Workloads -------------------------------------------------------------------------
    def draw_workloads(self, top, height, w):
        ws = self.visible_workloads()
        lw = max(46, w * 45 // 100)
        flt = f"  /{self.filter}" if self.filter else ""
        self.box(top, 0, height, lw, f"Workloads {len(ws)} · {'mine' if self.mine else 'everyone'}{flt}", True)
        rows = height - 3
        nw = max(10, lw - 41)
        self.put(top + 1, 2, f"  {'NAME':<{nw}} {'TYPE':<10} {'PHASE':<11} {'GPU':>4} {'AGE':>6}", self.C["dim"], lw - 3)
        t0, sel = self.scroll_list("wl", len(ws), rows)
        now = time.time()
        for i, wl in enumerate(ws[t0:t0 + rows]):
            y = top + 2 + i
            mark, style = PHASE_STYLE.get(wl.get("phase"), ("·", None))
            star = "★" if self.st.mine(wl) else " "
            age = dur(now - (parse_ts(wl.get("createdAt")) or now))
            g = wl_gpu(wl) or wl_gpu_req(wl)
            text = f"{wl['name'][:nw]:<{nw}} {(wl.get('type') or '')[:10]:<10} {(wl.get('phase') or '')[:11]:<11} " \
                   f"{num(g) if g else '':>4} {age:>6}"
            if t0 + i == sel:
                self.put(y, 1, f"{star}{mark} {text}".ljust(lw - 2), self.C["sel"] | curses.A_BOLD, lw - 2)
            else:
                self.put(y, 1, star, self.C["brand"], 1)
                self.put(y, 2, mark, self.C[style], 1)
                self.put(y, 4, text, 0, lw - 5)
        if not ws:
            msg = ("loading ..." if not self.st.updated else
                   "no workloads" + (" of yours (a shows everyone)" if self.mine else "") + " - n creates one")
            self.put(top + 3, 3, msg, self.C["dim"], lw - 5)
        if len(ws) > rows:
            self.put(top + height - 1, lw - 10, f" {sel + 1}/{len(ws)} ", self.C["dim"])
        dw = w - lw
        wl = ws[sel] if ws else None
        self.box(top, lw, height, dw, f"Details · {wl['name']}" if wl else "Details")
        if not wl:
            return
        if self.st.focus_id != wl["id"]:
            self.st.focus_id = wl["id"]
            self.dscroll = 0
            self.poller.wake()
        lines = []
        for text, style in self.detail_lines(wl):
            for part in (textwrap.wrap(text, dw - 4, subsequent_indent="    ") or [""]):
                lines.append((part, style))
        self.dscroll = max(0, min(self.dscroll, len(lines) - (height - 2)))
        for i, (text, style) in enumerate(lines[self.dscroll:self.dscroll + height - 2]):
            if style == "BAR":
                self.draw_progress(top + 1 + i, lw + 2, dw - 4, wl)
            else:
                self.put(top + 1 + i, lw + 2, text, self.C.get(style, 0), dw - 4)

    def detail_lines(self, w):
        L = []
        ph = w.get("phase") or "?"
        mark, style = PHASE_STYLE.get(ph, ("·", None))
        L.append((f"{mark} {ph}" + (f"  ({w.get('k8sPhase')})" if w.get("k8sPhase") and w.get("k8sPhase") != ph else ""),
                  style))
        L.append(("", "BAR"))
        L.append(("", None))
        reason = pending_reason(w)
        if reason:
            L.append((f"why      {reason}", "warn"))
        L.append((f"type     {w.get('type')}  ·  category {w.get('category', '-')}  ·  {w.get('preemptibility', '')}"
                  f"  ·  priority {w.get('priorityClassName') or w.get('priority', '-')}", None))
        L.append((f"project  {w.get('projectName')}  (namespace {w.get('namespace')})  ·  by "
                  f"{w.get('submittedBy') or 'kubectl (no submitter recorded)'}",
                  None))
        L.append((f"created  {w.get('createdAt', '')[:19].replace('T', ' ')} UTC  ·  pending "
                  f"{dur(w.get('totalPendingTimeSeconds'))}  ·  running {dur(w.get('totalRunningTimeSeconds'))}", None))
        rp = w.get("requestedPods") or {}
        L.append((f"pods     {w.get('runningPods', 0)} running of {rp.get('parallelism', 1)} "
                  f"(completions {rp.get('completions', '-')})", None))
        req, alloc = w.get("workloadRequestedResources") or {}, w.get("allocatedResources") or {}
        g = req.get("gpu") or {}
        rq = []
        if g:
            rq.append(f"GPU {num(g.get('request') or g.get('limit'))}")
        if (req.get("gpuMemory") or {}).get("request"):
            rq.append(f"GPU mem {req['gpuMemory']['request']}")
        if req.get("cpu"):
            rq.append(f"CPU {num(req['cpu'].get('request'))}" + (f"-{num(req['cpu']['limit'])}"
                                                                 if req['cpu'].get('limit') else ""))
        if req.get("cpuMemory"):
            rq.append(f"mem {req['cpuMemory'].get('request', '')}" + (f"-{req['cpuMemory']['limit']}"
                                                                      if req['cpuMemory'].get('limit') else ""))
        L.append((f"requests {' · '.join(rq) or '-'}", None))
        al = [f"GPU {num(alloc['gpu'])}"] if alloc.get("gpu") else []
        al += [f"GPU mem {alloc['gpuMemory']}"] if alloc.get("gpuMemory") else []
        al += [f"CPU {num(alloc['cpu'])}"] if alloc.get("cpu") else []
        al += [f"mem {alloc['cpuMemory']}"] if alloc.get("cpuMemory") else []
        L.append((f"alloc    {' · '.join(al) or '-'}", "ok" if al else "dim"))
        L.append((f"pools    requested {', '.join(w.get('requestedNodePools') or []) or 'any'}  ·  "
                  f"running in {', '.join(w.get('currentNodePools') or []) or '-'}", None))
        for img in w.get("images") or []:
            L.append((f"image    {img}", None))
        for u in w.get("urls") or []:
            L.append((f"url      {u}   (u copies)", "brand"))
        for e in w.get("environments") or []:
            for c in e.get("connections") or []:
                if not c.get("url"):
                    L.append((f"tool     {c.get('name')} on port {c.get('containerPort')} (f port-forwards)", "info"))
        cli = (w.get("additionalFields") or {}).get("cliCommand")
        if cli:
            L.append((f"cli      {cli}", "dim"))
        L.append(("", None))
        with self.st.lock:
            pods = self.st.pods.get(w["id"])
        if pods is None:
            L.append(("pods: loading ...", "dim"))
        elif isinstance(pods[1], str):
            L.append((f"pods: {pods[1]}", "err"))
        else:
            L.append((f"Pods ({len(pods[1])})", "info"))
            for p in pods[1]:
                conts = ", ".join(c.get("name", "") for c in p.get("containers") or [])
                L.append((f"  {p.get('name')}  {p.get('k8sPhase')}  {p.get('nodeName') or '-'}"
                          f" [{p.get('currentNodePool') or '-'}]  {p.get('ip') or ''}  ({conts})",
                          "ok" if p.get("k8sPhase") == "Running" else None))
        h = self.st.history.get(w["id"]) or []
        if len(h) > 1:
            L += [("", None), ("Seen phases", "info")]
            for i, (phase, t) in enumerate(h):
                nxt = h[i + 1][1] if i + 1 < len(h) else time.time()
                L.append((f"  {datetime.datetime.fromtimestamp(t):%H:%M:%S}  {phase:<13} {dur(nxt - t)}", None))
        return L

    def draw_progress(self, y, x, width, w, labels=False):
        frac, colour, stages, idx, anim = progress(w, self.st.history.get(w.get("id")))
        bw = max(10, width - 8)
        cells = bar_cells(bw, frac, anim, self.tick)
        self.cells(y, x, cells, {"fill": colour, "pulse": "pulse", "empty": "dim"})
        self.put(y, x + bw + 1, f"{int(frac * 100):>3}%", self.C[colour])
        if labels:
            n = len(stages) - 1
            for i, s in enumerate(stages):
                pos = x + int(i / n * (bw - 1)) - (len(s) // 2 if 0 < i < n else (len(s) - 1 if i == n else 0))
                pos = max(x, min(pos, x + bw - len(s)))
                if i < idx:
                    style = "ok"
                elif i == idx:
                    style = colour
                else:
                    style = "dim"
                self.put(y + 1, pos, s, self.C[style] | (curses.A_BOLD if i == idx else 0))

    # Activity ----------------------------------------------------------------------------
    def draw_activity(self, top, height, w):
        self.draw_tracker(top, 0, height, w)

    def draw_tracker(self, y, x, h, w, compact=False):
        st = self.st
        b = st.batch
        row = y + 1
        title = "In flight" if compact else "Activity · submissions and workloads in flight"
        self.box(y, x, h, w, title, not compact)
        inner = w - 4
        if b and not compact:
            done = sum(1 for it in b.items if it.get("state") in ("ok", "fail"))
            ok = sum(1 for it in b.items if it.get("state") == "ok")
            bad = sum(1 for it in b.items if it.get("state") == "fail")
            self.segs(row, x + 2, [("Submission ", "brand"),
                                   (f"{done}/{len(b.items)}", "bold"),
                                   (f"  ✔ {ok}" if ok else "", "ok"), (f"  ✖ {bad}" if bad else "", "err"),
                                   ("  done" if b.done else f"  {SPIN[self.tick % len(SPIN)]} submitting", "dim")],
                      inner)
            row += 1
            frac = done / max(1, len(b.items))
            bw = inner - 6
            self.cells(row, x + 2, bar_cells(bw, frac, not b.done, self.tick),
                       {"fill": "err" if bad and b.done and not ok else ("ok" if b.done else "info"),
                        "pulse": "pulse", "empty": "dim"})
            self.put(row, x + 3 + bw, f"{int(frac * 100):>3}%", 0)
            row += 1
            for it in b.items[-max(1, min(len(b.items), h // 4)):]:
                icon, style = {"ok": ("✔", "ok"), "fail": ("✖", "err"), "running": (SPIN[self.tick % 10], "info")}.get(
                    it.get("state"), ("·", "dim"))
                self.segs(row, x + 2, [(f" {icon} ", style), (f"{it.get('label', '')}", "bold"),
                                       (f"  {it.get('method', '')}", "dim"), (f"  {it.get('msg', '')}", style)], inner)
                row += 1
            row += 1
        ws = st.watched()
        if not ws:
            self.put(row, x + 2, "nothing in flight: n creates a workload, F submits YAML files", self.C["dim"], inner)
            return
        per = 2 if compact else 3
        avail = (y + h - 1 - row) // per
        t0, sel = self.scroll_list("act", len(ws), max(1, avail))
        for i, wl in enumerate(ws[t0:t0 + avail]):
            ph = wl.get("phase") or "?"
            mark, style = PHASE_STYLE.get(ph, ("·", None))
            hist = st.history.get(wl.get("id")) or []
            since = hist[-1][1] if hist else None
            in_phase = dur(time.time() - since) if since else ""
            g = wl_gpu(wl) or wl_gpu_req(wl)
            focused = (t0 + i == sel) and not compact
            reason = pending_reason(wl) if ph == "Pending" else ""
            self.segs(row, x + 2, [("▶ " if focused else "  ", "brand"), (f"{mark} ", style),
                                   (wl.get("name", ""), "bold"),
                                   (f"  {wl.get('projectName', '')} · {wl.get('type') or ''}", "dim"),
                                   (f"  {ph}", style), (f" {in_phase}" if in_phase else "", "dim"),
                                   (f"  GPU {num(g)}" if g else "", None),
                                   (f"  - {reason}" if reason else "", "warn")], inner)
            self.draw_progress(row + 1, x + 4, inner - 2, wl, labels=not compact)
            row += per

    # Setup ---------------------------------------------------------------------------------
    def draw_setup(self, top, height, w):
        lw = max(44, w * 42 // 100)
        self.box(top, 0, height, lw, "Setup checklist", True)
        if self.check_progress:
            i, n, cur = self.check_progress
            self.put(top + 1, 2, f"{SPIN[self.tick % 10]} checking {i}/{n}  {cur}", self.C["info"], lw - 4)
            self.cells(top + 2, 2, bar_cells(lw - 6, i / max(n, 1), True, self.tick),
                       {"fill": "info", "pulse": "pulse", "empty": "dim"})
        row0 = top + (4 if self.check_progress else 1)
        t0, sel = self.scroll_list("setup", len(self.checks), height - (row0 - top) - 1)
        for i, c in enumerate(self.checks[t0:]):
            y = row0 + i
            if y >= top + height - 1:
                break
            icon, style = {"ok": ("✔", "ok"), "warn": ("⚠", "warn"), "fail": ("✖", "err")}.get(c.state, ("·", None))
            line = f"{icon} {c.title}" + ("  [fix]" if c.fix and c.state != "ok" else "")
            if t0 + i == sel:
                self.put(y, 1, f" {line}".ljust(lw - 2), self.C["sel"] | curses.A_BOLD, lw - 2)
            else:
                self.put(y, 2, icon, self.C[style], 1)
                self.put(y, 4, line[2:], 0, lw - 6)
        ok = sum(1 for c in self.checks if c.state == "ok")
        if self.checks and not self.check_progress:
            self.put(top + height - 1, 2, f" {ok}/{len(self.checks)} ok ", self.C["ok" if ok == len(self.checks)
                                                                                  else "warn"])
        dw = w - lw
        self.box(top, lw, height, dw, "Details")
        if not self.checks:
            return
        c = self.checks[sel]
        L = [(c.title, "brand"), ("", None), (c.detail, None), ("", None)]
        if c.fix and c.state != "ok":
            L.append((f"Enter: {c.fix[0]}", "info"))
            if c.fix[1] == "cmd":
                L.append(("  $ " + " ".join(shlex.quote(a) for a in c.fix[2]), "dim"))
        elif c.fix:
            L.append((f"Enter: {c.fix[0]}", "dim"))
        if c.help:
            L += [("", None), (c.help, None)]
        L += [("", None), ("What setup gives you", "info"),
              ("runai CLI talks to this site, you are logged in (token renews itself for days),", "dim"),
              ("a default project for -p, and kubectl with your Run:ai login in your project's", "dim"),
              ("namespace (needed for kubectl apply and YAML export).", "dim")]
        row = top + 1
        for text, style in L:
            for part in [p for line in text.split("\n") for p in textwrap.wrap(line, dw - 4) or [""]]:
                if row >= top + height - 1:
                    return
                self.put(row, lw + 2, part, self.C.get(style, 0), dw - 4)
                row += 1

    # --- input widgets ---------------------------------------------------------------
    def prompt(self, label, default="", complete_paths=False, secret=False, at=None, redraw=None):
        """One-line editor on the status line, or at (y, x, width) with the label as given.
        redraw() repaints behind it after Tab lists matches. Esc cancels (None)."""
        buf = list(default)
        curses.curs_set(1)
        self.scr.timeout(-1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                text = "".join(buf)
                shown = "*" * len(text) if secret else text
                if at:
                    y, x, width = at
                    line = f"{label}{shown}"
                    self.put(y, x, line[-(width - 1):].ljust(width), self.C["sel"] | curses.A_BOLD, width)
                    self.scr.move(y, x + min(len(line), width - 1))
                else:
                    line = f"{label}: {shown}"
                    self.put(h - 2, 0, line[-(w - 1):].ljust(w - 1), self.C["info"] | curses.A_BOLD)
                    self.scr.move(h - 2, min(len(line), w - 2))
                self.scr.refresh()
                if self.dump:
                    self.dump_screen()
                ch = self.scr.get_wch()
                if ch in ("\n", "\r", curses.KEY_ENTER):
                    return "".join(buf).strip()
                if ch == "\x1b":
                    return None
                if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    buf = buf[:-1]
                elif ch == "\x15":
                    buf = []
                elif ch == "\t" and complete_paths:
                    word = text.split(" ")[-1]
                    matches = sorted(glob.glob(os.path.expanduser(word) + "*"))
                    if len(matches) == 1:
                        m = matches[0] + ("/" if os.path.isdir(matches[0]) else "")
                        buf = list(text[:len(text) - len(word)] + m)
                    elif matches:
                        buf = list(text[:len(text) - len(word)] + os.path.commonprefix(matches))
                        self.flash("  ".join(os.path.basename(m.rstrip("/")) for m in matches[:14]))
                        (redraw or self.draw)()
                elif isinstance(ch, str) and ch.isprintable():
                    buf.append(ch)
        finally:
            curses.curs_set(0)
            self.scr.timeout(250)

    def confirm(self, question, default=False):
        ans = self.prompt(f"{question} [{'Y/n' if default else 'y/N'}]")
        if ans is None:
            return None
        return default if not ans else ans.lower().startswith("y")

    def pick(self, title, options, current=None):
        """Modal list; -> index or None."""
        if not options:
            return None
        sel = options.index(current) if current in options else 0
        self.scr.timeout(-1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                bw = min(w - 4, max(len(title) + 6, max(len(str(o)) for o in options) + 8))
                bh = min(h - 4, len(options) + 2)
                y0, x0 = (h - bh) // 2, (w - bw) // 2
                for i in range(bh):
                    self.put(y0 + i, x0, " " * bw, 0)
                self.box(y0, x0, bh, bw, title, True)
                top = max(0, sel - (bh - 3))
                for i, o in enumerate(options[top:top + bh - 2]):
                    attr = self.C["sel"] | curses.A_BOLD if top + i == sel else 0
                    self.put(y0 + 1 + i, x0 + 1, f" {o}".ljust(bw - 2), attr, bw - 2)
                self.scr.refresh()
                if self.dump:
                    self.dump_screen()
                k = self.scr.getch()
                if k in (27, ord("q")):
                    return None
                if k in (10, 13, curses.KEY_ENTER):
                    return sel
                if k in (curses.KEY_DOWN, ord("j")):
                    sel = min(len(options) - 1, sel + 1)
                elif k in (curses.KEY_UP, ord("k")):
                    sel = max(0, sel - 1)
        finally:
            self.scr.timeout(250)

    def choose_theme(self):
        """Theme picker: moving previews the theme, Enter keeps it, i imports a VS Code theme."""
        start = theme_choice()[0]
        current, msg = start, ""
        self.scr.timeout(-1)
        try:
            while True:
                self.scr.timeout(-1)                    # prompt() and confirm() leave it at 250 ms
                themes = load_themes()
                names = list(themes)
                if current not in names:
                    current = "default"
                sel = names.index(current)
                self.apply_theme(current)
                self.draw()
                h, w = self.scr.getmaxyx()
                bw = min(w - 4, 64)
                rows = min(len(names), max(3, h - 12))
                bh = rows + 7
                y0, x0 = max(1, (h - bh) // 2), (w - bw) // 2
                for i in range(bh):
                    self.put(y0 + i, x0, " " * bw, 0)
                self.box(y0, x0, bh, bw, f"Colour theme ({len(names)})", True)
                top = max(0, min(sel - rows + 1, len(names) - rows))
                user_dir = os.path.expanduser(THEME_DIRS[-1])
                for i, n in enumerate(names[top:top + rows]):
                    t = themes[n]
                    src = ("built-in" if t["_src"] == "built-in" else
                           "yours" if t["_src"].startswith(user_dir) else "site")
                    mark = "●" if n == start else " "
                    line = f" {mark} {str(t.get('name') or n)[:bw - 18]}".ljust(bw - 12) + src.rjust(8)
                    attr = self.C["sel"] | curses.A_BOLD if top + i == sel else 0
                    self.put(y0 + 1 + i, x0 + 1, line.ljust(bw - 2), attr, bw - 2)
                y = y0 + 2 + rows
                x = x0 + 3
                for style in ("ok", "warn", "info", "err", "brand", "mag", "dim"):
                    self.put(y, x, style, self.C[style] | curses.A_BOLD)
                    x += len(style) + 2
                self.put(y, x, " sel ", self.C["sel"] | curses.A_BOLD)
                self.put(y + 1, x0 + 3, " head ", self.C["head"] | curses.A_BOLD)
                self.put(y + 1, x0 + 10, " tab ", self.C["tab"])
                self.put(y + 2, x0 + 2, (msg or f"previewing {themes[current].get('name') or current}")[:bw - 4],
                         self.C["warn" if msg.startswith("✖") else "dim"], bw - 4)
                self.put(y0 + bh - 2, x0 + 2, "j/k preview  ⏎ use  i import  d delete  Esc cancel"[:bw - 4],
                         self.C["info"], bw - 4)
                self.scr.refresh()
                if self.dump:
                    self.dump_screen()
                msg = ""
                k = self.scr.getch()
                if k in (27, ord("q")):
                    self.apply_theme(start)
                    return
                if k in (10, 13, curses.KEY_ENTER):
                    try:
                        save_theme_choice(current)
                        self.flash(f"theme: {themes[current].get('name') or current} "
                                   f"(saved in {THEME_CHOICE})")
                    except OSError as e:
                        self.flash(f"✖ theme not saved: {e}")
                    return
                if k in (curses.KEY_DOWN, ord("j")):
                    current = names[min(len(names) - 1, sel + 1)]
                elif k in (curses.KEY_UP, ord("k")):
                    current = names[max(0, sel - 1)]
                elif k == ord("i"):
                    path = self.prompt("import a VS Code theme (.json) or a runai-tui theme (.yaml)",
                                       complete_paths=True)
                    if path:
                        try:
                            current = import_theme(path)
                            msg = f"imported as {current}: Enter keeps it"
                        except (OSError, ValueError, yaml.YAMLError) as e:
                            msg = f"✖ {str(e).splitlines()[0]}"
                elif k in (ord("d"), curses.KEY_DC):
                    if not themes[current]["_src"].startswith(user_dir):
                        msg = "✖ only themes you imported can be deleted"
                    elif self.confirm(f"delete theme {current}?"):
                        try:
                            os.remove(themes[current]["_src"])
                            if start == current:
                                save_theme_choice("default")
                                start = "default"
                            current = names[max(0, sel - 1)]
                        except OSError as e:
                            msg = f"✖ {e}"
        finally:
            self.scr.timeout(250)

    def pager(self, title, text):
        lines = text.split("\n")
        top, search = 0, ""
        self.scr.timeout(-1)
        try:
            while True:
                self.scr.erase()
                self.shadow = {}
                h, w = self.scr.getmaxyx()
                self.put(0, 0, f" {title}".ljust(w), self.C["head"] | curses.A_BOLD)
                for i, l in enumerate(lines[top:top + h - 2]):
                    attr = self.C["warn"] if search and search.lower() in l.lower() else 0
                    self.put(1 + i, 0, l, attr, w)
                pos = f" {min(top + h - 2, len(lines))}/{len(lines)} "
                self.put(h - 1, 0, " ↑/↓ PgUp/PgDn g/G scroll · / search · n next · q back ", self.C["sel"])
                self.put(h - 1, w - len(pos) - 1, pos, self.C["dim"])
                self.scr.refresh()
                if self.dump:
                    self.dump_screen()
                k = self.scr.getch()
                if k in (ord("q"), 27, 10):
                    return
                if k == ord("/"):
                    s = self.prompt("search", search)
                    search = s if s is not None else search
                    k = ord("n")
                if k == ord("n") and search:
                    hit = next((i for i in range(top + 1, len(lines)) if search.lower() in lines[i].lower()), None)
                    if hit is not None:
                        top = hit
                    continue
                step = {curses.KEY_DOWN: 1, ord("j"): 1, curses.KEY_UP: -1, ord("k"): -1,
                        curses.KEY_NPAGE: h - 3, ord(" "): h - 3, curses.KEY_PPAGE: -(h - 3),
                        ord("g"): -10 ** 9, ord("G"): 10 ** 9}.get(k, 0)
                top = max(0, min(top + step, len(lines) - (h - 2)))
        finally:
            self.scr.timeout(250)

    def external(self, title, argv, note="", pause=True):
        """Leave curses, run interactively on the terminal, come back."""
        curses.def_prog_mode()
        curses.endwin()
        sys.stdout.write("\033[2J\033[H")
        print(f"\033[1;38;5;208m▶ {title}\033[0m  \033[2m{' '.join(shlex.quote(a) for a in argv)}\033[0m")
        if note:
            print(f"\033[2m{note}\033[0m")
        print(flush=True)
        old = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            rc = subprocess.call(argv, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        except FileNotFoundError:
            print(f"{argv[0]}: not found")
            rc = 127
        finally:
            signal.signal(signal.SIGINT, old)
        log_action(title, argv, rc)
        print(f"\n\033[1;{'32' if rc == 0 else '31'}m■ exit {rc}\033[0m", flush=True)
        if pause:
            try:
                input("[Enter] back to runai-tui ")
            except EOFError:
                pass
        curses.reset_prog_mode()
        self.scr.clear()
        self.poller.wake()
        return rc

    def busy(self, title, fn):
        """Run fn() in a thread while showing a spinner; -> its result."""
        box = {}

        def work():
            try:
                box["r"] = fn()
            except Exception as e:
                box["e"] = e
        t = threading.Thread(target=work, daemon=True)
        t.start()
        while t.is_alive():
            self.flash(f"{SPIN[self.tick % 10]} {title}")
            self.draw()
            t.join(0.12)
        self.status = ""
        if "e" in box:
            raise box["e"]
        return box.get("r")

    # --- actions ----------------------------------------------------------------------
    def need_workload(self):
        w = self.selected() if self.tab == 1 else None
        if self.tab == 2:
            ws = self.st.watched()
            w = ws[min(self.sel["act"], len(ws) - 1)] if ws else None
        if not w or w.get("_ghost"):
            self.flash("select a workload first")
            return None
        return w

    def pick_pod(self, w, running=True):
        try:
            pods = self.busy("pods ...", lambda: (self.sess.api(f"{CFG['api']['workloads']}/{w['id']}/pods") or {})
                             .get("pods") or [])
        except Exception as e:
            self.flash(f"pods: {e}")
            return None, None
        if running:
            pods = [p for p in pods if p.get("k8sPhase") == "Running"] or pods
        if not pods:
            return "", ""
        p = pods[0]
        if len(pods) > 1:
            i = self.pick("pod", [f"{x['name']}  {x.get('k8sPhase')}  {x.get('nodeName')}" for x in pods])
            if i is None:
                return None, None
            p = pods[i]
        conts = [c.get("name") for c in p.get("containers") or []]
        c = conts[0] if conts else ""
        if len(conts) > 1:
            i = self.pick("container", conts)
            if i is None:
                return None, None
            c = conts[i]
        return p["name"], c

    def do_shell(self, w=None):
        w = w or self.need_workload()
        if not w:
            return
        if w.get("phase") in TERMINAL:
            self.flash(f"{w['name']} is {w.get('phase')}: nothing to shell into (l shows its logs)")
            return
        pod, cont = self.pick_pod(w)
        if pod is None:
            return
        argv = [RUNAI, "workload", "exec", w["name"], "-p", w["projectName"]]
        argv += (["--pod", pod] if pod else []) + (["-c", cont] if cont else [])
        argv += ["-i", "-t", "--wait-timeout", "120s", "--", "sh", "-c", SHELL_CMD]
        self.external(f"shell in {w['name']}", argv, "exit (or Ctrl-D) returns to runai-tui", pause=False)

    def do_attach(self, w=None):
        """The workload's main process on this terminal (its stdin/stdout), not a new shell."""
        w = w or self.need_workload()
        if not w:
            return
        if w.get("phase") in TERMINAL:
            self.flash(f"{w['name']} is {w.get('phase')}: nothing to attach to (l shows its logs)")
            return
        pod, cont = self.pick_pod(w)
        if pod is None:
            return
        sub = attach_words(w)
        ns = w.get("namespace") or f"runai-{w['projectName']}"
        runai_argv = None
        if sub:
            runai_argv = [RUNAI] + sub + ["attach", w["name"], "-p", w["projectName"]]
            runai_argv += (["--pod", pod] if pod else []) + (["-c", cont] if cont else [])
            runai_argv += ["-i", "-t", "--wait-timeout", "120s"]
        note = ("This is the program's own terminal: what you type goes to it; exit (or Ctrl-D) ends the\n"
                "program, and with it the workload. If nothing shows, press Enter. ")
        if pod and kubectl_can_detach():
            argv = [KUBECTL, "attach", "-it", pod, "-n", ns] + (["-c", cont] if cont else [])
            rc = self.external(f"attach to {w['name']}", argv, note + "Ctrl-P Ctrl-Q detaches and leaves it running.")
            if rc == 0 or not runai_argv or not self.confirm(
                    f"kubectl attach failed (exit {rc}): try runai attach (it cannot detach)?", True):
                return
        elif not runai_argv:
            self.flash("no pod to attach to" if not pod else "this kubectl cannot attach (needs --detach-keys)")
            return
        self.external(f"attach to {w['name']}", runai_argv,
                      note + "runai attach cannot detach: to leave it running, close this terminal.")

    def show_pvcs(self):
        proj = self.project or cli_project()
        if not proj:
            self.flash("pick a project first (p)")
            return
        ns = project_namespace(self.st, proj)
        try:
            got, why = self.busy("PVCs ...", lambda: list_pvcs(proj, ns))
        except Exception as e:
            self.flash(f"PVCs: {e}")
            return
        used = {}
        with self.st.lock:
            for w in self.st.workloads:
                if w.get("projectName") == proj:
                    for c in (w.get("pvcs") or w.get("datasources") or []):
                        n = c.get("claimName") or c.get("name") if isinstance(c, dict) else None
                        if n:
                            used.setdefault(n, []).append(w["name"])
        head = f"{'NAME':<28} {'SIZE':>7}  {'STATUS':<8} {'ACCESS':<8} {'CLASS':<14} MANAGED BY"
        lines = [f"PVCs in project {proj} (namespace {ns})", "", head, "-" * len(head)]
        lines += [pvc_line(d) + (f"   used by {', '.join(used[d['name']])}" if d["name"] in used else "")
                  for d in got] or ["(none)"]
        lines += ["", "In the form (n): Existing PVCs → + picks one of these and asks where to mount it.",
                  f"M in the form saves a mount as a Run:ai data source. {why}".rstrip()]
        self.pager(f"PVCs · {proj}", "\n".join(lines))

    # Quota and Apps tabs ---------------------------------------------------------------------
    def draw_org(self, top, height, w):
        st = self.st
        with st.lock:
            rows = org_rows(st.projects, st.departments)
            err = st.dept_error
        self.box(top, 0, height, w, "Quota by department · GPUs allocated / guaranteed", True)
        inner = w - 4
        if not rows:
            self.put(top + 1, 2, "no project data yet" if st.logged_in is not False else "log in to see quotas",
                     self.C["dim"])
            return
        flat = []                                # (kind, name, totals, project)
        for dname, dt, kids in rows:
            flat.append(("dept", dname, dt, None))
            flat += [("proj", p["name"], t, p) for p, t in kids]
        notes = ["Green: within the guaranteed quota (┆). Yellow: over quota - borrowed idle GPUs, preemptible.",
                 "limit: most a project may use (∞ none) · pend: GPUs requested but waiting · weight: share of idle "
                 "GPUs over quota."]
        if err:
            notes.insert(0, f"departments: {err} - department totals are added up from your projects")
        lines = height - 3 - len(notes)
        t0, sel = self.scroll_list("quota", len(flat), lines)
        bw = max(12, min(36, inner - 66))
        hdr = f"{'':<24}{'GPU':<{bw + 2}}{'alloc':>6} {'quota':>6} {'over':>6} {'limit':>6} {'pend':>5} {'weight':>6}"
        self.put(top + 1, 2, hdr, self.C["dim"], inner)
        for i, (kind, name, t, p) in enumerate(flat[t0:t0 + lines]):
            y = top + 2 + i
            quota = t["quota"]
            limit = t.get("limit")
            over = max(0, t["alloc"] - (quota or 0)) if quota is not None else 0
            label = (f"▸ {name}" if kind == "dept" else f"   {name}")[:23]
            attr = self.C["sel"] | curses.A_BOLD if t0 + i == sel else (self.C["brand"] | curses.A_BOLD if kind == "dept"
                                                                        else 0)
            mark = "◂" if kind == "proj" and name == self.project else " "
            self.put(y, 1, f" {label:<23}".ljust(24) + mark, attr, 25)
            self.cells(y, 26, quota_cells(bw, t["alloc"], quota or 0, limit), {})
            if kind == "dept":
                txt = (f"  {num(t['alloc']):>5} {num(quota) if quota is not None else '-':>6} "
                       f"{num(over) if over else '':>6}" + ("" if t["known"] else "   (projects: "
                                                             f"{num(t['projects_quota'])} guaranteed)"))
            else:
                txt = (f"  {num(t['alloc']):>5} {num(quota):>6} {num(over) if over else '':>6} "
                       f"{num(limit) if limit is not None else '∞':>6} {num(t['pending']) if t['pending'] else '':>5} "
                       f"{num(t['weight']) if t['weight'] else '':>6}")
            self.put(y, 26 + bw, txt, self.C["warn"] if over else 0, inner - bw - 24)
        for j, n in enumerate(notes):
            self.put(top + height - 1 - len(notes) + j, 2, n, self.C["warn" if n.startswith("departments") else "dim"],
                     inner)

    def quota_enter(self):
        with self.st.lock:
            rows = org_rows(self.st.projects, self.st.departments)
        flat = []
        for dname, _, kids in rows:
            flat.append(("dept", dname))
            flat += [("proj", p["name"]) for p, _ in kids]
        if not flat:
            return
        kind, name = flat[min(self.sel["quota"], len(flat) - 1)]
        if kind == "proj":
            self.project = name
            self.flash(f"project {name} (p switches; the CLI default stays {cli_project() or '-'})")
        else:
            self.flash(f"department {name}: Enter on one of its projects shows that project")

    def visible_apps(self):
        with self.st.lock:
            apps = list(self.st.apps or [])
        return [a for a in apps if not self.project or app_info(a)["project"] in ("", self.project)]

    def draw_apps(self, top, height, w):
        st = self.st
        apps = self.visible_apps()
        lw = max(46, w * 45 // 100)
        self.box(top, 0, height, lw, f"AI applications {len(apps)} · {self.project or 'all projects'}", True)
        if st.apps is None:
            msg = st.apps_error or ("loading ..." if st.logged_in is not False else "log in first")
            for i, part in enumerate(textwrap.wrap(msg, lw - 4)[:height - 2]):
                self.put(top + 1 + i, 2, part, self.C["warn" if st.apps_error else "dim"], lw - 4)
            self.box(top, lw, height, w - lw, "Details")
            return
        if not apps:
            self.put(top + 1, 2, f"none: {key_name(KEYS['app_submit'])} submits one from a Helm chart", self.C["dim"],
                     lw - 4)
        t0, sel = self.scroll_list("apps", len(apps), height - 2)
        nw = max(10, lw - 34)
        for i, a in enumerate(apps[t0:t0 + height - 2]):
            info = app_info(a)
            line = f" {info['name'][:nw]:<{nw}} {info['status'][:11]:<11} {info['project'][:16]:<16}"
            attr = self.C["sel"] | curses.A_BOLD if t0 + i == sel else 0
            self.put(top + 1 + i, 1, line.ljust(lw - 2), attr, lw - 2)
        dw = w - lw
        self.box(top, lw, height, dw, "Details")
        if not apps:
            L = ["AI applications are Helm charts that Run:ai deploys as a group of workloads",
                 "(a model server with its UI, a vector database, ...).", "",
                 f"{key_name(KEYS['app_submit'])} asks for a name, a chart URL (.tgz), and optionally a values file,",
                 "--set overrides and an NGC credential, then runs `runai application submit`."]
        else:
            a = apps[sel]
            info = app_info(a)
            L = [f"{k:<10}{info[k]}" for k in ("name", "project", "status", "chart", "created", "workloads")
                 if info[k] not in ("", None)]
            L += ["", "-- from the API --"] + yaml.safe_dump(a, sort_keys=False, width=dw - 6).splitlines()
        for i, line in enumerate(L[:height - 2]):
            self.put(top + 1 + i, lw + 2, line, self.C["dim"] if line.startswith("--") else 0, dw - 4)

    def selected_app(self):
        apps = self.visible_apps()
        if not apps:
            self.flash("no application selected")
            return None
        info = app_info(apps[min(self.sel["apps"], len(apps) - 1)])
        if not info["project"] and not self.project:
            self.flash("its project is unknown: pick one with p first")
            return None
        info["project"] = info["project"] or self.project
        return info

    def app_describe(self):
        a = self.selected_app()
        if not a:
            return
        argv = [RUNAI, "application", "describe", a["name"], "-p", a["project"]]
        rc, out, err = self.busy("describe ...", lambda: cmd(argv, timeout=60))
        log_action(f"describe application {a['name']}", argv, rc)
        self.pager(f"application {a['name']} · {a['project']}", (out + err) or f"exit {rc}")

    def app_delete(self):
        a = self.selected_app()
        if not a or not self.confirm(f"delete application {a['name']} (and its workloads)?"):
            return
        argv = [RUNAI, "application", "delete", a["name"], "-p", a["project"], "-y"]
        rc, out, err = self.busy("deleting ...", lambda: cmd(argv, timeout=120))
        log_action(f"delete application {a['name']}", argv, rc, out + err)
        self.flash((last_line(out, err) or f"deleted {a['name']}") if rc == 0 else f"✖ {first_error(err, out)}")
        self.poller.wake()

    def app_submit(self):
        proj = self.project or cli_project()
        if not proj:
            i = self.pick("project", self.projects())
            if i is None:
                return
            proj = self.projects()[i]
        name = self.prompt(f"application name (in {proj})")
        if not name:
            return
        if not NAME_RE.match(name):
            self.flash("names: lowercase letters, digits and '-'")
            return
        url = self.prompt("Helm chart URL (.tgz)")
        if not url:
            return
        values = self.prompt("values file (optional, Enter skips)", complete_paths=True)
        if values is None:
            return
        sets = self.prompt("--set overrides (optional: key=value; key2=value2)")
        if sets is None:
            return
        cred = self.prompt("NGC API key credential for a private chart (optional)")
        if cred is None:
            return
        argv = [RUNAI, "application", "submit", name, "-p", proj, "--chart-url", url]
        if values:
            argv += ["--values", os.path.expanduser(values)]
        for kv in [x.strip() for x in sets.split(";") if x.strip()]:
            argv += ["--set", kv]
        if cred:
            argv += ["--credential", cred]
        if not self.confirm(f"run {' '.join(shlex.quote(a) for a in argv[1:])[:90]}?", True):
            return
        env = {"RUNAI_TUI_PROJECT": proj, "RUNAI_TUI_LABEL": name, "RUNAI_TUI_ARGV": json.dumps(argv)}
        ok, _, msg = run_hooks("pre_submit", "", env, name)
        if not ok:
            self.flash(f"✖ {msg}")
            return
        rc, out, err = self.busy(f"submitting {name} ...", lambda: cmd(argv, timeout=300))
        log_action(f"submit application {name}", argv, rc, out + err)
        if rc != 0:
            self.flash(f"✖ {first_error(err, out)}")
            return
        run_hooks("post_submit", "", {"RUNAI_TUI_PROJECT": proj, "RUNAI_TUI_NAMES": name,
                                      "RUNAI_TUI_METHOD": "runai application submit"}, name)
        self.flash(f"✔ {last_line(out, err) or 'submitted ' + name}")
        self.poller.wake()

    def do_logs(self, follow=False):
        w = self.need_workload()
        if not w:
            return
        pod, cont = self.pick_pod(w, running=False)
        if pod is None:
            return
        argv = [RUNAI, "workload", "logs", w["name"], "-p", w["projectName"]]
        argv += (["--pod", pod] if pod else []) + (["-c", cont] if cont else [])
        if follow:
            self.external(f"logs of {w['name']}", argv + ["-f", "--timestamps"], "Ctrl-C returns to runai-tui")
            return
        rc, out, err = self.busy("logs ...", lambda: cmd(argv + ["--tail", "3000"], timeout=60))
        self.pager(f"logs {w['name']}{' / ' + pod if pod else ''}  (L follows)", (out + err) if rc == 0 else
                   f"{first_error(err, out)}\n\n{out}{err}")

    def do_describe(self):
        w = self.need_workload()
        if not w:
            return
        rc, out, err = self.busy("describe ...", lambda: cmd(
            [RUNAI, "workload", "describe", w["name"], "-p", w["projectName"], "--containers"], timeout=60))
        self.pager(f"runai workload describe {w['name']}", out + err)

    def do_save(self):
        w = self.need_workload()
        if not w:
            return
        try:
            text, how = self.busy("exporting ...", lambda: export_workload(self.sess, w))
        except Exception as e:
            self.flash(f"save: {e}")
            return
        path = self.prompt("save YAML to (Tab completes)", os.path.join(SAVE_DIR, f"{w['name']}.yaml"), True)
        if not path:
            return
        path = os.path.expanduser(path)
        if os.path.exists(path) and not self.confirm(f"{path} exists - overwrite?"):
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        except OSError as e:
            self.flash(f"save: {e}")
            return
        log_action(f"saved {w['name']} -> {path} ({how})")
        self.flash(f"saved {path} (from {how}); F submits it again")

    def do_delete(self):
        w = self.need_workload()
        if not w:
            return
        if not self.confirm(f"delete {w['name']} in {w['projectName']}? (kubectl-applied Services/Ingresses stay)"):
            return
        argv = [RUNAI, "workload", "delete", "-p", w["projectName"], "-y", w["name"]]
        rc, out, err = self.busy(f"deleting {w['name']} ...", lambda: cmd(argv, timeout=60))
        log_action(f"delete {w['name']}", argv, rc, out + err)
        self.flash((last_line(out, err) or f"{w['name']} deleted") if rc == 0 else f"delete: {first_error(err, out)}")
        self.poller.wake()

    def do_suspend(self):
        w = self.need_workload()
        if not w:
            return
        sub = {"Workspace": ["workspace"], "Training": ["training", "standard"]}.get(w.get("type"))
        if not sub:
            self.flash(f"{w.get('type')} workloads cannot be suspended from the CLI (delete and resubmit instead)")
            return
        verb = "resume" if w.get("phase") in ("Stopped", "Suspended") else "suspend"
        if not self.confirm(f"{verb} {w['name']}?", True):
            return
        argv = [RUNAI] + sub + [verb, w["name"], "-p", w["projectName"]]
        rc, out, err = self.busy(f"{verb} ...", lambda: cmd(argv, timeout=60))
        log_action(f"{verb} {w['name']}", argv, rc, out + err)
        self.flash((last_line(out, err) or f"{w['name']}: {verb}ed") if rc == 0 else f"{verb}: {first_error(err, out)}")
        self.poller.wake()

    def do_port_forward(self):
        w = self.need_workload()
        if not w:
            return
        port = next((c.get("containerPort") for e in w.get("environments") or [] for c in e.get("connections") or []
                     if c.get("containerPort")), 8888)
        spec = self.prompt("ports <local>:<container>", f"{port}:{port}")
        if not spec:
            return
        pod, _ = self.pick_pod(w)
        if not pod:
            self.flash("no pod to forward to")
            return
        local = spec.split(":")[0]
        argv = [KUBECTL, "port-forward", f"pod/{pod}", spec, "-n", w.get("namespace") or f"runai-{w['projectName']}"]
        self.external(f"port-forward {w['name']} {spec}", argv,
                      f"open http://localhost:{local} on this node; from your laptop first: "
                      f"ssh -L {local}:localhost:{local} {getpass.getuser()}@<login node>\nCtrl-C stops forwarding")

    def do_copy(self, what):
        w = self.need_workload()
        if not w:
            return
        if what == "url":
            urls = w.get("urls") or []
            if not urls:
                self.flash("no URL: this workload exposes no tool (f port-forwards instead)")
                return
            text = urls[0] if len(urls) == 1 else urls[self.pick("URL", urls) or 0]
        else:
            text = w["name"]
        osc52(text)
        self.flash(f"copied {text}")

    def switch_project(self):
        names = self.projects()
        if not names:
            self.flash("no projects (yet)")
            return
        opts = names + ["(all projects)"]
        i = self.pick("project", opts, self.project or "(all projects)")
        if i is None:
            return
        self.project = "" if i == len(names) else names[i]
        self.sel["wl"] = self.top["wl"] = 0
        if self.project and self.project != cli_project() and self.confirm(
                f"also make {self.project} your default project (runai project set)?"):
            rc, out, err = cmd([RUNAI, "project", "set", self.project], timeout=30)
            self.flash(first_error(out, err))

    # submission ----------------------------------------------------------------------------
    def start_batch(self, items):
        if self.st.batch and not self.st.batch.done:
            self.flash("a submission is still running - wait for it to finish")
            return
        for it in items:
            it.setdefault("state", "queued")
        b = Batch(items, self.st, self.poller, self.sess)
        self.st.batch = b
        b.start()
        self.tab = 2
        self.sel["act"] = 0

    def do_submit_files(self):
        start = SAVE_DIR + "/" if os.path.isdir(SAVE_DIR) else os.getcwd() + "/"
        spec = self.prompt("YAML file(s) or glob, space-separated (Tab completes)", start, complete_paths=True)
        if not spec:
            return
        files = []
        for word in shlex.split(spec):
            got = sorted(glob.glob(os.path.expanduser(word)))
            if os.path.isdir(os.path.expanduser(word)):
                got = sorted(glob.glob(os.path.join(os.path.expanduser(word), "*.y*ml")))
            files += [g for g in got if os.path.isfile(g)]
        if not files:
            self.flash(f"no files match {spec}")
            return
        lines, items = [], []
        for f in files:
            try:
                docs = load_docs(f)
                names = workload_names(docs)
                proj = doc_project(docs) or self.project
                how = "kubectl apply" if len(docs) > 1 else "runai workload submit"
                kinds = ", ".join(f"{d.get('kind')}/{(d.get('metadata') or {}).get('name')}" for d in docs)
                lines.append(f"  {os.path.basename(f):<34} -> {proj or '?':<14} {how:<22} {kinds}")
                items.append({"file": f, "label": os.path.basename(f), "project": proj})
            except Exception as e:
                lines.append(f"  {os.path.basename(f):<34}    ✖ {str(e).splitlines()[0]}")
        self.pager("submit these? (q to continue)",
                   f"{len(items)} file(s) to submit, in this order:\n\n" + "\n".join(lines) +
                   "\n\nOne object per file -> runai workload submit -f; several objects -> kubectl apply -f."
                   "\nWorkloads whose name already exists in the project are skipped.")
        if not items or not self.confirm(f"submit {len(items)} file(s)?", True):
            return
        self.start_batch(items)

    def form_assets(self, v):
        """Assets usable from the form's project: {kind: [names]}."""
        p = self.st.project(v["project"])
        names = lambda items: [asset_name(x) for x in items if asset_in_scope(x, p)]
        with self.st.lock:
            return {"env": names(self.st.envs), "compute": names(self.st.computes),
                    "datasources": [asset_name(d) for d in self.st.datasources
                                    if asset_in_scope(d, p) and ds_kind(d) in DS_KINDS],
                    "template": names(self.st.templates)}

    def form_fields(self, v):
        assets = self.form_assets(v)
        pools = ["any"]
        p = self.st.project(v["project"])
        if p:
            pools += [q["pool"] for q in project_pools(p) if q["pool"] != "default"]
        secrets = ["(none)"] + list(SITE.get("pull_secrets") or [])
        F = [("project", "Project", "choice", self.projects() or [v["project"]]),
             ("template", "Template", "choice", ["(none)"] + assets["template"]),
             ("kind", "Kind", "choice", [k for k, _ in KINDS]),
             ("name", "Name", "text", None),
             ("env", "Environment", "choice", ["(custom image)"] + assets["env"]),
             ("image", "Image", "text", None),
             ("command", "Command (sh -c)", "text", None),
             ("compute", "Compute resource", "choice", ["(custom)"] + assets["compute"]),
             ("gpu", "GPU", "choice", ["none", "fraction", "whole"]),
             ("gpu_amount", "  GPU amount", "text", None),
             ("pool", "Node pool", "choice", pools),
             ("cpu", "CPU cores", "text", None),
             ("memory", "Memory", "text", None),
             ("run_as_me", "Run as me + home", "bool", None),
             ("datasources", "Data sources", "multi", assets["datasources"]),
             ("pvcs", "Existing PVCs", "pvcs", None),
             ("binds", "Bind mounts", "multi", list(site_binds()[0])),
             ("extra_binds", "Extra bind mounts", "binds", None),
             ("scratch", "Scratch /scratch", "text", None),
             ("shm", "/dev/shm size", "text", None),
             ("env_vars", "Env (A=1,B=2)", "text", None),
             ("pull_secret", "Image pull secret", "choice", secrets),
             ("completions", "Completions", "text", None),
             ("ttl", "Keep after end (s)", "text", None),
             ("priority", "Priority", "choice", ["default"] + PRIORITIES),
             ("preempt", "Preemptibility", "choice", ["default", "preemptible", "non-preemptible"])]
        hide = set()
        if v["gpu"] == "none":
            hide.add("gpu_amount")
        if v["kind"] != "training":
            hide |= {"completions", "ttl"}
        if v["kind"] == "workspace":
            hide |= {"scratch", "shm", "pull_secret"}
            if v["env"] != "(custom image)":
                hide |= {"image", "command"}
        for key in ("template", "compute"):
            if not assets[key] and v[key] in ("(none)", "(custom)"):
                hide.add(key)
        if not assets["datasources"] and not v["datasources"]:
            hide.add("datasources")
        if not site_binds()[0] and not v["binds"]:
            hide.add("binds")
        if len(secrets) == 1 and v["pull_secret"] == "(none)":
            hide.add("pull_secret")
        return [f for f in F if f[0] not in hide]

    def apply_env(self, v):
        e = next((x for x in self.st.envs if asset_name(x) == v["env"]), None)
        if not e:
            return
        s = e.get("spec") or {}
        v["image"] = s.get("image", v["image"])
        v["command"] = join_command(s.get("command"), s.get("args"))
        v["run_as_me"] = s.get("uidGidSource") == "fromIdpToken" or v["run_as_me"]

    def apply_compute(self, v):
        c = next((x for x in self.st.computes if asset_name(x) == v["compute"]), None)
        if c:
            vals, note = compute_values(c)
            v.update(vals)
            if note:
                self.flash(f"compute {v['compute']}: {note}")

    def apply_template(self, v):
        t = next((x for x in self.st.templates if asset_name(x) == v["template"]), None)
        if not t:
            return
        vals, notes = template_values(t, self.st)
        if vals.get("kind") and vals["kind"] != v["kind"]:
            v["kind"] = vals["kind"]
            self.form_changed(v, "kind")
        if vals.get("env") and vals["env"] != "(custom image)":
            v["env"] = vals["env"]
            self.apply_env(v)
        if vals.get("compute") and vals["compute"] != "(custom)":
            v["compute"] = vals["compute"]
            self.apply_compute(v)
        for k, val in vals.items():
            if k in FORM_EMPTY and k not in ("kind", "compute") and not (k == "env" and val != "(custom image)"):
                v[k] = val
        if vals.get("compute") == "(custom)":
            v["compute"] = "(custom)"
        self.flash(f"template {v['template']} applied" + (f" ({'; '.join(notes)})" if notes else ""))

    def form_defaults(self):
        proj = self.project or self.projects()[0]
        v = dict(FORM_EMPTY, **CFG["form"]["defaults"])
        if v["kind"] not in dict(KINDS):
            v["kind"] = "training"
        prefix = {"training": "job", "interactive": "dev", "service": "svc", "workspace": "ws"}[v["kind"]]
        v.update(project=proj, name=f"{prefix}-{datetime.datetime.now():%m%d-%H%M}")
        v["binds"] = [n for n, b in site_binds()[0].items() if b["default"]]
        dflt = next((e for e in self.st.envs if asset_name(e) == CFG["form"]["default_environment"]), None)
        v["image"] = v.get("image") or (dflt or {}).get("spec", {}).get("image") or \
            CFG["form"]["fallback_image"].format(registry=registry_host(self.st))
        start = CFG["form"].get("start_preset")
        if start and start in list_presets():
            self.load_preset(v, start)
        return v

    def load_preset(self, v, name):
        try:
            vals = read_preset(name)
        except (OSError, ValueError, yaml.YAMLError) as e:
            self.flash(f"preset {name}: {str(e).splitlines()[0]}")
            return
        if vals.get("project") not in self.projects():
            vals.pop("project", None)
        v.update({k: val for k, val in vals.items() if k in FORM_EMPTY and k != "name"})
        if v["kind"] not in dict(KINDS):
            v["kind"] = "training"
        self.form_changed(v, "kind")
        self.flash(f"preset {name} loaded")

    def form_presets(self, v):
        have = list_presets()
        opts = [f"load: {n}" + ("  (site)" if not p.startswith(user_preset_dir()) else "")
                for n, p in have.items()]
        opts += ["save the form as a preset ..."]
        opts += [f"delete: {n}" for n, p in have.items() if p.startswith(user_preset_dir())]
        i = self.pick("presets", opts)
        if i is None:
            return
        o = opts[i]
        if o.startswith("load: "):
            self.load_preset(v, list(have)[i])
        elif o.startswith("save"):
            name = self.prompt("preset name (letters, digits, - and _)",
                               CFG["form"].get("start_preset") if not have else "")
            if not name:
                return
            if not re.match(r"^[\w-]+$", name):
                self.flash("preset names: letters, digits, - and _")
                return
            if name in have and have[name].startswith(user_preset_dir()) and not self.confirm(
                    f"overwrite preset {name}?"):
                return
            try:
                self.flash(f"saved preset {name}: {write_preset(name, v)}")
            except OSError as e:
                self.flash(f"preset {name}: {e}")
        else:
            name = o.split(": ", 1)[1]
            if self.confirm(f"delete preset {name}?"):
                os.remove(have[name])
                self.flash(f"deleted preset {name}")

    def pick_many(self, title, options, chosen):
        """Modal checklist: Space toggles, Enter accepts, Esc cancels. -> list or None."""
        if not options:
            self.flash("nothing to choose from")
            return None
        on, sel = [o in chosen for o in options], 0
        self.scr.timeout(-1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                bw = min(w - 4, max(len(title) + 30, max(len(str(o)) for o in options) + 10))
                bh = min(h - 4, len(options) + 2)
                y0, x0 = (h - bh) // 2, (w - bw) // 2
                for i in range(bh):
                    self.put(y0 + i, x0, " " * bw, 0)
                self.box(y0, x0, bh, bw, f"{title} (Space toggles, Enter done)", True)
                top = max(0, sel - (bh - 3))
                for i, o in enumerate(options[top:top + bh - 2]):
                    attr = self.C["sel"] | curses.A_BOLD if top + i == sel else 0
                    self.put(y0 + 1 + i, x0 + 1, f" [{'x' if on[top + i] else ' '}] {o}".ljust(bw - 2), attr, bw - 2)
                self.scr.refresh()
                if self.dump:
                    self.dump_screen()
                k = self.scr.getch()
                if k in (27, ord("q")):
                    return None
                if k in (10, 13, curses.KEY_ENTER):
                    return [o for o, x in zip(options, on) if x]
                if k == ord(" "):
                    on[sel] = not on[sel]
                elif k in (curses.KEY_DOWN, ord("j")):
                    sel = min(len(options) - 1, sel + 1)
                elif k in (curses.KEY_UP, ord("k")):
                    sel = max(0, sel - 1)
        finally:
            self.scr.timeout(250)

    def edit_binds(self, text, pvc_project=None):
        """Modal list of extra bind mounts (or, with pvc_project, of existing PVCs) -> the form text.
        Rows are typed in place and the box grows with them: a/Enter add, e edit, Space ro/rw,
        d delete, q done."""
        pvc = pvc_project is not None
        binds, _ = parse_pvcs(text) if pvc else parse_binds(text)
        sel = 0
        title = "Existing PVCs" if pvc else "Extra bind mounts"
        note = (f"claims in project {pvc_project}; read-write unless [ro]" if pvc else
                "host path is on the compute node; read-only unless [rw]")
        claims = []

        def draw(adding=False, typing=False):
            """Paint the box (with an empty row to type into when adding) -> row index -> (y, x, width)."""
            n = len(binds) + 1                   # the "+ add" row, or the row being added
            h, w = self.scr.getmaxyx()
            longest = max([len(b["host"]) + len(b["path"]) for b in binds] + [40])
            bw = min(w - 4, max(72, longest + 20))
            bh = min(h - 2, n + 5)
            y0, x0 = (h - bh) // 2, (w - bw) // 2
            vis = bh - 5
            top = max(0, min(sel - vis + 1, n - vis))
            for i in range(bh):
                self.put(y0 + i, x0, " " * bw, 0)
            self.box(y0, x0, bh, bw, f"{title} ({len(binds)})", True)
            for j in range(top, min(n, top + vis)):
                if j < len(binds):
                    b = binds[j]
                    line, attr = f" [{'ro' if b['ro'] else 'rw'}] {b['host']}  →  {b['path']}", 0
                elif adding:
                    line, attr = "", 0
                else:
                    line, attr = " + add a PVC" if pvc else " + add a bind mount", self.C["info"]
                if j == sel and not (adding or typing):
                    attr = self.C["sel"] | curses.A_BOLD
                self.put(y0 + 1 + j - top, x0 + 1, line.ljust(bw - 2), attr, bw - 2)
            msg = self.status
            self.put(y0 + bh - 3, x0 + 2, msg or note, self.C["warn" if msg else "dim"], bw - 4)
            if adding or typing:
                hint = "⏎ next  Esc cancels" if pvc else "⏎ next  Tab completes  Esc cancels"
            elif sel < len(binds):
                hint = "a/⏎ add  e edit  Space ro/rw  d delete  q done"
            else:
                hint = "⏎ add  q done"
            self.put(y0 + bh - 2, x0 + 2, hint, self.C["dim"], bw - 4)
            self.scr.refresh()
            if self.dump:
                self.dump_screen()
            return lambda j: (y0 + 1 + j - top, x0 + 2, bw - 4)

        def ask_pvc(j, b=None):
            """Pick a claim (with its size), then type its mount path into row j."""
            adding = b is None
            if not claims:
                try:
                    got, why = self.busy("PVCs ...", lambda: list_pvcs(pvc_project, project_namespace(
                        self.st, pvc_project)))
                except Exception as e:
                    got, why = [], str(e)
                claims[:] = got
                if why:
                    self.flash(why)
            if not claims:
                self.flash(f"no PVCs in {pvc_project}" + (f": {self.status}" if self.status else ""))
                return None
            names = [c["name"] for c in claims]
            i = self.pick(f"PVC in {pvc_project}: name, size, status, access, class", [pvc_line(c) for c in claims],
                          pvc_line(claims[names.index(b["host"])]) if b and b["host"] in names else None)
            self.status = ""
            if i is None:
                return None
            claim = names[i]
            mode = "[ro] " if b and b["ro"] else "[rw] "
            pos = draw(adding, True)(j)
            path = self.prompt(f"{mode}{claim}  →  ", b["path"] if b and b["host"] == claim else f"/data/{claim}",
                               at=pos, redraw=lambda: draw(adding, True))
            self.status = ""
            if not path:
                return None
            if not path.startswith("/") or ":" in path or "," in path:
                self.flash(f"'{path}': an absolute path without ':' or ','")
                return None
            return {"host": claim, "path": path, "ro": b["ro"] if b else False, "type": ""}

        def ask(j, b=None):
            """Type a bind into row j (b: the one being edited) -> bind or None."""
            if pvc:
                return ask_pvc(j, b)
            adding = b is None
            mode = "[ro] " if adding or b["ro"] else "[rw] "
            pos = draw(adding, True)(j)
            host = self.prompt(f"{mode}host path: ", b["host"] if b else "/", complete_paths=True, at=pos,
                               redraw=lambda: draw(adding, True))
            self.status = ""
            if not host:
                return None
            if not host.startswith("/") or ":" in host or "," in host:
                self.flash(f"'{host}': an absolute path without ':' or ','")
                return None
            pos = draw(adding, True)(j)
            # same path as the host unless this bind already mounted somewhere else
            path = self.prompt(f"{mode}{host}  →  ", b["path"] if b and b["path"] != b["host"] else host,
                               at=pos, redraw=lambda: draw(adding, True))
            self.status = ""
            if not path:
                return None
            if not path.startswith("/") or ":" in path or "," in path:
                self.flash(f"'{path}': an absolute path without ':' or ','")
                return None
            return {"host": host, "path": path, "ro": b["ro"] if b else True, "type": ""}

        try:
            while True:
                self.scr.timeout(-1)                    # prompt() leaves it at 250 ms
                sel = max(0, min(sel, len(binds)))
                draw()
                k = self.scr.getch()
                self.status = ""
                if k in (27, ord("q")):
                    return format_pvcs(binds) if pvc else format_binds(binds)
                if k in (curses.KEY_DOWN, ord("j")):
                    sel += 1
                elif k in (curses.KEY_UP, ord("k")):
                    sel -= 1
                elif k == ord("a") or (k in (10, 13, curses.KEY_ENTER) and sel == len(binds)):
                    sel = len(binds)
                    b = ask(sel)
                    if b:
                        binds.append(b)
                        sel = len(binds)             # straight on to the next "+ add"
                elif sel < len(binds) and k in (ord("e"), 10, 13, curses.KEY_ENTER):
                    b = ask(sel, binds[sel])
                    if b:
                        binds[sel] = b
                elif sel < len(binds) and k == ord(" "):
                    binds[sel]["ro"] = not binds[sel]["ro"]
                elif sel < len(binds) and k in (ord("d"), ord("x"), curses.KEY_DC):
                    del binds[sel]
                self.scr.timeout(-1)
        finally:
            self.scr.timeout(250)

    def do_new(self):
        if not self.st.projects:
            self.flash("no projects loaded yet (logged in? see Setup)")
            return
        v = self.form_defaults()
        acts = {KEYS["form_submit"]: "S", KEYS["form_write"]: "W", KEYS["form_edit"]: "E",
                KEYS["form_presets"]: "P", KEYS["form_go"]: "G", KEYS["form_runai"]: "M"}
        sel, pscroll = 0, 0
        self.scr.timeout(-1)
        try:
            while True:
                fields = self.form_fields(v)
                sel = max(0, min(sel, len(fields) - 1))
                man, argv, warn = build_workload(v, self.sess, self.st)
                preview = (manifest_text(man, v["project"]) if man else
                           "# Run:ai workspace through the CLI (keeps environment tools and URLs):\n\n"
                           + pretty_argv(argv))
                self.draw_form(fields, v, sel, preview, warn, pscroll)
                k = self.scr.get_wch()
                key, typ, choices = fields[sel][0], fields[sel][2], fields[sel][3]
                if k in ("\x1b", "q"):
                    if self.confirm("discard this workload?", True):
                        return
                    self.scr.timeout(-1)
                    continue
                if k in (curses.KEY_DOWN, "j", "\t"):
                    sel += 1
                elif k in (curses.KEY_UP, "k", curses.KEY_BTAB):
                    sel -= 1
                elif k == curses.KEY_NPAGE:
                    pscroll += 10
                elif k == curses.KEY_PPAGE:
                    pscroll = max(0, pscroll - 10)
                elif k in (curses.KEY_LEFT, curses.KEY_RIGHT, "h", "l", " ") and typ in ("choice", "bool"):
                    if typ == "bool":
                        v[key] = not v[key]
                    else:
                        i = choices.index(v[key]) if v[key] in choices else 0
                        v[key] = choices[(i + (-1 if k in (curses.KEY_LEFT, "h") else 1)) % len(choices)]
                        self.form_changed(v, key)
                elif k in ("\n", "\r", curses.KEY_ENTER, " ") and typ in ("binds", "pvcs"):
                    v[key] = self.edit_binds(v[key], v["project"] if typ == "pvcs" else None)
                    self.scr.timeout(-1)
                elif k in ("\n", "\r", curses.KEY_ENTER, " ") and typ == "multi":
                    got = self.pick_many(fields[sel][1].strip(), choices, v[key])
                    self.scr.timeout(-1)
                    if got is not None:
                        v[key] = got
                elif k in ("\n", "\r", curses.KEY_ENTER):
                    if typ == "text":
                        new = self.prompt(fields[sel][1].strip(), v[key])
                        if new is not None:
                            v[key] = new
                            self.form_changed(v, key)
                        self.scr.timeout(-1)
                    elif typ == "bool":
                        v[key] = not v[key]
                    else:
                        i = self.pick(fields[sel][1].strip(), choices, v[key])
                        self.scr.timeout(-1)
                        if i is not None:
                            v[key] = choices[i]
                            self.form_changed(v, key)
                elif acts.get(k) == "P":
                    self.form_presets(v)
                    self.scr.timeout(-1)
                elif acts.get(k) == "M":
                    self.form_save_runai(v)
                    self.scr.timeout(-1)
                elif acts.get(k):
                    if self.form_action(acts[k], v, man, argv, warn):
                        return
                    self.scr.timeout(-1)
        finally:
            self.scr.timeout(250)

    def form_changed(self, v, key):
        if key == "template" and v["template"] != "(none)":
            self.apply_template(v)
        if key == "env" and v["env"] != "(custom image)":
            self.apply_env(v)
        if key == "compute" and v["compute"] != "(custom)":
            self.apply_compute(v)
        if key in ("gpu", "gpu_amount", "cpu", "memory") and v["compute"] != "(custom)":
            v["compute"] = "(custom)"            # edited by hand: no longer that compute resource
        if key == "kind":
            prefix = {"training": "job", "interactive": "dev", "service": "svc", "workspace": "ws"}[v["kind"]]
            if re.match(r"^(job|dev|svc|ws)-\d{4}-\d{4}$", v["name"]):
                v["name"] = f"{prefix}-{v['name'].split('-', 1)[1]}"
            if v["kind"] in ("interactive", "service", "workspace") and v["command"].startswith("echo hello"):
                v["command"] = ""
        if key == "gpu":
            v["gpu_amount"] = "0.5" if v["gpu"] == "fraction" else "1"

    def form_action(self, k, v, man, argv, warn):
        """S submit, W write, E edit+submit, G submit and attach/shell in. -> True when the form is done."""
        proj = v["project"]
        go = k == "G"
        if go:
            k = "S"
            attach = not man and v["env"] == "(custom image)" and not v["command"].strip()
            if attach:                           # the image's own program (e.g. bash) on a terminal
                at = argv.index("--command") if "--command" in argv else len(argv)
                argv = argv[:at] + ["--stdin", "--tty"] + argv[at:]
        if k == "W" or (k == "E" and man):
            ext = ".yaml" if man else ".sh"
            path = self.prompt("write to", os.path.join(SAVE_DIR, v["name"] + ext), complete_paths=True)
            if not path:
                return False
            path = os.path.expanduser(path)
            if os.path.exists(path) and not self.confirm(f"{path} exists - overwrite?"):
                return False
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w") as f:
                f.write(manifest_text(man, proj) if man else
                        "#!/bin/sh\n# runai-tui: Run:ai workspace\n" + pretty_argv(argv, "  ") + "\n")
            if not man:
                os.chmod(path, 0o755)
            if k == "W":
                self.flash(f"wrote {path}" + (f"; {key_name(KEYS['submit_files'])} submits it" if man else ""))
                return True
            editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
            curses.def_prog_mode()
            curses.endwin()
            subprocess.call(shlex.split(editor) + [path])
            curses.reset_prog_mode()
            self.scr.clear()
            try:
                load_docs(path)
            except (OSError, yaml.YAMLError) as e:
                self.flash(f"{path}: invalid YAML now: {str(e).splitlines()[0]}")
                return True
            if self.confirm(f"submit {path}?", True):
                self.start_batch([{"file": path, "label": os.path.basename(path), "project": proj}])
            else:
                self.flash(f"kept {path}; {key_name(KEYS['submit_files'])} submits it later")
            return True
        if k == "E":
            self.flash(f"{key_name(KEYS['form_edit'])} edits manifests; this kind is a CLI command "
                       f"({key_name(KEYS['form_write'])} writes it as a script)")
            return False
        blocking = [x for x in warn if "already exists" in x or x.startswith(("name:", "image is empty"))]
        if blocking:
            self.flash(blocking[0])
            return False
        if warn and not self.confirm(f"{len(warn)} warning(s): {warn[0][:60]}... submit anyway?"):
            return False
        if man:
            os.makedirs(SUBMITTED, exist_ok=True)
            path = os.path.join(SUBMITTED, f"{v['name']}-{datetime.datetime.now():%Y%m%d-%H%M%S}.yaml")
            with open(path, "w") as f:
                f.write(manifest_text(man, proj))
            self.start_batch([{"file": path, "label": v["name"], "project": proj}])
        else:
            self.start_batch([{"argv": argv, "label": v["name"], "project": proj, "names": [v["name"]]}])
        if go:
            self.after_start = {"project": proj, "name": v["name"], "attach": attach, "t0": time.time()}
        return True

    def form_save_runai(self, v):
        """M in the form: create an environment, compute resource, data source or workload template
        from the form, in the form's project (visible in the Run:ai UI too)."""
        p = self.st.project(v["project"])
        if not p:
            self.flash("no project")
            return
        binds = form_binds(v)[0]
        pvcs = parse_pvcs(v.get("pvcs"))[0]
        opts = [("template", f"workload template: the whole form ({v['kind']})"),
                ("environment", f"environment: {v['image'] or '(no image)'}" + (" + command" if v["command"] else "")
                 + (" + run as me" if v["run_as_me"] else "")),
                ("compute", f"compute resource: {v['cpu'] or '-'} CPU, {v['memory'] or '-'}, GPU {v['gpu']}"
                 + (f" {v['gpu_amount']}" if v["gpu"] != "none" else "") + (", large /dev/shm" if v["shm"] else ""))]
        opts += [("bind", f"data source: host path {b['host']} → {b['path']}", b) for b in binds]
        opts += [("pvc", f"data source: PVC {c['host']} → {c['path']}", c) for c in pvcs]
        i = self.pick(f"save to Run:ai, in project {p['name']}", [o[1] for o in opts])
        if i is None:
            return
        what = opts[i]
        item = what[2] if len(what) > 2 else None
        default = {"template": v["name"], "environment": (v["image"].split("/")[-1].split(":")[0] or "env"),
                   "compute": "cpu{}-{}".format(v["cpu"] or 0, "gpu" + v["gpu_amount"] if v["gpu"] != "none" else
                                               "nogpu").replace(".", "p")}.get(what[0])
        if item:
            default = os.path.basename(item["host"].rstrip("/")) or "root" if what[0] == "bind" else item["host"]
        name = self.prompt(f"name for the {what[0] if what[0] not in ('bind', 'pvc') else 'data source'}",
                           re.sub(r"[^a-z0-9-]+", "-", (default or "").lower()).strip("-"))
        if not name:
            return
        if not NAME_RE.match(name):
            self.flash("names: lowercase letters, digits and '-'")
            return
        desc = f"saved from runai-tui by {self.sess.email or getpass.getuser()}"
        api = CFG["api"]
        notes = []
        try:
            if what[0] == "template":
                tkind, body, notes = template_body(name, v, self.st, desc)
                path, key = f"{api['templates']}/{tkind}", "templates"
            elif what[0] == "environment":
                if not v["image"].strip():
                    self.flash("no image to save")
                    return
                path, body, key = api["environments"], environment_body(name, v, p, desc), "environments"
            elif what[0] == "compute":
                path, body, key = api["computes"], {"meta": asset_meta(name, p, desc), "spec": form_compute(v)}, \
                    "computes"
            elif what[0] == "bind":
                path, key = f"{api['datasources']}/host-path", "datasources"
                body = {"meta": asset_meta(name, p, desc),
                        "spec": {"path": item["host"], "mountPath": item["path"], "readOnly": item["ro"]}}
            else:
                path, key = f"{api['datasources']}/pvc", "datasources"
                body = {"meta": asset_meta(name, p, desc), "spec": {
                    "claimName": item["host"], "path": item["path"], "existingPvc": True, "readOnly": item["ro"]}}
        except ValueError as e:
            self.flash(str(e))
            return
        try:
            self.busy(f"creating {name} ...", lambda: self.sess.api(path, "POST", body))
        except ApiError as e:
            log_action(f"save {what[0]} {name} to Run:ai: {e}")
            msg = str(e)
            if msg.startswith("403"):
                msg += " (your role may not create these in this project: ask an administrator)"
            self.flash(f"✖ {what[0]} {name}: {msg}")
            return
        log_action(f"saved {what[0]} {name} to Run:ai project {p['name']}", ["POST", path])
        try:                                     # refresh that list now, and use what was saved
            fresh = sorted(fetch_assets(self.sess, key), key=lambda e: (e.get("meta") or {}).get("name", ""))
            with self.st.lock:
                setattr(self.st, {"environments": "envs"}.get(key, key), fresh)
        except ApiError:
            pass
        if what[0] == "environment":
            v["env"] = name
        elif what[0] == "compute":
            v["compute"] = name
        elif what[0] == "template":
            v["template"] = name
        elif what[0] in ("bind", "pvc"):
            if what[0] == "bind":
                v["extra_binds"] = format_binds([b for b in parse_binds(v["extra_binds"])[0] if b != item])
                v["binds"] = [n for n in v["binds"] if site_binds()[0].get(n) != item]
            else:
                v["pvcs"] = format_pvcs([c for c in pvcs if c != item])
            v["datasources"] = list(v["datasources"]) + [name]
        self.flash(f"✔ saved {what[0] if what[0] not in ('bind', 'pvc') else 'data source'} {name} in {p['name']}"
                   + (f" ({'; '.join(notes)})" if notes else "") + "; the form now uses it")

    def draw_form(self, fields, v, sel, preview, warn, pscroll):
        self.scr.erase()
        self.shadow = {}
        h, w = self.scr.getmaxyx()
        self.put(0, 0, " ▲ New workload".ljust(w), self.C["head"] | curses.A_BOLD)
        lw = max(52, w * 46 // 100)
        fh = h - 3
        self.box(1, 0, fh, lw, "Fields", True)
        ftop = max(0, sel - (fh - 3))
        for i, (key, label, typ, choices) in enumerate(fields[ftop:ftop + fh - 2]):
            val = v[key]
            if typ == "multi":
                val = ", ".join(val) or "(none)"
            elif typ in ("binds", "pvcs"):
                n = len((parse_pvcs if typ == "pvcs" else parse_binds)(val)[0])
                val = f"{n}: {val}" if n else "(none) - Enter to add"
            elif typ == "bool":
                val = "[x] yes" if val else "[ ] no"
            elif key == "kind":
                val = dict(KINDS)[val]
            elif typ == "choice":
                val = f"‹ {val} ›"
            line = f"{label:<20} {val}"
            if ftop + i == sel:
                self.put(2 + i, 1, f" {line}".ljust(lw - 2), self.C["sel"] | curses.A_BOLD, lw - 2)
            else:
                self.put(2 + i, 2, f"{label:<20}", self.C["dim"], lw - 4)
                self.put(2 + i, 23, str(val), 0, lw - 25)
        # quota hint + warnings below the fields
        row = 2 + min(len(fields) - ftop, fh - 2) + 1
        p = self.st.project(v["project"])
        if p and row < fh:
            qs = [f"{q['pool']} {num(q['gpu'])}/{num(q['quota'])}" for q in project_pools(p) if pool_visible(q)]
            self.put(row, 2, f"GPU used/quota  {'  '.join(qs) or '-'}", self.C["info"], lw - 4)
            row += 1
        for x in warn:
            for part in textwrap.wrap("⚠ " + x, lw - 4):
                if row < fh:
                    self.put(row, 2, part, self.C["warn"], lw - 4)
                    row += 1
        pw = w - lw
        self.box(1, lw, fh, pw, "Preview (PgUp/PgDn)")
        lines = []
        for l in preview.split("\n"):
            lines += textwrap.wrap(l, pw - 4, subsequent_indent="    ", drop_whitespace=False) or [""]
        pscroll = max(0, min(pscroll, len(lines) - (fh - 2)))
        for i, l in enumerate(lines[pscroll:pscroll + fh - 2]):
            style = "dim" if l.lstrip().startswith("#") else (
                "info" if re.match(r"^\s*-?\s*[\w./-]+:", l) else None)
            self.put(2 + i, lw + 2, l, self.C.get(style, 0), pw - 4)
        K = lambda a: key_name(KEYS[a])
        keys = [("↑↓", "field"), ("⏎ ←→", "change"), (K("form_submit"), "submit"), (K("form_write"), "write YAML"),
                (K("form_edit"), "edit+submit"), (K("form_go"), "submit+attach"),
                (K("form_runai"), "save to Run:ai"), (K("form_presets"), "presets"), ("Esc", "cancel")]
        x = 0
        for k, label in keys:
            if x + len(k) + len(label) + 4 > w:
                break
            self.put(h - 1, x, f" {k} ", self.C["sel"] | curses.A_BOLD)
            self.put(h - 1, x + len(k) + 2, f"{label} ", 0)
            x += len(k) + len(label) + 4
        if self.status:
            self.put(h - 2, 0, self.status.ljust(w), self.C["warn"])
        self.scr.refresh()
        if self.dump:
            self.dump_screen()

    # setup -----------------------------------------------------------------------------------
    def recheck(self, quiet=False):
        def cb(i, n, c):
            self.check_progress = (i, n, c.title)

        def work():
            try:
                self.checks = run_checks(self.sess, self.st, cb)
            finally:
                self.check_progress = None
                self.poller.wake()
        threading.Thread(target=work, daemon=True).start()
        if not quiet:
            self.flash("checking setup ...")

    def wait_checks(self):
        while self.check_progress is not None or not self.checks:
            self.draw()
            time.sleep(0.12)
            if self.check_progress is None and not self.checks:
                break

    def do_fix(self, c):
        if not c.fix:
            self.flash(c.help or "nothing to fix automatically here")
            return
        kind = c.fix[1]
        if kind == "login":
            self.do_login()
        elif kind == "theme":
            self.choose_theme()
        elif kind == "project":
            opts = c.fix[2] or self.projects()
            i = self.pick("default project", opts, cli_project())
            if i is not None:
                rc, out, err = cmd([RUNAI, "project", "set", opts[i]], timeout=30)
                self.project = opts[i] if rc == 0 else self.project
                self.flash(first_error(out, err))
        else:
            ok, msg = self.busy(c.fix[0], lambda: apply_fix(c))
            self.flash(("✔ " if ok else "✖ ") + msg)
        self.recheck(quiet=True)

    def fix_all(self):
        if any(c.key == "login" and c.state == "fail" for c in self.checks):
            self.do_login()
            return
        todo = [c for c in self.checks if c.state != "ok" and c.fix and c.fix[1] in ("cmd", "py")]
        if not todo:
            self.flash("nothing left to fix automatically" + (
                "" if all(c.state == "ok" for c in self.checks) else " (see the items marked ✖/⚠)"))
            return
        results, done = [], set()
        for _ in range(4):
            # later checks depend on earlier ones (kubeconfig -> namespace): re-plan after each round
            todo = [c for c in self.checks if c.state != "ok" and c.fix and c.fix[1] in ("cmd", "py")
                    and c.key not in done]
            if not todo:
                break
            for i, c in enumerate(todo):
                self.check_progress = (i, len(todo), c.fix[0])
                ok, msg = self.busy(f"{i + 1}/{len(todo)} {c.fix[0]}", lambda c=c: apply_fix(c))
                results.append(f"{'✔' if ok else '✖'} {c.title}: {msg}")
                done.add(c.key)
            self.checks = self.busy("re-checking ...", lambda: run_checks(self.sess, self.st))
        self.check_progress = None
        self.pager("setup fixes", "\n".join(results))
        self.recheck(quiet=True)

    def do_login(self):
        site = site_url()
        if cli_cp_url() != site:
            cmd([RUNAI, "config", "set", f"--cp-url={site}", "--auto-update=disabled", "-q"], timeout=30)
        domain = re.sub(r"^https?://[^.]+\.", "", site).split("/")[0]
        i = self.pick("log in to Run:ai", ["user name and password (here)", "single sign-on (browser on your laptop)"])
        if i is None:
            return
        if i == 0:
            user = self.prompt("Run:ai user", self.sess.email or f"{getpass.getuser()}@{domain}")
            if not user:
                return
            rc = self.external("runai login", [RUNAI, "login", "user", "-u", user],
                               "Same password as the Run:ai web UI: type it, Enter, then Enter again on [ Submit ].\n"
                               "Ten wrong passwords lock the account for a while.", pause=False)
        else:
            rc = self.external("runai login (SSO)", [RUNAI, "login", "remote-browser"],
                               "Open the printed URL in a browser, log in, paste the code back here.")
        self.sess.reset()
        if rc != 0:
            self.flash("login failed - see Setup for details")
            self.recheck(quiet=True)
            return
        self.after_login()

    def after_login(self):
        """The housekeeping the guide lists after `runai login`, with a progress bar."""
        try:
            self.sess.token(force=True)
            projects = [p["name"] for p in (self.sess.api(CFG["api"]["projects"]) or {}).get("projects") or []]
        except Exception as e:
            self.flash(f"logged in, but: {e}")
            projects = []
        steps = []
        if not read_kubeconfig().get("clusters") and os.path.exists(SKEL_KUBECONFIG):
            steps.append(("kubeconfig skeleton", copy_skeleton))
        steps.append(("runai kubeconfig set", lambda: cmd([RUNAI, "kubeconfig", "set", "--bypass-validation"], 60)))
        proj = cli_project()
        if projects and proj not in projects:
            if len(projects) == 1:
                proj = projects[0]
            else:
                i = self.pick("default project", projects)
                proj = projects[i] if i is not None else projects[0]
            steps.append((f"runai project set {proj}", lambda p=proj: cmd([RUNAI, "project", "set", p], 30)))
        if proj:
            steps.append((f"kubectl namespace runai-{proj}", lambda p=proj: cmd(
                [KUBECTL, "config", "set-context", "--current", f"--namespace=runai-{p}"], 30)))
        out = []
        for i, (label, fn) in enumerate(steps):
            self.check_progress = (i, len(steps), label)
            r = self.busy(f"setup {i + 1}/{len(steps)}: {label}", fn)
            ok = not isinstance(r, tuple) or r[0] == 0
            out.append(f"{'✔' if ok else '✖'} {label}" + ("" if ok else f": {first_error(r[2], r[1])}"))
            log_action(f"after login: {label}", output=str(r))
        self.check_progress = None
        self.project = proj or self.project
        self.flash(f"logged in as {self.sess.email} · " + " · ".join(out))
        self.recheck(quiet=True)
        self.poller.wake()

    def do_logout(self):
        if not self.confirm("log out of Run:ai (runai logout)?"):
            return
        rc, out, err = cmd([RUNAI, "logout"], timeout=30)
        self.sess.reset()
        self.st.logged_in = False
        self.flash(first_error(out, err) or "logged out")
        self.recheck(quiet=True)

    def show_log(self):
        try:
            with open(LOG, errors="replace") as f:
                text = "".join(f.readlines()[-3000:])
        except OSError:
            text = "(no log yet)"
        self.pager(LOG, text)

    # --- main loop --------------------------------------------------------------------------
    def run(self):
        self.scr.timeout(250)
        self.recheck(quiet=True)
        first_login_check = True
        while True:
            if first_login_check and self.st.logged_in is False:
                first_login_check = False
                self.tab = T_SETUP
                self.flash(f"not logged in: {key_name(KEYS['login'])} logs you in (or Enter on the ✖ items)")
            elif first_login_check and self.st.logged_in:
                first_login_check = False
            if not self.project and self.st.projects and cli_project():
                self.project = cli_project()
            if self.st.scope != self.project:
                self.st.scope = self.project
                self.poller.wake()
            if (self.st.want_quota, self.st.want_apps) != (self.tab == T_QUOTA, self.tab == T_APPS):
                self.st.want_quota, self.st.want_apps = self.tab == T_QUOTA, self.tab == T_APPS
                if self.tab in (T_QUOTA, T_APPS):
                    self.poller.wake()
            if self.after_start:
                self.check_after_start()
            self.draw()
            k = self.scr.getch()
            if k == -1:
                continue
            h = self.scr.getmaxyx()[0]
            if k == 27:
                if self.after_start:
                    self.flash(f"stopped waiting for {self.after_start['name']} (it keeps starting)")
                    self.after_start = None
                continue
            if ord("1") <= k < ord("1") + len(TABS):
                self.tab = k - ord("1")
            elif k == ord("\t"):
                self.tab = (self.tab + 1) % len(TABS)
            elif k == curses.KEY_BTAB:
                self.tab = (self.tab - 1) % len(TABS)
            elif self.tab in (1, 2) and k in (curses.KEY_DOWN, ord("j")):
                self.sel["wl" if self.tab == 1 else "act"] += 1
            elif self.tab in (1, 2) and k in (curses.KEY_UP, ord("k")):
                key = "wl" if self.tab == 1 else "act"
                self.sel[key] = max(0, self.sel[key] - 1)
            elif self.tab == T_SETUP and k in (curses.KEY_DOWN, ord("j")):
                self.sel["setup"] += 1
            elif self.tab == T_SETUP and k in (curses.KEY_UP, ord("k")):
                self.sel["setup"] = max(0, self.sel["setup"] - 1)
            elif self.tab in (T_QUOTA, T_APPS) and k in (curses.KEY_DOWN, ord("j")):
                self.sel["quota" if self.tab == T_QUOTA else "apps"] += 1
            elif self.tab in (T_QUOTA, T_APPS) and k in (curses.KEY_UP, ord("k")):
                key = "quota" if self.tab == T_QUOTA else "apps"
                self.sel[key] = max(0, self.sel[key] - 1)
            elif self.tab == T_QUOTA and k in (10, 13, curses.KEY_ENTER):
                self.quota_enter()
            elif self.tab == T_APPS and k in (10, 13, curses.KEY_ENTER):
                self.app_describe()
            elif self.tab == 1 and k == curses.KEY_NPAGE:
                self.dscroll += h - 8
            elif self.tab == 1 and k == curses.KEY_PPAGE:
                self.dscroll = max(0, self.dscroll - (h - 8))
            elif self.tab == 1 and k in (10, 13, curses.KEY_ENTER):
                self.do_shell()
            elif self.tab == 2 and k in (10, 13, curses.KEY_ENTER):
                self.go_to_workload()
            elif self.tab == T_SETUP and k in (10, 13, curses.KEY_ENTER):
                if self.checks:
                    self.do_fix(self.checks[min(self.sel["setup"], len(self.checks) - 1)])
            elif 0 <= k < 0x110000:
                if self.dispatch(chr(k)) == "quit":
                    return

    def check_after_start(self):
        """G in the form: once the workload runs, attach to it (or shell in); stop if it fails."""
        a = self.after_start
        w = self.st.find(a["project"], a["name"])
        verb = "attach" if a["attach"] else "shell in"
        if w and w.get("phase") == "Running":
            self.after_start = None
            self.tab = 2
            (self.do_attach if a["attach"] else self.do_shell)(w)
        elif w and w.get("phase") in TERMINAL:
            self.after_start = None
            self.flash(f"{a['name']} is {w.get('phase')}: not starting a {verb} (l shows its logs)")
        elif time.time() - a["t0"] > 900:
            self.after_start = None
            self.flash(f"{a['name']} did not start in 15 minutes: stopped waiting (it is still queued)")
        elif not self.st.in_flight() and not w and time.time() - a["t0"] > 60:
            self.after_start = None
            self.flash(f"{a['name']} did not appear: see Activity")
        elif not self.status or "waiting for" in self.status:
            phase = (w or {}).get("phase") or "submitting"
            self.flash(f"{SPIN[self.tick % 10]} waiting for {a['name']} to run ({phase}), then {verb} · Esc stops waiting")

    def dispatch(self, ch):
        """Run the action bound to ch on this tab (keys: in the config; plugins add their own)."""
        where = {a: w for a, _, w, _ in ACTIONS}
        where.update({a[0]: a[2] for a in PLUGINS.actions})
        act = next((a for a, w in where.items() if w != "form" and KEYS.get(a) == ch
                    and self.tab in SCOPE_TABS[w]), None)
        if not act:
            return None
        plugin = PLUGINS.action(act)
        if plugin:
            w = None
            if plugin[2] in ("workload", "workloads", "activity"):
                w = self.need_workload()
                if not w:
                    return act
            try:
                plugin[4](self, w)
            except Exception as e:
                self.flash(f"{act}: {type(e).__name__}: {e}")
            return act
        handlers = {
            "quit": lambda: None,
            "help": lambda: self.pager("runai-tui help", help_text()),
            "log": self.show_log,
            "refresh": self.refresh,
            "project": self.switch_project,
            "new": self.do_new,
            "submit_files": self.do_submit_files,
            "login": self.do_login,
            "shell": self.do_shell,
            "logs": self.do_logs,
            "follow_logs": lambda: self.do_logs(follow=True),
            "describe": self.do_describe,
            "save": self.do_save,
            "delete": self.do_delete,
            "suspend": self.do_suspend,
            "port_forward": self.do_port_forward,
            "copy_url": lambda: self.do_copy("url"),
            "copy_name": lambda: self.do_copy("name"),
            "toggle_mine": self.toggle_mine,
            "filter": self.set_filter,
            "forget_done": self.forget_done,
            "fix_all": self.fix_all,
            "logout": self.do_logout,
            "theme": self.choose_theme,
            "pvcs": self.show_pvcs,
            "attach": self.do_attach,
            "app_describe": self.app_describe,
            "app_delete": self.app_delete,
            "app_submit": self.app_submit,
        }
        handlers[act]()
        return act

    def refresh(self):
        self.poller.wake()
        if self.tab == T_SETUP:
            self.recheck()
        else:
            self.flash("refreshing ...")

    def go_to_workload(self):
        w = self.need_workload()
        if w:
            ws = self.visible_workloads()
            if w not in ws:
                self.mine, self.filter, self.project = False, "", w.get("projectName")
                ws = self.visible_workloads()
            self.sel["wl"] = next((i for i, x in enumerate(ws) if x.get("id") == w.get("id")), 0)
            self.tab = 1

    def forget_done(self):
        with self.st.lock:
            for key, info in list(self.st.watch.items()):
                wl = self.st.find(*key)
                if wl is None or settled(wl):
                    del self.st.watch[key]
            if self.st.batch and self.st.batch.done:
                self.st.batch = None
        self.sel["act"] = 0

    def toggle_mine(self):
        self.mine = not self.mine
        self.sel["wl"] = self.top["wl"] = 0

    def set_filter(self):
        f = self.prompt("filter (name, phase, type, image)", self.filter)
        if f is not None:
            self.filter = f
            self.sel["wl"] = self.top["wl"] = 0


# --- command-line mode -----------------------------------------------------------------------
ANSI = {"ok": "32", "warn": "33", "info": "36", "err": "31", "dim": "2", "brand": "38;5;208", "pulse": "1;38;5;208",
        "bold": "1", None: "0", "fill": "32", "empty": "2"}


def _ansi_theme():
    for style, (fg, _) in theme().items():
        if style == "base":
            continue
        code = "39" if fg < 0 else f"3{fg}" if fg < 8 else f"38;5;{fg}"
        ANSI[style] = code
        if style == "brand":
            ANSI["pulse"] = "1;" + code


_ansi_theme()


def ansi(text, style):
    return f"\033[{ANSI.get(style, '0')}m{text}\033[0m" if sys.stdout.isatty() else text


def render_cells(cells, attr_map):
    return "".join(ansi(ch, attr_map.get(kind, kind)) for ch, kind in cells)


def cli_session(require_login=True):
    sess, st = Session(), State()
    if require_login:
        try:
            sess.token()
        except NotLoggedIn as e:
            sys.exit(f"not logged in: {e}\n  run `runai-tui` (Setup tab) or `runai login user -u <you>@<site>`")
    return sess, st


def cli_load(sess, st):
    Poller(sess, st).poll()
    if st.error:
        sys.exit(f"Run:ai: {st.error}")


def cli_quota(args):
    sess, st = cli_session()
    st.want_quota = True
    cli_load(sess, st)
    rows = org_rows(st.projects, st.departments)
    print(ansi(f"{'':<26}{'GPU':<26}{'alloc':>6} {'quota':>6} {'over':>6} {'limit':>6} {'pend':>5}", "dim"))
    for dname, dt, kids in rows:
        q = dt["quota"]
        bar = render_cells(quota_cells(24, dt["alloc"], q or 0, None), {})
        print(ansi(f"▸ {dname:<23} ", "brand") + f" {bar}  {num(dt['alloc']):>5} {num(q) if q is not None else '-':>6}")
        for p, t in kids:
            over = max(0, t["alloc"] - t["quota"])
            bar = render_cells(quota_cells(24, t["alloc"], t["quota"], t["limit"]), {})
            print(f"    {p['name']:<22} {bar}  {num(t['alloc']):>5} {num(t['quota']):>6} "
                  + ansi(f"{num(over) if over else '':>6}", "warn")
                  + f" {num(t['limit']) if t['limit'] is not None else '∞':>6} {num(t['pending']) if t['pending'] else '':>5}")
    if st.dept_error:
        print(ansi(f"\ndepartments: {st.dept_error} (totals added up from your projects)", "warn"))


def cli_status(args):
    sess, st = cli_session()
    cli_load(sess, st)
    cl = sess.claims
    print(ansi(f"{cl.get('name', '')} <{sess.email}>", "bold"), f" uid/gid {cl.get('uid')}/{cl.get('gid')}",
          f" groups {','.join(cl.get('groups') or [])}  token {dur(sess.exp - time.time())}")
    print(f"default project: {cli_project() or '(none)'}   site: {sess.base}\n")
    for p in st.projects:
        if args.project and p["name"] != args.project:
            continue
        print(ansi(p["name"], "brand"), ansi(f"department {(p.get('parent') or {}).get('name', '-')}", "dim"))
        for q in project_pools(p):
            if not pool_visible(q):
                continue
            bar = render_cells(quota_cells(24, q["gpu"], q["quota"], q["limit"]), {})
            print(f"  {q['pool']:<8} {bar}  GPU {num(q['gpu'])}/{num(q['quota'])} (limit {num(q['limit'])}, "
                  f"non-preemptible {num(q['gpu_np'])})  CPU {num(q['cpu'])}  mem {human_mem(q['mem'])}")
        print()
    mine = [w for w in st.workloads if st.mine(w)]
    print(f"your workloads: {len(mine)}")
    for w in mine[:20]:
        mark, style = PHASE_STYLE.get(w.get("phase"), ("·", None))
        print(f"  {ansi(mark, style)} {w['name']:<28} {w.get('projectName', ''):<14} {w.get('type', ''):<11} "
              f"{w.get('phase', ''):<12} GPU {num(wl_gpu(w))}")


def cli_ls(args):
    sess, st = cli_session()
    cli_load(sess, st)
    proj = args.project or ("" if args.all else cli_project())
    print(f"{'':2}{'NAME':<30} {'PROJECT':<14} {'TYPE':<11} {'PHASE':<12} {'GPU':>5} {'AGE':>7}  SUBMITTED BY")
    now = time.time()
    for w in st.workloads:
        if proj and w.get("projectName") != proj:
            continue
        if not args.all and not st.mine(w):
            continue
        mark, style = PHASE_STYLE.get(w.get("phase"), ("·", None))
        print(f"{ansi(mark, style)} {w['name']:<30} {w.get('projectName', ''):<14} {w.get('type', ''):<11} "
              f"{w.get('phase', ''):<12} {num(wl_gpu(w) or wl_gpu_req(w)):>5} "
              f"{dur(now - (parse_ts(w.get('createdAt')) or now)):>7}  {w.get('submittedBy') or '(kubectl)'}")


def cli_doctor(args):
    sess, st = Session(), State()
    icons = {"ok": ansi("✔", "ok"), "warn": ansi("⚠", "warn"), "fail": ansi("✖", "err")}

    def show(checks):
        for c in checks:
            print(f"{icons.get(c.state, '·')} {c.title:<34} {c.detail}")
            if c.state != "ok" and c.fix:
                print(ansi(f"    fix: {c.fix[0]}" + ("  (interactive: use the TUI)"
                                                     if c.fix[1] in ("login", "project") else ""), "dim"))
    checks = run_checks(sess, st)
    show(checks)
    fixed = False
    # later fixes depend on earlier ones (kubeconfig -> namespace): re-check after each round
    for _ in range(4 if args.fix else 0):
        todo = [c for c in checks if c.state != "ok" and c.fix and c.fix[1] in ("cmd", "py")]
        if not todo:
            break
        print()
        for c in todo:
            ok, msg = apply_fix(c)
            print(f"  {icons['ok' if ok else 'fail']} {c.fix[0]}: {msg}")
        checks, fixed = run_checks(sess, st), True
    if fixed:
        print(ansi("\nafter fixes:", "bold"))
        show(checks)
    sys.exit(0 if all(c.state == "ok" for c in checks) else 1)


def watch_loop(sess, st, keys, until_settled=True):
    """ANSI progress bars, redrawn in place, until all keys are terminal (or running services)."""
    poller = Poller(sess, st)
    drawn, tick, last = 0, 0, None
    try:
        while True:
            if tick % 8 == 0:
                poller.poll()
            lines = []
            settled = True
            for proj, name in keys:
                w = st.find(proj, name) or {"name": name, "projectName": proj, "phase": "Submitted"}
                frac, colour, stages, idx, anim = progress(w, st.history.get(w.get("id")))
                if anim:
                    settled = False
                mark, style = PHASE_STYLE.get(w.get("phase"), ("·", None))
                reason = pending_reason(w) if w.get("phase") == "Pending" else ""
                lines.append(f"{ansi(mark, style)} {name:<28} {proj:<14} {ansi(w.get('phase', ''), style):<12} "
                             + (ansi(reason[:60], "warn") if reason else ""))
                bar = render_cells(bar_cells(40, frac, anim, tick), {"fill": colour, "pulse": "pulse", "empty": "dim"})
                lines.append(f"    {bar} {int(frac * 100):>3}%  " + ansi(" › ".join(
                    s if i != idx else f"[{s}]" for i, s in enumerate(stages)), "dim"))
            if not sys.stdout.isatty():                  # piped: print phase changes only
                state_ = [l for l in lines[::2]]
                if state_ != last:
                    print("\n".join(state_), flush=True)
                    last = state_
            else:
                if drawn:
                    sys.stdout.write(f"\033[{drawn}F")
                sys.stdout.write("".join(f"\033[2K{l}\n" for l in lines))
                sys.stdout.flush()
                drawn = len(lines)
            if until_settled and settled and tick > 0:
                return True
            tick += 1
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\n(stopped watching; the workloads keep going)")
        return False


def cli_submit(args):
    sess, st = cli_session()
    cli_load(sess, st)
    items = [{"file": f, "label": os.path.basename(f), "project": args.project or ""} for f in args.files]
    for it in items:
        if not it["project"]:
            try:
                it["project"] = doc_project(load_docs(it["file"])) or cli_project()
            except Exception:
                it["project"] = cli_project()
    b = Batch(items, st)
    b.start()
    tick = 0
    while not b.done:
        done = sum(1 for it in items if it.get("state") in ("ok", "fail"))
        bar = render_cells(bar_cells(40, done / len(items), True, tick), {"fill": "info", "pulse": "pulse",
                                                                         "empty": "dim"})
        cur = items[b.current]["label"] if 0 <= b.current < len(items) else ""
        if sys.stdout.isatty():
            sys.stdout.write(f"\r\033[2Ksubmitting {bar} {done}/{len(items)}  {cur}")
            sys.stdout.flush()
        tick += 1
        time.sleep(0.15)
    if sys.stdout.isatty():
        sys.stdout.write("\r\033[2K")
    for it in items:
        ok = it["state"] == "ok"
        print(f"{ansi('✔' if ok else '✖', 'ok' if ok else 'err')} {it['label']:<32} {it.get('method', ''):<22} "
              f"{it.get('msg', '')}")
    keys = list(st.watch.keys())
    if keys and not args.no_wait:
        print()
        watch_loop(sess, st, keys)
    sys.exit(0 if all(it["state"] == "ok" for it in items) else 1)


def cli_watch(args):
    sess, st = cli_session()
    cli_load(sess, st)
    proj = args.project or cli_project()
    if args.names:
        keys = [(proj, n) for n in args.names]
    else:
        keys = [(w["projectName"], w["name"]) for w in st.workloads
                if st.mine(w) and not settled(w)]
    if not keys:
        print("nothing in flight")
        return
    watch_loop(sess, st, keys)


def cli_save(args):
    sess, st = cli_session()
    cli_load(sess, st)
    proj = args.project or cli_project()
    w = st.find(proj, args.name)
    if not w:
        sys.exit(f"no workload {args.name} in project {proj}")
    text, how = export_workload(sess, w)
    if args.output == "-":
        sys.stdout.write(text)
        return
    path = os.path.expanduser(args.output or os.path.join(SAVE_DIR, f"{args.name}.yaml"))
    if os.path.exists(path) and not args.force:
        sys.exit(f"{path} exists (--force overwrites, -o - prints)")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    print(f"saved {path} ({how})")


def cli_shell(args):
    proj = args.project or cli_project()
    argv = [RUNAI, "workload", "exec", args.name, "-p", proj, "-i", "-t", "--wait-timeout", "120s",
            "--", "sh", "-c", SHELL_CMD]
    os.execvp(argv[0], argv)


def cli_config(args):
    if args.defaults:
        print("# runai-tui configuration: copy what you change to ~/.config/runai-tui/config.yaml\n"
              "# (yours) or /etc/runai-tui/config.yaml (the site's).")
        sys.stdout.write(yaml.safe_dump(DEFAULTS, sort_keys=False, default_flow_style=False, width=100))
        return
    load_plugins()
    print(ansi("files", "brand"))
    for f in CONFIG_FILES + ([os.environ["RUNAI_TUI_CONFIG"]] if os.environ.get("RUNAI_TUI_CONFIG") else []):
        there = os.path.exists(os.path.expanduser(f))
        print(f"  {ansi('✔', 'ok') if there else ansi('·', 'dim')} {f}")
    print(ansi("\nvalues", "brand") + ansi("  (source: default unless shown)", "dim"))

    def walk(node, path=""):
        for k, val in node.items():
            key = f"{path}{k}"
            if isinstance(val, dict) and val and key not in ("keys", "theme"):
                walk(val, key + ".")
            else:
                src = CFG_SOURCES.get(key) or next((v for p_, v in CFG_SOURCES.items()
                                                    if p_.startswith(key + ".")), "")
                print(f"  {key:<36} {json.dumps(val, ensure_ascii=False)}" + (f"  {ansi(src, 'info')}" if src else ""))
    walk(CFG)
    print(ansi("\nkeys", "brand"))
    for a, _, where, label in list(ACTIONS) + [x[:4] for x in PLUGINS.actions]:
        print(f"  {key_name(KEYS[a]):<6} {a:<24} {where:<10} {label}")
    presets = list_presets()
    print(ansi("\npresets", "brand") + f"  {', '.join(presets) or '(none)'}")
    cur, src = theme_choice()
    print(ansi("theme", "brand") + f"    {cur}" + (ansi(f"  {src}", "info") if src else "")
          + ansi(f"  (of {', '.join(load_themes())}; `runai-tui theme` to change)", "dim"))
    if PLUGINS.loaded:
        print(ansi("plugins", "brand") + "  " + ", ".join(PLUGINS.loaded))
    errs = CONFIG_ERRORS + PLUGINS.errors + site_binds()[1]
    for e in errs:
        print(ansi(f"⚠ {e}", "warn"))
    sys.exit(1 if errs else 0)


def cli_theme(args):
    if args.import_file:
        try:
            args.name = import_theme(args.import_file)
        except (OSError, ValueError, yaml.YAMLError) as e:
            sys.exit(f"runai-tui: can't import {args.import_file}: {str(e).splitlines()[0]}")
        print(f"imported {args.import_file} as {args.name} ({THEME_DIRS[-1]}/{args.name}.yaml)")
    themes = load_themes()
    if args.name:
        if args.name not in themes:
            sys.exit(f"runai-tui: no theme {args.name} (themes: {', '.join(themes)})")
        save_theme_choice(args.name)
        print(f"using {themes[args.name].get('name') or args.name} (saved in {THEME_CHOICE})")
        return
    cur, src = theme_choice()
    for n, t in themes.items():
        mark = ansi("●", "ok") if n == cur else " "
        print(f" {mark} {n:<20} {str(t.get('name') or n):<28} {ansi(t['_src'], 'dim')}")
    if src:
        print(ansi(f"\nin use: {cur} (from {src})", "dim"))


def main():
    sanitize_env()
    if sys.argv[1:2] != ["config"]:
        load_plugins()
    if ENV_NOTE:
        print(f"runai-tui: {ENV_NOTE}", file=sys.stderr)
    commands = {"status", "ls", "doctor", "submit", "watch", "save", "shell", "config", "theme", "quota"}
    if len(sys.argv) > 1 and sys.argv[1] in commands:
        p = argparse.ArgumentParser(prog="runai-tui", description=__doc__.split("\n")[0])
        sub = p.add_subparsers(dest="cmd", required=True)
        s = sub.add_parser("status", help="identity, quota bars, your workloads")
        s.add_argument("-p", "--project")
        s = sub.add_parser("ls", help="workloads")
        s.add_argument("-p", "--project")
        s.add_argument("-A", "--all", action="store_true", help="everyone's, in every project you can see")
        s = sub.add_parser("doctor", help="setup checklist")
        s.add_argument("--fix", action="store_true", help="apply the non-interactive fixes")
        s = sub.add_parser("submit", help="submit manifests and follow them")
        s.add_argument("files", nargs="+")
        s.add_argument("-p", "--project")
        s.add_argument("--no-wait", action="store_true")
        s = sub.add_parser("watch", help="progress bars")
        s.add_argument("names", nargs="*")
        s.add_argument("-p", "--project")
        s = sub.add_parser("save", help="workload -> YAML")
        s.add_argument("name")
        s.add_argument("-p", "--project")
        s.add_argument("-o", "--output", help="file, or - for stdout (default ~/runai/<name>.yaml)")
        s.add_argument("--force", action="store_true")
        s = sub.add_parser("shell", help="shell into a workload")
        s.add_argument("name")
        s.add_argument("-p", "--project")
        s = sub.add_parser("config", help="the configuration in use, and where each value comes from")
        s.add_argument("--defaults", action="store_true", help="print the built-in defaults as YAML")
        sub.add_parser("quota", help="GPU quota by department and project")
        s = sub.add_parser("theme", help="list colour themes, choose one, or import a VS Code theme")
        s.add_argument("name", nargs="?", help="the theme to use from now on")
        s.add_argument("--import", dest="import_file", metavar="FILE",
                       help="import a VS Code colour theme (.json) or a runai-tui theme (.yaml), and use it")
        args = p.parse_args()
        {"theme": cli_theme, "quota": cli_quota, "status": cli_status, "ls": cli_ls, "doctor": cli_doctor, "submit": cli_submit, "watch": cli_watch,
         "save": cli_save, "shell": cli_shell, "config": cli_config}[args.cmd](args)
        return
    p = argparse.ArgumentParser(prog="runai-tui", usage="%(prog)s [-p PROJECT] | {status,ls,doctor,submit,watch,save,shell,config} ...",
                                description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-p", "--project", help="start in this project")
    p.add_argument("--setup", action="store_true", help="start on the Setup tab")
    args = p.parse_args()
    if not sys.stdout.isatty():
        sys.exit("runai-tui: the TUI needs a terminal (try `runai-tui status`)")
    sess, st = Session(), State()
    poller = Poller(sess, st)
    poller.start()
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: Tui(scr, sess, st, poller, args.project, T_SETUP if args.setup else 0).run())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
