# Validation record

## Evidence lanes

| Lane | Result | What it establishes |
| --- | --- | --- |
| Static EXE/DLL/driver analysis, 2026-09 | Command generation, embedded buffering, endpoint indexing and response classifications | Behavior of analyzed host software; not firmware/mechanical semantics |
| Isolated GUI with synthetic replies, 2026-09-16 | Named XY/Z/speed actions captured and correlated | Actual application output under the copied state; no hardware acceptance |
| Python offline suite | 69 tests in the recovered snapshot | Framing, mocks, protocol, planner, runtime, PTY and CLI software behavior |
| Candle 1.1.8 GUI, 2026-09-16 | Original eight-line simulated job completed, 8/8 rows | Integration with a real GUI and synthetic backend |
| One live RP transaction, 2026-09-16 | Three bytes written; first IN packet empty | Narrow USB transfer observation; no position or completion |
| Delayed input-only observation | Empty packets within a 1000 ms window | No payload in that delayed window; not continuous observation after RP |
| Cached sysfs descriptors, 2026-10-13 | VID/PID and three endpoints agree with original observation | Device enumeration/configuration only; no active command or readiness proof |

## Historical software checks

The September source suite passed 69 tests. The recovered source baseline was rerun on 2026-10-13: all 69 passed. The packaged wheel was also installed into a separate environment, and all 69 tests passed from a directory outside the source import path using Python 3.13.12. Source and wheel archives were checked for excluded private/vendor artifacts. The public source hashes are recorded in [source-snapshot.json](../evidence/source-snapshot.json).

The original GUI experiment used a PTY bridge with 250 ms status polling. Candle displayed Job done and logged `job completed: 8 8`; terminal order was Run, program-end message, ok, Idle. All values and state transitions came from simulation. No machining was performed.

Cleanup limitation: Xvfb closed before Candle finished exiting, producing an X11 disconnect. Normal GUI exit was not demonstrated. The bridge/backend/resource-tracker exited with code zero, and the owned processes were confirmed absent. The original private screenshots/logs are not included here, so this is a historical summary rather than a public end-to-end receipt.

## Historical live observation

The single command was bulk OUT `52 50 0d` (`RP\r`) on endpoint `0x02`; its write returned 3. The first interrupt IN `0x81` read returned zero bytes approximately 10 ms later. The first script stopped there. An empty USB packet is not a timeout, response, acknowledgement or completion.

A separately authorized input-only continuation began about 201 seconds later. Its 1000 ms window contained 123 empty completed reads followed by a timeout. There was no second OUT in that continuation. The gap prevents ruling out a transient response after the original RP.

A corrected immediate-polling transaction was prepared and tested offline but held from live execution. The published decoder/query helper incorporates bounded polling without empty-packet termination; that software property does not constitute a new hardware result.

## Reproducing the public checks

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[test]' build
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m build
```

CI runs these software checks on Linux with Python 3.11, 3.12 and 3.13. The workflow's presence is not evidence of a successful hosted run; consult its actual run status. Tests use fake USB objects, temporary directories and PTYs, and do not open live USB devices.

Still unverified: valid live position payload; measured counts-to-distance/feed conversion; coordinate origin and physical axis directions; motion interpolation; mechanical completion; physical stop/hold/reset; Z, spindle and tool-change behavior; real Candle machining.
