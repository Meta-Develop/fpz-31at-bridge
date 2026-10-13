# Command and response findings

Notation: `\r` is byte `0x0d`; all numeric fields below are signed integer controller units unless stated otherwise. Formats are evidence of application behavior, not permission to send them to a machine. Addresses refer to the executable identified in [methodology](methodology.md).

## Command inventory

| Format | Application evidence | Remaining limit |
| --- | --- | --- |
| `RP\r` | Position routine `0x4114f8`, send at `0x4115cc` | Live reply prefix and valid position unobserved |
| `M dx,dy\r` | Relative XY formatter `0x410194`, format `0xa5a240` | Physical scale/direction/backlash unmeasured |
| `MA x,y\r` | Absolute XY formatter `0x410554`, format `0xa5a251` | Application/controller/G-code origins not equated |
| `C\r` | Initialization `0x40f8ac`, Wait PEND `0x410b9c`, followed by blocking status read | Mechanical effect and completion semantics unproven |
| `S n\r` | Integer XY feed branch at `0x40ff00` | GUI mm/sec evidence; physical speed and safe ranges unmeasured |
| `R0\r` | Spindle-off wrapper `0x41322c` calls formatter `0x4107e8` with zero | Queued output; no immediate stop or physical completion proof |
| `RS n\r`, `R1\r` | Speed formatter `0x410010`; spindle-on wrapper `0x41326c` | UI suggests thousands of rpm; do not send a G-code RPM value directly |
| `ZPindex count\r` | Z preset formatter `0x40fae8`, preset setup `0x412ef4` | Preset positions require machine-specific context |
| `Pindex\r` | Preset selector `0x410710`; Down/Up callsites | Not arbitrary Z interpolation |
| `ZSindex n\r` | Z speed formatter `0x40fdc0` | Model-specific preset/speed workflow |
| `O3\r`, `O0\r` | Synthetic GUI capture of To Initial | Not a validated GRBL homing sequence |
| `W 0\r`, `W 1\r` | Synthetic GUI action traces | Meaning is unresolved; do not infer it from the letter |
| `PM 1\r` | Model-conditional initialization path `0x412578` | Selected model and PM meaning unresolved; no live replay |

The Python encoder intentionally implements only RP, C, M, MA, R0 and integer S. Finding a literal is insufficient to implement the remaining operations safely.

## XY conversion and feed

The application helper `0x471a94` divides physical XY values by selected per-axis resolution and rounds half away from zero; `0x471b38` performs the inverse. Configuration selection uses `Resolt`, not necessarily the `XYResolt` custom value. The observed copied state selected scale 0.00635 for X/Y/Z.

In the synthetic GUI, RIGHT/LEFT at 1.0000 mm generated `M 157,0` / `M -157,0`; BACK/FRONT generated `M 0,-157` / `M 0,157`. At the copied scale, 157 counts represents a commanded value of 0.99695. Neither scale nor direction was measured on the machine.

The integer S formatter truncates toward zero using helper `0xa38428`. Named GUI actions corroborated 2.5 → 2, 5.5 → 5, and 7.9 → 7; captions identify mm/sec. G-code feed uses mm/min, so an adapter needs explicit conversion and quantization reporting. A physical speed calibration is still missing.

The pure `plan_xy()` API requires explicit axis permutation, direction, mm/count, controller origin, travel limits and feed limits. It has no machine defaults. It returns quantized commanded setpoints, never measured final telemetry; it does not execute a move or certify G0/G1 interpolation.

## Z observations

Down at displayed depth 0.1 mm generated `P1`, `P2`, `W 1`, `S 2`, `C`; Up afterward generated `P0`, `W 1`, `S 5`, `C` for the chosen GUI speeds. Raising displayed depth from 0.1 to 0.2 mm updated preset2 by 15 counts. Separate quantization of two absolute setpoints explains why this delta differs from rounding their difference.

Z count conversion at `0x472114` depends on a model-selected scale. Source configuration had duplicate ZResolt keys, and fallback paths are model-dependent. Preset construction also depends on machine offsets and thickness/depth state. These observations do not establish an arbitrary signed G-code Z mapping, common origins or simultaneous XYZ interpolation.

The original GUI can emit setup commands before rejecting a later field. In the capture, invalid lower speeds were rejected after earlier sends. A replacement must validate the entire requested operation before its first hardware write.

## Responses

The application's payload reader accepts `R`, `X` or `Z` as a prefix, consumes that prefix separately, and then parses three signed counts separated by spaces/commas and terminated by CR or NUL. The observed code does not establish which prefix a live RP response uses.

The static response switch at `0x4907fd` maps the following characters to application return codes:

| Character | A | C | D | E | H | I | L | M | P | S | T |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Code | -24 | -21 | -40 | -25 | -41 | -22 | -23 | -20 | -8 | -26 | -42 |

F/R/X/Z return zero in that switch; other letters return -30. These are application classifications, not a fully resolved firmware error vocabulary. In particular, a received C is not an established echo acknowledgement, and a zero return does not establish completed motion. XON `0x11` and XOFF `0x13` are handled separately.

`StreamDecoder` supports fragmentation/coalescing, raw signed counts and flow-control events. F is classified with explicit pending context or as unsolicited; it never generates Idle/completion. Malformed/ambiguous payloads latch failure. The caller owns request context and must exclude stale responses.

`request_position()` writes exactly one RP on an already-open exclusive transport and polls with an overall deadline, pacing empty or incomplete reads at 10 ms. It never opens, initializes, resets, resends or supplies machine-state claims. The default 1000 ms deadline is an implementation setting, not a measured device specification.

## Initialization, completion and stop

The embedded opener performs nine initial reads and clears host buffers. No outbound enable/XON requirement was established. The wider initializer can send C, model-specific commands, Z presets, preset moves, spindle-off and speed commands. Replaying it is not a position-only probe.

The ESC generator `0x410b24` emits `0x1b` without CR, while the embedded writer flushes on CR. This call alone does not establish immediate transmission. The [manufacturer's manual](https://www.mitspcb.com/support/mdp_fpz_en.pdf) describes ESC as allowing already-sent milling data to finish before stopping. It cannot be substituted for immediate GRBL hold/reset semantics.

Synthetic F replies were inputs to the GUI experiment, not captured firmware acknowledgements. C/F semantics, physical completion, stop and error recovery must be established before live bridge integration.
