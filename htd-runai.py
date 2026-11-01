#!/usr/bin/env python3
"""htd-runai: a terminal UI for researchers over the Run:ai CLI (login node).

The researcher's companion to htd-images: everything runs as you, with your
own Run:ai login. Reads come from the Run:ai API with your CLI token
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

  htd-runai                         TUI
  htd-runai status                  identity, quota bars, your workloads
  htd-runai ls [-A]                 workloads (all users of the project with -A)
  htd-runai doctor [--fix]          setup checklist (and apply the safe fixes)
  htd-runai submit FILE... [-p P]   submit manifests, then follow them
  htd-runai watch [NAME...]         progress bars until everything settles
  htd-runai save NAME [-o FILE]     workload -> YAML manifest
  htd-runai shell NAME              shell into a workload

Manifests submitted from the form are kept in ~/.local/state/htd-runai/
submitted/; actions are logged to ~/.local/state/htd-runai/htd-runai.log.
"""
import argparse
import base64
import calendar
import curses
import datetime
import getpass
import glob
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
import urllib.request

import yaml

SHARE = "/usr/local/share/runai"                 # runai/48-user-access.sh
SKEL_KUBECONFIG = f"{SHARE}/kubeconfig"
EXAMPLES = f"{SHARE}/examples"
PROFILE_HOOK = "/etc/profile.d/runai-cli.sh"
DEFAULT_CP = "https://runai.htd.local"
STATE = os.path.expanduser("~/.local/state/htd-runai")
LOG = os.path.join(STATE, "htd-runai.log")
SUBMITTED = os.path.join(STATE, "submitted")
SAVE_DIR = os.path.expanduser("~/runai")
# by name when on PATH: Run:ai records the command line (shown as "cli" in the UI)
RUNAI = "runai" if shutil.which("runai") else "/usr/local/bin/runai"
KUBECTL = "kubectl" if shutil.which("kubectl") else "/usr/local/bin/kubectl"
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
MEM_RE = re.compile(r"^\d+(\.\d+)?(Mi|Gi|Ti|M|G|T)$")
WORKLOAD_KINDS = {"Job", "Deployment", "StatefulSet", "Pod", "ReplicaSet", "CronJob"}
PRIORITIES = ["very-low", "low", "medium-low", "medium", "medium-high", "high", "very-high"]
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
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
    """The control plane this site's profile hook points the CLI at."""
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
    from root keeps root's KUBECONFIG=/etc/rancher/rke2/rke2.yaml (cluster admin,
    root-only); `runai kubeconfig set` then fails with 'permission denied'. Without
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
        self.envs, self.computes = [], []
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


class Poller(threading.Thread):
    def __init__(self, sess, state):
        super().__init__(daemon=True)
        self.sess, self.state = sess, state
        self.ev = threading.Event()
        self.assets_at = 0

    def wake(self):
        self.ev.set()

    def run(self):
        while True:
            self.poll()
            if self.state.logged_in is False:
                delay = 15
            else:
                delay = 2 if self.state.in_flight() else 6
            self.ev.wait(delay)
            self.ev.clear()

    def poll(self):
        st, sess = self.state, self.sess
        try:
            projects = (sess.api("/api/v1/org-unit/projects") or {}).get("projects") or []
            wls = (sess.api("/api/v1/workloads?limit=500") or {}).get("workloads") or []
            envs = computes = None
            if time.time() - self.assets_at > 300:
                envs = (sess.api("/api/v1/asset/environment") or {}).get("entries") or []
                try:
                    computes = (sess.api("/api/v1/asset/compute") or {}).get("entries") or []
                except ApiError:
                    computes = []
                self.assets_at = time.time()
            pods = None
            fid = st.focus_id
            if fid:
                try:
                    pods = (sess.api(f"/api/v1/workloads/{fid}/pods") or {}).get("pods") or []
                except ApiError as e:
                    pods = str(e)
            now = time.time()
            with st.lock:
                st.me = sess.email
                st.projects = sorted(projects, key=lambda p: p.get("name", ""))
                st.workloads = sorted(wls, key=lambda w: w.get("createdAt") or "", reverse=True)
                if envs is not None:
                    st.envs = sorted(envs, key=lambda e: e["meta"]["name"])
                if computes is not None:
                    st.computes = computes
                if fid:
                    st.pods[fid] = (now, pods)
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
            head = [f"# {w.get('type')} {name} in project {w.get('projectName')}, saved by htd-runai "
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
    spec = sess.api(f"/api/v1/workloads/{kind}/{w['id']}") or {}
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
    if image and reg and not image.startswith(reg + "/") and not image.startswith("docker.io/library/"):
        warn.append(f"image is not in the local registry ({reg}/workloads/...): compute nodes are offline")
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
    home = f"/home/{user}"

    if kind == "workspace":                      # Run:ai CLI, keeps the environment's tools/URLs
        argv = [RUNAI, "workspace", "submit", name, "-p", proj]
        if v["env"] != "(custom image)":
            argv += ["--environment", v["env"]]
        else:
            argv += ["-i", image]
        if cpu:
            argv += ["--cpu-core-request", cpu]
        if mem:
            argv += ["--cpu-memory-request", mem.replace("i", "")]
        if gpu == "whole":
            argv += ["-g", str(int(g or 1))]
        elif gpu == "fraction":
            argv += ["--gpu-portion-request", amount]
        if v["pool"] != "any":
            argv += ["--node-pools", v["pool"]]
        if v["run_as_me"]:
            argv += ["--run-as-user", "--host-path", "path=/home,mount=/home,readwrite",
                     "--working-dir", home, "-e", f"HOME={home}"]
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
        pod_meta.setdefault("labels", {})["runai/node-pool"] = v["pool"]
    if gpu == "fraction":
        pod_meta.setdefault("annotations", {})["gpu-fraction"] = amount
    if kind == "service":
        pod_meta.setdefault("labels", {})["app"] = name
    env = list(env_vars)
    mounts, vols = [], []
    spec = {}
    if v["run_as_me"]:
        spec["securityContext"] = {"runAsUser": uid, "runAsGroup": gid, "supplementalGroups": [gid]}
        container["workingDir"] = home
        env.insert(0, {"name": "HOME", "value": home})
        mounts.append({"name": "home", "mountPath": "/home"})
        vols.append({"name": "home", "hostPath": {"path": "/home", "type": "Directory"}})
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
    return man, None, warn


def registry_host(state):
    for e in state.envs:
        img = e.get("spec", {}).get("image", "")
        if "/workloads/" in img:
            return img.split("/workloads/")[0]
    return ""


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
    head = (f"# Created with htd-runai {datetime.datetime.now():%F %T}\n"
            f"# submit: runai workload submit -f <this file> -p {project}\n")
    return head + yaml.safe_dump(man, sort_keys=False, default_flow_style=False, width=100, allow_unicode=True)


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
        if it.get("argv"):
            it["method"] = "runai workspace submit"
            return it["argv"]
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
        text = open(src).read().replace("5102", str(uid)).replace("5100", str(gid)).replace("lperez", user)
        with open(dst, "w") as f:
            f.write(text)
        done.append(os.path.basename(dst))
    return f"{len(done)} examples personalised into {SAVE_DIR}" if done else "examples already there"


def run_checks(sess, state, cb=None):
    checks, total = [], 11

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
                  help="Ask an administrator: runai/48-user-access.sh installs /usr/local/bin/runai."))
    # 2 control plane URL
    cfg, site = read_cli_config(), site_url()
    cp = cli_cp_url(cfg)
    fix = ("point the CLI at " + site, "cmd", [RUNAI, "config", "set", f"--cp-url={site}", "--auto-update=disabled", "-q"])
    if cp == site:
        add(Check("cpurl", "CLI points at this site", "ok", cp))
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
                  help="Expect 'token is expired' errors. Tell an administrator (ww1 time sync: runai/09-time.sh)."))
    # 5 default project
    projects = []
    if logged:
        try:
            projects = [p["name"] for p in (sess.api("/api/v1/org-unit/projects") or {}).get("projects") or []]
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
                  + ("" if uid >= 5000 else "  (not a site user: /home is not mounted into pods as you)")))
    except OSError as e:
        add(Check("home", "Home directory", "warn", str(e)))
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
HELP = """\
 htd-runai: Run:ai for researchers

 Everywhere
   1 2 3 4        Overview · Workloads · Activity · Setup     Tab  next view
   n              new workload (form -> YAML -> submit)       F    submit YAML file(s)
   p              switch project                              r    refresh now
   !              action log                                  ?    this help     q  quit

 Workloads
   ↑/↓ j/k        move                 PgUp/PgDn  scroll details     /  filter
   Enter s        shell into it        l  logs        L  follow logs (Ctrl-C returns)
   d              describe (events)    y  save as YAML (~/runai/<name>.yaml)
   f              port-forward         u  copy URL    c  copy name
   z              suspend / resume     x  delete      a  mine / everyone in the project

 Activity       progress of everything you submitted or have in flight
   D              forget finished entries

 Setup          the one-time checklist
   Enter          fix the selected item     A  fix everything safe     i  log in     o  log out

 Form (n)
   ↑/↓            field     Enter / ←→  edit or change     S  submit     W  write YAML     E  $EDITOR
   Esc            cancel

 Marks  ★ yours   ● running   ◐ pending/starting   ✔ completed   ✖ failed

 Submitting: one object per file -> `runai workload submit -f`; files with several
 objects (Service, Ingress, ...) -> `kubectl apply -f`. Everything is logged to
 ~/.local/state/htd-runai/htd-runai.log."""

TABS = ["Overview", "Workloads", "Activity", "Setup"]
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
        self.sel = {"wl": 0, "act": 0, "setup": 0}
        self.top = {"wl": 0, "act": 0, "setup": 0}
        self.dscroll = 0
        self.status, self.status_at = "", 0
        self.checks, self.check_progress = [], None
        self.tick = 0
        self.dump = os.environ.get("HTD_RUNAI_SCREEN")       # test hook: plain-text copy of each frame
        self.shadow = {}
        curses.curs_set(0)
        curses.use_default_colors()
        c256 = curses.COLORS >= 256
        for i, (fg, bg) in enumerate([(curses.COLOR_WHITE, 17 if c256 else curses.COLOR_BLUE),
                                      (curses.COLOR_GREEN, -1), (curses.COLOR_YELLOW, -1), (curses.COLOR_CYAN, -1),
                                      (curses.COLOR_RED, -1), (curses.COLOR_BLACK, curses.COLOR_CYAN),
                                      (208 if c256 else curses.COLOR_YELLOW, -1), (curses.COLOR_MAGENTA, -1),
                                      (244 if c256 else curses.COLOR_WHITE, -1),
                                      (curses.COLOR_WHITE, 24 if c256 else curses.COLOR_BLUE)], start=1):
            curses.init_pair(i, fg, bg)
        self.C = {k: curses.color_pair(i) for i, k in enumerate(
            ["head", "ok", "warn", "info", "err", "sel", "brand", "mag", "dim", "tab"], start=1)}
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
            self.put(0, 0, "terminal too small for htd-runai (need 80x16)", self.C["err"])
            self.scr.refresh()
            return
        st = self.st
        who = self.sess.email or "not logged in"
        head = f" ▲ HTD Researcher · Run:ai   {who}"
        self.put(0, 0, head.ljust(w), self.C["head"] | curses.A_BOLD)
        if st.logged_in is False:
            right = "✖ not logged in (4 → Setup)"
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
        [self.draw_overview, self.draw_workloads, self.draw_activity, self.draw_setup][self.tab](*body, w)
        # status + keys
        if self.status and time.time() - self.status_at > 12:
            self.status = ""
        msg = self.status or (st.error and f"⚠ {st.error}") or ""
        self.put(h - 2, 0, msg.ljust(w), self.C["warn"] if msg else 0)
        keys = {0: [("n", "new"), ("F", "submit files"), ("p", "project"), ("2", "workloads")],
                1: [("⏎", "shell"), ("l", "logs"), ("d", "describe"), ("y", "save YAML"), ("x", "delete"),
                    ("a", "mine/all"), ("/", "filter"), ("n", "new")],
                2: [("⏎", "go to"), ("D", "forget done"), ("n", "new"), ("F", "submit files")],
                3: [("⏎", "fix"), ("A", "fix all"), ("i", "log in"), ("o", "log out"), ("r", "re-check")]}[self.tab]
        keys += [("?", "help"), ("q", "quit")]
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
            with open(self.dump, "w") as f:
                f.write("\n".join(rows) + "\n")
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
            lines += [[("Not logged in.", "err")], [("Press 4 (Setup) and i to log in.", None)]]
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
                          [("  press 4 (Setup), then A to fix", "dim")]]
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
            for part in textwrap.wrap(text, dw - 4) or [""]:
                if row >= top + height - 1:
                    return
                self.put(row, lw + 2, part, self.C.get(style, 0), dw - 4)
                row += 1

    # --- input widgets ---------------------------------------------------------------
    def prompt(self, label, default="", complete_paths=False, secret=False):
        """One-line editor on the status line. Esc cancels (None)."""
        buf = list(default)
        curses.curs_set(1)
        self.scr.timeout(-1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                text = "".join(buf)
                shown = "*" * len(text) if secret else text
                line = f"{label}: {shown}"
                self.put(h - 2, 0, line[-(w - 1):].ljust(w - 1), self.C["info"] | curses.A_BOLD)
                self.scr.move(h - 2, min(len(line), w - 2))
                self.scr.refresh()
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
                        self.draw()
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

    def pager(self, title, text):
        lines = text.split("\n")
        top, search = 0, ""
        self.scr.timeout(-1)
        try:
            while True:
                self.scr.erase()
                h, w = self.scr.getmaxyx()
                self.put(0, 0, f" {title}".ljust(w), self.C["head"] | curses.A_BOLD)
                for i, l in enumerate(lines[top:top + h - 2]):
                    attr = self.C["warn"] if search and search.lower() in l.lower() else 0
                    self.put(1 + i, 0, l, attr, w)
                pos = f" {min(top + h - 2, len(lines))}/{len(lines)} "
                self.put(h - 1, 0, " ↑/↓ PgUp/PgDn g/G scroll · / search · n next · q back ", self.C["sel"])
                self.put(h - 1, w - len(pos) - 1, pos, self.C["dim"])
                self.scr.refresh()
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
                input("[Enter] back to htd-runai ")
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
            pods = self.busy("pods ...", lambda: (self.sess.api(f"/api/v1/workloads/{w['id']}/pods") or {})
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

    def do_shell(self):
        w = self.need_workload()
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
        self.external(f"shell in {w['name']}", argv, "exit (or Ctrl-D) returns to htd-runai", pause=False)

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
            self.external(f"logs of {w['name']}", argv + ["-f", "--timestamps"], "Ctrl-C returns to htd-runai")
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

    def form_fields(self, v):
        envs = ["(custom image)"] + [e["meta"]["name"] for e in self.st.envs]
        pools = ["any"]
        p = self.st.project(v["project"])
        if p:
            pools += [q["pool"] for q in project_pools(p) if q["pool"] != "default"]
        F = [("project", "Project", "choice", self.projects() or [v["project"]]),
             ("kind", "Kind", "choice", [k for k, _ in KINDS]),
             ("name", "Name", "text", None),
             ("env", "Environment", "choice", envs),
             ("image", "Image", "text", None),
             ("command", "Command (sh -c)", "text", None),
             ("gpu", "GPU", "choice", ["none", "fraction", "whole"]),
             ("gpu_amount", "  GPU amount", "text", None),
             ("pool", "Node pool", "choice", pools),
             ("cpu", "CPU cores", "text", None),
             ("memory", "Memory", "text", None),
             ("run_as_me", "Run as me + /home", "bool", None),
             ("scratch", "Scratch /scratch", "text", None),
             ("shm", "/dev/shm size", "text", None),
             ("env_vars", "Env (A=1,B=2)", "text", None),
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
            hide |= {"scratch", "shm"}
            if v["env"] != "(custom image)":
                hide |= {"image", "command"}
        return [f for f in F if f[0] not in hide]

    def apply_env(self, v):
        e = next((x for x in self.st.envs if x["meta"]["name"] == v["env"]), None)
        if not e:
            return
        s = e.get("spec") or {}
        v["image"] = s.get("image", v["image"])
        v["command"] = " ".join(x for x in (s.get("command"), s.get("args")) if x)
        v["run_as_me"] = s.get("uidGidSource") == "fromIdpToken" or v["run_as_me"]

    def do_new(self):
        if not self.st.projects:
            self.flash("no projects loaded yet (logged in? see Setup)")
            return
        proj = self.project or self.projects()[0]
        v = {"project": proj, "kind": "training", "name": f"job-{datetime.datetime.now():%m%d-%H%M}",
             "env": "(custom image)", "image": "", "command": "echo hello from $(hostname); nproc; sleep 30",
             "gpu": "none", "gpu_amount": "0.5", "pool": "any", "cpu": "0.5", "memory": "1Gi", "run_as_me": True,
             "scratch": "", "shm": "", "env_vars": "", "completions": "1", "ttl": "3600",
             "priority": "default", "preempt": "default"}
        ubuntu = next((e for e in self.st.envs if e["meta"]["name"] == "ubuntu"), None)
        v["image"] = (ubuntu or {}).get("spec", {}).get("image") or (registry_host(self.st) + "/workloads/ubuntu:24.04")
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
                elif k in ("\n", "\r", curses.KEY_ENTER):
                    if typ == "text":
                        new = self.prompt(fields[sel][1].strip(), v[key])
                        if new is not None:
                            v[key] = new
                        self.scr.timeout(-1)
                    elif typ == "bool":
                        v[key] = not v[key]
                    else:
                        i = self.pick(fields[sel][1].strip(), choices, v[key])
                        self.scr.timeout(-1)
                        if i is not None:
                            v[key] = choices[i]
                            self.form_changed(v, key)
                elif k in ("S", "W", "E"):
                    if self.form_action(k, v, man, argv, warn):
                        return
                    self.scr.timeout(-1)
        finally:
            self.scr.timeout(250)

    def form_changed(self, v, key):
        if key == "env" and v["env"] != "(custom image)":
            self.apply_env(v)
        if key == "kind":
            prefix = {"training": "job", "interactive": "dev", "service": "svc", "workspace": "ws"}[v["kind"]]
            if re.match(r"^(job|dev|svc|ws)-\d{4}-\d{4}$", v["name"]):
                v["name"] = f"{prefix}-{v['name'].split('-', 1)[1]}"
            if v["kind"] in ("interactive", "service", "workspace") and v["command"].startswith("echo hello"):
                v["command"] = ""
        if key == "gpu":
            v["gpu_amount"] = "0.5" if v["gpu"] == "fraction" else "1"

    def form_action(self, k, v, man, argv, warn):
        """S submit, W write, E edit+submit. -> True when the form is done."""
        proj = v["project"]
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
                        "#!/bin/sh\n# htd-runai: Run:ai workspace\n" + pretty_argv(argv, "  ") + "\n")
            if not man:
                os.chmod(path, 0o755)
            if k == "W":
                self.flash(f"wrote {path}" + ("; F submits it" if man else ""))
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
                self.flash(f"kept {path}; F submits it later")
            return True
        if k == "E":
            self.flash("E edits manifests; this kind is a CLI command (W writes it as a script)")
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
        return True

    def draw_form(self, fields, v, sel, preview, warn, pscroll):
        self.scr.erase()
        self.shadow = {}
        h, w = self.scr.getmaxyx()
        self.put(0, 0, " ▲ New workload".ljust(w), self.C["head"] | curses.A_BOLD)
        lw = max(52, w * 46 // 100)
        fh = h - 3
        self.box(1, 0, fh, lw, "Fields", True)
        for i, (key, label, typ, choices) in enumerate(fields[:fh - 2]):
            val = v[key]
            if typ == "bool":
                val = "[x] yes" if val else "[ ] no"
            elif key == "kind":
                val = dict(KINDS)[val]
            elif typ == "choice":
                val = f"‹ {val} ›"
            line = f"{label:<20} {val}"
            if i == sel:
                self.put(2 + i, 1, f" {line}".ljust(lw - 2), self.C["sel"] | curses.A_BOLD, lw - 2)
            else:
                self.put(2 + i, 2, f"{label:<20}", self.C["dim"], lw - 4)
                self.put(2 + i, 23, str(val), 0, lw - 25)
        # quota hint + warnings below the fields
        row = 2 + len(fields) + 1
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
        keys = [("↑↓", "field"), ("⏎ ←→", "change"), ("S", "submit"), ("W", "write YAML"), ("E", "edit+submit"),
                ("Esc", "cancel")]
        x = 0
        for k, label in keys:
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
            projects = [p["name"] for p in (self.sess.api("/api/v1/org-unit/projects") or {}).get("projects") or []]
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
                self.tab = 3
                self.flash("not logged in: i logs you in (or Enter on the ✖ items)")
            elif first_login_check and self.st.logged_in:
                first_login_check = False
            if not self.project and self.st.projects and cli_project():
                self.project = cli_project()
            self.draw()
            k = self.scr.getch()
            if k == -1:
                continue
            h = self.scr.getmaxyx()[0]
            if k in (ord("q"),):
                return
            if k == 27:
                continue
            if ord("1") <= k <= ord("4"):
                self.tab = k - ord("1")
            elif k == ord("\t"):
                self.tab = (self.tab + 1) % len(TABS)
            elif k == curses.KEY_BTAB:
                self.tab = (self.tab - 1) % len(TABS)
            elif k == ord("?"):
                self.pager("htd-runai help", HELP)
            elif k == ord("!"):
                self.show_log()
            elif k == ord("r"):
                self.poller.wake()
                if self.tab == 3:
                    self.recheck()
                else:
                    self.flash("refreshing ...")
            elif k == ord("p"):
                self.switch_project()
            elif k == ord("n"):
                self.do_new()
            elif k == ord("F"):
                self.do_submit_files()
            elif k == ord("i"):
                self.do_login()
            elif self.tab in (1, 2) and k in (curses.KEY_DOWN, ord("j")):
                self.sel["wl" if self.tab == 1 else "act"] += 1
            elif self.tab in (1, 2) and k in (curses.KEY_UP, ord("k")):
                key = "wl" if self.tab == 1 else "act"
                self.sel[key] = max(0, self.sel[key] - 1)
            elif self.tab == 3 and k in (curses.KEY_DOWN, ord("j")):
                self.sel["setup"] += 1
            elif self.tab == 3 and k in (curses.KEY_UP, ord("k")):
                self.sel["setup"] = max(0, self.sel["setup"] - 1)
            elif self.tab == 1 and k == curses.KEY_NPAGE:
                self.dscroll += h - 8
            elif self.tab == 1 and k == curses.KEY_PPAGE:
                self.dscroll = max(0, self.dscroll - (h - 8))
            elif self.tab == 1 and k in (10, 13, curses.KEY_ENTER, ord("s")):
                self.do_shell()
            elif self.tab == 2 and k in (10, 13, curses.KEY_ENTER):
                w = self.need_workload()
                if w:
                    ws = self.visible_workloads()
                    if w not in ws:
                        self.mine, self.filter, self.project = False, "", w.get("projectName")
                        ws = self.visible_workloads()
                    self.sel["wl"] = next((i for i, x in enumerate(ws) if x.get("id") == w.get("id")), 0)
                    self.tab = 1
            elif self.tab == 2 and k == ord("D"):
                with self.st.lock:
                    for key, info in list(self.st.watch.items()):
                        wl = self.st.find(*key)
                        if wl is None or settled(wl):
                            del self.st.watch[key]
                    if self.st.batch and self.st.batch.done:
                        self.st.batch = None
                self.sel["act"] = 0
            elif self.tab in (1, 2) and k == ord("l"):
                self.do_logs()
            elif self.tab in (1, 2) and k == ord("L"):
                self.do_logs(follow=True)
            elif self.tab in (1, 2) and k == ord("d"):
                self.do_describe()
            elif self.tab in (1, 2) and k == ord("y"):
                self.do_save()
            elif self.tab in (1, 2) and k == ord("x"):
                self.do_delete()
            elif self.tab in (1, 2) and k == ord("z"):
                self.do_suspend()
            elif self.tab in (1, 2) and k == ord("f"):
                self.do_port_forward()
            elif self.tab in (1, 2) and k == ord("u"):
                self.do_copy("url")
            elif self.tab in (1, 2) and k == ord("c"):
                self.do_copy("name")
            elif self.tab == 2 and k == ord("s"):
                self.do_shell()
            elif self.tab == 1 and k == ord("a"):
                self.mine = not self.mine
                self.sel["wl"] = self.top["wl"] = 0
            elif self.tab == 1 and k == ord("/"):
                f = self.prompt("filter (name, phase, type, image)", self.filter)
                if f is not None:
                    self.filter = f
                    self.sel["wl"] = self.top["wl"] = 0
            elif self.tab == 3 and k in (10, 13, curses.KEY_ENTER):
                if self.checks:
                    self.do_fix(self.checks[min(self.sel["setup"], len(self.checks) - 1)])
            elif self.tab == 3 and k == ord("A"):
                self.fix_all()
            elif self.tab == 3 and k == ord("o"):
                self.do_logout()


# --- command-line mode -----------------------------------------------------------------------
ANSI = {"ok": "32", "warn": "33", "info": "36", "err": "31", "dim": "2", "brand": "38;5;208", "pulse": "1;38;5;208",
        "bold": "1", None: "0", "fill": "32", "empty": "2"}


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
            sys.exit(f"not logged in: {e}\n  run `htd-runai` (Setup tab) or `runai login user -u <you>@<site>`")
    return sess, st


def cli_load(sess, st):
    Poller(sess, st).poll()
    if st.error:
        sys.exit(f"Run:ai: {st.error}")


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


def main():
    sanitize_env()
    if ENV_NOTE:
        print(f"htd-runai: {ENV_NOTE}", file=sys.stderr)
    commands = {"status", "ls", "doctor", "submit", "watch", "save", "shell"}
    if len(sys.argv) > 1 and sys.argv[1] in commands:
        p = argparse.ArgumentParser(prog="htd-runai", description=__doc__.split("\n")[0])
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
        args = p.parse_args()
        {"status": cli_status, "ls": cli_ls, "doctor": cli_doctor, "submit": cli_submit, "watch": cli_watch,
         "save": cli_save, "shell": cli_shell}[args.cmd](args)
        return
    p = argparse.ArgumentParser(prog="htd-runai", usage="%(prog)s [-p PROJECT] | {status,ls,doctor,submit,watch,save,shell} ...",
                                description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-p", "--project", help="start in this project")
    p.add_argument("--setup", action="store_true", help="start on the Setup tab")
    args = p.parse_args()
    if not sys.stdout.isatty():
        sys.exit("htd-runai: the TUI needs a terminal (try `htd-runai status`)")
    sess, st = Session(), State()
    poller = Poller(sess, st)
    poller.start()
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: Tui(scr, sess, st, poller, args.project, 3 if args.setup else 0).run())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
