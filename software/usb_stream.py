#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Asynchronous USB bulk IN reader (libusb1) for high-throughput streaming tests."""

import time

import usb1

class USBStreamReader:
    def __init__(self, vid=0x1209, pid=0x0001, ep=0x81, transfer_size=512*1024, transfers=8, interface=1):
        self.ctx    = usb1.USBContext()
        self.handle = self.ctx.openByVendorIDAndProductID(vid, pid)
        if self.handle is None:
            raise RuntimeError("Device not found.")
        self.handle.setAutoDetachKernelDriver(True)
        self.handle.claimInterface(interface)
        self.interface = interface
        self.ep            = ep
        self.transfer_size = transfer_size
        self.transfers     = transfers

    def read(self, size, callback, timeout=2.0):
        """Read `size` bytes, calling callback(data) in order for each completed transfer."""
        state   = {"received": 0, "submitted": 0, "error": None}
        pending = []

        def on_complete(transfer):
            status = transfer.getStatus()
            if status != usb1.TRANSFER_COMPLETED:
                if state["error"] is None:
                    state["error"] = {1: "error", 2: "timeout", 3: "cancelled", 4: "stall",
                        5: "no device", 6: "overflow"}.get(status, status)
                return
            data = transfer.getBuffer()[:transfer.getActualLength()]
            state["received"] += len(data)
            callback(bytes(data))
            if state["submitted"] < size:
                state["submitted"] += self.transfer_size
                transfer.submit()

        for _ in range(self.transfers):
            transfer = self.handle.getTransfer()
            transfer.setBulk(self.ep, self.transfer_size, callback=on_complete, timeout=int(timeout*1000))
            state["submitted"] += self.transfer_size
            transfer.submit()
            pending.append(transfer)

        start = time.time()
        while state["received"] < size and state["error"] is None:
            self.ctx.handleEventsTimeout(0.1)
            if time.time() - start > timeout + size/50e6:
                break
        for transfer in pending:
            try:
                transfer.cancel()
            except usb1.USBError:
                pass
        while any(t.isSubmitted() for t in pending):
            self.ctx.handleEventsTimeout(0.1)
        return state["received"], time.time() - start, state["error"]

    def close(self):
        self.handle.releaseInterface(self.interface)
        self.handle.close()
        self.ctx.close()
