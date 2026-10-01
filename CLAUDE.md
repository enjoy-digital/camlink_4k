# CamLink 4K

Open gateware (LiteX) + bare-metal FX3 firmware (C) + host tools for the Elgato Cam Link 4K (gen 1).

- Plan and status: `doc/PLAN.md`. Hardware knowledge: `doc/HARDWARE.md`. Bench: `doc/BENCH.md`.
- Gateware: `camlink_4k.py` (target), `camlink_4k_platform.py` (IOs), `camlink_4k/gateware/*.py`
  (one core per file). Follow LiteX style: SPDX headers, 100-col `# Section ---` banners,
  aligned assignments, `# # #` separator, full sentence comments.
- FX3 firmware: `firmware/fx3/`, minimal clean C, arm-none-eabi-gcc, no Cypress SDK.
- Host tools: `software/camlink.py`.
- Tests: `python3 -m pytest test` (Migen sim), hardware checks scripted through `software/`.
- Toolchain: Yosys, nextpnr-ecp5, Project Trellis (e.g. OSS CAD Suite).
- Never commit stock firmware or flash dumps (copyrighted, contain the unit serial number): keep
  them outside the repository.
- Commits: `Area: summary`, bullet body, `Hardware: ... OK` line when validated on the board.
