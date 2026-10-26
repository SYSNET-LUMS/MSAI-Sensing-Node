# Sensing-node firmware

ESP32-S3 sensing-node firmware **0.1.21-wifi-ui**, built with **ESP-IDF 5.5.5**. A node selects Wi-Fi, Ethernet or BLE; the combined PC receiver accepts different nodes over these links together.

| Folder / file | Purpose |
| --- | --- |
| `main/` | Acquisition, calibration, networking, BLE, packet codec and setup webpage source. |
| `release/` | Ready-to-flash images, esptool, licence and checksum/source manifest. |
| `managed_components/` | Required mDNS dependency, retained for rebuilding. |
| `tests/` | Native deadline-recovery regression checks. |
| `private-backups/` | Device configuration/calibration backups; keep for recovery. |
| `sdkconfig`, `sdkconfig.defaults`, `partitions.csv`, `dependencies.lock` | Build, memory layout and dependency configuration. |
| `RECOVERY.txt` | Firmware changes, recovery behavior and historical validation. |
| `THIRD-PARTY.txt` | Dependency licensing information. |

**Install:** connect the sensing node's programming USB port and run `Install-Firmware.cmd`, or `powershell -ExecutionPolicy Bypass -File .\Flash.ps1 -Port COM17` with the actual port. The installer checks release hashes and backs up configuration before flashing. Use `Open-Console.cmd` for diagnostics.

**Build:** open an ESP-IDF 5.5.5 PowerShell terminal and run `./Build.ps1`. It recreates the removed `build/` folder. Run `./Package.ps1 -EsptoolPath <path-to-esptool.exe>` to package a new build; its upstream `LICENSE` must be beside esptool.

The ESP-IDF toolchain must provide Python on PATH; the Windows Store Python alias is insufficient. Flash the BLE USB base using the separate `ble_usb_basestation` installer.

On **25 October 2026**, the firmware build and deadline-recovery checks passed, and all release/source manifest hashes matched. No hardware was flashed during cleanup. Historical `diagnostics/` references in `RECOVERY.txt` refer to files no longer in this repository.

## Firmware payload (MSAC1 / MSC1)

On connection the node opens a TCP session to the receiver (default port **5443**) and sends one newline-terminated JSON hello:

```json
{"protocol":"MSAC1","device":"…","boot_id":"…","firmware":"0.1.21-wifi-ui","rate":10000,"calibration":{…}}
```

The receiver replies `OK\n` when ready; the node then streams binary **MSC1 packets** back to back. Over BLE the same packet is sent in fragments (8-byte fragment header: `<IHH` number, offset, size) and reassembled by the USB base station. Source: `main/transport.cpp`, `main/codec.cpp`; reference decoder: `receiver/backend/vendor/protocol.py`.

### Packet layout (little-endian)

| Offset | Size | Field | Notes |
| --- | --- | --- | --- |
| 0 | 4 | `magic` | `"MSC1"` |
| 4 | 1 | `version` | `1` |
| 5 | 1 | `codec` | `1` = waveform block, `0` = status JSON |
| 6 | 2 | `channels` | `8` for waveform, `0` for status |
| 8 | 2 | `count` | `128` samples per channel for waveform, `0` for status |
| 10 | 2 | `selectors` | Two input-selector pin bits (GPIO 8 = bit 0, GPIO 21 = bit 1); value `3` is invalid |
| 12 | 4 | `epoch` | Acquisition-run counter; increments on every (re)start |
| 16 | 4 | `rate` | Sample rate in Hz: `1000`, `2000` or `10000` |
| 20 | 8 | `first` | Index of the block's first sample within the epoch (0 for status) |
| 28 | 8 | `time_us` | Node `esp_timer` timestamp in microseconds |
| 36 | 4 | `size` | Payload length in bytes, at most 3600 |
| 40 | `size` | `payload` | Waveform bitstream or status JSON |
| 40 + `size` | 4 | `crc32` | CRC-32 (IEEE, as `zlib.crc32`) of all preceding bytes |

Total packet size is `size + 44` bytes. The receiver rejects packets with a wrong magic/version, oversized payload, CRC mismatch, unsupported rate/selector value, or unexpected codec/dimensions.

### Waveform payload (codec 1)

128 interleaved samples × 8 channels of signed 24-bit ADC data (ADS131M08), compressed losslessly. Each block decodes independently:

- Per channel, in channel order: a 5-bit Rice parameter `k` (0–24), the first sample as raw 24-bit two's complement, then 127 sample-to-sample differences.
- Each difference is ZigZag-mapped (`d ≥ 0 → 2d`, `d < 0 → −2d−1`) and Rice coded: the quotient `z >> k` in unary (that many `0` bits then a `1`), followed by the `k` low bits of `z`. Bits are packed MSB first.
- `k` is chosen per channel to minimise the coded length. The stream is zero-padded to a whole byte; any other padding is invalid.
- Decoded samples must stay within −8 388 608 … 8 388 607.

### Status payload (codec 0)

A UTF-8 JSON object sent periodically between waveform blocks. Fields: `fault`, `running`, `blocks`, `overflows`, `adc_crc`, `missed_adc`, `adc_recoveries`, `adc_input_highwater`, `queued_blocks`, `queued_ms`, `encode_us_avg`/`encode_us_max`, `send_us_avg`/`send_us_max`, `link` (Wi-Fi, Ethernet or BLE), `wifi_rssi_dbm`, `wifi_channel`, `wifi_disconnects`, `wifi_disconnect_reason`, `tcp_sessions`, `send_stalls`, `feedback`, `transport_paused`, `transport_pauses` and `firmware`.

The transport is intended for a trusted LAN; there is no TLS or authentication (the `OK` reply only signals that the receiver is ready).
