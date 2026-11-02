#!/usr/bin/env bash
# Install runai-tui as /usr/local/bin/runai-tui.
#   ./install.sh                  on the login node itself (as root)
#   ./install.sh root@HOST        from a workstation: copy over ssh, then install
#                                 (HOST defaults to $RUNAI_TUI_HOST)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
src="$here/runai-tui.py"
dest=/usr/local/bin/runai-tui
target=${1:-${RUNAI_TUI_HOST:-}}

python3 -m py_compile "$src" 2>/dev/null || { echo "runai-tui.py does not compile" >&2; exit 1; }

if [[ -z "$target" ]]; then
  [[ $EUID -eq 0 ]] || { echo "run as root (or pass root@HOST)" >&2; exit 1; }
  python3 -c 'import sys, yaml; assert sys.version_info >= (3, 9)' \
    || { echo "needs python3 >= 3.9 and PyYAML (dnf install python3-pyyaml)" >&2; exit 1; }
  install -m 755 "$src" "$dest"
  echo "runai-tui: $dest"
else
  scp -q "$src" "$target:/tmp/runai-tui.py"
  ssh "$target" "install -m 755 /tmp/runai-tui.py $dest && rm -f /tmp/runai-tui.py && md5sum $dest"
  echo "local:  $(md5 -q "$src" 2>/dev/null || md5sum "$src" | cut -d' ' -f1)"
fi
