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
# A start is complete only after actual runtime and every configured app probe pass.
# Test seams: PVE_NUT_STATE_DIR NODE_ONLINE GATE_RETRIES GATE_DELAY LOCK_WAIT GUEST_READY.
set -uo pipefail

STATE_DIR=${PVE_NUT_STATE_DIR:-/var/lib/pve-nut-shutdown}
PARKED="$STATE_DIR/parked"
SHED="$STATE_DIR/shed"
NODE_ONLINE=${PVE_NUT_NODE_ONLINE:-/usr/local/sbin/pve-ha-node-online.py}
GATE_RETRIES=${PVE_NUT_GATE_RETRIES:-18}
GATE_DELAY=${PVE_NUT_GATE_DELAY:-10}
LOCK_WAIT=${PVE_NUT_LOCK_WAIT:-60}
ABORT_FILE=${PVE_NUT_ABORT_FILE:-}
NODE=$(hostname)
TAG=pve-nut-restore
CONF=${PVE_NUT_CONF:-/etc/nut/pve-nut.conf}
GUEST_READY=${PVE_NUT_GUEST_READY:-/usr/local/sbin/pve-nut-guest-ready.py}
BOOT_ID=${PVE_NUT_BOOT_ID:-$(cat /proc/sys/kernel/random/boot_id)}
STATE_WRITE=${PVE_NUT_STATE_WRITER:-/usr/local/sbin/pve-nut-state.py}
UPS_SYS=""
RESTORE_HEALTH_CHECKS=""
RESTORE_METRIC_CHECKS=""
if [[ -r "$CONF" ]]; then
  # shellcheck disable=SC1090
  . "$CONF"
fi
export PVE_NUT_HEALTH_CHECKS="$RESTORE_HEALTH_CHECKS"
export PVE_NUT_METRIC_CHECKS="$RESTORE_METRIC_CHECKS"

log() { logger -t "$TAG" -- "$*"; echo "$*"; }

pending=0
for f in "$PARKED" "$SHED"; do
  if [[ -s "$f" ]]; then pending=1; fi
done
if ((!pending)); then
  log "nothing to restore in $STATE_DIR"
  exit 0
fi

gate_open() {
  local status
  # Capture, then match: a pipe into grep would SIGPIPE ha-manager under pipefail.
  status=$(timeout --kill-after=1 10 ha-manager status 2> /dev/null || true)
  [[ "$status" == *"quorum OK"* ]] || return 1
  [[ "$status" =~ (^|$'\n')"master "[^[:space:]]+" (active" ]] || return 1
  timeout --kill-after=1 10 "$NODE_ONLINE" "$NODE" 2> /dev/null
}

aborted() {
  local st
  [[ -n "$ABORT_FILE" && -e "$ABORT_FILE" ]] && return 0
  [[ -r "$STATE_DIR/final-wave" && "$(cat "$STATE_DIR/final-wave")" == "$BOOT_ID" ]] && return 0
  st=$(timeout --kill-after=1 5 upsc "$UPS_SYS" ups.status 2> /dev/null || true)
  [[ " $st " != *" OL "* || " $st " == *" OB "* || " $st " == *" FSD "* || " $st " == *" LB "* ]]
}

for ((i = 0; i < GATE_RETRIES; i++)); do
  if aborted; then
    log "shed requested or UPS/final-wave gate closed, restore abandoned; rows stay in $STATE_DIR"
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
jobs=$(timeout --kill-after=1 10 systemctl list-jobs 2> /dev/null || true)
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

safe_to_start() {
  if aborted; then log "restore ownership lost; pending rows retained"; return 1; fi
  timeout --kill-after=1 45 "$GUEST_READY" preflight "$1"
}

adopt() { # sid; one recovery from HA error per recorded outage, never an endless reset loop
  local state
  aborted && return 1
  if timeout --kill-after=1 45 "$GUEST_READY" ready "$1" --managed; then log "verified $1 running and healthy"; return 0; fi
  safe_to_start "$1" || return 1
  state=$(timeout --kill-after=1 15 "$GUEST_READY" ha-state "$1" || true)
  case "$state" in
    started | ignored | stopped | disabled | error) ;;
    *) log "$1 HA runtime state is unknown or transitional ($state); retained"; return 1 ;;
  esac
  if [[ "$state" == error ]]; then
    if [[ -e "$STATE_DIR/error-reset-$1" ]]; then
      log "ERROR: $1 remains in HA error after its one recovery; manual review required"
      return 1
    fi
    # Record intent first so an interrupted reset cannot be repeated without a bound.
    printf 'requested\n' | timeout --kill-after=1 5 "$STATE_WRITE" write "$STATE_DIR/error-reset-$1" || return 1
    aborted && return 1
    timeout --kill-after=1 10 ha-manager set "$1" --state disabled || return 1
    log "$1 HA error recovery requested; wait for confirmed disabled on the next pass"
    return 1
  fi
  if [[ -e "$STATE_DIR/error-reset-$1" && "$state" != disabled && "$state" != stopped && "$state" != started ]]; then
    log "$1 HA recovery has not settled ($state), retained"
    return 1
  fi
  aborted && return 1
  if ! timeout --kill-after=1 10 ha-manager set "$1" --state started; then log "ERROR: restore request for $1 failed"; return 1; fi
  if timeout --kill-after=1 45 "$GUEST_READY" ready "$1" --managed; then
    log "verified $1 running and healthy"
    return 0
  fi
  log "$1 start accepted but runtime/application not yet healthy; pending state retained"
  return 1
}

start_guest() { # sid
  local id=${1#*:} tool status
  case "$1" in
    vm:*) tool=qm ;;
    ct:*) tool=pct ;;
    *) log "ERROR: unknown sid $1"; return 1 ;;
  esac
  aborted && return 1
  if timeout --kill-after=1 45 "$GUEST_READY" ready "$1"; then log "verified $1 running and healthy"; return 0; fi
  safe_to_start "$1" || return 1
  status=$(timeout --kill-after=1 10 "$tool" status "$id" 2> /dev/null || true)
  if [[ "$status" != *"status: running"* ]]; then
    aborted && return 1
    timeout --kill-after=1 30 "$tool" start "$id" || return 1
  fi
  timeout --kill-after=1 45 "$GUEST_READY" ready "$1" || return 1
  log "verified $1 running and healthy"
}

failed=0

if [[ -s "$PARKED" ]]; then
  remaining=()
  while IFS= read -r sid; do
    [[ -n "$sid" ]] || continue
    adopt "$sid" || remaining+=("$sid")
  done < "$PARKED"
  # A final wave publishes ownership before waiting for our lock. It may proceed
  # after its bounded wait, so never overwrite/archive newly recorded state then.
  if aborted; then log "restore ownership lost before checkpoint; original pending file retained"; exit 1; fi
  if ((${#remaining[@]} > 0)); then
    printf '%s\n' "${remaining[@]}" | timeout --kill-after=1 5 "$STATE_WRITE" write "$PARKED" || exit 1
    log "ERROR: ${#remaining[@]} row(s) still parked in $PARKED"
    failed=1
  else
    printf '%s\n' "$STATE_DIR/restored-$(date +%Y%m%dT%H%M%S)" | timeout --kill-after=1 5 "$STATE_WRITE" move "$PARKED" || exit 1
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
  # A final wave publishes ownership before waiting for our lock. It may proceed
  # after its bounded wait, so never overwrite/archive newly recorded state then.
  if aborted; then log "restore ownership lost before checkpoint; original pending file retained"; exit 1; fi
  if ((${#remaining[@]} > 0)); then
    printf '%s\n' "${remaining[@]}" | timeout --kill-after=1 5 "$STATE_WRITE" write "$SHED" || exit 1
    log "ERROR: ${#remaining[@]} shed guest(s) still down, kept in $SHED"
    failed=1
  else
    printf '%s\n' "$STATE_DIR/restored-shed-$(date +%Y%m%dT%H%M%S)" | timeout --kill-after=1 5 "$STATE_WRITE" move "$SHED" || exit 1
    log "all shed guests restored"
  fi
fi

exit "$failed"
