#!/usr/bin/env bash
# Install htd-runai as /usr/local/bin/htd-runai.
#   ./install.sh                  on the login node itself (as root)
#   ./install.sh root@HOST        from a workstation: copy over ssh, then install
#                                 (HOST defaults to $HTD_RUNAI_HOST)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
src="$here/htd-runai.py"
dest=/usr/local/bin/htd-runai
target=${1:-${HTD_RUNAI_HOST:-}}

python3 -m py_compile "$src" 2>/dev/null || { echo "htd-runai.py does not compile" >&2; exit 1; }

if [[ -z "$target" ]]; then
  [[ $EUID -eq 0 ]] || { echo "run as root (or pass root@HOST)" >&2; exit 1; }
  python3 -c 'import sys, yaml; assert sys.version_info >= (3, 9)' \
    || { echo "needs python3 >= 3.9 and PyYAML (dnf install python3-pyyaml)" >&2; exit 1; }
  install -m 755 "$src" "$dest"
  echo "htd-runai: $dest"
else
  scp -q "$src" "$target:/tmp/htd-runai.py"
  ssh "$target" "install -m 755 /tmp/htd-runai.py $dest && rm -f /tmp/htd-runai.py && md5sum $dest"
  echo "local:  $(md5 -q "$src" 2>/dev/null || md5sum "$src" | cut -d' ' -f1)"
fi
