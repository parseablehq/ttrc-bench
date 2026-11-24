#!/bin/sh
# The runner pod's install init container (deploy/k8s/runner/statefulset.yaml): puts everything the three
# containers run onto the PVC at /work, from public, pinned sources, and converges on reruns. Runs as uid 10001 on
# a read-only root filesystem with /work and /tmp writable. The pins (versions, sha256 per architecture, expected
# python versions) come from the otb-runner-install ConfigMap, generated from pins.yaml.
#
#   /work/otb.bundle       the otb checkout as a git bundle (kubectl cp it in; this script waits for it)
#   /work/app              the checkout at the bundle's HEAD, with its uv venv (.venv) and the .otb home
#   /work/tools/bin        uv, kubectl, helm, claude
#   /work/tools/python     the uv-managed interpreters every venv links to
#   /work/tools/dl         the verified archives, kept so a restart re-verifies instead of re-downloading
#   /work/tools/versions.json  what was installed, read by otb run (agent.cli_version) and the tests
set -eu

TOOLS=/work/tools
BIN=$TOOLS/bin
DL=$TOOLS/dl
APP=/work/app
BUNDLE=/work/otb.bundle
mkdir -p "$BIN" "$DL" "$TOOLS/python" "$TOOLS/uv-cache" /work/home /work/runs /work/corpora

case "$(uname -m)" in
  aarch64) ARCH=arm64 UV_ARCH=aarch64 CLAUDE_ARCH=arm64 ;;
  x86_64) ARCH=amd64 UV_ARCH=x86_64 CLAUDE_ARCH=x64 ;;
  *) echo "install: no pins for $(uname -m)" >&2; exit 1 ;;
esac

# fetch <url> <sha256> <file>: download unless the file is already there with that sha256; refuse any other.
fetch() {
  if [ -f "$3" ] && echo "$2  $3" | sha256sum -c --quiet 2>/dev/null; then return 0; fi
  rm -f "$3" "$3.part"
  curl -fsSL --retry 5 --retry-all-errors -o "$3.part" "$1"
  if ! echo "$2  $3.part" | sha256sum -c --quiet; then
    echo "install: $1: sha256 differs from the pin $2" >&2
    rm -f "$3.part"
    exit 1
  fi
  mv "$3.part" "$3"
}

# place <archive> <sha256> <member|-> <name>: the binary from a verified archive (or the raw file) into $BIN,
# skipped when the stamp says it came from the same archive.
place() {
  stamp="$BIN/$4.sha256"
  if [ -x "$BIN/$4" ] && [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$2" ]; then return 0; fi
  rm -rf /tmp/place && mkdir -p /tmp/place
  if [ "$3" = "-" ]; then cp "$1" "/tmp/place/$4"; else tar -xzf "$1" -C /tmp/place "$3" && mv "/tmp/place/$3" "/tmp/place/$4"; fi
  chmod 0755 "/tmp/place/$4"
  mv "/tmp/place/$4" "$BIN/$4"
  echo "$2" > "$stamp"
}

pin() { eval "echo \"\$${1}_SHA256_$(echo "$ARCH" | tr a-z A-Z)\""; }

fetch "$(echo "$UV_URL" | sed "s/{version}/$UV_VERSION/g; s/{uv_arch}/$UV_ARCH/g")" "$(pin UV)" "$DL/uv-$UV_VERSION-$ARCH.tar.gz"
place "$DL/uv-$UV_VERSION-$ARCH.tar.gz" "$(pin UV)" "uv-$UV_ARCH-unknown-linux-gnu/uv" uv
fetch "$(echo "$KUBECTL_URL" | sed "s/{version}/$KUBECTL_VERSION/g; s/{arch}/$ARCH/g")" "$(pin KUBECTL)" "$DL/kubectl-$KUBECTL_VERSION-$ARCH"
place "$DL/kubectl-$KUBECTL_VERSION-$ARCH" "$(pin KUBECTL)" - kubectl
fetch "$(echo "$HELM_URL" | sed "s/{version}/$HELM_VERSION/g; s/{arch}/$ARCH/g")" "$(pin HELM)" "$DL/helm-$HELM_VERSION-$ARCH.tar.gz"
place "$DL/helm-$HELM_VERSION-$ARCH.tar.gz" "$(pin HELM)" "linux-$ARCH/helm" helm
fetch "$(echo "$CLAUDE_URL" | sed "s/{version}/$CLAUDE_VERSION/g; s/{claude_arch}/$CLAUDE_ARCH/g")" "$(pin CLAUDE)" "$DL/claude-$CLAUDE_VERSION-$ARCH.tgz"
place "$DL/claude-$CLAUDE_VERSION-$ARCH.tgz" "$(pin CLAUDE)" package/claude claude

export PATH="$BIN:$PATH" HOME=/work/home TMPDIR=/tmp
export UV_PYTHON_INSTALL_DIR="$TOOLS/python" UV_CACHE_DIR="$TOOLS/uv-cache" UV_LINK_MODE=copy
# only the interpreters on the PVC: the image's python is not in tools or agent, so no venv may link to it
export UV_PYTHON_PREFERENCE=only-managed
export DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

got="$(claude --version | sed 's/ .*//')"
if [ "$got" != "$CLAUDE_VERSION" ]; then echo "install: claude reports $got, pinned $CLAUDE_VERSION" >&2; exit 1; fi

# uv checks each interpreter's sha256 against its own manifest; the pin here keeps uv's choice visible.
uv python install --quiet 3.12 3.13
version_of() { "$(uv python find "$1")" -c 'import platform; print(platform.python_version())'; }
for want in $PYTHON_VERSIONS; do
  minor="${want%.*}"
  have="$(version_of "$minor")"
  if [ "$have" != "$want" ]; then echo "install: uv resolved python $minor to $have, pinned $want" >&2; exit 1; fi
done

# kubectl cp creates the file before it has written it, so the bundle arrives as $BUNDLE.part and is renamed;
# a bundle that does not clone (an older copy cut short) or names no HEAD to check out is dropped and waited for
# again, before anything in $APP is touched
while :; do
  while [ ! -f "$BUNDLE" ]; do
    echo "install: waiting for $BUNDLE (kubectl cp <bundle> otb-runner-0:$BUNDLE.part -c install, then mv it)"
    sleep 10
  done
  rm -rf /tmp/bundle-check
  want=""
  if git clone --quiet --bare "$BUNDLE" /tmp/bundle-check; then
    want="$(git bundle list-heads "$BUNDLE" HEAD | cut -d' ' -f1)"
  fi
  [ -n "$want" ] && break
  echo "install: $BUNDLE does not clone or has no HEAD (git bundle create <file> HEAD <branch>), removing it" >&2
  rm -f "$BUNDLE"
done
rm -rf /tmp/bundle-check
if [ -d "$APP/.git" ] && [ "$(git -C "$APP" rev-parse HEAD 2>/dev/null)" = "$want" ]; then
  echo "install: $APP is at $want"
else
  # what otb keeps inside its checkout outlives the checkout: the charts, secrets and server environments
  # (.otb), the ingest sessions (state), the suites, and the venv (uv sync then only verifies it)
  # /work/keep is the only copy while the checkout is replaced: an install cut short in between left it there,
  # and the rerun restores it rather than starting it over
  mkdir -p /work/keep
  for d in .otb state suites .venv; do
    if [ -e "$APP/$d" ] && [ ! -e "/work/keep/$d" ]; then mv "$APP/$d" "/work/keep/$d"; fi
  done
  kept="$(ls -A /work/keep | tr '\n' ' ')"
  rm -rf "$APP"
  git clone --quiet "$BUNDLE" "$APP"
  git -C "$APP" checkout --quiet --detach "$want"
  for d in .otb state suites .venv; do
    if [ -e "/work/keep/$d" ]; then rm -rf "${APP:?}/$d" && mv "/work/keep/$d" "$APP/$d"; fi
  done
  echo "install: $APP re-cloned at $want, kept: $kept"
fi
cd "$APP"
uv sync --frozen --all-groups --python 3.12
mkdir -p .otb/servers .otb/bin .otb/venvs .otb/node state suites
if [ -n "$(git status --porcelain)" ]; then echo "install: the checkout is dirty after sync:" >&2; git status --porcelain >&2; exit 1; fi

python_versions="$(version_of 3.12),$(version_of 3.13)"
cat > "$TOOLS/versions.json" <<JSON
{"arch": "$ARCH", "uv": "$(uv --version | sed 's/uv //; s/ .*//')", "kubectl": "$KUBECTL_VERSION", "helm": "$(helm version --template '{{.Version}}')",
 "claude": "$got", "python": "$python_versions", "otb_commit": "$want"}
JSON
cat "$TOOLS/versions.json"
echo "install: done"
