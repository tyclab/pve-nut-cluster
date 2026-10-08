# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-10-08

### Fixed

- Retain pending restoration until guest runtime, HA runtime and configured application probes pass.
- Gate starts on exact snippet/storage readiness; retry with bounded attempts and one HA error recovery.
- Never restore or wake during FSD, unknown UPS state, or this boot's committed final wave.
- Retain failed wake state, serialize restore actors, and expose optional recovery metrics.
- Include command, edge-count, NFS/host teardown and UPS-output reserves in shutdown planning.

## [1.0.0] - 2026-09-29

### Added

- Final wave (`pve-nut-shutdown.sh`): HA park, watchdog release, parallel stop, force sweep, edge VMs last, NAS and
  peer wait before the killpower, halt. `--dry-run` and drill mode.
- Tier 1 shed at an upssched ONBATT timer, optionally halting a node; LB and FSD cancel it.
- Power-return restore: Wake-on-LAN of halted peers and HA re-adoption behind the node-online gate.
- Example NUT configs and an offline test suite.

[1.0.0]: https://github.com/tyclab/pve-nut-cluster/releases/tag/v1.0.0

[1.1.0]: https://github.com/tyclab/pve-nut-cluster/releases/tag/v1.1.0
