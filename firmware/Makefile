#
# This file is part of CamLink 4K.
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
# FX3 PLL: 20 = 384MHz, 21 = 403.2MHz (default: GPIF at 100.8MHz for 4K30 M420).
PLL_FBDIV ?= 21
CFLAGS += -DFX3_PLL_FBDIV=$(PLL_FBDIV)
ifdef NO_FLASH_BOOT
CFLAGS += -DNO_FLASH_BOOT
endif
# FX3 DMA buffer / UVC payload size (bytes).
DMA_BUF_SIZE ?= 32768
CFLAGS += -DGPIF_DMA_BUF_SIZE=$(DMA_BUF_SIZE)
LDFLAGS = -nostartfiles -static -T fx3.ld -Wl,--gc-sections -Wl,-Map,$(BUILD)/fx3.map

OBJS = start.o fx3.o usb.o usb_desc.o fpga.o i2c.o gpif.o fpga_ctrl.o sdram.o uvc.o it6802.o spi_flash.o main.o
OBJS := $(addprefix $(BUILD)/,$(OBJS))

all: $(BUILD)/fx3.img

$(BUILD)/fx3.img: $(BUILD)/fx3.elf elf2img.py
	python3 elf2img.py $< $@
	@$(CROSS)size $<

$(BUILD)/fx3.elf: $(OBJS) fx3.ld
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(OBJS)

CSR_CSV ?= ../build/csr.csv

# The firmware uses the CSR map (and DRAM PHY settings) of a gateware build.
ifneq ($(MAKECMDGOALS),clean)
ifeq ($(wildcard $(CSR_CSV)),)
$(error $(CSR_CSV) not found: build the gateware first (./camlink_4k.py --build) or set CSR_CSV=...)
endif
endif

generated/fpga_csr.h: $(CSR_CSV) csr2h.py
	mkdir -p generated
	python3 csr2h.py $< $@

# DRAM PHY settings/init sequence from the same gateware build (empty for builds without DRAM).
SDRAM_PHY_H = $(dir $(CSR_CSV))software/include/generated/sdram_phy.h

generated/sdram_init.h: $(CSR_CSV) gen_sdram.py ../software/sdram_phy.py
	mkdir -p generated
	python3 gen_sdram.py $(SDRAM_PHY_H) $@

generated/edid.h: gen_edid.py ../software/edid.py
	mkdir -p generated
	python3 gen_edid.py $@

$(BUILD)/it6802.o: generated/edid.h

$(OBJS): generated/fpga_csr.h generated/edid.h generated/sdram_init.h

# Objects embedding the git version rebuilt when it changes.
$(BUILD)/version.txt: FORCE | $(BUILD)
	@echo "$(VERSION)" | cmp -s - $@ || echo "$(VERSION)" > $@

$(BUILD)/main.o: $(BUILD)/version.txt

$(BUILD)/%.o: %.c | $(BUILD)
	$(CC) $(CFLAGS) -c -o $@ $<

$(BUILD)/%.o: %.S | $(BUILD)
	$(CC) $(CFLAGS) -c -o $@ $<

$(BUILD):
	mkdir -p $@

clean:
	rm -rf $(BUILD) generated

-include $(OBJS:.o=.d)

.PHONY: all clean FORCE

FORCE:
