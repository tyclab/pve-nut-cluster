#!/usr/bin/env bash
# The UPS actor on every Proxmox VE node: upsmon's SHUTDOWNCMD, and tier 1 through pve-nut-tier.sh.
# Settings come from /etc/nut/pve-nut.conf (examples/pve-nut.conf).
#
#   default  upsmon SHUTDOWNCMD, the final wave at LB:
#     1. park this node's HA rows (state started or error) as `ignored` and record them. Without
#        the park, qm/pct shutdown forwards to `ha-manager crm-command stop`, which persists request
#        state `stopped`; nothing restores that on power return and pve-guests startall skips
#        HA-managed guests. Ignored rows behave as unmanaged, so the stops below reach qm/pct
#        directly. Not a drain exemption: pve-nut-restore.service re-adopts the rows on boot.
#     2. stop every other guest at once, 3. force what is left, 4. stop the EDGE_VMIDS last (a
#        router VM routes for everything else), 4b. primary only: wait for the NAS (Synology DSM
#        enters Standby at FSD) to close NAS_PORTS and for PEER_HOSTS to stop answering ping, since
#        this node's halt runs upsdrvctl shutdown and cuts UPS output at the driver/firmware delay.
#         upsmon's own HOSTSYNC does not cover them: a secondary upsmon logs off the moment it
#        starts its shutdown command (SHUTDOWNEXIT), long before its guests are down. 5. halt.
#     1b. between park and stop: pve-ha-lrm and pve-ha-crm are stopped, which closes the HA watchdog
#        cleanly. A node that loses quorum mid-wave with an active LRM is fenced 60 s later; a drill
#        without this step saw the primary reset at +97 s, before step 4b, the halt and the killpower.
#   drill    `touch $STATE_DIR/drill` before `upsmon -c fsd` on the primary: its wave runs steps 1-4b,
#            keeps the edge VMs and the node up (no halt, no killpower), records its plain guests, then
#            restarts HA and starts pve-nut-restore.service, which resets NUT, wakes the halted peers and
#            re-adopts everything. Proves all of it except the killpower and the BIOS power-on after AC
#            loss. The NUT reset is the restore's job because this script runs inside nut-monitor's
#            cgroup: upsd keeps FSD latched (server/netmisc.c sets it, nothing clears it) so a booting
#            peer's upsmon would shut it down again, and upsmon exits after SHUTDOWNCMD (its unit is
#            Restart=on-failure), leaving the primary unmonitored and POWERDOWNFLAG on disk.
#   --shed   tier 1 (pve-nut-tier.sh, upssched ONBATT timer): steps 1-3 on the SHED_VMIDS this
#            node hosts, recorded as `<sid> ha|plain` for the restore; SHED_HALT_NODE=1 then runs
#            the final wave. An enumeration failure sheds nothing (exit 1).
#
# Budget. Explicit command limits include both serial edge VMs, discovery, lock wait,
# polling and command kill grace. With two configured edges and KILLPOWER_WAIT=180:
#   lock30 + park30 + HA30 + graceful/discovery102 + force/discovery32 + edge110
#   + summary10 + kill-grace allowance20 + NAS/peer182 =552s primary (370s secondary), including6s durable ownership publication.
# Add pre-script FINALDELAY5 + primary HOSTSYNC15, HOST_TEARDOWN_RESERVE120 (includes
# an observed96s,90s of it an NFS unmount), UPS_OUTPUT_RESERVE120 on the primary,
# and BUDGET_MARGIN120 =>1032s primary. The default600s runtime-low is below this plan; size it to measured reserve.
# These host/UPS values are planning reserves, not enforced hardware bounds. An
# ageing battery can collapse before any runtime estimate; measure them under load.
# Blocked kernel I/O cannot be made safe by a shell timeout. Do not globally use
# soft NFS or force-unmount active storage to make a paper budget look shorter.
#
# --dry-run prints the plan and changes nothing (combine with --shed).
# Test seams: PVE_NUT_STATE_DIR LOG HALT_CMD CONF NAS_POLL SYSTEMCTL.
set -uo pipefail

DRY=0
SHED=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --shed) SHED=1 ;;
    *)
      echo "usage: $0 [--dry-run] [--shed]" >&2
      exit 2
      ;;
  esac
done

LOG=${PVE_NUT_LOG:-/var/log/nut-shutdown.log}
((DRY)) && LOG=/dev/stderr
STATE_DIR=${PVE_NUT_STATE_DIR:-/var/lib/pve-nut-shutdown}
PARKED="$STATE_DIR/parked"
SHED_FILE="$STATE_DIR/shed"
HALT_CMD=${PVE_NUT_HALT_CMD:-/sbin/shutdown -h +0}
SYSTEMCTL=${PVE_NUT_SYSTEMCTL:-systemctl}
DRILL_MARK="$STATE_DIR/drill"
WAKE_MARK="$STATE_DIR/tier1"
NUT_RESET_MARK="$STATE_DIR/nut-reset"
DRILL=0
CONF=${PVE_NUT_CONF:-/etc/nut/pve-nut.conf}
BOOT_ID=${PVE_NUT_BOOT_ID:-$(cat /proc/sys/kernel/random/boot_id)}
STATE_WRITE=${PVE_NUT_STATE_WRITER:-/usr/local/sbin/pve-nut-state.py}
POLL=${PVE_NUT_NAS_POLL:-5}
NODE=$(hostname)

# pve-nut.conf keys and the defaults a missing file falls back to.
NUT_ROLE=client
EDGE_VMIDS=""
SHED_VMIDS=""
SHED_HALT_NODE=0
NAS_HOST=""
NAS_PORTS=""
PEER_HOSTS=""
KILLPOWER_WAIT=180
BUDGET_LB=600
HOST_TEARDOWN_RESERVE=120
UPS_OUTPUT_RESERVE=120
BUDGET_MARGIN=120
CONF_OK=1
if [[ -r "$CONF" ]]; then
  # shellcheck disable=SC1090
  . "$CONF"
else
  CONF_OK=0
fi

# qm/pct list are node-local, so each node holds back only the edge VMs it hosts.
EDGE_VMIDS=" $EDGE_VMIDS "

PARK_TIMEOUT=5
PARK_BOUND=30
HA_STOP_BOUND=30
VM_TIMEOUT=90
CT_TIMEOUT=60
STOP_BOUND=90
FORCE_BOUND=20
EDGE_TIMEOUT=45
LOCK_WAIT=30
BUDGET_FINALDELAY=5
BUDGET_HOSTSYNC=15
QUERY_TIMEOUT=5
FORCE_TIMEOUT=10
# Include discovery, polling quantisation and every configured edge, even when HA
# has temporarily placed both on this node. Host/UPS reserves are assumptions to
# measure in an attended acceptance test; they are not firmware guarantees.
EDGE_COUNT=0
for _edge in $EDGE_VMIDS; do EDGE_COUNT=$((EDGE_COUNT + 1)); done
COMMON_STOP_BOUND=$((PARK_BOUND + 4 * QUERY_TIMEOUT + STOP_BOUND + FORCE_BOUND + 4))
# Tier selection adds one discovery pair before the common stop path; it has its
# own lock and command/publication allowances, not the primary NAS/edge tail.
SHED_BOUND=$((LOCK_WAIT + COMMON_STOP_BOUND + 2 * QUERY_TIMEOUT + 20 + 6))
TOTAL_BOUND=$((LOCK_WAIT + COMMON_STOP_BOUND + HA_STOP_BOUND + EDGE_COUNT * (EDGE_TIMEOUT + FORCE_TIMEOUT) + 2 * QUERY_TIMEOUT + 20 + 6))
WAIT_BOUND=0
[[ "$NUT_ROLE" == server && -n "$NAS_HOST$PEER_HOSTS" ]] && WAIT_BOUND=$((KILLPOWER_WAIT + 2))

log() {
  if ((DRY)); then
    echo "$*"
  else
    echo "$(date -Is) +${SECONDS}s $*" >> "$LOG"
    logger -t pve-nut-shutdown -- "+${SECONDS}s $*"
  fi
}

# An enumeration failure must never read as "no guests left": every listing is
# status-checked, and a broken qm/pct degrades the summary to UNKNOWN.
ENUM_FAILED=0
running_vms() { timeout --kill-after=1 "$QUERY_TIMEOUT" qm list 2>> "$LOG" | awk 'NR>1 && $(NF-3)=="running" {print $1}'; }
running_cts() { timeout --kill-after=1 "$QUERY_TIMEOUT" pct list 2>> "$LOG" | awk 'NR>1 && $2=="running" {print $1}'; }
local_sids() {
  local vms cts
  vms=$(timeout --kill-after=1 "$QUERY_TIMEOUT" qm list 2>> "$LOG") || return 1
  cts=$(timeout --kill-after=1 "$QUERY_TIMEOUT" pct list 2>> "$LOG") || return 1
  {
    awk 'NR>1 {print "vm:"$1}' <<< "$vms"
    awk 'NR>1 {print "ct:"$1}' <<< "$cts"
  } | tr '\n' ' '
}

ha_rows() { # "sid state" per row
  timeout --kill-after=1 "$QUERY_TIMEOUT" pvesh get /cluster/ha/resources --output-format json 2>> "$LOG" | python3 -c '
import json, sys
for r in json.load(sys.stdin):
    print(r["sid"], r.get("state", ""))'
}
count_words() { local n=0; for _ in $1; do n=$((n + 1)); done; echo "$n"; }
is_edge() { [[ "$EDGE_VMIDS" == *" $1 "* ]]; }
# Tier 1 touches exactly the guests shed_select recorded; the final wave takes every guest.
in_scope() { # vm|ct id
  if ((SHED)); then [[ " $SHED_IDS " == *" $1:$2 "* ]]; else return 0; fi
}

# ─── step 1: park ────────────────────────────────────────────────────────
park_row() {
  if ((DRY)); then echo "    ha-manager set $1 --state ignored"; return 0; fi
  if timeout --kill-after=1 "$PARK_TIMEOUT" ha-manager set "$1" --state ignored 2>> "$LOG"; then
    log "parked $1"
    return 0
  fi
  log "ERROR: park of $1 failed; its stop goes through crm-command and leaves request state stopped"
  return 1
}

park_ha_rows() {
  local rows local_ids sid state parked=0 deadline=$((SECONDS + PARK_BOUND)) remaining intents=""
  if ! rows=$(ha_rows); then
    log "ERROR: HA resources could not be read, nothing parked; HA recovery requires manual review"
    return
  fi
  if ! local_ids=$(local_sids); then
    ENUM_FAILED=1
    log "ERROR: local guest enumeration failed before HA parking"
    return
  fi
  local_ids=" $local_ids"
  while read -r sid state; do
    [[ -n "$sid" && "$local_ids" == *" $sid "* ]] || continue
    case "$state" in started | error) intents+="$sid"$'\n' ;; esac
  done <<< "$rows"
  [[ -n "$intents" ]] || return 0
  # Journal ALL intended rows before any request, even those the park deadline
  # later skips. qm shutdown can persist stopped for an unparked HA guest.
  if ((!DRY)) && ! printf '%s' "$intents" | timeout --kill-after=1 5 "$STATE_WRITE" append "$PARKED"; then
    log "ERROR: cannot persist recovery intents; HA rows left untouched, shutdown continues"
    return
  fi
  while read -r sid; do
    [[ -n "$sid" ]] || continue
    remaining=$((deadline - SECONDS))
    if ((!DRY && remaining <= 0)); then log "ERROR: HA park deadline reached; remaining intents retained"; break; fi
    ((remaining < PARK_TIMEOUT && remaining > 0)) && PARK_TIMEOUT=$remaining
    if park_row "$sid" && ((!DRY)); then parked=$((parked + 1)); fi
  done <<< "$intents"
  ((DRY)) || log "step 1 done: $parked row(s) parked; all recovery intents retained in $PARKED"
}

# ─── step 1b: release the HA watchdog ────────────────────────────────────
ha_release() {
  if ((DRY)); then echo "    $SYSTEMCTL stop pve-ha-lrm pve-ha-crm"; return 0; fi
  if timeout --kill-after=1 "$HA_STOP_BOUND" "$SYSTEMCTL" stop pve-ha-lrm pve-ha-crm 2>> "$LOG"; then
    log "step 1b done: pve-ha-lrm and pve-ha-crm stopped, HA watchdog released"
  else
    log "WARN: stopping pve-ha-lrm/pve-ha-crm failed or exceeded ${HA_STOP_BOUND} s; a quorum loss now fences this node"
  fi
}

# Drill only: the plain guests the wave stops have no HA row to re-adopt, so record them for the restore.
drill_record_plain() {
  local vmids ctids id rows sid n=0
  rows=$(ha_rows 2> /dev/null || true)
  if vmids=$(running_vms) && ctids=$(running_cts); then
    for id in $vmids; do
      is_edge "$id" && continue
      sid="vm:$id"
      [[ "$rows" == *"$sid "* ]] && continue
      if ((DRY)); then echo "    record $sid plain"; else echo "$sid plain" >> "$SHED_FILE"; fi
      n=$((n + 1))
    done
    for id in $ctids; do
      sid="ct:$id"
      [[ "$rows" == *"$sid "* ]] && continue
      if ((DRY)); then echo "    record $sid plain"; else echo "$sid plain" >> "$SHED_FILE"; fi
      n=$((n + 1))
    done
  else
    ENUM_FAILED=1
    log "ERROR: guest enumeration failed, plain guests not recorded for the drill restore"
  fi
  ((DRY)) || log "drill: $n plain guest(s) recorded in $SHED_FILE"
}

drill_finish() {
  log "drill done: killpower skipped, edge kept; restarting HA; the restore resets NUT, wakes the peers, re-adopts"
  : > "$WAKE_MARK"
  : > "$NUT_RESET_MARK"
  "$SYSTEMCTL" start pve-ha-crm pve-ha-lrm 2>> "$LOG" || log "ERROR: pve-ha-crm/pve-ha-lrm did not start; start them by hand"
  "$SYSTEMCTL" start --no-block pve-nut-restore.service 2>> "$LOG" || log "ERROR: pve-nut-restore.service did not start; run pve-nut-tier.sh boot"
}

# ─── tier 1: select and record ───────────────────────────────────────────
SHED_IDS=""
shed_select() {
  local vmids ctids id
  if vmids=$(running_vms) && ctids=$(running_cts); then
    for id in $vmids; do [[ " $SHED_VMIDS " == *" $id "* ]] && ! is_edge "$id" && SHED_IDS+="vm:$id "; done
    for id in $ctids; do [[ " $SHED_VMIDS " == *" $id "* ]] && SHED_IDS+="ct:$id "; done
  else
    ENUM_FAILED=1
    log "ERROR: guest enumeration failed, nothing shed"
  fi
}

# `ha` rows were parked and come back with --state started; `plain` ones with qm/pct start
# (which forwards to HA itself when a row exists).
shed_record() {
  local rows sid state kind intents="" deadline=$((SECONDS + PARK_BOUND)) remaining
  if ! rows=$(ha_rows); then
    rows=""
    log "ERROR: HA resources could not be read; shed HA rows stop through crm-command and are recorded plain"
  fi
  for sid in $SHED_IDS; do
    kind=plain
    state=$(awk -v s="$sid" '$1==s {print $2}' <<< "$rows")
    case "$state" in started | error) kind=ha ;; esac
    intents+="$sid $kind"$'\n'
    ((DRY)) && echo "    record $sid $kind"
  done
  [[ -n "$intents" ]] || return 0
  if ((!DRY)) && ! printf '%s' "$intents" | timeout --kill-after=1 5 "$STATE_WRITE" append "$SHED_FILE"; then
    log "ERROR: cannot persist recovery intents; aborting tier shed"
    exit 1
  fi
  while read -r sid kind; do
    [[ "$kind" == ha ]] || continue
    remaining=$((deadline - SECONDS))
    if ((!DRY && remaining <= 0)); then log "ERROR: tier park deadline reached; remaining intents retained"; break; fi
    ((remaining < PARK_TIMEOUT && remaining > 0)) && PARK_TIMEOUT=$remaining
    park_row "$sid" || true
  done <<< "$intents"
  ((DRY)) || log "shed: $(count_words "$SHED_IDS") guest(s) recorded in $SHED_FILE"
}

# ─── steps 2-4: stop ─────────────────────────────────────────────────────
# Each stop is one background job; wait_jobs bounds the wall clock, not the sum. An abandoned
# job leaves its qm/pct task running under its own worker; the force sweep and pve-guests
# stopall at halt are the backstops. A force stop overrules a shutdown task still holding the lock.
JOBS=()

stop_vm() {
  if ((DRY)); then echo "    qm shutdown $1 --forceStop 1 --timeout $2"; return 0; fi
  log "stopping VM $1 (timeout $2 s)"
  if timeout --kill-after=1 "$2" qm shutdown "$1" --forceStop 1 --timeout "$2" 2>> "$LOG"; then log "VM $1 stopped"; return 0; fi
  log "WARN: qm shutdown $1 failed, force-stopping"
  if timeout --kill-after=1 "$FORCE_TIMEOUT" qm stop "$1" --overrule-shutdown 1 2>> "$LOG"; then log "VM $1 force-stopped"; return 0; fi
  log "ERROR: qm stop $1 failed"
  return 1
}

stop_ct() {
  if ((DRY)); then echo "    pct shutdown $1 --forceStop 1 --timeout $2"; return 0; fi
  log "stopping CT $1 (timeout $2 s)"
  if timeout --kill-after=1 "$2" pct shutdown "$1" --forceStop 1 --timeout "$2" 2>> "$LOG"; then log "CT $1 stopped"; return 0; fi
  log "WARN: pct shutdown $1 failed, force-stopping"
  if timeout --kill-after=1 "$FORCE_TIMEOUT" pct stop "$1" --overrule-shutdown 1 2>> "$LOG"; then log "CT $1 force-stopped"; return 0; fi
  log "ERROR: pct stop $1 failed"
  return 1
}

force_vm() {
  if ((DRY)); then echo "    qm stop $1 --overrule-shutdown 1"; return 0; fi
  log "force-stopping VM $1"
  timeout --kill-after=1 "$FORCE_TIMEOUT" qm stop "$1" --overrule-shutdown 1 2>> "$LOG" || log "ERROR: qm stop $1 failed"
}

force_ct() {
  if ((DRY)); then echo "    pct stop $1 --overrule-shutdown 1"; return 0; fi
  log "force-stopping CT $1"
  timeout --kill-after=1 "$FORCE_TIMEOUT" pct stop "$1" --overrule-shutdown 1 2>> "$LOG" || log "ERROR: pct stop $1 failed"
}

wait_jobs() { # bound-seconds label
  local deadline=$((SECONDS + $1)) p alive
  while :; do
    alive=0
    for p in "${JOBS[@]}"; do kill -0 "$p" 2> /dev/null && alive=1; done
    if ((!alive)); then
      wait
      JOBS=()
      return 0
    fi
    if ((SECONDS >= deadline)); then
      log "WARN: $2 bound of $1 s reached, abandoning the remaining stop task(s)"
      kill "${JOBS[@]}" 2> /dev/null
      JOBS=()
      return 1
    fi
    sleep 2
  done
}

# Prepended, so the list reverses qm's ascending VMID order: the lowest edge id (for a router
# pair, give the preferred member the lower one) is the very last guest to go.
EDGE_RUNNING=""
graceful_wave() {
  local id vmids ctids
  EDGE_RUNNING=""
  if vmids=$(running_vms); then
    for id in $vmids; do
      if is_edge "$id"; then ((SHED)) || EDGE_RUNNING="$id $EDGE_RUNNING"; continue; fi
      in_scope vm "$id" || continue
      if ((DRY)); then stop_vm "$id" "$VM_TIMEOUT"; else stop_vm "$id" "$VM_TIMEOUT" & JOBS+=($!); fi
    done
  else
    ENUM_FAILED=1
    log "ERROR: qm list failed, running VMs could NOT be enumerated"
  fi
  if ctids=$(running_cts); then
    for id in $ctids; do
      in_scope ct "$id" || continue
      if ((DRY)); then stop_ct "$id" "$CT_TIMEOUT"; else stop_ct "$id" "$CT_TIMEOUT" & JOBS+=($!); fi
    done
  else
    ENUM_FAILED=1
    log "ERROR: pct list failed, running CTs could NOT be enumerated"
  fi
  ((DRY)) || wait_jobs "$STOP_BOUND" "step 2"
}

force_sweep() {
  local id vmids ctids left=0
  if ((DRY)); then echo "    qm stop / pct stop --overrule-shutdown 1 on whatever step 2 leaves running"; return; fi
  if vmids=$(running_vms) && ctids=$(running_cts); then
    for id in $vmids; do
      is_edge "$id" && continue
      in_scope vm "$id" || continue
      force_vm "$id" & JOBS+=($!)
      left=$((left + 1))
    done
    for id in $ctids; do
      in_scope ct "$id" || continue
      force_ct "$id" & JOBS+=($!)
      left=$((left + 1))
    done
  else
    ENUM_FAILED=1
    log "ERROR: guest enumeration failed before the force sweep"
  fi
  if ((left == 0)); then log "step 3 done: nothing left to force"; return; fi
  wait_jobs "$FORCE_BOUND" "step 3"
}

edge_round() {
  local id
  if ((DRY)); then
    for id in $EDGE_RUNNING; do stop_vm "$id" "$EDGE_TIMEOUT"; done
    return
  fi
  for id in $EDGE_RUNNING; do stop_vm "$id" "$EDGE_TIMEOUT" || true; done
}

# ─── step 4b: NAS and peers ──────────────────────────────────────────────
port_open() { timeout --kill-after=1 2 bash -c "exec 3<>/dev/tcp/$1/$2" 2> /dev/null; }
peer_up() { ping -c 1 -W 2 "$1" > /dev/null 2>&1; }

killpower_wait() {
  local deadline port host open remaining
  ((WAIT_BOUND > 0)) || return 0
  if ((DRY)); then echo "    probe $NAS_HOST ports $NAS_PORTS and ping ${PEER_HOSTS:-<no peers>} every $POLL s until closed and down"; return 0; fi
  deadline=$((SECONDS + KILLPOWER_WAIT))
  while :; do
    open=""
    for port in $NAS_PORTS; do
      ((SECONDS >= deadline)) && break
      port_open "$NAS_HOST" "$port" && open+="$NAS_HOST:$port "
    done
    for host in $PEER_HOSTS; do
      ((SECONDS >= deadline)) && break
      peer_up "$host" && open+="$host "
    done
    if ((SECONDS >= deadline)); then
      log "WARN: NAS/peer readiness not confirmed within $KILLPOWER_WAIT s: ${open% }; halting anyway"
      return 1
    fi
    if [[ -z "$open" ]]; then log "step 4b done: NAS $NAS_HOST closed ${NAS_PORTS// /,}, peers down: ${PEER_HOSTS:-none}"; return 0; fi
    remaining=$((deadline - SECONDS))
    if ((remaining < POLL)); then sleep "$remaining"; else sleep "$POLL"; fi
  done
}

summary_and_halt() {
  local vmids ctids running=0 id
  if vmids=$(running_vms) && ctids=$(running_cts); then
    running=$(count_words "$ctids")
    for id in $vmids; do running=$((running + 1)); done
  else
    ENUM_FAILED=1
  fi
  if ((ENUM_FAILED)); then
    log "ERROR: guest state UNKNOWN (enumeration failed), halting anyway"
  elif ((running > 0)); then
    log "WARN: $running guest(s) still running, halting anyway"
  else
    log "all guests stopped, halting"
  fi
  $HALT_CMD
}

budget_report() {
  local before=$BUDGET_FINALDELAY output=0 required script=$((TOTAL_BOUND + WAIT_BOUND))
  if [[ "$NUT_ROLE" == server ]]; then before=$((before + BUDGET_HOSTSYNC)); output=$UPS_OUTPUT_RESERVE; fi
  required=$((before + script + HOST_TEARDOWN_RESERVE + output + BUDGET_MARGIN))
  log "planned reserve: pre-script ${before}s + script ${script}s + host teardown ${HOST_TEARDOWN_RESERVE}s + UPS output ${output}s + margin ${BUDGET_MARGIN}s = ${required}s; runtime-low ${BUDGET_LB}s"
  if ((required > BUDGET_LB)); then log "WARN: runtime-low is below planned reserve by $((required - BUDGET_LB))s"; fi
}

# ─── dry run ─────────────────────────────────────────────────────────────
dry_run() {
  local available_primary=$((BUDGET_LB - BUDGET_FINALDELAY - BUDGET_HOSTSYNC))
  local available_secondary=$((BUDGET_LB - BUDGET_FINALDELAY))
    [[ "$NUT_ROLE" == server && -e "$DRILL_MARK" ]] && DRILL=1
  echo "pve-nut-shutdown --dry-run on $NODE, role $NUT_ROLE, conf $CONF$( ((CONF_OK)) || echo ' (MISSING, defaults)')$( ((DRILL)) && echo ' DRILL marker present') (nothing is changed)"
  if ((SHED)); then
    echo "tier 1: shed ${SHED_VMIDS:-<none>} where hosted here (bound ${SHED_BOUND} s), halt node: $SHED_HALT_NODE"
    shed_select
    ((ENUM_FAILED)) && { echo "ERROR: guest enumeration failed, nothing would be shed"; exit 1; }
    echo "step 1  park + record: $SHED_IDS"
    shed_record
    echo "step 2  graceful stop, all at once (bound ${STOP_BOUND} s)"
    graceful_wave
    echo "step 3  force sweep (bound ${FORCE_BOUND} s)"
    force_sweep
    if ((!SHED_HALT_NODE)); then
      ((ENUM_FAILED)) && { echo "ERROR: guest enumeration failed, the plan above is incomplete"; exit 1; }
      exit 0
    fi
    echo "then the final wave:"
    SHED=0
  fi
  echo "budget: ${BUDGET_LB} s from LB - FINALDELAY ${BUDGET_FINALDELAY} s - HOSTSYNC ${BUDGET_HOSTSYNC} s on the primary = ${available_primary} s (primary) / ${available_secondary} s (secondary)"
  echo "step 1  park HA rows homed here (bound ${PARK_BOUND} s)"
  park_ha_rows
  echo "step 1b stop the HA services, releasing the watchdog (bound ${HA_STOP_BOUND} s)"
  ha_release
  ((DRILL)) && { echo "drill: record plain guests for the restore"; drill_record_plain; }
  echo "step 2  graceful stop, all at once (bound ${STOP_BOUND} s)"
  graceful_wave
  echo "step 3  force sweep, all at once (bound ${FORCE_BOUND} s)"
  force_sweep
  if ((DRILL)); then echo "step 4  edge VM: kept running (drill)"; else echo "step 4  edge VM last (bound ${EDGE_TIMEOUT} s)"; edge_round; fi
  echo "step 4b NAS shares closed and peers down (primary only, bound ${KILLPOWER_WAIT} s)"
  killpower_wait
  if ((DRILL)); then echo "step 5  drill: no halt, no killpower; HA restarted, pve-nut-restore.service wakes the peers"; else echo "step 5  halt: $HALT_CMD"; fi
  budget_report
  ((ENUM_FAILED)) && { echo "ERROR: guest enumeration failed, the plan above is incomplete"; exit 1; }
  exit 0
}

((DRY)) && dry_run

# ─── live ────────────────────────────────────────────────────────────────
mkdir -p "$STATE_DIR"
# Publish final-wave ownership before waiting for the restore lock. This boot may never restore
# just because mains returns after FSD. A later boot has a different kernel boot id.
if ((!SHED)); then printf '%s\n' "$BOOT_ID" | timeout --kill-after=1 5 "$STATE_WRITE" write "$STATE_DIR/final-wave" || log "ERROR: cannot persist final-wave ownership; critical shutdown continues under FSD guard"; fi
exec 9> "$STATE_DIR/.lock"
flock -w "$LOCK_WAIT" 9 || log "WARN: $STATE_DIR/.lock busy for $LOCK_WAIT s (a restore running?), proceeding"
# A new recorded wave owns a fresh bounded recovery attempt set.
rm -f "$STATE_DIR/restore-attempts" "$STATE_DIR"/error-reset-*
((CONF_OK)) || log "ERROR: $CONF unreadable; defaults: role client, no shed list, no step 4b"
if ((SHED)); then
  log "tier 1 on $NODE: shed ${SHED_VMIDS:-<none>} (bound ${SHED_BOUND} s), halt node: $SHED_HALT_NODE"
  shed_select
  ((ENUM_FAILED)) && exit 1
  shed_record
  graceful_wave
  force_sweep
  if ((!SHED_HALT_NODE)); then
    log "tier 1 done on $NODE"
    exit 0
  fi
  log "tier 1 done, SHED_HALT_NODE=1: final wave follows"
  SHED=0
  printf '%s\n' "$BOOT_ID" | timeout --kill-after=1 5 "$STATE_WRITE" write "$STATE_DIR/final-wave" || log "ERROR: cannot persist final-wave ownership; critical shutdown continues under FSD guard"
fi
if [[ "$NUT_ROLE" == server && -e "$DRILL_MARK" ]]; then
  DRILL=1
  rm -f "$DRILL_MARK"
  log "DRILL on $NODE: final wave without halt and killpower; the edge stays up"
fi
log "NUT shutdown initiated on $NODE; script bound $((TOTAL_BOUND + WAIT_BOUND)) s"
budget_report
park_ha_rows
ha_release
((DRILL)) && drill_record_plain
graceful_wave
force_sweep
((DRILL)) || edge_round
killpower_wait
if ((DRILL)); then drill_finish; exit 0; fi
summary_and_halt
