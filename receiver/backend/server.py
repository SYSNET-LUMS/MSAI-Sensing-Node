"""MSAC1 TCP/BLE -> raw recordings + dashboard HTTP API. Run one worker only."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import socket
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import struct
import time
import uuid

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .store import LiveStore, load_calibration
from .analysis import AnalysisWorker, live_response, frame_response
from .vendor.protocol import HEADER, MAX_PAYLOAD, unpack
from . import serial_base

LOG = logging.getLogger('msai.bridge')
ROOT = Path(__file__).resolve().parent.parent
SERVICE = 'd0be1001-63f0-4dd1-a28d-e0201450a001'
DATA = 'd0be1002-63f0-4dd1-a28d-e0201450a001'
CONTROL = 'd0be1003-63f0-4dd1-a28d-e0201450a001'


@dataclass
class Settings:
    tcp_bind: str = '0.0.0.0'
    tcp_port: int = 5443
    recordings: Path = ROOT / 'recordings'
    calibration: Path = ROOT / 'backend' / 'calibration.json'
    names: Path | None = None
    tcp_enabled: bool = True
    ble_name: str = ''
    stale_seconds: float = 3.0
    max_clients: int = 16
    recording_part_bytes: int = 64 * 1024 * 1024
    minimum_free_bytes: int = 256 * 1024 * 1024

    @classmethod
    def from_env(cls):
        port = int(os.getenv('MSAI_TCP_PORT', '5443'))
        if not 1 <= port <= 65535:
            raise ValueError('MSAI_TCP_PORT must be 1..65535')
        return cls(tcp_bind=os.getenv('MSAI_TCP_BIND', '0.0.0.0'), tcp_port=port,
                   recordings=Path(os.getenv('MSAI_RECORDINGS', str(ROOT / 'recordings'))).resolve(),
                   calibration=Path(os.getenv('MSAI_CALIBRATION', str(ROOT / 'backend' / 'calibration.json'))).resolve(),
                   tcp_enabled=os.getenv('MSAI_TCP_ENABLED', '1') != '0',
                   ble_name=os.getenv('MSAI_BLE_NAME', '').strip())


def validate_hello(hello):
    # MSAC1 handshake: one permanent hardware ID per connection.
    if not isinstance(hello, dict) or hello.get('protocol') != 'MSAC1':
        raise ValueError('Expected an MSAC1 receiver connection')
    device = hello.get('device')
    if not isinstance(device, str) or len(device) != 12 or any(c not in '0123456789ABCDEFabcdef' for c in device):
        raise ValueError('Invalid device ID')
    if not isinstance(hello.get('calibration', {}), dict):
        raise ValueError('Invalid calibration metadata')
    boot = hello.get('boot_id')
    if boot is not None and (not isinstance(boot, str) or len(boot) != 16
                             or any(c not in '0123456789abcdef' for c in boot)):
        raise ValueError('Invalid boot session ID')
    json.dumps(hello, allow_nan=False)
    return hello


class Recording:
    """Exact validated frames; 64 MiB parts; never overwrites or deletes files."""
    def __init__(self, settings: Settings, node, hello):
        self.settings, self.node, self.hello = settings, node, hello
        self.stream = None
        self.size = self.part = 0
        self.checked = self.flushed = 0.0
        self.stem = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-') + node.device + '-' + node.session_id[:8]
        settings.recordings.mkdir(parents=True, exist_ok=True)
        self.open_part()

    def open_part(self):
        if self.stream:
            self.stream.close()
        self.stream = None
        self.part += 1
        path = self.settings.recordings / f'{self.stem}-part{self.part:04d}.msac'
        self.stream = path.open('xb')
        try:
            path.with_suffix('.json').write_text(json.dumps({
                'device': self.node.device, 'name': self.node.name, 'calibration': self.hello.get('calibration', {}),
                'hello': self.hello, 'transport': self.node.transport,
                'note': 'Exact raw MSAC1 frames. Dashboard calibration is separate.',
            }, indent=2, allow_nan=False), encoding='utf-8')
        except BaseException:
            self.close()
            raise
        self.size = 0
        with self.node.lock:
            self.node.recording = path.name
            self.node.recording_active = True

    def write(self, wire):
        now = time.monotonic()
        if now - self.checked >= 2:
            self.checked = now
            if shutil.disk_usage(self.settings.recordings).free < self.settings.minimum_free_bytes:
                raise OSError('Recording stopped: less than 256 MiB free disk space')
        if self.size and self.size + len(wire) > self.settings.recording_part_bytes:
            self.open_part()
        self.stream.write(wire)
        self.size += len(wire)
        if now - self.flushed >= 1:
            self.stream.flush()
            self.flushed = now

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        with self.node.lock:
            self.node.recording_active = False


class Bridge:
    def __init__(self, settings: Settings, store: LiveStore):
        self.settings, self.store = settings, store
        self.collector = ThreadPoolExecutor(max_workers=4, thread_name_prefix='msai-collect')
        self.server = None
        self.tasks = set()
        self.tcp_sessions = {}
        self.session_lock = asyncio.Lock()
        self.ble_task = None
        self.last_error = None
        self.ble_state = 'disabled' if not settings.ble_name else 'starting'
        self.serial_bases = {}
        self.serial_lock = asyncio.Lock()

    def serial_status(self):
        return [base.snapshot() for base in self.serial_bases.values()]

    async def add_ble_base(self, port):
        async with self.serial_lock:
            # Resolve only enumerated local serial devices: never open arbitrary
            # filesystem paths or pyserial URL transports supplied by an HTTP client.
            for name, base in self.serial_bases.items():
                if name.casefold() == port.casefold():
                    return base.snapshot()
            listing = await asyncio.to_thread(serial_base.list_serial_ports)
            if not listing['available']:
                raise RuntimeError('Serial support unavailable. Run Setup.cmd: '+str(listing['error']))
            selected = next((p['port'] for p in listing['ports'] if p['port'].casefold() == port.casefold()), None)
            if selected is None:
                raise ValueError('Serial port is not available. Connect the base station and refresh the ports.')
            if len(self.serial_bases) + len(self.tasks) >= self.settings.max_clients:
                raise RuntimeError('Receiver connection limit reached')
            base = serial_base.SerialBase(self, selected)
            self.serial_bases[selected] = base
            try:
                base.start()
            except BaseException:
                self.serial_bases.pop(selected, None)
                raise
            return base.snapshot()

    async def remove_ble_base(self, port):
        async with self.serial_lock:
            name = next((name for name in self.serial_bases if name.casefold() == port.casefold()), None)
            if name is None:
                return False
            base = self.serial_bases[name]
            await asyncio.to_thread(base.stop)
            self.serial_bases.pop(name, None)
            return True

    async def collect(self, fn, *args):
        # One awaited operation per client: work cannot accumulate without bound.
        job = asyncio.get_running_loop().run_in_executor(self.collector, partial(fn, *args))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            # Finish the current frame before closing its recording on shutdown.
            with suppress(Exception):
                result = await job
                if fn == self.begin:
                    node, recording = result
                    with node.lock:
                        node.connected = False
                    await asyncio.get_running_loop().run_in_executor(self.collector, recording.close)
            raise

    def begin(self, hello, transport):
        node = self.store.connect(hello, transport, uuid.uuid4().hex)
        try:
            recording = Recording(self.settings, node, hello)
        except BaseException:
            node.connected = False
            raise
        LOG.info('Connected %s via %s', node.device, transport)
        return node, recording

    def packet(self, node, recording, wire):
        started = time.perf_counter()
        meta, payload = unpack(wire)  # Original lossless Rice decoder + packet CRC.
        if isinstance(payload, dict):
            json.dumps(payload, allow_nan=False)
        recording.write(wire)  # Never silently drop raw data to keep the UI running.
        with node.lock:
            node.accept(meta, payload)
            node.received_bytes += len(wire)
            node.bandwidth_bytes += len(wire)
            elapsed = time.monotonic() - node.bandwidth_started
            if elapsed >= 1:
                node.bandwidth_bps = node.bandwidth_bytes / elapsed
                node.bandwidth_bytes = 0
                node.bandwidth_started = time.monotonic()
            node.processing_ms_max = max(node.processing_ms_max, (time.perf_counter() - started) * 1000)

    def fail(self, node, error):
        reason = str(error) or type(error).__name__
        self.last_error = reason
        if node:
            with node.lock:
                node.error = reason
                node.receiver_errors += 1
        LOG.warning('Receiver: %s', reason)

    async def handle_tcp(self, reader, writer):
        task = asyncio.current_task()
        if len(self.tasks) + len(self.serial_bases) >= self.settings.max_clients:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            return
        self.tasks.add(task)
        # Absorb a short scheduling/disk stall without advertising a tiny TCP window.
        node = recording = None
        try:
            sock = writer.get_extra_info('socket')
            if sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            line = await asyncio.wait_for(reader.readline(), 10)
            if len(line) > 2048 or not line.endswith(b'\n'):
                raise ValueError('Invalid or oversized hello')
            hello = validate_hello(json.loads(line))
            device, boot = hello['device'].upper(), hello.get('boot_id')
            async with self.session_lock:
                previous = self.tcp_sessions.get(device)
                if previous and boot and previous[0] == boot:
                    # Same running firmware reconnecting after a stalled socket.
                    # Flush/close the old recording before accepting a new one.
                    # A boot ID is correlation metadata, not authentication.
                    LOG.info('Replacing stale TCP session for %s', device)
                    previous[1].cancel()
                    await asyncio.gather(previous[1], return_exceptions=True)
                node, recording = await self.collect(self.begin, hello, 'TCP')
                self.tcp_sessions[device] = (boot, task)
            writer.write(b'OK\n')
            await writer.drain()
            while True:
                header = await asyncio.wait_for(reader.readexactly(HEADER.size), 12)
                size = HEADER.unpack(header)[-1]
                if size > MAX_PAYLOAD:
                    raise ValueError('Oversized packet')
                rest = await asyncio.wait_for(reader.readexactly(size + 4), 12)
                await self.collect(self.packet, node, recording, header + rest)
        except asyncio.CancelledError:
            raise
        except asyncio.IncompleteReadError as error:
            if error.partial:
                self.fail(node, ValueError('Connection ended inside a packet or header'))
        except Exception as error:
            self.fail(node, error)
        finally:
            if node:
                with node.lock:
                    node.connected = False
                current = self.tcp_sessions.get(node.device)
                if current and current[1] is task:
                    self.tcp_sessions.pop(node.device, None)
            if recording:
                with suppress(OSError):
                    await self.collect(recording.close)
            writer.close()
            with suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)
            self.tasks.discard(task)

    async def start(self):
        if self.settings.tcp_enabled:
            self.server = await asyncio.start_server(self.handle_tcp, self.settings.tcp_bind,
                                                     self.settings.tcp_port, limit=256 * 1024)
            # Port zero is useful only in isolated tests.
            self.settings.tcp_port = self.server.sockets[0].getsockname()[1]
            LOG.info('MSAC1 plain TCP listening on %s:%s', self.settings.tcp_bind, self.settings.tcp_port)
        if self.settings.ble_name:
            try:
                import bleak  # noqa: F401 -- optional; fail startup clearly when requested but absent.
            except ImportError as error:
                if self.server:
                    self.server.close()
                    await self.server.wait_closed()
                raise RuntimeError('BLE requested; install backend/requirements-ble.txt') from error
            self.ble_task = asyncio.create_task(self.ble_loop())

    async def stop(self):
        if self.server:
            self.server.close()
        async with self.serial_lock:
            await asyncio.gather(*(asyncio.to_thread(base.stop) for base in self.serial_bases.values()))
            self.serial_bases.clear()
        jobs = list(self.tasks) + ([self.ble_task] if self.ble_task else [])
        for task in jobs:
            task.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        # Python 3.12 waits for existing connections too. Close their handlers
        # (and flush recordings) before awaiting the listener's final closure.
        if self.server:
            await self.server.wait_closed()
        await asyncio.to_thread(self.collector.shutdown, wait=True, cancel_futures=True)

    async def ble_loop(self):
        from bleak import BleakClient, BleakScanner
        while True:
            node = recording = None
            try:
                self.ble_state = 'scanning'
                device = await BleakScanner.find_device_by_filter(
                    lambda d, a: (d.name == self.settings.ble_name or a.local_name == self.settings.ble_name)
                    and SERVICE in (a.service_uuids or []), timeout=20)
                if device is None:
                    self.ble_state = 'not found; retrying'
                    await asyncio.sleep(3)
                    continue
                self.ble_state = 'connecting'
                frames = asyncio.Queue(maxsize=256)
                overflow = False

                def notification(_, value):
                    nonlocal overflow
                    try:
                        frames.put_nowait(bytes(value))
                    except asyncio.QueueFull:
                        overflow = True

                async with BleakClient(device) as client:
                    self.ble_state = 'reading node information'
                    hello = validate_hello(json.loads(bytes(await client.read_gatt_char(CONTROL))))
                    if hello.get('transport') != 'ble-plain-v1':
                        raise ValueError('BLE firmware protocol mismatch')
                    self.ble_state = 'subscribing'
                    await client.start_notify(DATA, notification)
                    node, recording = await self.collect(self.begin, hello, 'BLE/GATT')
                    assembler = BleFrames()
                    self.ble_state = 'starting stream'
                    await client.write_gatt_char(CONTROL, b'START', response=True)
                    self.ble_state = 'connected'
                    while client.is_connected:
                        fragment = await asyncio.wait_for(frames.get(), 10)
                        if overflow:
                            raise ValueError('BLE queue overflow')
                        complete = assembler.feed(fragment)
                        if complete:
                            number, wire = complete
                            await self.collect(self.packet, node, recording, wire)
                            await client.write_gatt_char(CONTROL, struct.pack('<Q', number), response=True)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOG.warning('BLE failed during %s', self.ble_state, exc_info=True)
                self.fail(node, error)
                self.ble_state = 'disconnected; retrying'
            finally:
                if node:
                    with node.lock:
                        node.connected = False
                if recording:
                    with suppress(OSError):
                        await self.collect(recording.close)
            await asyncio.sleep(3)


class BleFrames:
    """Same ordered/bounded fragment assembly as the supplied bench receiver."""
    def __init__(self):
        self.expected = 0
        self.packet = bytearray()
        self.total = 0

    def feed(self, fragment):
        if len(fragment) < 9:
            raise ValueError('Truncated BLE fragment')
        number, offset, size = struct.unpack_from('<IHH', fragment)
        if number != self.expected or offset != len(self.packet):
            raise ValueError('BLE fragment discontinuity')
        if not self.packet:
            self.total = size
        if size != self.total or not HEADER.size + 4 <= size <= HEADER.size + MAX_PAYLOAD + 4 or len(self.packet) + len(fragment) - 8 > size:
            raise ValueError('Invalid BLE frame size')
        self.packet.extend(fragment[8:])
        if len(self.packet) == self.total:
            wire = bytes(self.packet)
            self.packet.clear()
            self.expected += 1
            return number, wire
        return None


class NodeName(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80, strict=True)


class BleBasePort(BaseModel):
    model_config = ConfigDict(extra='forbid')
    port: str = Field(min_length=1, max_length=128, strict=True)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    store = LiveStore(load_calibration(settings.calibration), settings.stale_seconds,
                      names_path=settings.names or settings.calibration.with_name('node_names.json'))
    bridge = Bridge(settings, store)
    fft_worker = AnalysisWorker('msai-fft', workers=1, capacity=32)
    display_worker = AnalysisWorker('msai-display', workers=2, capacity=32)

    @asynccontextmanager
    async def lifespan(app):
        await bridge.start()
        try:
            yield
        finally:
            await bridge.stop()
            await fft_worker.close()
            await display_worker.close()

    app = FastAPI(title='MSAI dashboard bridge', version='1.1.0', lifespan=lifespan)
    app.state.store, app.state.bridge = store, bridge
    app.state.fft_worker, app.state.display_worker = fft_worker, display_worker

    @app.middleware('http')
    async def response_headers(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.get('/api/health')
    async def health():
        return {'ok': True, 'tcp_enabled': settings.tcp_enabled,
                'tcp_port': settings.tcp_port, 'ble': bridge.ble_state,
                'ble_name': settings.ble_name,
                'ble_bases': bridge.serial_status(),
                'last_receiver_error': bridge.last_error,
                'active_devices': sum(node.connected for node in list(store.nodes.values())),
                'fft_worker': fft_worker.status(), 'display_worker': display_worker.status(),
                'collection_workers': 4,
                'security': 'BENCH ONLY: node transport has no TLS or authentication'}

    @app.get('/api/live')
    async def live():
        receiver = {'ble': bridge.ble_state, 'ble_name': settings.ble_name,
                    'ble_bases': bridge.serial_status(),
                    'tcp_enabled': settings.tcp_enabled, 'tcp_port': settings.tcp_port,
                    'last_error': bridge.last_error}
        return await display_worker.run(lambda: live_response(store, receiver))

    @app.get('/api/serial-ports')
    async def serial_ports():
        return await asyncio.to_thread(serial_base.list_serial_ports)

    @app.post('/api/ble-bases')
    async def connect_ble_base(body: BleBasePort):
        try:
            return await bridge.add_ble_base(body.port.strip())
        except ValueError as error:
            raise HTTPException(422, str(error))
        except RuntimeError as error:
            raise HTTPException(409, str(error))

    @app.delete('/api/ble-bases/{port:path}')
    async def disconnect_ble_base(port: str):
        if not await bridge.remove_ble_base(port):
            raise HTTPException(404, 'This base station is not connected to the receiver')
        return {'port': port, 'state': 'stopped'}

    @app.get('/api/waveform/{device}')
    async def waveform(device: str, channels: str = '0,1,2,3,4,5,6,7',
                       window_ms: int = Query(200, ge=100, le=1000),
                       max_points: int = Query(600, ge=32, le=2000), center: bool = True):
        try:
            selected = sorted(set(int(ch.strip()) for ch in channels.split(',')))
            if not selected or len(selected) > 8 or any(ch < 0 or ch > 7 for ch in selected):
                raise ValueError()
        except ValueError:
            raise HTTPException(422, 'channels must be comma-separated integers from 0 to 7')
        if device.upper() not in store.nodes:
            raise HTTPException(404, 'No connection received from this node')
        return await display_worker.run(lambda: frame_response(store, 'waveform', device, selected, window_ms, max_points, center))

    @app.get('/api/fft/{device}')
    async def spectrum(device: str, channel: int = Query(0, ge=0, le=7),
                       window_ms: int = Query(100, ge=50, le=1000)):
        if device.upper() not in store.nodes:
            raise HTTPException(404, 'No connection received from this node')
        return await fft_worker.run(lambda: frame_response(store, 'spectrum', device, channel, window_ms))

    @app.put('/api/nodes/{device}/name')
    async def rename(device: str, body: NodeName):
        if device.upper() not in store.nodes:
            raise HTTPException(404, 'No connection received from this node')
        try:
            return await display_worker.run(lambda: {'device': device.upper(), 'name': store.rename(device, body.name)})
        except ValueError as error:
            raise HTTPException(422, str(error))
        except OSError:
            raise HTTPException(500, 'Could not save node name; check receiver storage permissions')

    @app.get('/api/recordings')
    async def recordings():
        def listing():
            with store.lock:
                active = {node.recording for node in store.nodes.values() if node.recording_active}
            result = {}
            for path in sorted(settings.recordings.glob('*.msac'), reverse=True)[:100]:
                if path.resolve().parent != settings.recordings.resolve():
                    continue
                metadata = path.with_suffix('.json')
                try:
                    data = json.loads(metadata.read_text(encoding='utf-8')) if metadata.is_file() else {}
                    result[path.name] = {'device': data.get('device', 'Unknown'), 'name': data.get('name'),
                                         'bytes': path.stat().st_size, 'active': path.name in active,
                                         'metadata': metadata.name if metadata.is_file() else None}
                except (OSError, ValueError):
                    continue
            return {'nodes': result}
        return await display_worker.run(listing)

    @app.get('/api/recordings/{filename}')
    async def recording_download(filename: str):
        path = (settings.recordings / filename).resolve()
        if (Path(filename).name != filename or path.parent != settings.recordings.resolve()
                or path.suffix not in ('.msac', '.json') or not path.is_file()):
            raise HTTPException(404, 'Recording not found')
        with store.lock:
            if any(node.recording_active and node.recording and Path(node.recording).stem == path.stem
                   for node in store.nodes.values()):
                raise HTTPException(409, 'This recording part is still active')
        return FileResponse(path, filename=filename, media_type='application/octet-stream')

    @app.get('/node-logo.png')
    async def node_logo():
        return FileResponse(ROOT / 'public' / 'node-logo.png')

    # After npm run build, this also serves the built dashboard on port 8000.
    # /api routes remain real APIs; unknown API paths never become index.html.
    dist = ROOT / 'dist'
    if (dist / 'assets').is_dir():
        app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='assets')

    @app.get('/')
    async def index():
        if (dist / 'index.html').is_file():
            return FileResponse(dist / 'index.html')
        return {'message': 'Bridge is running. Start the frontend with npm run dev and open http://localhost:5173.',
                'health': '/api/health', 'data': '/api/live'}

    return app


app = create_app()
