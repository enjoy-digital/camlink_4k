#!/usr/bin/env python3

#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from setuptools import setup
from setuptools import find_packages


with open("README.md", "r") as fp:
    long_description = fp.read()


setup(
    name                          = "camlink_4k",
    version                       = "2026.09",
    description                   = "CamLink 4K: LiteX based gateware and FX3 firmware for the Elgato Cam Link 4K",
    long_description              = long_description,
    long_description_content_type = "text/markdown",
    author                        = "Florent Kermarrec",
    author_email                  = "florent@enjoy-digital.fr",
    url                           = "https://github.com/enjoy-digital/camlink_4k",
    license                       = "BSD-2-Clause",
    python_requires               = ">=3.9",
    install_requires              = ["litex", "litedram", "pyusb", "numpy"],
    extras_require                = {"bench": ["libusb1", "Pillow", "scipy", "pytest"]},
    packages                      = find_packages(exclude=["test*"]),
    py_modules                    = ["camlink_4k", "camlink_4k_platform"],
    keywords                      = "FPGA LiteX ECP5 FX3 UVC HDMI capture",
    classifiers                   = [
        "Topic :: Scientific/Engineering :: Electronic Design Automation (EDA)",
        "Environment :: Console",
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: BSD License",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python",
    ],
)
