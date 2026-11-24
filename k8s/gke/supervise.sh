#!/usr/bin/env bash
# Usage: k8s/gke/supervise.sh <corpus dir> <log dir> -- <capture command...> -- <ingest command...>
# Keeps a long capture and its follow-mode ingest running: each is restarted when it exits, with exponential
# backoff, until the corpus is sealed. Both resume where they stopped: `otb capture` from capture.json and
# events.jsonl, `otb ingest --mode follow` from its session's ledgers. The capture is not restarted once the corpus
# is sealed. The ingest is not restarted once it exits 0, which it does only after the seal, or once it has failed
# SUPERVISE_SEALED_TRIES times after the seal.
# Each command runs in its own process group, killed when its leader exits, so a straggler of a dead run cannot
# hold the gateway port or a ledger lock against the next. SIGTERM or SIGINT stops both commands and exits.
# Each command's output is appended to <log dir>/<corpus name>-capture.log and -ingest.log; one line per start and
# exit goes to stdout. Exits 0 when the corpus is sealed and the ingest finished, 1 otherwise.
set -uo pipefail
set -m

usage() { echo "usage: $0 <corpus dir> <log dir> -- <capture command...> -- <ingest command...>" >&2; exit 2; }
(($# >= 2)) || usage
CORPUS=$1 LOGS=$2
shift 2
[[ ${1:-} == -- ]] || usage
shift
CAPTURE=()
while (($#)) && [[ $1 != -- ]]; do CAPTURE+=("$1"); shift; done
[[ ${1:-} == -- ]] || usage
shift
INGEST=("$@")
((${#CAPTURE[@]} && ${#INGEST[@]})) || usage

MIN=${SUPERVISE_BACKOFF_MIN_S:-5}
MAX=${SUPERVISE_BACKOFF_MAX_S:-300}
HEALTHY=${SUPERVISE_HEALTHY_S:-600}
TRIES=${SUPERVISE_SEALED_TRIES:-3}
NAME=$(basename "$CORPUS")
mkdir -p "$LOGS"

ROLES=(capture ingest)
PID=(0 0)
STARTED=(0 0)
DELAY=("$MIN" "$MIN")
NEXT=(0 0)
FINISHED=(0 0)
SEALED_FAILS=(0 0)
LAST_RC=(1 1)

say() { echo "$(date -u +%FT%TZ) supervise: $*"; }
sealed() { grep -qx 'sealed: true' "$CORPUS/manifest.yaml" 2>/dev/null; }

launch() {
  local i=$1 log="$LOGS/$NAME-${ROLES[$1]}.log"
  if ((i == 0)); then "${CAPTURE[@]}" >>"$log" 2>&1 & else "${INGEST[@]}" >>"$log" 2>&1 & fi
  PID[i]=$!
  STARTED[i]=$SECONDS
  say "${ROLES[i]} started pid ${PID[i]}"
}

reap() {
  local i=$1 rc ran
  wait "${PID[i]}"
  rc=$?
  kill -KILL -- "-${PID[i]}" 2>/dev/null
  ran=$((SECONDS - STARTED[i]))
  PID[i]=0
  LAST_RC[i]=$rc
  if sealed; then
    if ((i == 0 || rc == 0)); then
      FINISHED[i]=1
      say "${ROLES[i]} exited $rc after ${ran} s; the corpus is sealed, not restarting"
      return
    fi
    SEALED_FAILS[i]=$((SEALED_FAILS[i] + 1))
    if ((SEALED_FAILS[i] > TRIES)); then
      FINISHED[i]=1
      say "${ROLES[i]} exited $rc after ${ran} s; failed ${SEALED_FAILS[i]} times after the seal, giving up"
      return
    fi
  fi
  ((ran >= HEALTHY)) && DELAY[i]=$MIN
  NEXT[i]=$((SECONDS + DELAY[i]))
  say "${ROLES[i]} exited $rc after ${ran} s; restarting in ${DELAY[i]} s"
  DELAY[i]=$((DELAY[i] * 2 > MAX ? MAX : DELAY[i] * 2))
}

STOP=0
trap 'STOP=1' TERM INT

launch 0
launch 1
while ((!FINISHED[0] || !FINISHED[1])); do
  if ((STOP)); then
    say "stopping"
    for i in 0 1; do ((PID[i])) && kill -TERM -- "-${PID[i]}" 2>/dev/null; done
    for i in 0 1; do ((PID[i])) && wait "${PID[i]}"; done
    exit 1
  fi
  for i in 0 1; do
    ((FINISHED[i])) && continue
    if ((PID[i])); then
      kill -0 "${PID[i]}" 2>/dev/null || reap "$i"
    elif ((SECONDS >= NEXT[i])); then
      launch "$i"
    fi
  done
  sleep 1 &
  wait $!
done
sealed && ((LAST_RC[1] == 0))
