# The scripts and units name /usr/local/{bin,sbin} and /etc paths directly, so there is no PREFIX;
# DESTDIR stages a package root.
DESTDIR ?=

BIN  := bin/pve-nut-shutdown.sh bin/pve-nut-upssched-cmd.sh
SBIN := sbin/pve-nut-tier.sh sbin/pve-nut-restore.sh sbin/pve-ha-node-online.py
UNITS := systemd/pve-nut-restore.service systemd/pve-nut-tier.path systemd/pve-nut-tier.service

.PHONY: install lint test

install:
	install -d $(DESTDIR)/usr/local/bin $(DESTDIR)/usr/local/sbin $(DESTDIR)/etc/systemd/system $(DESTDIR)/etc/tmpfiles.d
	install -m 0755 $(BIN) $(DESTDIR)/usr/local/bin/
	install -m 0755 $(SBIN) $(DESTDIR)/usr/local/sbin/
	install -m 0644 $(UNITS) $(DESTDIR)/etc/systemd/system/
	install -m 0644 tmpfiles.d/pve-nut.conf $(DESTDIR)/etc/tmpfiles.d/pve-nut.conf

lint:
	shellcheck $(BIN) sbin/*.sh
	python3 -m py_compile sbin/pve-ha-node-online.py tests/test_pve_nut_scripts.py

test:
	python3 -m unittest discover -s tests -v
