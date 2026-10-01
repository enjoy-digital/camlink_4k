#
# This file is part of CamLinX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

# CamLinX 4K low latency viewer (needs libusb-1.0 and SDL2 development packages).

CFLAGS ?= -O2 -Wall -Wextra
PKGS    = libusb-1.0 sdl2 x11 gl libdrm xcb vulkan
# libxcb-randr (RandR 1.6 lease) by path: its development package is not always installed.
XCB_RANDR = $(firstword $(wildcard /usr/lib/x86_64-linux-gnu/libxcb-randr.so /usr/lib/x86_64-linux-gnu/libxcb-randr.so.0 /usr/lib/libxcb-randr.so*))

all: camlinx_view latgrab

camlinx_view: camlinx_view.c drm_out.c drm_out.h vk_out.c vk_out.h
	$(CC) $(CFLAGS) $(shell pkg-config --cflags $(PKGS)) -o $@ camlinx_view.c drm_out.c vk_out.c \
		$(shell pkg-config --libs $(PKGS)) -lX11-xcb -lXrandr $(XCB_RANDR) -lvulkan -lpthread

# Display latency probe (X11 + XShm), see doc/LATENCY.md.
latgrab: latgrab.c
	$(CC) $(CFLAGS) -o $@ $< $(shell pkg-config --libs x11 xext)

clean:
	rm -f camlinx_view latgrab

.PHONY: all clean
