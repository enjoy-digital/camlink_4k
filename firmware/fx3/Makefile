#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

CROSS   ?= arm-none-eabi-
CC       = $(CROSS)gcc
BUILD   ?= build
VERSION := $(shell git describe --always --dirty 2>/dev/null || echo unknown)

CFLAGS  = -std=gnu11 -mcpu=arm926ej-s -marm -Os -g
CFLAGS += -Wall -Wextra -Werror -ffunction-sections -fdata-sections -fno-stack-protector
CFLAGS += -I. -MMD -MP -DGIT_VERSION=\"$(VERSION)\"
LDFLAGS = -nostartfiles -static -T fx3.ld -Wl,--gc-sections -Wl,-Map,$(BUILD)/fx3.map

OBJS = start.o fx3.o usb.o usb_desc.o fpga.o i2c.o gpif.o main.o
OBJS := $(addprefix $(BUILD)/,$(OBJS))

all: $(BUILD)/fx3.img

$(BUILD)/fx3.img: $(BUILD)/fx3.elf elf2img.py
	python3 elf2img.py $< $@
	@$(CROSS)size $<

$(BUILD)/fx3.elf: $(OBJS) fx3.ld
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(OBJS)

$(BUILD)/%.o: %.c | $(BUILD)
	$(CC) $(CFLAGS) -c -o $@ $<

$(BUILD)/%.o: %.S | $(BUILD)
	$(CC) $(CFLAGS) -c -o $@ $<

$(BUILD):
	mkdir -p $@

clean:
	rm -rf $(BUILD)

-include $(OBJS:.o=.d)

.PHONY: all clean
