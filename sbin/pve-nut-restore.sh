#!/usr/bin/env bash
# Re-adopt what pve-nut-shutdown.sh left behind: `parked` (HA sids the final wave set ignored) and
# `shed` (`<sid> ha|plain`, tier 1). Run by pve-nut-tier.sh (boot and restore).
#
# Gate before any `ha-manager set --state started`: this node quorate, a live CRM master, and
# pve-ha-node-online.py. A row restored before HA counts this node online is recovered onto
# the peer (Manager.pm next_state_started), which for a local-zfs guest is an offline disk
# migration, measured in a reboot drill. A pending shutdown leaves the rows parked.
# Re-adopting a guest pve-guests startall already started is a no-op start.
#
# Exit 1 leaves the unfinished lines in place: `systemctl start pve-nut-restore` retries once
# the gate opens. $STATE_DIR/.lock serialises the rewrite with a final wave that starts meanwhile;
# PVE_NUT_ABORT_FILE (a shed.request) aborts the gate wait.
# Test seams: PVE_NUT_STATE_DIR NODE_ONLINE GATE_RETRIES GATE_DELAY LOCK_WAIT.
set -uo pipefail

STATE_DIR=${PVE_NUT_STATE_DIR:-/var/lib/pve-nut-shutdown}
PARKED="$STATE_DIR/parked"
SHED="$STATE_DIR/shed"
NODE_ONLINE=${PVE_NUT_NODE_ONLINE:-/usr/local/sbin/pve-ha-node-online.py}
GATE_RETRIES=${PVE_NUT_GATE_RETRIES:-90}
GATE_DELAY=${PVE_NUT_GATE_DELAY:-10}
LOCK_WAIT=${PVE_NUT_LOCK_WAIT:-60}
ABORT_FILE=${PVE_NUT_ABORT_FILE:-}
NODE=$(hostname)
TAG=pve-nut-restore

log() { logger -t "$TAG" -- "$*"; echo "$*"; }

pending=0
for f in "$PARKED" "$SHED"; do
  if [[ -s "$f" ]]; then pending=1; elif [[ -e "$f" ]]; then rm -f "$f"; fi
done
if ((!pending)); then
  log "nothing to restore in $STATE_DIR"
  exit 0
fi

gate_open() {
  local status
  # Capture, then match: a pipe into grep would SIGPIPE ha-manager under pipefail.
  status=$(ha-manager status 2> /dev/null || true)
  [[ "$status" == *"quorum OK"* ]] || return 1
  [[ "$status" =~ (^|$'\n')"master "[^[:space:]]+" (active" ]] || return 1
  "$NODE_ONLINE" "$NODE" 2> /dev/null
}

aborted() { [[ -n "$ABORT_FILE" && -e "$ABORT_FILE" ]]; }

for ((i = 0; i < GATE_RETRIES; i++)); do
  if aborted; then
    log "shed requested, restore abandoned; rows stay in $STATE_DIR"
    exit 1
  fi
  gate_open && break
  sleep "$GATE_DELAY"
done
if ! gate_open; then
  log "ERROR: HA gate did not open in $((GATE_RETRIES * GATE_DELAY)) s; rows stay in $STATE_DIR"
  exit 1
fi

# No pipe into grep: an early grep -q exit would SIGPIPE systemctl and fail the guard open.
jobs=$(systemctl list-jobs 2> /dev/null || true)
case "$jobs" in
  *reboot.target* | *shutdown.target*)
    log "shutdown pending, rows stay in $STATE_DIR"
    exit 1
    ;;
esac

exec 9> "$STATE_DIR/.lock"
if ! flock -w "$LOCK_WAIT" 9; then
  log "ERROR: $STATE_DIR/.lock held for $LOCK_WAIT s (a UPS wave running?); rows stay in $STATE_DIR"
  exit 1
fi

adopt() { # sid
  if ha-manager set "$1" --state started; then log "restored $1"; return 0; fi
  log "ERROR: restore of $1 failed"
  return 1
}

start_guest() { # sid
  local id=${1#*:} tool status
  case "$1" in
    vm:*) tool=qm ;;
    ct:*) tool=pct ;;
    *) log "ERROR: unknown sid $1"; return 1 ;;
  esac
  status=$($tool status "$id" 2> /dev/null || true)
  if [[ "$status" == *"status: running"* ]]; then log "$1 already running"; return 0; fi
  if $tool start "$id"; then log "started $1"; return 0; fi
  log "ERROR: start of $1 failed"
  return 1
}

failed=0

if [[ -s "$PARKED" ]]; then
  remaining=()
  while IFS= read -r sid; do
    [[ -n "$sid" ]] || continue
    adopt "$sid" || remaining+=("$sid")
  done < "$PARKED"
  if ((${#remaining[@]} > 0)); then
    printf '%s\n' "${remaining[@]}" > "$PARKED"
    log "ERROR: ${#remaining[@]} row(s) still parked in $PARKED"
    failed=1
  else
    mv "$PARKED" "$STATE_DIR/restored-$(date +%Y%m%dT%H%M%S)"
    log "all parked rows restored"
  fi
fi

if [[ -s "$SHED" ]]; then
  remaining=()
  while read -r sid kind; do
    [[ -n "$sid" ]] || continue
    case "$kind" in
      ha) adopt "$sid" || remaining+=("$sid $kind") ;;
      *) start_guest "$sid" || remaining+=("$sid $kind") ;;
    esac
  done < "$SHED"
  if ((${#remaining[@]} > 0)); then
    printf '%s\n' "${remaining[@]}" > "$SHED"
    log "ERROR: ${#remaining[@]} shed guest(s) still down, kept in $SHED"
    failed=1
  else
    mv "$SHED" "$STATE_DIR/restored-shed-$(date +%Y%m%dT%H%M%S)"
    log "all shed guests restored"
  fi
fi

exit "$failed"
