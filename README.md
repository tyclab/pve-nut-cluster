# pve-nut-cluster

UPS shutdown and power-return restore for a Proxmox VE cluster behind one UPS, driven by
[Network UPS Tools](https://networkupstools.org/). This covers the whole cluster, where most UPS scripts stop at one
node:

- **HA rows survive the outage.** Before stopping anything, each node parks its HA resources as `ignored` and records
  them. A plain `qm shutdown` on an HA guest persists request state `stopped`, and nothing starts that guest again
  when power returns. On boot, the restore re-adopts each row only once HA counts the node online. Restoring it
  earlier makes HA recover the guest onto a peer.
- **The HA watchdog is released.** `pve-ha-lrm` and `pve-ha-crm` stop before the guests. Otherwise a node that loses
  quorum mid-wave is fenced (hard reset) 60 s later.
- **Router VMs go last.** `EDGE_VMIDS` stop after every other guest, because they route for the rest.
- **Killpower waits for the NAS and the peers.** The primary's halt cuts UPS output about 20 s later. Before halting,
  it waits until the NAS has closed its shares (Synology DSM goes to Standby on FSD) and the other nodes stop
  answering ping. upsmon's own `HOSTSYNC` does not cover this: a secondary logs off as soon as its shutdown command
  starts.
- **Tiered.** At ONBATT + 120 s, tier 1 sheds `SHED_VMIDS` early, and can halt a whole node, to buy runtime. The
  final wave runs at LB.
- **Nodes come back by themselves.** When mains returns, the primary sends Wake-on-LAN magic packets to the peers it
  halted, then re-adopts the parked and shed guests.
- **Drill mode.** Runs the full wave on the primary without the halt or the killpower, then restores everything.

Tested on a three-node Proxmox VE 9.2 cluster: CyberPower CP1500EPFCLCD on USB (`usbhid-ups`), Synology NAS as NUT
secondary, OPNsense router VMs. It has run a real mains cut end to end.

## How it runs

| When                  | Who                          | What                                                              |
| --------------------- | ---------------------------- | ----------------------------------------------------------------- |
| ONBATT + 120 s        | every node                   | tier 1: park and stop `SHED_VMIDS`; `SHED_HALT_NODE=1` halts      |
| LB (runtime < 600 s)  | primary sets FSD, every node | final wave: park, release HA, stop all, edge last, (4b), halt     |
| primary, before halt  | primary                      | step 4b: NAS ports closed, peers down, at most `KILLPOWER_WAIT` s |
| primary halt          | NUT                          | `upsdrvctl shutdown`: UPS output off after `ups.delay.shutdown`   |
| AC back               | BIOS "power on after AC loss" | nodes boot                                                       |
| boot / ONLINE + 180 s | every node; WoL from primary | primary wakes halted peers; each node re-adopts its guests        |

Time budget of the final wave from LB: park 30 s, HA release 30 s, graceful stop 90 s, force 20 s, edge 45 s, step
4b 180 s. That adds up to 395 s on the primary, of the 580 s left after `FINALDELAY` and `HOSTSYNC`. The header of
`bin/pve-nut-shutdown.sh` has the details.

LB and FSD cancel the tier-1 timer, and a shed that arrives anyway stands down. Once the final wave has started, it
owns the guests.

## Files

| Path                             | Installed to                   | Role                                                  |
| -------------------------------- | ------------------------------ | ----------------------------------------------------- |
| `bin/pve-nut-shutdown.sh`        | `/usr/local/bin/`              | upsmon `SHUTDOWNCMD`; `--shed` for tier 1; `--dry-run` |
| `bin/pve-nut-upssched-cmd.sh`    | `/usr/local/bin/`              | upssched `CMDSCRIPT` (as `nut`): drops a request file |
| `sbin/pve-nut-tier.sh`           | `/usr/local/sbin/`             | root half: shed, restore, wake, drill NUT reset       |
| `sbin/pve-nut-restore.sh`        | `/usr/local/sbin/`             | re-adopts parked/shed guests behind the HA gate       |
| `sbin/pve-ha-node-online.py`     | `/usr/local/sbin/`             | the gate: HA counts this node online for this boot    |
| `systemd/pve-nut-tier.{path,service}` | `/etc/systemd/system/`    | runs `pve-nut-tier.sh consume` on a request file      |
| `systemd/pve-nut-restore.service` | `/etc/systemd/system/`        | runs `pve-nut-tier.sh boot` when state is pending     |
| `tmpfiles.d/pve-nut.conf`        | `/etc/tmpfiles.d/`             | `/run/nut/tier`, owned by `nut`                       |
| `examples/*`                     | copy by hand to `/etc/nut/`    | `pve-nut.conf`, `ups.conf`, `upsd.users`, `upsmon.conf`, `upssched.conf` |

## Install

On every node:

```sh
apt install nut-client          # the primary also needs nut-server and etherwake
make install
systemd-tmpfiles --create /etc/tmpfiles.d/pve-nut.conf
systemctl daemon-reload
systemctl enable --now pve-nut-tier.path
systemctl enable pve-nut-restore.service   # a no-op boot unit until something is parked or shed
```

Then write `/etc/nut/pve-nut.conf` from `examples/pve-nut.conf`. `NUT_ROLE=server` goes on the node with the UPS on
USB, `client` everywhere else. Take `upsmon.conf` and `upssched.conf` from `examples/`, and on the server also
`ups.conf` and `upsd.users`. Set `nut.conf` `MODE=netserver` on the server and `MODE=netclient` on the clients.

Every node needs its BIOS set to power on after AC loss. Every node that tier 1 halts needs Wake-on-LAN enabled, and
its MAC in the server's `WOL_TARGETS`.

**Passwords must not contain `#`.** upsmon sends `PASSWORD` unquoted, and upsd's parser ends the word at an unescaped
`#`, so that login fails
([networkupstools/nut#3721](https://github.com/networkupstools/nut/issues/3721); the config-file side is
[#3711](https://github.com/networkupstools/nut/issues/3711)).

## Check it

```sh
pve-nut-shutdown.sh --dry-run            # the final wave this node would run now, with the budget
pve-nut-shutdown.sh --dry-run --shed     # tier 1
```

Drill, on the primary. The whole sequence runs, but the edge VMs and the primary stay up and the UPS keeps power:

```sh
touch /var/lib/pve-nut-shutdown/drill
upsmon -c fsd
```

The peers halt. The primary then resets NUT (upsd keeps FSD latched otherwise), wakes the peers and re-adopts
everything. Logs go to `/var/log/nut-shutdown.log` and to the journal (`-t pve-nut-shutdown`, `pve-nut-tier`,
`pve-nut-restore`). A drill does not prove the killpower or the BIOS power-on. Only a real mains cut does.

## Limits

- One UPS and one NUT primary. A cluster on several UPSes needs one instance per UPS, and step 4b only knows its own
  peers.
- `EDGE_VMIDS` and `SHED_VMIDS` name VMIDs. The final wave also stops containers, but only a VM can be an edge.
- The budget assumes `battery.runtime.low` 600 s. Measure your runtime under your load and move the threshold, not
  the bounds.

## Development

```sh
make lint test
```

The tests put fake `qm`, `pct`, `ha-manager`, `pvesh`, `upsc`, `ping` and `etherwake` on `PATH`. They need bash,
python3 and util-linux, not Proxmox.

## License

MIT
