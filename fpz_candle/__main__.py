"""PTY runner. Default mode exposes an unavailable hardware session with no I/O."""

import argparse
import signal
import sys

from .backend import HardwareBackend, SimulationBackend
from .runtime import PTYServer


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("unavailable", "simulation"), default="unavailable",
                        help="simulation is offline; unavailable never opens USB (default)")
    args = parser.parse_args(argv)
    backend = SimulationBackend() if args.mode == "simulation" else HardwareBackend()
    server = PTYServer(backend)
    stop = False

    def stopping(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    print(server.path, flush=True)  # Only the PTY path is printed to stdout.
    print(backend.description, file=sys.stderr, flush=True)
    # Keep slave open so disconnect/reconnect does not destroy the serial device.
    # Candle sends ctrl-X on connection, generating a fresh banner independently.
    try:
        while not stop:
            server.step()
    finally:
        shutdown = server.close()
    if not shutdown.clean:
        print("Backend shutdown was unclean or work was abandoned; physical state unknown", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
