# Hardware validation prerequisites

Connecting USB does not establish that the machine is powered, homed or physically ready. None of the public CLI modes performs live I/O. A working software simulation is insufficient authorization or evidence for a physical experiment.

Before a new live transaction, confirm main power, completed power-on homing, clear travel space and a nearby operator able to stop/power off the machine. Verify current VID/PID, exact bus/address, endpoint descriptors and exclusive ownership. Respect the manufacturer's operating requirements and machine-specific setup.

## Next bounded observation

The next proposed experiment is one explicit `RP\r` write followed immediately by interrupt IN reads within a finite observation window. Empty packets continue until the deadline; no automatic resend or recovery command is allowed. Record write intent before execution and preserve raw replies, timing and outcome afterward. A partial write, malformed reply, application error, timeout or disconnect ends the transaction without escalation.

RP is the host application's position-request format, but a side-effect-free effect on every machine state has not been established. This plan therefore requires physical prerequisites and specific authorization before implementation/execution. It is not a runnable live script in this repository.

Do not send C, PM, Z presets, homing, spindle commands, motion, reset, endpoint guesses or the complete vendor initializer to make a silent machine respond. The old delayed observation is not a valid continuous RP timeout and does not justify such escalation.

## Gates after a successful position capture

1. Establish raw reply framing/context and repeatable position observations under a separately scoped experiment.
2. Measure axis direction, origin, scale and speed. A value found in software configuration remains a hypothesis until physically checked.
3. Establish command acceptance versus mechanical completion, and verify a physical stop path before executing planned motion through the bridge.
4. Integrate a codec that returns measured status and confirmed completion, then validate controlled XY operations. Keep Z, spindle and tool change separately gated.

Process termination, USB close, dispatch inhibition and an Alarm string cannot certify physical stop. Do not substitute queued R0, buffered ESC or an inferred C/F exchange for a tested stop mechanism.
