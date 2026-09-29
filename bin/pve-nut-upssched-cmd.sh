#!/usr/bin/env bash
# upssched CMDSCRIPT, run as nut (examples/upssched.conf): drops /run/nut/tier/<timer>.request for
# pve-nut-tier.path, which runs pve-nut-tier.sh as root. PVE_NUT_REQ_DIR is a test seam.
set -uo pipefail

REQ_DIR=${PVE_NUT_REQ_DIR:-/run/nut/tier}
TAG=pve-nut-upssched

case "${1:-}" in
  shed | restore) ;;
  *)
    logger -t "$TAG" -- "ERROR: unknown timer '${1:-}'"
    exit 1
    ;;
esac

if : > "$REQ_DIR/$1.request"; then
  logger -t "$TAG" -- "timer $1 expired, requested $1"
else
  logger -t "$TAG" -- "ERROR: cannot write $REQ_DIR/$1.request"
  exit 1
fi
