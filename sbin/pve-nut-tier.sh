#!/usr/bin/env bash
# Root half of the UPS tier path: pve-nut-tier.path runs `consume`, pve-nut-restore.service runs
# `boot`; the nut-user half is pve-nut-upssched-cmd.sh.
#   shed     pve-nut-shutdown.sh --shed; leaves the tier1 marker so the restore knows to wake.
#   restore  with the marker (primary): one round of magic packets to every WOL_TARGETS peer that does
#            not answer ping, then pve-nut-restore.sh while further rounds run beside it until every
#            peer answers. The restore's gate needs quorum, which the halted peers hold, so the rounds
#            cannot wait for it; a restore that gave up before the peers were back runs once more. A
#            peer that answers is up and gets no packet; one still powering off ignores a packet.
#   boot     the same after our own reboot (pve-nut-restore.service). After a drill (nut-reset marker)
#            it first restarts nut-server, whose FSD flag never clears otherwise, and nut-monitor, whose
#            upsmon exited after SHUTDOWNCMD, and removes a POWERDOWNFLAG the restart left behind.
#   consume  every request file present, shed before restore, repeated until none is left, so a
#            request written during a long action is never swept away.
# A request is checked against ups.status first: a UPS has been seen reporting OL for one poll
# mid-outage; a shed also stands down, writing no marker, at LB or FSD, where the final wave owns
# the guests. A shed.request arriving during a restore aborts it and keeps the marker.
# Test seams: PVE_NUT_CONF REQ_DIR STATE_DIR SHUTDOWN_CMD RESTORE_CMD WOL_ROUNDS WOL_DELAY UPSMON_CONF.
set -uo pipefail

CONF=${PVE_NUT_CONF:-/etc/nut/pve-nut.conf}
REQ_DIR=${PVE_NUT_REQ_DIR:-/run/nut/tier}
STATE_DIR=${PVE_NUT_STATE_DIR:-/var/lib/pve-nut-shutdown}
SHUTDOWN_CMD=${PVE_NUT_SHUTDOWN_CMD:-/usr/local/bin/pve-nut-shutdown.sh}
RESTORE_CMD=${PVE_NUT_RESTORE_CMD:-/usr/local/sbin/pve-nut-restore.sh}
WOL_ROUNDS=${PVE_NUT_WOL_ROUNDS:-10}
WOL_DELAY=${PVE_NUT_WOL_DELAY:-60}
MARKER="$STATE_DIR/tier1"
NUT_RESET="$STATE_DIR/nut-reset"
UPSMON_CONF=${PVE_NUT_UPSMON_CONF:-/etc/nut/upsmon.conf}
SHED_REQ="$REQ_DIR/shed.request"
TAG=pve-nut-tier
BOOT_ID=${PVE_NUT_BOOT_ID:-$(cat /proc/sys/kernel/random/boot_id)}
STATE_WRITE=${PVE_NUT_STATE_WRITER:-/usr/local/sbin/pve-nut-state.py}
RESTORE_ATTEMPTS=${PVE_NUT_RESTORE_ATTEMPTS:-10}

NUT_ROLE=client
UPS_SYS=""
WOL_IFACE=""
WOL_TARGETS=""
RESTORE_METRICS_FILE=""
if [[ -r "$CONF" ]]; then
  # shellcheck disable=SC1090
  . "$CONF"
else
  logger -t "$TAG" -- "ERROR: $CONF unreadable, no peer will be woken"
fi

log() { logger -t "$TAG" -- "$*"; echo "$*"; }

publish_restore_metrics() {
  [[ -n "$RESTORE_METRICS_FILE" ]] || return 0
  local pending=0 attempts=0 exhausted=0 temp="$RESTORE_METRICS_FILE.$$"
  [[ -s "$STATE_DIR/parked" || -s "$STATE_DIR/shed" || -e "$MARKER" ]] && pending=1
  [[ -r "$STATE_DIR/restore-attempts" ]] && read -r attempts < "$STATE_DIR/restore-attempts"
  [[ "$attempts" =~ ^[0-9]+$ ]] || attempts=$RESTORE_ATTEMPTS
  ((pending && attempts >= RESTORE_ATTEMPTS)) && exhausted=1
  {
    printf 'pve_nut_restore_pending %s\n' "$pending"
    printf 'pve_nut_restore_attempts %s\n' "$attempts"
    printf 'pve_nut_restore_exhausted %s\n' "$exhausted"
    printf 'pve_nut_restore_checked_timestamp_seconds %s\n' "$(date +%s)"
  } > "$temp" && mv "$temp" "$RESTORE_METRICS_FILE"
}
trap publish_restore_metrics EXIT

# Empty when upsd is unreachable: the timer then stands on its own.
ups_status() { [[ -n "$UPS_SYS" ]] && timeout --kill-after=1 5 upsc "$UPS_SYS" ups.status 2> /dev/null; }
restore_safe() {
  local st
  [[ -e "$SHED_REQ" ]] && return 1
  [[ -r "$STATE_DIR/final-wave" && "$(cat "$STATE_DIR/final-wave")" == "$BOOT_ID" ]] && return 1
  st=$(ups_status)
  [[ " $st " == *" OL "* && " $st " != *" OB "* && " $st " != *" FSD "* && " $st " != *" LB "* ]]
}

do_shed() {
  local st
  st=$(ups_status)
  if [[ -n "$st" && (" $st " != *" OB "* || " $st " == *" LB "* || " $st " == *" FSD "*) ]]; then
    log "shed requested but $UPS_SYS reports '$st', skipped"
    return 0
  fi
  mkdir -p "$STATE_DIR"
  : > "$MARKER"
  log "tier 1: $SHUTDOWN_CMD --shed"
  $SHUTDOWN_CMD --shed || log "ERROR: $SHUTDOWN_CMD --shed exited $?"
}

peer_up() { ping -c 1 -W 2 "$1" > /dev/null 2>&1; }
can_wake() { [[ "$NUT_ROLE" == server && -n "$WOL_TARGETS" ]]; }

# One round: a packet to every target whose address does not answer. Sets PENDING.
PENDING=""
wake_round() { # round-number
  local t mac out
  PENDING=""
  for t in $WOL_TARGETS; do
    peer_up "${t#*=}" && continue
    PENDING+="$t "
    restore_safe || return 2
    mac=${t%%=*}
    if out=$(timeout --kill-after=1 5 etherwake -i "$WOL_IFACE" "$mac" 2>&1); then
      log "wake round $1: magic packet to $mac (${t#*=}) on $WOL_IFACE"
    else
      log "ERROR: etherwake -i $WOL_IFACE $mac: $out"
    fi
  done
}

# 0 every peer answers, 1 rounds exhausted, 2 a shed request arrived. Runs in the background beside
# the restore, so it reports through its exit status only.
wake_peers() { # first-round; every round after round 1 waits WOL_DELAY first
  local round
  for ((round = $1; round <= WOL_ROUNDS; round++)); do
    ((round > 1)) && sleep "$WOL_DELAY"
    if ! restore_safe; then
      log "shed requested or UPS/final-wave gate closed, wake abandoned"
      return 2
    fi
    wake_round "$round" || return 2
    if [[ -z "$PENDING" ]]; then
      log "wake done after $round round(s): every peer answers"
      return 0
    fi
  done
  log "WARN: wake rounds exhausted, not seen back: $PENDING"
  return 1
}

# Drill only. upsd first: a peer woken below sees FSD until then. Then upsmon, which clears the
# POWERDOWNFLAG at start; if the file outlives that, remove it, or the next clean reboot of this
# node runs upsdrvctl shutdown from the nutshutdown hook and cuts the UPS output.
nut_reset() {
  local u flag
  [[ -e "$NUT_RESET" ]] || return 0
  for u in nut-server nut-monitor; do
    if systemctl restart "$u"; then
      log "drill: $u restarted"
    else
      log "ERROR: systemctl restart $u failed; FSD may still be set, restart it by hand"
    fi
  done
  flag=$(sed -n 's/^POWERDOWNFLAG[[:space:]]*//p' "$UPSMON_CONF" 2> /dev/null | tr -d '"')
  if [[ -n "$flag" && -e "$flag" ]]; then
    rm -f "$flag"
    log "WARN: $flag outlived the upsmon restart, removed"
  fi
  rm -f "$NUT_RESET" "$STATE_DIR/final-wave"
}

run_restore() {
  log "restore ($1): $RESTORE_CMD"
  PVE_NUT_ABORT_FILE="$SHED_REQ" $RESTORE_CMD
}

do_restore() { # timer|boot
  local wake=0 rc wake_pid="" wake_rc=0 attempt=0
  [[ -s "$STATE_DIR/parked" || -s "$STATE_DIR/shed" || -e "$MARKER" ]] || return 0
  mkdir -p "$STATE_DIR"
  # The request path and retry timer may fire together. Never duplicate wake/start attempts.
  exec 8> "$STATE_DIR/.restore-lock"
  flock -n 8 || { log "another restore is active"; return 1; }
  nut_reset
  if ! restore_safe; then
    log "restore gate closed (UPS must be OL without OB/LB/FSD and no final wave); pending state kept"
    return 1
  fi
  [[ -r "$STATE_DIR/restore-attempts" ]] && read -r attempt < "$STATE_DIR/restore-attempts"
  [[ "$attempt" =~ ^[0-9]+$ ]] || { log "ERROR: invalid restore-attempts; manual review required"; return 1; }
  if ((attempt >= RESTORE_ATTEMPTS)); then
    log "ERROR: restore attempt limit $RESTORE_ATTEMPTS reached; pending state kept for manual review"
    return 1
  fi
  printf '%s\n' "$((attempt + 1))" | timeout --kill-after=1 5 "$STATE_WRITE" write "$STATE_DIR/restore-attempts" || { log "ERROR: cannot persist retry intent"; return 1; }
  [[ -e "$MARKER" ]] && can_wake && wake=1
  if ((wake)); then
    wake_round 1 || return 1
    if [[ -z "$PENDING" ]]; then
      log "every peer answers"
    else
      wake_peers 2 &
      wake_pid=$!
    fi
  fi
  run_restore "$1"
  rc=$?
  if [[ -n "$wake_pid" ]]; then
    wait "$wake_pid"
    wake_rc=$?
    if ((rc != 0 && wake_rc == 0)) && restore_safe; then
      log "the peers are back, restore once more"
      run_restore "$1"
      rc=$?
    fi
  fi
  if ((rc != 0 || wake_rc != 0)) || ! restore_safe; then
    log "WARN: restore incomplete (guest=$rc wake=$wake_rc); retry timer retains pending state"
    return 1
  fi
  rm -f "$MARKER" "$STATE_DIR/restore-attempts" "$STATE_DIR"/error-reset-*
  log "restore verified; pending wake and retry state cleared"
}

consume() {
  local f name found=0 more=1
  shopt -s nullglob
  while ((more)); do
    more=0
    for name in shed restore; do
      f="$REQ_DIR/$name.request"
      [[ -e "$f" ]] || continue
      rm -f "$f"
      found=1
      more=1
      log "request: $name"
      "do_$name" timer
    done
  done
  for f in "$REQ_DIR"/*.request; do
    rm -f "$f"
    log "ERROR: unknown request $(basename "$f") removed"
    found=1
  done
  ((found)) || log "no request in $REQ_DIR"
}

case "${1:-}" in
  consume) consume ;;
  shed) do_shed ;;
  restore) do_restore timer ;;
  boot) do_restore boot ;;
  *)
    echo "usage: $0 consume|shed|restore|boot" >&2
    exit 2
    ;;
esac
