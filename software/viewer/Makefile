#
# This file is part of CamLinX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

# CamLinX 4K low latency viewer (needs libusb-1.0 and SDL2 development packages).

CFLAGS ?= -O2 -Wall -Wextra
PKGS    = libusb-1.0 sdl2 x11 gl

all: camlinx_view latgrab

camlinx_view: camlinx_view.c
	$(CC) $(CFLAGS) $(shell pkg-config --cflags $(PKGS)) -o $@ $< $(shell pkg-config --libs $(PKGS)) -lpthread

# Display latency probe (X11 + XShm), see doc/LATENCY.md.
latgrab: latgrab.c
	$(CC) $(CFLAGS) -o $@ $< $(shell pkg-config --libs x11 xext)

clean:
	rm -f camlinx_view latgrab

.PHONY: all clean
