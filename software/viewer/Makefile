#
# This file is part of CamLinX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

# CamLinX 4K low latency viewer (needs libusb-1.0 and SDL2 development packages).

CFLAGS ?= -O2 -Wall -Wextra
PKGS    = libusb-1.0 sdl2

camlinx_view: camlinx_view.c
	$(CC) $(CFLAGS) $(shell pkg-config --cflags $(PKGS)) -o $@ $< $(shell pkg-config --libs $(PKGS)) -lpthread

clean:
	rm -f camlinx_view

.PHONY: clean
