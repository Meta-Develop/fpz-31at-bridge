# FPZ-31AT Bridge

[日本語](README.md)

An adapter under development to use the MITS FPZ-31AT PCB prototyping machine from open-source CAM and G-code senders, using its existing USB controller instead of Design Pro.

```text
OSS CAM → G-code → G-code sender → FPZ-31AT Bridge → FPZ USB controller
```

The current input interface is a GRBL-style serial port, tested offline with Candle. Compatibility with individual CAM outputs and other senders still requires validation. September 2026 reverse-engineering findings provide the protocol and implementation foundation.

**This is a research project. It cannot yet run an FPZ machine from Candle.** Successful writes and synthetic acknowledgements do not establish physical position, completion, or a safe stop.

The repository includes evidence-derived command encoders, a fragmented-response decoder, an explicitly calibrated XY planner, an opt-in USB transport, and a responsive GRBL-style PTY adapter. The CLI supports only `unavailable` (default, no USB access) and `simulation` (synthetic state, no USB access). The real-machine codec remains unimplemented.

## Offline use

Linux/POSIX, Python 3.11 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m fpz_candle --mode simulation
```

The installed `fpz-31at-bridge --mode simulation` command starts the same adapter. The existing `fpz_candle` module and `fpz-candle` command are retained for compatibility.

Enter the printed `/dev/pts/N` path in Candle's editable Port field, baud 115200. Simulation requires no machine. It models client protocol behavior, not physical trajectories or timing.

## Research documentation

- [Method and provenance](docs/methodology.md)
- [USB transport](docs/usb-transport.md)
- [Command and response findings](docs/protocol.md)
- [Bridge architecture and API](docs/bridge.md)
- [Validation and limitations](docs/validation.md)
- [Hardware validation prerequisites](docs/hardware-validation.md)
- [Contribution guidance](CONTRIBUTING.md)
- [Publication scope](docs/publication.md)

The original offline suite contains 69 tests. Historical Candle 1.1.8 testing completed an eight-line simulated job. A single historical real `RP` write returned three bytes written, followed by an empty input packet; valid position and mechanical completion remain unproven. The delayed follow-up observation cannot exclude a transient reply in the intervening gap.

Vendor executables, drivers, machine configuration, licenses and private conversations are excluded. This is an independent project and is not endorsed by MITS. Original code and documentation are under the [MIT License](LICENSE).
