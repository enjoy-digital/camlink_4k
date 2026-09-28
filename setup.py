#!/usr/bin/env python3

from setuptools import setup
from setuptools import find_packages


with open("README.md", "r") as fp:
    long_description = fp.read()


setup(
    name                          = "litecamlink",
    version                       = "2026.09",
    description                   = "LiteX based gateware and FX3 firmware for the Elgato Cam Link 4K",
    long_description              = long_description,
    long_description_content_type = "text/markdown",
    author                        = "Florent Kermarrec",
    author_email                  = "florent@enjoy-digital.fr",
    url                           = "https://github.com/enjoy-digital/camlink_4k_test",
    license                       = "BSD-2-Clause",
    python_requires               = ">=3.9",
    install_requires              = ["litex", "litedram", "pyusb"],
    packages                      = find_packages(exclude=["test*"]),
    py_modules                    = ["litecamlink_platform"],
    keywords                      = "HDL ASIC FPGA hardware design",
    classifiers                   = [
        "Topic :: Scientific/Engineering :: Electronic Design Automation (EDA)",
        "Environment :: Console",
        "Development Status :: 2 - Pre-Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: BSD License",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
    ],
)
