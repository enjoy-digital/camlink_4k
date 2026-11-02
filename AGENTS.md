# AGENTS.md: CamLink 4K

Open gateware (LiteX) + bare-metal FX3 firmware (C) + host tools for the Elgato Cam Link 4K (gen 1).

- Plan and status: `doc/PLAN.md`. Hardware knowledge: `doc/HARDWARE.md`. Bench: `doc/BENCH.md`.
- Gateware: `camlink_4k.py` (target), `camlink_4k_platform.py` (IOs), `gateware/*.py`
  (one core per file). Follow LiteX style: SPDX headers, 100-col `# Section ---` banners,
  aligned assignments, `# # #` separator, full sentence comments.
  Cores: control/status as plain Signals before `# # #`, CSRs created in `add_csr()` only when
  `with_csr=True` (simulation drives the Signals); the CSR map must stay stable (firmware/host).
- FX3 firmware: `firmware/`, minimal clean C, arm-none-eabi-gcc, no Cypress SDK.
- Host tools: `software/camlink.py`.
- Tests: `python3 -m pytest test` (Migen sim), hardware checks scripted through `software/`.
- Toolchain: Yosys, nextpnr-ecp5, Project Trellis (e.g. OSS CAD Suite).
- Never commit stock firmware or flash dumps (copyrighted, contain the unit serial number): keep
  them outside the repository.
- Commits: `Area: summary`, bullet body, `Hardware: ... OK` line when validated on the board.

## LiteX

This project is built with LiteX: read https://github.com/enjoy-digital/litex/wiki/LiteX-for-AI-Agents (framework map, conventions,
how to verify a change, contribute back the generic fixes to LiteX and its cores).

## Debug on hardware

Method: https://github.com/enjoy-digital/litex/wiki/Debug-A-SoC-On-Hardware (check the link and the SoC identity first, registers
before traces, narrow LiteScope captures, read the generated Verilog, turn findings into tests).
`litex_mcp` gives an agent the same loop as MCP tools through `litex_server` (CSRs by name, LiteScope
captures as data, console, builds with parsed timing, simulation; writes and `--load`/`--flash`
only with `--allow-write`): https://github.com/enjoy-digital/litex/wiki/LiteX-for-AI-Agents#drive-a-soc-through-mcp.

## Rules

- LiteX style: https://github.com/enjoy-digital/litex/blob/master/doc/coding_style.md.
- Never edit generated files (`build/`, `csr.h`, `csr.csv`, generated Verilog): change the Python.
- Never flash a board or write its SPI flash unless the user asked; `--load` (volatile) for experiments.
- Never commit flash dumps, stock firmware or vendor bitstreams.
- Pull requests: one topic each, with what was tested and on which hardware; say that an agent wrote
  it and who supervised.
