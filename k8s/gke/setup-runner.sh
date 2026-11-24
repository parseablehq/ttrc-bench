#!/usr/bin/env bash
# Installs the runner's pinned tools on the work volume. Run inside the runner container; safe to rerun.
set -euo pipefail
NODE_VERSION=26.9.0
UV_VERSION=0.11.25
CLAUDE_VERSION=2.1.295
T=/work/tools/bin
mkdir -p "$T" "$HOME" "$DOCKER_CONFIG/cli-plugins"

if ! dpkg -s git curl xz-utils libatomic1 jq >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends ca-certificates curl git xz-utils procps jq libatomic1 >/dev/null
fi

cp /tools/docker "$T/docker"
cp /tools/cli-plugins/* "$DOCKER_CONFIG/cli-plugins/"

if [[ "$(node --version 2>/dev/null)" != "v$NODE_VERSION" ]]; then
  N=/work/tools/node-v$NODE_VERSION
  mkdir -p "$N"
  curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz" | tar -xJ -C "$N" --strip-components=1
  ln -sf "$N/bin/node" "$N/bin/npm" "$N/bin/npx" "$T/"
fi

if [[ "$(uv --version 2>/dev/null | awk '{print $2}')" != "$UV_VERSION" ]]; then
  curl -fsSL "https://astral.sh/uv/$UV_VERSION/install.sh" | env UV_INSTALL_DIR="$T" UV_NO_MODIFY_PATH=1 sh
fi

if [[ "$(claude --version 2>/dev/null | awk '{print $1}')" != "$CLAUDE_VERSION" ]]; then
  curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_VERSION"
fi

docker version --format 'docker server {{.Server.Version}}'
docker compose version
echo "node $(node --version); $(uv --version); claude $(claude --version)"
