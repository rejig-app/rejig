#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
runtime_dir="$HOME/Library/Application Support/Rejig"
app="$project_dir/build/Rejig.app"
if [[ ! -x /opt/homebrew/bin/python3.13 ]]; then
    echo 'Python 3.13 is required: brew install python@3.13' >&2
    exit 1
fi
mkdir -p "$runtime_dir"
chmod 700 "$runtime_dir"
if [[ ! -x "$runtime_dir/.venv/bin/python3" ]]; then
    /opt/homebrew/bin/python3.13 -m venv "$runtime_dir/.venv"
fi
python="$runtime_dir/.venv/bin/python3"
# Upstream's tested dependency set, hash-checked; then the engine itself at its pinned commit.
"$python" -m pip install --require-hashes -r "$project_dir/requirements.lock"
# --force-reinstall: start from the unpatched engine every run, so an update whose patches
# changed applies cleanly instead of meeting the previous run's patched files.
"$python" -m pip install --no-deps --force-reinstall -r "$project_dir/requirements.txt"
engine_dir="$("$python" -c 'import pathlib, ios_homescreen; print(pathlib.Path(ios_homescreen.__file__).parent.parent)')"
for engine_patch in "$project_dir"/patches/*.patch; do
    # Patches sharing a file apply at an offset; keep no .orig copy in site-packages.
    /usr/bin/patch --batch --forward --no-backup-if-mismatch -p1 -d "$engine_dir" < "$engine_patch"
done
# Always rebuild, so an update never installs an app built from older source.
bash "$project_dir/scripts/build.sh"
mkdir -p "$HOME/Applications"
rm -rf "$HOME/Applications/Rejig.app"
ditto "$app" "$HOME/Applications/Rejig.app"
codesign --verify --strict "$HOME/Applications/Rejig.app"
echo "Installed: $HOME/Applications/Rejig.app"
