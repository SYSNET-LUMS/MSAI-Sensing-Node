# Combined receiver

One dashboard receives sensing nodes over **Wi-Fi, Ethernet and USB BLE base stations together**. Each sensing node uses one selected link. The receiver has 16 connection slots by default; each BLE base uses one slot and receives one node.

## Run

1. On a new PC, install Python 3.12–3.14 and run `Setup.cmd`.
2. Run `Start-Dashboard.cmd`, then open <http://127.0.0.1:8000>. Keep its window open; Ctrl+C stops reception and closes recordings.
3. For Wi-Fi/Ethernet, enter this PC's LAN IP and TCP port **5443** in the sensing node setup. Allow inbound TCP 5443 on the private network if needed.
4. For BLE, connect an ESP32-S3 base by USB. In **Receiver connections → BLE**, refresh ports, select its COM port and connect. In the sensing node setup, select BLE, scan/select/confirm that base, then save and start.

Selecting Wi-Fi, Ethernet or BLE in the dashboard changes the connection instructions; existing connections keep running. Select **All nodes** to view them together. `Start-BLE-Dashboard.cmd` opens this same combined receiver.

Close the optional standalone BLE dashboard on port 8001 before using its COM port here. Windows Bluetooth pairing is unnecessary. Re-select base COM ports after restarting the receiver. Add another USB base for another BLE node.

## Contents

| Folder / file | Purpose |
| --- | --- |
| `backend/` | Python receiver, transport protocols, recording, calibration and HTTP API. |
| `backend/tests/` | Automated tests and synthetic/live measurement utilities. |
| `src/` | Editable React dashboard source. |
| `public/` | Dashboard image assets. |
| `dist/` | Built dashboard served by the launcher; required to run. |
| `.venv/` | Installed Python environment; keep for immediate use or recreate with `Setup.cmd`. |
| `recordings/` | Original `.msac` recordings and JSON metadata; keep your data. |
| `backend/calibration.json` | Your calibration overrides; keep when updating. |
| `backend/node_names.json` | Saved node names; keep when updating. |
| `backend/calibration.example.json` | Calibration configuration example. |
| `package.json`, `package-lock.json` | Frontend dependencies and reproducible install. |

`node_modules/` is generated frontend tooling, optional for normal operation. Recreate it with `npm ci` when editing the dashboard. Recordings and settings are not disposable build output.

## Development and checks

With Node.js 22.12+ installed, run `npm ci`, `npm run lint`, then `npm run build` to refresh `dist/`. Run only one Uvicorn worker.

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
.\.venv\Scripts\python.exe -B -m pytest backend\tests -q -p no:cacheprovider
```

Pytest writes temporary recordings to the operating system's temporary folder; the command disables repository cache/bytecode output.

On **25 October 2026**, all **80 automated tests**, frontend lint and the production build passed. Combined tests exercised two loopback TCP streams and two simulated USB BLE bases, including independent plots/recordings and disconnect/reconnect. No connected hardware was available for a fresh physical Wi-Fi, Ethernet or BLE test.

Optional direct laptop BLE uses `backend/requirements-ble.txt` and `MSAI_BLE_NAME`; leave that variable unset when using USB bases. The node transport is intended for a trusted LAN and has no TLS/authentication. See `../RECOVERY.txt` for protocol/recovery details.
