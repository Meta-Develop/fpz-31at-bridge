# Method and provenance

The September 2026 investigation combined static PE/driver analysis, isolated execution of the original GUI against a synthetic transport, pure Python implementation and tests, and one narrowly scoped live position-query attempt. These evidence lanes establish different properties.

## Analyzed application

The command findings and synthetic GUI observations concern a DesignPro.exe with SHA256:

```text
6262a148be022f5858b9a78bf34407b1ee541cc0401d666b5593ff6a032781c2
```

Preferred image base: `0x400000`. Addresses below and in the protocol document are preferred-image virtual addresses, not ASLR runtime addresses. The executable is not distributed. Another version or machine configuration may behave differently.

Static analysis traced command-generation callsites, numeric conversion helpers, the application response switch, embedded USB routines and the x86 kernel driver's endpoint selection. Tools included `objdump`, PE import/export inspection and direct literal inspection. No binary patch is required by the published Python modules.

The standalone DLL uses preferred base `0x10000000`; the x86 driver uses `0x10000`. WDF calls were labeled with Microsoft's public KMDF 1.15 function-index header, while the analyzed driver identifies KMDF 1.9. The newer header is a labeling aid; it does not prove that the driver uses 1.15 or establish complete x64 equivalence. See [the transport findings](usb-transport.md).

## Synthetic GUI observations

The copied application ran in an isolated mount/PID/network environment without hardware device nodes. Transport calls were intercepted at the application/Windows API boundary. A responder supplied synthetic `F` bytes so the GUI could continue. Its position payload capability was not exercised: no RP exchange occurred in the named-action capture.

Each named UI action was bounded by master-trace line offsets and accompanied by metadata and, where applicable, a screenshot. The original review checked action slices against the master trace, including failed actions. The published [summary](../evidence/offline-gui-summary.json) contains selected non-identifying results. It is a curated summary, not an independently replayable substitute for the private master traces or evidence of device firmware behavior.

GUI labels such as RIGHT/BACK are application labels. They do not establish physical world coordinates. Observed runtime scales and entered speed values describe the copied configuration, not calibration of every FPZ-31AT.

## Source recovery

The public Python package and its four test files were recovered from the original bridge snapshot without source changes. [The source manifest](../evidence/source-snapshot.json) records their SHA256 values. Documentation and packaging were prepared separately for public use.

The published tree is a fresh, selected source snapshot; it does not contain the private host-configuration repository or its history. Chat transcripts, orchestration messages, raw instrumentation logs and machine files remain local. That boundary also means readers cannot independently reproduce all historical GUI and physical observations using this repository alone.

## External references

- [MITS FPZ operating manual](https://www.mitspcb.com/support/mdp_fpz_en.pdf), especially PDF pages 13–15 for individual setup files and startup, and 76/78 for buffered ESC behavior (zero-based PDF indexes; displayed page numbers differ).
- [Microsoft WDF function-index header](https://github.com/microsoft/Windows-Driver-Frameworks/blob/master/src/publicinc/wdf/kmdf/1.15/wdffuncenum.h), used with the version caveat above.
- [GRBL 1.1 interface](https://github.com/gnea/grbl/wiki/Grbl-v1.1-Interface).
- [Candle 1.1.8](https://github.com/Denvi/Candle/tree/v1.1.8).

These materials are linked, not redistributed. No manufacturer support or endorsement is implied.
