# USB transport findings

## Observed descriptors

The original saved USB observation and a fresh read of cached Linux sysfs descriptors on 2026-10-13 agree on:

| Property | Value |
| --- | --- |
| VID:PID | `140c:0001` |
| Manufacturer / product | `MITS Board Maker` |
| Interface / alternate setting | 0 / 0 |
| Endpoint `0x81` | Interrupt IN, 64-byte maximum packet, interval 10 |
| Endpoint `0x02` | Bulk OUT, 64-byte maximum packet |
| Endpoint `0x83` | Bulk IN, 64-byte maximum packet |

Bus/address are transient and deliberately omitted. The device's reported serial string in the original observation was a generic product string, not a unique unit identity. Reading existing sysfs files did not open the device or issue a command. Enumeration does not establish main power, readiness or current machine state.

## Windows binding and endpoint mapping

Both vendor INFs match `USB\\VID_140C&PID_0001` and install the `fpusb` kernel service using KMDF 1.9. Interface GUID: `{DBD10C41-D4B6-4C1C-91B8-17501EE387F4}`. This is a dedicated USB interface, not a virtual COM port.

The standalone 32-bit `fpusbdll.dll` exports `Fpusb_Open`, `Fpusb_Close`, `Fpusb_Read`, `Fpusb_Write`. Read/write have a two-argument stdcall ABI and synchronous Windows file calls. It opens named pipe handles ending in `IF0000PIPE0000` and `IF0000PIPE0001`. The x86 driver parses the trailing decimal index and selects the corresponding configured endpoint.

Combining that static indexing with descriptor order gives pipe 0 → interrupt IN `0x81` and pipe 1 → bulk OUT `0x02`. The extra bulk IN endpoint `0x83` is not the read path used here. Do not substitute it simply because it is bulk.

INF maximum-transfer settings (4096 for x86, 256 for x64) are host request limits. They are neither physical packet sizes nor established firmware command-length limits. The x86 driver chunks requests, and packet boundaries do not supply application framing.

## Important EXE/DLL distinction

The analyzed DesignPro.exe uses embedded transport routines rather than proven imports from this DLL. Replacing the standalone DLL alone is not an established interception method.

| Embedded routine | Preferred-image VA | Relevant behavior |
| --- | --- | --- |
| Open | `0x40dbc4` | Same interface GUID; handles ending `PIPE00` / `PIPE01` |
| Read | `0x40dcb8` | Synchronous ReadFile; command-layer request length observed statically as 128 bytes |
| Write | `0x40df3c` | Accumulates non-CR bytes; CR triggers a WriteFile of the accumulated command |
| Actual buffered write call | `0x40dfc9` | Sends accumulated bytes and length |

Consequently, a caller's one-byte loop is not one physical transfer per byte. Embedded return values can report the caller's count without proving the kernel wrote that many bytes. Preserve CR buffering and check actual transport results in a replacement.

## Reset operations and Python transport

The x86 driver handles IOCTLs `0x220000` (cached configuration descriptor), `0x220004` (device/port reset path), and `0x220008` (pipe reset path). The latter two are active resets. They were not used as discovery probes.

The Python `USBTransport` is disarmed by default. Construction/import perform no discovery. An opted-in caller must specify an exact current bus/address; opening verifies identity and relevant descriptors, uses an advisory lock and a libusb interface claim, and refuses an active kernel driver. It does not detach a driver, set configuration, reset or clear a halt. The advisory lock coordinates cooperating processes; it does not exclude arbitrary privileged software.

The bridge accepts complete printable ASCII frames ending in CR, at most 64 bytes including CR. This is a conservative implementation bound, not a measured firmware limit. Empty IN packets mean no data. Short writes, timeouts, disconnects and descriptor mismatches fail without automatic retries. Closing releases owned resources and sends no inferred stop command.
