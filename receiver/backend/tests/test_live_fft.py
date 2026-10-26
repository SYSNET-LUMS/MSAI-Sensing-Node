import asyncio
import json
import math
import socket
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from backend.server import Bridge, create_app
from backend.store import LiveStore
from backend.simulate_node import waveform_packet
from backend.vendor.protocol import unpack
from backend.tests.test_bridge import HELLO, DEVICE, accept, settings


@pytest.mark.parametrize('rate', [1000, 2000, 10000])
@pytest.mark.parametrize('window_ms', [50, 100, 200, 1000])
def test_fft_raw_frequency_amplitude_and_sliding_window(rate, window_ms):
    store = LiveStore({DEVICE: {'profiles': {'0': {'channels': {'0': {'unit': 'A', 'units_per_count': .001, 'verified': True}}}}}})
    node = store.connect(HELLO, 'TCP', 'fft-test')
    n = rate * window_ms // 1000
    frequency = (1000 / window_ms) * 5
    # Five coherent cycles; exact peak scaling, with large DC offset removed.
    for first in range(0, n + 256, 128):
        rows = [[round(5000 + 2000 * math.sin(2 * math.pi * frequency * (first + i) / rate))] * 8 for i in range(128)]
        node.accept(*unpack(waveform_packet(rows, first=first, rate=rate)))
    frame = store.spectrum(DEVICE, 0, window_ms)
    assert frame['source_samples'] == n
    assert frame['bin_hz'] == pytest.approx(rate / n)
    assert frame['peak_hz'] == pytest.approx(frequency)
    assert frame['peak_amplitude'] == pytest.approx(2, abs=.002)
    assert frame['amplitudes'][0] < .002
    assert frame['unit'] == 'A'
    index = frame['next_index']
    accept(node, node.next_index, rate=rate)
    assert int(store.spectrum(DEVICE, 0, window_ms)['next_index']) == int(index) + 128


def test_fft_gap_staleness_reconnect_and_nyquist():
    store = LiveStore(); node = store.connect(HELLO, 'TCP', 'a')
    for first in range(0, 1024, 128): accept(node, first)
    assert store.spectrum(DEVICE, 0, 100)['amplitudes']
    accept(node, 2048)
    assert not store.spectrum(DEVICE, 0, 100)['amplitudes']
    for first in range(2176, 3200, 128): accept(node, first)
    node.last_wave -= 5
    assert not store.spectrum(DEVICE, 0, 100)['amplitudes']
    node.connected = False
    node = store.connect(HELLO, 'TCP', 'b')
    assert not store.spectrum(DEVICE, 0, 100)['amplitudes']
    for first in range(0, 1024, 128):
        rows = [[1000 * (-1)**(first+i)] * 8 for i in range(128)]
        node.accept(*unpack(waveform_packet(rows, first=first)))
    assert store.spectrum(DEVICE, 0, 100)['amplitudes'][-1] == pytest.approx(1000)


def test_name_persists_reconnect_restart_and_failed_save(tmp_path, monkeypatch):
    path = tmp_path / 'names.json'
    store = LiveStore(names_path=path); node = store.connect(HELLO, 'TCP', 'one')
    assert store.rename(DEVICE.lower(), '  Pump room / Motor 01  ') == 'Pump room / Motor 01'
    node.connected = False
    assert store.connect(HELLO, 'TCP', 'two').name == 'Pump room / Motor 01'
    assert LiveStore(names_path=path).connect(HELLO, 'TCP', 'three').name == 'Pump room / Motor 01'
    from pathlib import Path
    def fail(*args): raise OSError('disk error')
    monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(OSError): store.rename(DEVICE, 'Wrong')
    assert store.nodes[DEVICE].name == 'Pump room / Motor 01'
    assert json.loads(path.read_text())[DEVICE] == 'Pump room / Motor 01'


def test_two_simultaneous_nodes_independent_fft_recording_and_api(tmp_path):
    app = create_app(settings(tmp_path))
    second = 'DE0000000002'
    with TestClient(app) as client:
        with socket.create_connection(('127.0.0.1', app.state.bridge.settings.tcp_port), timeout=2) as a, socket.create_connection(('127.0.0.1', app.state.bridge.settings.tcp_port), timeout=2) as b:
            for peer, device, freq in [(a, DEVICE, 50), (b, second, 120)]:
                peer.sendall(json.dumps({**HELLO, 'device': device}).encode() + b'\n')
                assert peer.recv(3) == b'OK\n'
                for first in range(0, 2048, 128):
                    values = [[round(3000*math.sin(2*math.pi*freq*(first+i)/10000))]*8 for i in range(128)]
                    peer.sendall(waveform_packet(values, first=first))
            for _ in range(100):
                snapshot = client.get('/api/live').json()['nodes']
                if len(snapshot) == 2 and all(n['samples'] == 2048 for n in snapshot.values()): break
                time.sleep(.01)
            assert client.get('/api/health').json()['active_devices'] == 2
            for device, freq in [(DEVICE, 50), (second, 120)]:
                frame = client.get(f'/api/fft/{device}').json()
                assert frame['online'] and frame['peak_hz'] == freq
            assert snapshot[DEVICE]['recording'] != snapshot[second]['recording']
            assert client.put(f'/api/nodes/{DEVICE}/name', json={'name': 'Room A / Motor 1'}).status_code == 200
            assert client.get('/api/live').json()['nodes'][DEVICE]['name'] == 'Room A / Motor 1'
            for name in ['', '   ', 'x'*81, 'line\nbreak']:
                assert client.put(f'/api/nodes/{DEVICE}/name', json={'name': name}).status_code == 422
            assert client.get(f'/api/fft/{DEVICE}?channel=8').status_code == 422
            assert client.get(f'/api/fft/{DEVICE}?window_ms=49').status_code == 422
            assert client.get('/api/fft/unknown').status_code == 404
            a.shutdown(socket.SHUT_RDWR)
            for _ in range(100):
                if client.get('/api/health').json()['active_devices'] == 1: break
                time.sleep(.01)
            assert client.get('/api/fft/' + second).json()['online']
            assert not client.get('/api/fft/' + DEVICE).json()['online']
        assert len(list(app.state.bridge.settings.recordings.glob('*.msac'))) == 2
