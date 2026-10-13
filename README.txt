MULTISENSOR AI -- SENSING NODE
ESP32-S3 / MCSA R4 / ADS131M08 / W5500

WORKFLOW
1. Double-click Install-Firmware.cmd on Windows to install using official
   Espressif esptool. It detects a sole COM port or asks you to choose one.
   Close any other serial console first. Developers
   build the native C++ ESP-IDF project; operators do not need Arduino IDE.
2. Power the board normally. USB is for installation/status, not the board supply.
3. Open-Console.cmd shows the USB console without an IDE. Type setup and Enter,
   or press the PCB RESET button, to see the per-device setup code.
   Join MSAI-<device suffix> using that code. The USB console at
   115200 prints that code; put it on the device label during manufacturing.
4. The captive setup page may open automatically. If the OS does not open it,
   open http://192.168.4.1/ explicitly. Captive popups are OS-dependent.
5. With Ethernet connected to a DHCP router, use the IP printed on USB or shown
   in the router's clients, or http://msai-<suffix>.local/ where mDNS is supported.
   Wired setup does not forcibly redirect other computers' browsers.
6. Enter the setup code and choose Wi-Fi, Ethernet or BLE.
   Wi-Fi: scan, select SSID, enter password and connect.
   Wi-Fi supports 2.4 GHz personal/open networks. Enterprise 802.1X credentials
   and upstream captive-login networks are not implemented by this setup form.
   Ethernet: DHCP by default; optional static IP/mask/router/DNS.
   BLE: local base station only; there is no cloud destination option.
7. Wi-Fi/Ethernet: choose Local gateway or Cloud/remote, import the receiver's
   connection.json, and set its reachable hostname/IP and port.
   BLE: import connection.json from the local BLE receiver and select the rate.
8. Save & start. Settings persist in NVS. The node restarts, authenticates the
   receiver and starts acquisition if the CT selectors/relay feedback are valid.
   The setup hotspot closes after authentication. Setup stays available while
   the receiver is unavailable, so a bad endpoint can be corrected.

RECOVERY AND CALIBRATION
RESET simply reboots and retains configuration. Hold BOOT for 10 seconds AFTER
the application has booted to clear connection settings and reopen setup.
Holding BOOT during reset instead enters the ESP32 ROM programming mode.
Configuration reset preserves device identity/setup code and sensor calibration.
USB console commands: status, setup, reset-setup. The last command clears saved
network/receiver settings and reboots, just like the 10-second BOOT hold.
The flash script backs up the existing 0x9000..0xdfff NVS region before writing
firmware. Keep private-backups private: it may contain network credentials.
Do not use erase-flash to perform an ordinary settings reset.

The CR reference factors from the existing firmware apply ONLY to device
38F5F090A994. Other devices report nominal CR scaling, not calibrated results.
SCT013-030 scaling is nominal until independently calibrated. Existing voltage
calibration is read from the legacy isov4c-r4-100 namespace without modification.

RECEIVER -- WINDOWS, LINUX, OR A CLOUD VM
Windows shortcut: install Python 3.12 from python.org, run Setup-Receiver.cmd,
enter the receiver's reachable address, then run Start-Receiver.cmd.
This is the NEW receiver for MSAC1; the previous Home_Receiver scripts cannot
decode this protocol. Keep their installation/identity if you need to roll back.

Install Python 3.12 and, from receiver/, run:
  python -m venv .venv
  Windows: .venv\Scripts\python -m pip install -r requirements.txt
  Linux:   .venv/bin/python -m pip install -r requirements.txt

Use that environment's Python for subsequent commands:
  python receiver.py init --host <reachable_receiver_IP_or_hostname> --port 5443
  python receiver.py serve --bind 0.0.0.0 --port 5443

init creates identity/server.key, server.crt and connection.json. Import only
connection.json into the node setup page; it contains a private authentication
token and the receiver's certificate fingerprint. Reuse the same identity across
restarts. The command refuses to overwrite an existing identity.

For BLE, run the receiver on a Windows/Linux computer with a working BLE adapter:
  python receiver.py ble --name MSAI-<device suffix>
This connects locally, proves possession of the receiver token, decrypts frames
and uses the same lossless decoder as Ethernet/Wi-Fi. BLE scanning/authentication
is independent of the browser; browser Bluetooth support is not required.

On a cloud VM, run serve as a supervised service and permit its selected TCP
port in the cloud firewall. On the home PC behind a router, forward that port to
the PC as with the previous Ethernet test. Do not expose the node's setup page.
An arbitrary HTTPS API, AWS IoT endpoint or MQTT broker cannot consume MSAC1
without a receiver/adapter. This release supplies the receiver so the cloud
provider can be selected later. Local/cloud selection does not change the codec.

The receiver records compressed packets to recordings/*.msac, plus calibration
metadata in the matching .json. protocol.unpack(packet) returns metadata and
the exact eight-channel integer sample array for analysis/integration. Recordings
can be decoded offline. Arrange storage retention for continuous deployments;
the receiver does not delete old recordings automatically.
To export an exact integer-count CSV for analysis:
  python export_recording.py recordings/<session>.msac samples.csv
The output includes acquisition epoch, sample index, uptime and all eight counts.
Use the session .json calibration slopes to convert counts to engineering units.
Do not stitch separate epochs into a continuous waveform.

DATA RATES AND COMPRESSION
Wi-Fi/Ethernet use the established 16 kSPS ADC -> filtered 10 kSPS output pipeline.
Compression is lossless relative to these output samples, not a reversal of the
16-to-10 kSPS filter/resampling step. BLE offers 1, 2 or 10 kSPS per channel.
At 1/2 kSPS the ADC uses the corresponding OSR; there is no naive sample skipping.
All eight channels remain present. Lower-rate BLE has a lower signal bandwidth.
The BLE 10 kSPS option needs throughput qualification on the actual adapter.

MSAC1 encodes signed 24-bit samples using channel-wise first differences,
ZigZag mapping and an optimal Rice parameter per channel per 128-sample block.
It is an integer waveform codec, not the floating-point Gorilla bitstream.
There is no raw-payload fallback and no quantization/truncation in the codec.
Noisy data can expand slightly; compression cannot guarantee a bandwidth ratio.
Each block has an independent predictor, sequence number, acquisition epoch,
sample rate, first-sample timestamp and CRC32. The receiver checks all framing.

If acquisition or output buffers overflow, firmware latches a fault and releases
the relay request. It does not silently discard samples and continue claiming a
complete stream. An outage/reconnect starts a new acquisition epoch. The PSRAM
queue is bounded; there is no unlimited or power-loss-safe offline recording.
Restart after resolving a latched acquisition fault. A new epoch is explicit.
Faults: 1 selector/initial interlock; 3 hardware interlock; 4 ADC initialization;
5 missed ADC conversion; 6 ADC CRC; 7 ADC status; 8 ADC timeout; 9 queue overflow;
10 ADC sample rate; 11 allocation; 12 task shutdown; 13 codec failure.

WIRE FORMAT (little endian)
40-byte header, Python struct <4sBBHHHIIQQI:
  magic MSC1, version=1, codec, channel_count, sample_count, selector_bits,
  epoch, samples_per_second, first_sample_index, uptime_timestamp_us, payload_size
Payload follows, then CRC32(header+payload) as uint32 LE.
codec=1: Rice waveform, 8 channels, 128 samples per channel.
codec=0: JSON status, channel_count=sample_count=0.
Payload limit: 3600 bytes. Timestamps are monotonic device uptime, not UTC.
The receiver records UTC arrival time in its console/session filename.

TLS transport uses an MSAC1 JSON hello plus newline, with device/token/rate/
calibration; the receiver responds OK plus newline, followed by framed packets.
Exact SHA-256 leaf certificate pinning is the trust model. CA hostname, expiry
and revocation checks are not a substitute/fallback for this provisioned pin.
Rotating the server certificate requires re-provisioning its fingerprint.

BLE service: d0be1001-63f0-4dd1-a28d-e0201450a001
Data characteristic: d0be1002-63f0-4dd1-a28d-e0201450a001
Auth/control characteristic: d0be1003-63f0-4dd1-a28d-e0201450a001
Auth read returns device/challenge/rate/calibration. Auth write is HMAC-SHA256
over challenge16||device12 with the 32-byte receiver token. Data key is HMAC
over challenge16||ASCII MSAI-DATA. A new challenge is created each connection.
Each MSAC1 packet is AES-256-GCM encrypted, with BLE1 as AAD; nonce12 comprises
challenge[0:4]||packet_counter64LE. Envelope: BLE1||nonce12||ciphertext||tag16.
Notifications fragment it as counter32LE||offset16LE||total16LE||fragment.
The receiver reassembles, authenticates, checks sequence, decrypts and decodes;
it then writes counter64LE||HMAC(data_key,counter64LE)[0:16] as acknowledgement.
BLE MTU below 100 is refused. Transport throughput must be tested with the host.

OFFICIAL ESPRESSIF DEVELOPMENT WORKFLOW
Use ESP-IDF 5.5.5, matching the ESP-IDF version underneath the already-tested
Arduino-ESP32 3.3.11, without depending on Arduino headers or libraries here.
Espressif Installation Manager (EIM) installs the SDK and ESP32-S3 tools.
Use the official ESP-IDF VS Code extension or an ESP-IDF terminal:
  idf.py set-target esp32s3
  idf.py build
  idf.py -p COM17 flash monitor
Replace COM17 with the actual programming port. sdkconfig.defaults provides
ESP32-S3, 8 MB QIO flash, 8 MB OPI PSRAM and USB Serial/JTAG console settings.
Keep dependencies.lock with releases for reproducible component versions.
The two app partitions reserve space for later signed OTA support; an OTA update
service, secure boot eFuse provisioning and flash encryption are not implemented
or enabled by this development package. Do not burn eFuses during bench testing.

Operators receive release/ binaries, their SHA-256 manifest, official esptool.exe
and Flash.ps1. No Arduino IDE or development environment is needed for flashing.
Only release a binary after compilation and the required hardware qualification.
Package.ps1 -EsptoolPath <official-esptool.exe> creates the binaries/manifest after
building; the source hash list includes the resolved sdkconfig and component lock.
The supplied 0.1.0-bench package is for bench validation, not a qualified release.

HARDWARE INTERLOCKS
CR uses released burden contacts. SCT013-030 requires SCT / 30A with all four
relays energized and feedback verified. SCT / 75A is invalid and blocked.
Selector changes, bad relay feedback, analog fault, ADC CRC/status/rate/timing
errors or buffer overflow stop acquisition and remove the relay request.
All four CT inputs share a mode; do not mix current-output CR and voltage-output
SCT sensors. Power down the test circuit before changing sensor connections.

VALIDATION
  python tests/test_protocol.py
  python tests/test_receiver.py
The optional native codec_bridge.dll runs the firmware's actual C++ codec against
the Python decoder in both directions, including full-range random samples.
See VALIDATION.txt for actual build and hardware results, not assumed passes.

References:
https://docs.espressif.com/projects/esp-idf/en/v5.5.5/esp32s3/
https://docs.espressif.com/projects/idf-im-ui/en/latest/
https://components.espressif.com/components/espressif/mdns
Brand colours taken from the MultiSensor AI public website CSS on 2026-10-13:
blue #2e44e9, yellow #ffcf00, dark #1d1f26; assets are self-contained/offline.
