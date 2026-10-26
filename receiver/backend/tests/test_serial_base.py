"""USB BLE transport tests; every node/port here is an explicit synthetic fixture."""
import asyncio
import json
import socket
import struct
import threading
import time
import zlib

import pytest
from fastapi.testclient import TestClient

from backend import serial_base
from backend.server import Bridge, Settings, create_app
from backend.serial_base import SerialBase
from backend.simulate_node import SYNTHETIC_CALIBRATION, waveform_packet
from backend.store import LiveStore


def hello(device='DE0000000001', scale=0.001):
    return {
        'protocol': 'MSAC1', 'transport': 'ble-plain-v1', 'device': device,
        'rate': 10000, 'calibration': {
            **SYNTHETIC_CALIBRATION, 'slopes': [scale] * 8,
        },
    }


def wave(first=0):
    return waveform_packet([[i + first + ch for ch in range(8)] for i in range(128)], first)


def envelope(kind, sequence, session, payload):
    raw = struct.pack('<4sBBHII', b'BSU1', 2, kind, len(payload), sequence, session) + payload
    return raw + struct.pack('<I', zlib.crc32(raw))


def metadata(device='DE0000000001', scale=0.001):
    return json.dumps(hello(device, scale)).encode()


def make_settings(tmp_path):
    calibration = tmp_path / 'calibration.json'
    calibration.write_text('{"devices":{}}')
    return Settings(tcp_bind='127.0.0.1', tcp_port=0,
                    recordings=tmp_path / 'recordings', calibration=calibration,
                    minimum_free_bytes=0)


def make_bridge(tmp_path):
    return Bridge(make_settings(tmp_path), LiveStore())


@pytest.fixture
def bridge(tmp_path):
    value = make_bridge(tmp_path)
    yield value
    value.collector.shutdown(wait=True, cancel_futures=True)


def wait_for(predicate, timeout=4):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= end:
            raise AssertionError('Timed out waiting for the synthetic serial receiver')
        time.sleep(0.005)


class FakeSerial:
    """Thread-safe byte stream, started only after the host writes HELLO."""
    def __init__(self, initial=b'', **kwargs):
        assert kwargs == {'port': None, 'baudrate': 2000000, 'timeout': .1, 'write_timeout': 1}
        self.port = None
        self.dtr = self.rts = None
        self.is_open = False
        self.closed = False
        self.initial = initial
        self.buffer = bytearray()
        self.writes = []
        self.unplugged = False
        self.condition = threading.Condition()

    def open(self):
        assert self.port and self.dtr is False and self.rts is False
        self.closed = False
        self.is_open = True

    def set_buffer_size(self, **_):
        pass

    @property
    def in_waiting(self):
        with self.condition:
            if self.unplugged:
                raise OSError('Synthetic USB unplug')
            return len(self.buffer)

    def read(self, size):
        with self.condition:
            if not self.buffer and not self.closed and not self.unplugged:
                self.condition.wait(.02)
            if self.unplugged:
                raise OSError('Synthetic USB unplug')
            value = bytes(self.buffer[:size])
            del self.buffer[:size]
            return value

    def write(self, raw):
        with self.condition:
            if self.unplugged:
                raise OSError('Synthetic USB unplug')
            self.writes.append(bytes(raw))
            if raw == b'HELLO\n':
                self.buffer.extend(self.initial)
                self.initial = b''
                self.condition.notify_all()
            return len(raw)

    def feed(self, raw):
        with self.condition:
            self.buffer.extend(raw)
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.is_open = False
            self.condition.notify_all()


def fake_factory(initial):
    instances = []

    def factory(**kwargs):
        fake = FakeSerial(initial if not instances else b'', **kwargs)
        instances.append(fake)
        return fake

    return factory, instances


def test_session_rollover_separates_identity_calibration_and_recordings(bridge):
    base = SerialBase(bridge, 'COM91')
    try:
        base.consume(3, 0, 100, b'{"state":"discovering"}')
        assert not bridge.store.nodes
        base.consume(2, 1, 100, metadata())
        base.consume(1, 2, 100, wave())
        first = bridge.store.nodes['DE0000000001']
        first_recording = bridge.settings.recordings / first.recording
        assert first.samples == 128
        assert first.calibration_meta['slopes'][0] == .001

        base.consume(3, 0, 101, b'{"state":"discovering"}')
        assert not first.connected and not first.recording_active
        assert first_recording.read_bytes() == wave()
        with pytest.raises(ValueError):
            base.consume(1, 1, 101, wave())
        base.consume(2, 1, 101, metadata('DE0000000002', .002))
        base.consume(1, 2, 101, wave())
        second = bridge.store.nodes['DE0000000002']
        assert second.samples == 128
        assert second.calibration_meta['slopes'][0] == .002
        assert second.recording != first.recording
        assert second.session_id != first.session_id
    finally:
        base.stop()
    assert not second.connected and not second.recording_active
    second_recording = bridge.settings.recordings / second.recording
    assert second_recording.read_bytes() == wave()
    assert json.loads(second_recording.with_suffix('.json').read_text())['device'] == 'DE0000000002'


def test_data_before_hello_or_incompatible_hello_never_creates_node(bridge):
    base = SerialBase(bridge, 'COM91')
    try:
        with pytest.raises(ValueError):
            base.consume(1, 0, 100, wave())
        for bad in ({}, {**hello(), 'transport': 'wrong-protocol'}, {**hello(), 'device': '../bad'}):
            with pytest.raises(ValueError):
                base.consume(2, 0, 100, json.dumps(bad).encode())
        assert not bridge.store.nodes
        assert not list(bridge.settings.recordings.glob('*.msac'))
    finally:
        base.stop()


def test_duplicate_ble_id_does_not_overwrite_active_tcp_session(bridge):
    tcp_node, recording = bridge.begin(hello(), 'TCP')
    base = SerialBase(bridge, 'COM91')
    try:
        bridge.packet(tcp_node, recording, wave())
        with pytest.raises(ValueError, match='already'):
            base.consume(2, 0, 100, metadata())
        assert bridge.store.nodes[tcp_node.device] is tcp_node
        assert tcp_node.connected and tcp_node.samples == 128
        assert tcp_node.transport == 'TCP'
    finally:
        base.stop()
        recording.close()
    assert (bridge.settings.recordings / tcp_node.recording).read_bytes() == wave()


def test_bad_inner_crc_never_enters_raw_recording_or_live_samples(bridge):
    base = SerialBase(bridge, 'COM91')
    try:
        base.consume(2, 0, 100, metadata())
        broken = bytearray(wave())
        broken[-1] ^= 1
        with pytest.raises(ValueError, match='CRC'):
            base.consume(1, 1, 100, bytes(broken))
        node = bridge.store.nodes['DE0000000001']
        assert node.samples == 0 and node.received_bytes == 0
    finally:
        base.stop()
    assert (bridge.settings.recordings / node.recording).read_bytes() == b''


def test_worker_reorders_retries_and_acks_only_contiguous_recorded_frames(bridge):
    frames = [envelope(2, 0, 100, metadata())]
    frames.extend(envelope(1, i + 1, 100, wave(i * 128)) for i in range(3))
    damaged = bytearray(frames[2])
    damaged[24] ^= 1
    factory, instances = fake_factory(b'boot noise\r\n' + frames[0] + frames[1] + damaged + frames[3])
    base = SerialBase(bridge, 'COM91', serial_factory=factory)
    try:
        base.start()
        wait_for(lambda: instances and b'ACK 100 1\n' in instances[0].writes)
        node = bridge.store.nodes['DE0000000001']
        assert node.samples == 128
        assert b'ACK 100 3\n' not in instances[0].writes
        instances[0].feed(frames[2])
        wait_for(lambda: b'ACK 100 3\n' in instances[0].writes)
        assert node.samples == 384 and node.discontinuities == 0
        instances[0].feed(frames[2])  # Lost ACK causes a harmless retry.
        wait_for(lambda: instances[0].writes.count(b'ACK 100 3\n') >= 2)
        assert node.samples == 384
        wait_for(lambda: b'PING\n' in instances[0].writes)
    finally:
        base.stop()
    assert instances[0].closed and not node.connected and not node.recording_active
    assert (bridge.settings.recordings / node.recording).read_bytes() == b''.join(wave(i * 128) for i in range(3))


def test_worker_never_acknowledges_invalid_inner_crc(bridge):
    broken = bytearray(wave())
    broken[-1] ^= 1
    factory, instances = fake_factory(envelope(2, 0, 100, metadata()) + envelope(1, 1, 100, bytes(broken)))
    base = SerialBase(bridge, 'COM91', serial_factory=factory)
    try:
        base.start()
        wait_for(lambda: instances and b'ACK 100 0\n' in instances[0].writes)
        wait_for(lambda: instances[0].closed)
        assert b'ACK 100 1\n' not in instances[0].writes
        node = bridge.store.nodes['DE0000000001']
        assert node.samples == 0 and not node.connected
    finally:
        base.stop()
    assert (bridge.settings.recordings / node.recording).read_bytes() == b''


def test_worker_acknowledges_base_status_without_recording_it_as_node_data(bridge):
    raw = (envelope(3, 0, 100, b'{"state":"discovering","firmware":"test-base"}')
           + envelope(2, 1, 100, metadata())
           + envelope(1, 2, 100, wave())
           + envelope(3, 3, 100, b'{"state":"streaming","firmware":"test-base"}'))
    factory, instances = fake_factory(raw)
    base = SerialBase(bridge, 'COM91', serial_factory=factory)
    try:
        base.start()
        wait_for(lambda: instances and b'ACK 100 3\n' in instances[0].writes)
        node = bridge.store.nodes['DE0000000001']
        assert node.samples == 128 and node.status == {}
    finally:
        base.stop()
    assert (bridge.settings.recordings / node.recording).read_bytes() == wave()


def test_tcp_and_two_serial_bases_share_dashboard_without_mixing_nodes(tmp_path):
    async def scenario():
        bridge = make_bridge(tmp_path)
        bases = []
        writer = None
        await bridge.start()
        try:
            for i in (1, 2):
                device = f'DE000000000{i}'
                factory, instances = fake_factory(envelope(2, 0, 100 + i, metadata(device, i * .001)) + envelope(1, 1, 100 + i, wave()))
                base = SerialBase(bridge, f'COM9{i}', serial_factory=factory)
                bases.append((base, instances))
                base.start()
            reader, writer = await asyncio.open_connection('127.0.0.1', bridge.settings.tcp_port)
            writer.write(json.dumps(hello('DE0000000003', .003)).encode() + b'\n')
            await writer.drain()
            assert await reader.readline() == b'OK\n'
            writer.write(wave())
            await writer.drain()
            for _ in range(400):
                if len(bridge.store.nodes) == 3 and all(node.samples == 128 for node in bridge.store.nodes.values()):
                    break
                await asyncio.sleep(.005)
            assert len(bridge.store.nodes) == 3
            for i in (1, 2, 3):
                node = bridge.store.nodes[f'DE000000000{i}']
                assert node.samples == 128 and node.connected
                assert node.calibration_meta['slopes'][0] == i * .001
            assert bridge.store.nodes['DE0000000003'].transport == 'TCP'
            assert all(bridge.store.nodes[f'DE000000000{i}'].transport != 'TCP' for i in (1, 2))

            bases[0][1][0].unplugged = True
            for _ in range(400):
                if not bridge.store.nodes['DE0000000001'].connected:
                    break
                await asyncio.sleep(.005)
            assert not bridge.store.nodes['DE0000000001'].connected
            assert bridge.store.nodes['DE0000000002'].connected
            assert bridge.store.nodes['DE0000000003'].connected
        finally:
            for base, _ in bases:
                await asyncio.to_thread(base.stop)
            if writer:
                writer.close()
                await writer.wait_closed()
            await bridge.stop()
        recordings = list(bridge.settings.recordings.glob('*.msac'))
        assert len(recordings) == 3
        assert all(path.read_bytes() == wave() for path in recordings)
        devices = {json.loads(path.with_suffix('.json').read_text())['device'] for path in recordings}
        assert devices == {f'DE000000000{i}' for i in (1, 2, 3)}

    asyncio.run(scenario())


def test_serial_api_enumeration_validation_idempotency_disconnect_and_shutdown(tmp_path, monkeypatch):
    listing = {'available': True, 'error': None, 'ports': [
        {'port': f'COM9{i}', 'description': 'Synthetic BLE base', 'hwid': 'TEST'}
        for i in (1, 2)]}
    created = []
    serial_instances = []

    def build_base(bridge, port):
        device = 'DE000000000' + port[-1]
        factory, instances = fake_factory(envelope(2, 0, 100, metadata(device)) + envelope(1, 1, 100, wave()))
        base = SerialBase(bridge, port, serial_factory=factory)
        created.append(base)
        serial_instances.append(instances)
        return base

    monkeypatch.setattr(serial_base, 'list_serial_ports', lambda: listing)
    monkeypatch.setattr(serial_base, 'SerialBase', build_base)
    settings = make_settings(tmp_path)
    settings.max_clients = 1
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get('/api/serial-ports')
        assert response.status_code == 200 and response.json() == listing
        assert response.headers['cache-control'] == 'no-store'
        for body in ({'port': 'COM99'}, {'port': 'socket://localhost:1234'},
                     {'port': ''}, {'port': 'COM91', 'unexpected': True}):
            assert client.post('/api/ble-bases', json=body).status_code == 422
        assert created == []

        listing['available'], listing['error'] = False, 'Synthetic missing serial dependency'
        assert client.post('/api/ble-bases', json={'port': 'COM91'}).status_code == 409
        listing['available'], listing['error'] = True, None
        response = client.post('/api/ble-bases', json={'port': 'com91'})
        assert response.status_code == 200 and response.json()['port'] == 'COM91'
        assert client.post('/api/ble-bases', json={'port': 'COM91'}).status_code == 200
        assert len(created) == 1  # Repeated clicks cannot open one COM port twice.
        assert client.post('/api/ble-bases', json={'port': 'COM92'}).status_code == 409
        wait_for(lambda: client.get('/api/live').json()['nodes'].get('DE0000000001', {}).get('samples') == 128)
        live = client.get('/api/live').json()
        assert live['receiver']['tcp_enabled']
        assert live['receiver']['ble_bases'][0]['port'] == 'COM91'
        assert client.get('/api/health').json()['active_devices'] == 1

        assert client.delete('/api/ble-bases/com91').status_code == 200
        assert serial_instances[0][0].closed
        assert client.get('/api/live').json()['nodes']['DE0000000001']['connected'] is False
        assert client.delete('/api/ble-bases/COM91').status_code == 404
        assert client.post('/api/ble-bases', json={'port': 'COM92'}).status_code == 200
        wait_for(lambda: client.get('/api/live').json()['nodes'].get('DE0000000002', {}).get('samples') == 128)
    assert len(created) == 2
    assert all(instances[0].closed for instances in serial_instances)
    assert all(not node.connected and not node.recording_active for node in app.state.store.nodes.values())
    assert app.state.bridge.serial_bases == {}


def test_combined_api_keeps_tcp_and_other_ble_base_live_during_reconnect(tmp_path, monkeypatch):
    """Exercise the dashboard's real API with two TCP streams and two USB bases."""
    listing = {'available': True, 'error': None, 'ports': [
        {'port': f'COM9{i}', 'description': 'Synthetic BLE base', 'hwid': 'TEST'}
        for i in (1, 2)]}
    serial_streams = {}
    batch = b''.join(wave(first) for first in range(0, 1024, 128))

    def build_base(bridge, port):
        device = 'DE000000000' + port[-1]
        frames = envelope(2, 0, 100, metadata(device))
        frames += b''.join(envelope(1, i + 1, 100, wave(i * 128)) for i in range(8))
        factory, instances = fake_factory(frames)
        serial_streams[port] = instances
        return SerialBase(bridge, port, serial_factory=factory)

    monkeypatch.setattr(serial_base, 'list_serial_ports', lambda: listing)
    monkeypatch.setattr(serial_base, 'SerialBase', build_base)
    settings = make_settings(tmp_path)
    app = create_app(settings)
    peers = []
    with TestClient(app) as client:
        try:
            # Wi-Fi and Ethernet both deliver MSAC1 over the same TCP listener.
            for i in (3, 4):
                peer = socket.create_connection(('127.0.0.1', settings.tcp_port), timeout=2)
                peers.append(peer)
                peer.sendall(json.dumps({**hello(f'DE000000000{i}'), 'transport': 'tcp-plain-v1'}).encode() + b'\n')
                assert peer.recv(3) == b'OK\n'
                peer.sendall(batch)
            for port in ('COM91', 'COM92'):
                assert client.post('/api/ble-bases', json={'port': port}).status_code == 200

            def received(expected):
                nodes = client.get('/api/live').json()['nodes']
                return all(nodes.get(device, {}).get('samples') == count for device, count in expected.items())

            devices = [f'DE000000000{i}' for i in (1, 2, 3, 4)]
            wait_for(lambda: received(dict.fromkeys(devices, 1024)))
            initial = client.get('/api/live').json()
            assert initial['receiver']['tcp_enabled']
            assert len(initial['receiver']['ble_bases']) == 2
            assert client.get('/api/health').json()['active_devices'] == 4
            assert len({node['recording'] for node in initial['nodes'].values()}) == 4
            for device in devices:
                assert initial['nodes'][device]['online']
                assert client.get(f'/api/waveform/{device}?channels=0').json()['channels']['0']
                assert client.get(f'/api/fft/{device}').json()['amplitudes']

            assert client.delete('/api/ble-bases/COM91').status_code == 200
            assert client.get('/api/health').json()['active_devices'] == 3
            old = initial['nodes'][devices[0]]
            assert client.get('/api/recordings/' + old['recording']).content == batch
            serial_streams['COM92'][0].feed(envelope(1, 9, 100, wave(1024)))
            for peer in peers:
                peer.sendall(wave(1024))
            wait_for(lambda: received(dict.fromkeys(devices[1:], 1152)))
            current = client.get('/api/live').json()['nodes']
            assert not current[devices[0]]['connected']
            for device in devices[1:]:
                assert current[device]['online']
                assert current[device]['session_id'] == initial['nodes'][device]['session_id']
                assert current[device]['discontinuities'] == 0

            assert client.post('/api/ble-bases', json={'port': 'COM91'}).status_code == 200
            wait_for(lambda: client.get('/api/health').json()['active_devices'] == 4)
            wait_for(lambda: received({devices[0]: 1024}))
            reconnected = client.get('/api/live').json()['nodes'][devices[0]]
            assert reconnected['session_id'] != old['session_id']
            assert reconnected['recording'] != old['recording']
        finally:
            for peer in peers:
                peer.close()
    paths = list(settings.recordings.glob('*.msac'))
    assert len(paths) == 5
    for path in paths:
        device = json.loads(path.with_suffix('.json').read_text())['device']
        assert path.read_bytes() == batch + (b'' if device == devices[0] else wave(1024))
