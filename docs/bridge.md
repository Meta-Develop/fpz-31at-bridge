# FPZ–Candle bridge foundation

This is a runnable GRBL 1.1 serial adapter with a separate FPZ USB transport
and protocol injection interface. **It does not yet control an FPZ machine.**
Evidence-derived FPZ frame encoders and a response decoder are included;
calibration, licenses and vendor binaries are not. Automated transport tests
use fake devices. No real FPZ codec is integrated into the Candle bridge yet.
Responsive serial input and dispatch inhibition **do not stop physical motion**;
verified device stop/status semantics and real-machine validation remain absent.

## Run

From the repository root, using Python 3 (pySerial is required for the test suite):

```sh
python3 -m fpz_candle
```

The default `unavailable` mode never imports PyUSB or opens any USB device.
The first stdout line is a private `/dev/pts/N` serial port. Enter that absolute
path into Candle's editable Port setting, with baud 115200. Keep the process
running. Candle's connection reset produces a new GRBL banner. Status is
`<Alarm>` with no coordinates, and motion is rejected.

For **offline simulation only**:

```sh
python3 -m fpz_candle --mode simulation
```

The startup banner message, build-info response and stderr explicitly identify
simulation. It cannot access hardware. The model accepts G0/G1, G17, G20/G21,
G54, G90/G91, G94, X/Y/Z/F/S, M3/M5, M2/M30 and `$J=` jogs. With M5 selected,
S only records desired speed; with M3 active, S requires backend execution and
confirmation before committing the new value. Jog requires explicit positive feed and does not alter modal
state. Synthetic work offsets are zero. The first status poll after motion
reports Run at the old position, the next reports Idle at the planned endpoint.
This is a client-protocol model, not a trajectory or timing simulation.

M2/M30 follow [GRBL v1.1h.20190825 program flow](https://github.com/gnea/grbl/blob/v1.1h.20190825/grbl/gcode.c#L991):
finish this block and pending motion, stop the spindle, reset motion to G1 and
distance to G90, and keep the units, feed value and desired spindle speed.
G17/G94/G54/M9 are already fixed in the supported subset. Program end resets
these selected modes only after successful completion, then emits `[MSG:Pgm End]`
and `ok`. In simulation, program end synchronizes the pending synthetic move;
the observed model Run event precedes acknowledgement and the resulting Idle
event follows it, allowing a complete original job including M2 to finish in
Candle. These ordered synthetic events are forbidden in the hardware adapter.

Responses use CRLF. Each normal line receives exactly one ordered `ok` or
`error:<code>`; realtime bytes never consume line acknowledgement credit.
Connection requests `Ctrl-X`, `S`, `$G` and `?` are supported. The responsive
runtime limits all pending, queued and partial ordinary input to 127 bytes
including line terminators, matching Candle's receive-window contract.

### Unavailable operations

G2/G3 arcs, G4 dwell, G53/G92 offset/origin restoration, probing, homing,
unlocking, settings writes, coolant and overrides are
not implemented. They are rejected or reported unavailable. This includes
Candle's possible automatic G53/G92 moves following an abort. A GRBL-looking
banner does not imply full GRBL compatibility.

In hardware mode a reset before any USB open is a protocol-only reset. Once a
hardware open is attempted, reset/hold/resume/jog-cancel are unavailable and
latch a fault; no successful stop, Idle state or reset banner is invented.
The manual's ESC behavior drains already-sent commands and does not establish
GRBL's immediate hold semantics. There is no real-motion CLI mode until the
missing protocol, completion, status and stop behavior is implemented and
validated.

## Evidence-derived protocol components

`fpz_candle/fpz_protocol.py` contains pure encoders for position requests,
relative/absolute XY counts, the original completion-barrier format, spindle
off and integer controller feed values. Encoding a frame does not establish
its safe operating limits, feed units or mechanical completion semantics.

`StreamDecoder` handles split/coalesced packets, CR/NUL-terminated position
payloads, XON/XOFF, application errors and explicitly contextualized `F`
responses. Positions retain signed **raw controller counts** from the response;
no millimetre conversion or coordinate origin is inferred. An `F` response
does not establish Idle or motion completion. Malformed or ambiguous data is
rejected rather than converted into a machine status.

`request_position()` sends one RP request on an already-open, exclusively
owned transport. Its default overall deadline is 1000 ms, including the write
and reads; incomplete responses and zero-length packets are paced at 10 ms.
An empty packet means no data, not failure or completion. The helper restores
the transport timeout on exit and never opens, resets or automatically retries
the device. It returns decoded raw counts and response events only. These
components do not yet provide real Candle motion, calibrated position/status,
completion or stop handling.

### Candidate XY plans

`fpz_motion.plan_xy(command, *, measured_counts, profile) -> XYPlan` is pure
and performs no transport I/O. `XYProfile` requires explicit logical-X/Y
`AxisMapping` values (controller-axis permutation, direction and mm per count),
controller origin counts, travel limits, feed limits and a separate G0 rapid
speed. There are no machine calibration defaults.

G91 yields candidate S/M frames; G90 yields S/MA frames. Explicit F is converted
from block units per minute; retained `modes.feed_mm_min` is already canonical.
The plan exposes requested and effective feed/work coordinates plus target and
delta counts. These are quantized **commanded setpoints, never measured final
telemetry**; the caller decides whether quantization is acceptable. Count
rounding uses rational arithmetic with half-away rounding; floats are interpreted
through their decimal string, not as a reproduction of original x87 arithmetic.
Z, spindle/program-end operations, arcs and unknown semantics are rejected.
The planner supplies no C barrier, completion/Idle claim or verified G0/G1
interpolation, and it is not connected to the runtime's hardware backend.

## Hardware integration API

`fpz_candle/model.py` defines:

- `Command(kind, source, words, modes, program_end=None)`: validated GRBL input. Axis values
  remain in requested units/distance mode. The codec must resolve measured
  position, origins, feed units and calibration. It must validate the whole
  block before any hardware write.
  `program_end` is `M2` or `M30` when present; the codec must execute this only
  after all movement in the block and previous accepted commands is complete.
- `FPZCodec.validate(command)`: pure validation before lazy USB open.
- `FPZCodec.execute(command, transport) -> Receipt`: controller-confirmed
  acceptance **and completion** are required before the initial hardware
  adapter returns `ok`. USB write success is not acknowledgement or completion.
  For M2/M30, the receipt must also include measured `state="Idle"`, zero
  spindle speed and `spindle_enabled=False`; zero speed alone does not prove
  the spindle has been disabled. Missing evidence or a failed stop leaves the
  session modes unchanged, latches a fault and produces no `ok`/program-end
  message. The default unknown codec rejects program end before USB open.
- `FPZCodec.status(transport) -> MachineStatus`: only measured real status may
  be returned, marked `measured=True`. No commanded position is substituted.
  Unknown position or work offset remains absent. Status failure latches a
  fault; cached coordinates are not reused.
- `FPZCodec.realtime(action, transport) -> Receipt`: reserved interface.
  HardwareBackend intentionally does not dispatch it yet: stop semantics and
  safe physical interruption of blocking operations remain unresolved.

`HardwareBackend(transport, codec)` is the integration point. The default
`UnknownFPZCodec` rejects operations before USB open. Faults block subsequent
commands. The current synchronous path permits one hardware command at a time
and requires proven completion before taking the next. The runtime executes
that path in a separate owner process so the serial loop remains responsive.
It still cannot interrupt a physical operation or establish a safe stop. Do not
enable production motion by merely injecting a translation table.

### Responsive dispatch and shutdown

The CLI uses `ResponsiveDispatcher` and `PTYServer` from `runtime.py`. One
spawned process owns every backend `execute`, `status`, `realtime` and `close`
call. Pass an unopened, spawn-picklable backend. The parent serial loop owns
parsing and modal state; queued blocks are parsed only after prior results have
committed their modes. There is one backend request in flight and no competing
USB reader, writer or closer.

During pending real work, `?` reports that physical state is unknown and emits
`<Alarm>` without coordinates. This is bridge unavailability, not a measured
machine alarm or stop. It performs no concurrent query and reuses no cached
coordinates. Repeated queries during one status request are coalesced. In
simulation, a query racing a short model operation is deferred until that
operation returns, then the same owner obtains the current synthetic status.

An unsupported hold, reset, jog cancel or resume during pending work immediately
inhibits further dispatch. Each outstanding normal line is rejected once, in
order; realtime messages themselves consume no line credit. An operation epoch
invalidates the in-flight result, so a late success cannot emit another `ok`,
commit modes or run the next queued block. A pending reset does not produce a
successful reset banner. Status/control failures and an owner-process failure
also inhibit subsequent work. Before any hardware open, an idle reset still
provides Candle's normal connection handshake. No owner process is restarted
automatically.

Input overflow inhibits dispatch and discards the overlong partial line through
its terminator while still recognizing realtime bytes. PTY output is
nonblocking with a local 16 KiB cap; an unread client cannot grow the output
buffer indefinitely. Exceeding that cap ends the server with an error.

`close(grace_seconds=1.0)` stops input and requests cleanup by the same backend
owner. The default grace matches the bridge's existing 1000 ms operation-timeout
setting, **not measured device timing**; callers may supply a finite positive
grace. A stuck owner is terminated, then killed if necessary, with bounded joins
at each stage. The result records whether it exited, whether termination was
forced, and whether work was abandoned. An unclean shutdown gives a nonzero CLI
exit. Process termination releases resources; it never proves a physical stop.

The synchronous `GRBLSession.feed()` remains a single-caller convenience/test
API; concurrent clients must use the runtime. The serial loop does not call the
backend methods on the parent's copy.

`USBTransport(bus=..., address=..., allow_io=False)` remains disarmed unless
explicitly opted in by a caller. Construction performs no discovery or I/O.
`open()` requires the exact bus/address, VID `140c`, PID `0001`, interface 0,
interrupt IN `0x81`, bulk OUT `0x02`, and 64-byte endpoint descriptors. It
acquires a per-target advisory lock in the user's private runtime directory
plus a libusb interface claim. It refuses an existing kernel driver and never
detaches, sets configuration, resets, or clears endpoint halts. This ownership
contract covers cooperating instances of this bridge and libusb claims; it is
not a sandbox or a lock against arbitrary privileged USB users.

- `write_command(frame)`: one complete printable ASCII frame ending in CR.
  The initial bridge limit is **64 bytes including CR**, chosen to fit one
  verified descriptor-sized packet; it is not a discovered firmware limit.
- `read_packet()`: one raw interrupt packet, not necessarily a command/reply.
  A zero-length packet means no data and does not fault, acknowledge or finish
  anything. Reply framing, XON/XOFF handling and completion belong in the codec.
  The codec must bound the overall response wait and avoid a busy loop on empty
  polls; `POLL_INTERVAL_SECONDS` is 0.010 from the observed interrupt interval.
- `CRFramer.feed(fragment)`: accumulates API fragments to CR and returns
  validated frames. It does not perform I/O or infer command semantics.
- Short writes, timeout, disconnect, ownership conflict and descriptor mismatch
  fail closed. Failed transfers are never retried automatically. The default
  finite USB timeout is 1000 ms and is configurable; it is a bridge setting,
  not an observed controller timing specification.
- `close()` releases only this instance's resources, without sending any
  inferred stop or reset command.

## Verification

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

The suite covers CR framing, fake USB identity/descriptors/exclusive ownership,
short writes/timeouts, absent real coordinates, unsupported commands, modal
state rollback on failure, no false hardware acknowledgements, simulation
units/jog/spindle behavior and a real PTY/pySerial exchange including reconnect.
Protocol tests also cover frame encoding, fragmented replies, raw counts,
zero-length polling, overall deadlines and rejection of ambiguous responses.
Runtime tests use blocked fake codecs and a real PTY to verify prompt realtime
receipt, one backend owner, ordered credits, bounded input, late-result rejection
and bounded shutdown. The same CLI runs an M2 job with 250 ms status polling.
It accesses no live USB device and launches no GUI.

Protocol references: [GRBL 1.1 interface](https://github.com/gnea/grbl/wiki/Grbl-v1.1-Interface),
[GRBL response codes](https://github.com/gnea/grbl/blob/master/grbl/report.h),
[Candle 1.1.8](https://github.com/Denvi/Candle/tree/v1.1.8),
[FPZ operating manual](https://www.mitspcb.com/support/mdp_fpz_en.pdf).
