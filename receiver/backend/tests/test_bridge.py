import asyncio
import json
import math
from pathlib import Path
import socket
import struct
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.server import BleFrames, Bridge, Settings, create_app, validate_hello
from backend.simulate_node import status_packet, waveform_packet
from backend.store import LiveStore, load_calibration
from backend.vendor.protocol import HEADER, unpack

DEVICE = '38F5F090A994'
HELLO = {'protocol': 'MSAC1', 'device': DEVICE, 'rate': 10000, 'calibration': {'slopes': [123], 'units': 'unknown'}}


def rows(first=0, rate=10000):
    return [[round(3000 * math.sin(2 * math.pi * 50 * (first + i) / rate + ch) + 123) for ch in range(8)] for i in range(128)]


def connect(store=None):
    store = store or LiveStore()
    return store, store.connect(HELLO, 'TCP', 'test-session')


def accept(node, first=0, rate=10000, epoch=1, selectors=0):
    wire = waveform_packet(rows(first, rate), first, rate, epoch, selectors)
    node.accept(*unpack(wire))
    return wire


def settings(tmp_path, **kwargs):
    config = tmp_path / 'calibration.json'
    config.write_text('{"devices": {}}')
    return Settings(tcp_bind='127.0.0.1', tcp_port=0, recordings=tmp_path / 'recordings',
                    calibration=config, minimum_free_bytes=0, **kwargs)


def test_handshake_validation():
    assert validate_hello(HELLO)['device'] == DEVICE
    for bad in [{}, {'protocol': 'TLS'}, {**HELLO, 'device': '../bad'}, {**HELLO, 'calibration': []}, {**HELLO, 'rate': float('nan')}]:
        with pytest.raises(ValueError):
            validate_hello(bad)


@pytest.mark.parametrize('rate', [1000, 2000, 10000])
def test_original_rice_and_crc(rate):
    source = rows(rate=rate)
    wire = waveform_packet(source, rate=rate)
    meta, decoded = unpack(wire)
    assert decoded == source and meta['rate'] == rate
    damaged = bytearray(wire); damaged[-1] ^= 1
    with pytest.raises(ValueError, match='CRC'):
        unpack(bytes(damaged))


def test_counts_default_and_firmware_slopes_not_guessed():
    store, node = connect()
    accept(node)
    data = store.snapshot()['nodes'][DEVICE]
    assert data['online'] and data['units']['0'] == 'counts'
    assert not any(data['calibrated'].values())
    assert data['readings']['0'] == pytest.approx(np.std(np.asarray(rows())[:, 0]))
    assert data['calibration_metadata'] == HELLO['calibration']


def test_verified_scale_and_matching_selector_only(tmp_path):
    config = {'devices': {DEVICE: {'profiles': {'0': {'channels': {'0': {'unit': 'A', 'units_per_count': 0.001, 'verified': True}}}}}}}
    path = tmp_path / 'calibration.json'; path.write_text(json.dumps(config))
    store, node = connect(LiveStore(load_calibration(path)))
    accept(node)
    summary = store.summary(node)
    assert summary['readings']['0'] == pytest.approx(summary['raw_rms']['0'] * 0.001)
    assert summary['units']['0'] == 'A'
    assert summary['units']['1'] == 'counts'
    accept(node, first=128, selectors=1)
    assert store.summary(node)['units']['0'] == 'counts'
    assert node.count == 128


def test_bad_calibration_fails_loudly(tmp_path):
    path = tmp_path / 'calibration.json'
    path.write_text(json.dumps({'devices': {DEVICE: {'profiles': {'0': {'channels': {'0': {'unit': 'A', 'units_per_count': None, 'verified': True}}}}}}}))
    with pytest.raises(ValueError, match='verified'):
        load_calibration(path)


@pytest.mark.parametrize('rate', [1000, 2000, 10000])
def test_ring_holds_exact_last_two_seconds(rate):
    _, node = connect()
    for first in range(0, rate * 3 + 256, 128):
        accept(node, first, rate)
    assert node.count == rate * 2
    all_rows = np.asarray([[round(3000 * math.sin(2 * math.pi * 50 * i / rate + ch) + 123) for ch in range(8)] for i in range(node.next_index)])
    np.testing.assert_array_equal(node.recent(), all_rows[-rate * 2:])
    np.testing.assert_array_equal(node.recent(97), all_rows[-97:])


def test_gap_epoch_and_mode_reset_window():
    store, node = connect()
    accept(node); accept(node, 128)
    accept(node, 512)
    assert node.discontinuities == 1 and node.count == 128
    accept(node, 0, epoch=2)
    assert node.count == 128 and node.discontinuities == 1
    accept(node, 128, epoch=2, selectors=3)
    assert node.count == 128 and node.selectors == 3
    assert store.summary(node)['samples'] == 640


def test_first_packet_nonzero_is_reported_as_gap():
    _, node = connect()
    accept(node, 256)
    assert node.discontinuities == 1


def test_stale_and_disconnected_values_are_not_live():
    store, node = connect()
    accept(node)
    node.last_wave -= 5
    assert not store.summary(node)['online']
    assert store.summary(node)['readings']['0'] is None
    assert not store.waveform(DEVICE, [0], 200, 300, True)['channels']
    accept(node, 128)
    node.connected = False
    assert store.summary(node)['readings']['0'] is None


def test_waveform_bounds_centering_and_peak_preservation():
    store, node = connect()
    for first in range(0, 10240, 128):
        accept(node, first)
    wave = store.waveform(DEVICE, [0, 4], 1000, 200, True)
    assert wave['source_samples'] == 10000
    assert wave['decimation'] == 'min-max-envelope'
    assert len(wave['channels']['0']) <= 200
    assert all(-1000 <= point[0] <= 0 for point in wave['channels']['0'])
    raw = node.recent(10000)[:, 0].astype(float)
    assert max(y for _, y in wave['channels']['0']) == pytest.approx(max(raw - raw.mean()))
    assert [x for x, _ in wave['channels']['0']] == sorted(x for x, _ in wave['channels']['0'])


def test_duplicate_device_cannot_overwrite_active_session():
    store, _ = connect()
    with pytest.raises(ValueError, match='already'):
        store.connect(HELLO, 'TCP', 'other-session')
    assert store.nodes[DEVICE].session_id == 'test-session'


def test_status_remains_separate_from_waveform():
    store, node = connect()
    accept(node)
    node.accept(*unpack(status_packet(status={'fault': 5, 'running': False, 'missed_adc': 1})))
    summary = store.summary(node)
    assert summary['fault'] == 5 and summary['running'] is False
    assert summary['rms_window_samples'] == 128
    assert summary['status']['missed_adc'] == 1


def test_tcp_fragmentation_coalescing_reconnect_and_recording(tmp_path):
    async def scenario():
        config = settings(tmp_path)
        store = LiveStore()
        bridge = Bridge(config, store)
        await bridge.start()
        wires = [waveform_packet(rows(0)), status_packet(), waveform_packet(rows(128), 128)]
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', config.tcp_port)
            hello = json.dumps(HELLO).encode() + b'\n'
            writer.write(hello[:7]); await writer.drain(); writer.write(hello[7:]); await writer.drain()
            assert await reader.readline() == b'OK\n'
            batch = b''.join(wires)
            for start in range(0, len(batch), 37):
                writer.write(batch[start:start + 37])
            await writer.drain()
            for _ in range(100):
                if store.nodes[DEVICE].samples == 256:
                    break
                await asyncio.sleep(0.01)
            assert store.nodes[DEVICE].samples == 256
            assert store.summary(store.nodes[DEVICE])['online']
            writer.close(); await writer.wait_closed(); await asyncio.sleep(0.05)
            assert not store.nodes[DEVICE].connected
            captured = list(config.recordings.glob('*.msac'))
            assert len(captured) == 1 and captured[0].read_bytes() == batch
            reader2, writer2 = await asyncio.open_connection('127.0.0.1', config.tcp_port)
            writer2.write(hello); await writer2.drain()
            assert await reader2.readline() == b'OK\n'
            assert store.nodes[DEVICE].samples == 0
            writer2.close(); await writer2.wait_closed()
        finally:
            await bridge.stop()
    asyncio.run(scenario())


def test_crc_error_never_enters_recording_or_readings(tmp_path):
    config = settings(tmp_path)
    store = LiveStore(); bridge = Bridge(config, store)
    node, recording = bridge.begin(HELLO, 'TCP')
    wire = bytearray(waveform_packet(rows())); wire[-1] ^= 2
    with pytest.raises(ValueError, match='CRC'):
        bridge.packet(node, recording, bytes(wire))
    recording.close()
    assert node.samples == 0
    assert next(config.recordings.glob('*.msac')).stat().st_size == 0


def test_recording_rotation_keeps_exact_packet_bytes(tmp_path):
    config = settings(tmp_path, recording_part_bytes=1000)
    store = LiveStore(); bridge = Bridge(config, store)
    node, recording = bridge.begin(HELLO, 'TCP')
    wires = [waveform_packet(rows(i * 128), i * 128) for i in range(3)]
    for wire in wires:
        bridge.packet(node, recording, wire)
    recording.close()
    paths = sorted(config.recordings.glob('*.msac'))
    assert len(paths) > 1
    assert b''.join(p.read_bytes() for p in paths) == b''.join(wires)


def test_low_disk_space_stops_instead_of_dropping_data(tmp_path):
    config = settings(tmp_path)
    config.minimum_free_bytes = 10**30
    store = LiveStore(); bridge = Bridge(config, store)
    node, recording = bridge.begin(HELLO, 'TCP')
    try:
        with pytest.raises(OSError, match='free disk'):
            bridge.packet(node, recording, waveform_packet(rows()))
        assert node.samples == 0
    finally:
        recording.close()


def test_ble_fragment_reassembly_and_order():
    wire = waveform_packet(rows())
    assembly = BleFrames(); result = None
    for start in range(0, len(wire), 91):
        result = assembly.feed(struct.pack('<IHH', 0, start, len(wire)) + wire[start:start + 91])
    assert result == (0, wire)
    with pytest.raises(ValueError, match='discontinuity'):
        assembly.feed(struct.pack('<IHH', 3, 0, len(wire)) + wire[:91])


def test_api_validation_no_cache_offline_and_real_tcp(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        assert client.get('/api/health').json()['ok']
        assert client.get('/api/live').json()['nodes'] == {}
        receiver = client.get('/api/live').json()['receiver']
        assert receiver['ble'] == 'disabled' and receiver['ble_name'] == ''
        assert receiver['tcp_port'] == app.state.bridge.settings.tcp_port
        assert client.get('/api/live').headers['cache-control'] == 'no-store'
        assert client.get('/api/waveform/unknown').status_code == 404
        assert client.get('/api/waveform/unknown?channels=99').status_code == 422
        assert client.get('/api/waveform/unknown?window_ms=99999').status_code == 422
        assert client.get('/api/does-not-exist').status_code == 404
        with socket.create_connection(('127.0.0.1', app.state.bridge.settings.tcp_port), timeout=2) as peer:
            peer.sendall(json.dumps(HELLO).encode() + b'\n')
            assert peer.recv(3) == b'OK\n'
            peer.sendall(waveform_packet(rows()) + status_packet())
            for _ in range(100):
                snapshot = client.get('/api/live').json()['nodes'].get(DEVICE, {})
                if snapshot.get('samples') == 128:
                    break
                time.sleep(0.01)
            assert snapshot['online'] and snapshot['units']['0'] == 'counts'
            wave = client.get(f'/api/waveform/{DEVICE}?channels=0,4&max_points=32').json()
            assert len(wave['channels']['0']) <= 32
        for _ in range(100):
            if not client.get('/api/live').json()['nodes'][DEVICE]['online']:
                break
            time.sleep(0.01)
        assert client.get('/api/live').json()['nodes'][DEVICE]['readings']['0'] is None


def test_gap_is_not_hidden_by_selector_change():
    _, node = connect()
    accept(node)
    accept(node, 384, selectors=1)
    assert node.discontinuities == 1 and node.count == 128


def test_mock_ble_start_ack_and_recording(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from backend.server import SERVICE, CONTROL, DATA

    writes = []
    wire = waveform_packet(rows())

    class Scanner:
        @staticmethod
        async def find_device_by_filter(predicate, timeout):
            device = SimpleNamespace(name='MSAI-TEST')
            advert = SimpleNamespace(local_name='MSAI-TEST', service_uuids=[SERVICE])
            assert predicate(device, advert)
            return device

    class Client:
        def __init__(self, device):
            self.is_connected = True
            self.notify = None
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            self.is_connected = False
        async def read_gatt_char(self, characteristic):
            assert characteristic == CONTROL
            return json.dumps({**HELLO, 'transport': 'ble-plain-v1'}).encode()
        async def start_notify(self, characteristic, callback):
            assert characteristic == DATA
            self.notify = callback
        async def write_gatt_char(self, characteristic, data, response):
            assert characteristic == CONTROL and response
            writes.append(bytes(data))
            if data == b'START':
                for start in range(0, len(wire), 90):
                    self.notify(None, struct.pack('<IHH', 0, start, len(wire)) + wire[start:start + 90])
            else:
                self.is_connected = False

    monkeypatch.setitem(sys.modules, 'bleak', SimpleNamespace(BleakClient=Client, BleakScanner=Scanner))

    async def scenario():
        config = settings(tmp_path, tcp_enabled=False, ble_name='MSAI-TEST')
        store = LiveStore(); bridge = Bridge(config, store)
        await bridge.start()
        try:
            for _ in range(100):
                if len(writes) == 2:
                    break
                await asyncio.sleep(0.01)
            assert writes == [b'START', struct.pack('<Q', 0)]
            assert store.nodes[DEVICE].samples == 128
            assert store.nodes[DEVICE].transport == 'BLE/GATT'
        finally:
            await bridge.stop()
        assert next(config.recordings.glob('*.msac')).read_bytes() == wire
    asyncio.run(scenario())
