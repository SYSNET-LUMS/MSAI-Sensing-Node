"""Engineering-unit conversion from the real firmware hello convention."""
from copy import deepcopy
import json
import socket
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.server import create_app
from backend.simulate_node import waveform_packet
from backend.store import LiveStore, load_calibration
from backend.vendor.protocol import unpack
from backend.tests.test_bridge import DEVICE, settings


# Captured from node 38F5F090A994; fixture only, never a production default.
CALIBRATION = {
    'slopes': [3.719892123128429e-05, 3.727362544546164e-05,
               3.726496645257227e-05, 3.7331737959916244e-05,
               0.0004119904770050198, 0.0004119904770050198,
               0.0004119904770050198, 0.0001430511474609375],
    'units': 'A,A,A,A,V,V,V,mV', 'ct_model': 'CR3110-3000',
    'reference_calibrated': [True] * 4 + [False] * 4,
}


def hello(calibration=None):
    return {'protocol': 'MSAC1', 'device': DEVICE, 'rate': 10000,
            'calibration': deepcopy(CALIBRATION if calibration is None else calibration)}


def samples():
    # Balanced AC, known DC offset, different amplitudes for each channel.
    return [[100 + (1 if i % 2 else -1) * (ch + 1) * 1000 for ch in range(8)] for i in range(128)]


def connected(calibration=None, selectors=0, store=None):
    store = store or LiveStore()
    node = store.connect(hello(calibration), 'TCP', 'calibration-test')
    node.accept(*unpack(waveform_packet(samples(), selectors=selectors)))
    return store, node


@pytest.mark.parametrize('selector', [0, 2])
def test_cr_factors_applied_once_to_rms_and_waveforms(selector):
    store, node = connected(selectors=selector)
    summary = store.summary(node)
    wave = store.waveform(DEVICE, list(range(8)), 200, 200, False)
    centered = store.waveform(DEVICE, list(range(8)), 200, 200, True)
    for ch, scale in enumerate(CALIBRATION['slopes']):
        key = str(ch)
        assert summary['readings'][key] == pytest.approx((ch + 1) * 1000 * abs(scale))
        assert summary['raw_rms'][key] == pytest.approx((ch + 1) * 1000)
        assert summary['units'][key] == ('A' if ch < 4 else 'V' if ch < 7 else 'mV')
        assert summary['calibrated'][key] == (ch < 4)
        assert summary['conversions'][key]['source'] == 'firmware'
        np.testing.assert_allclose([v for _, v in wave['channels'][key]], np.asarray(samples())[:, ch] * scale)
        np.testing.assert_allclose([v for _, v in centered['channels'][key]], (np.asarray(samples())[:, ch] - 100) * scale)
    np.testing.assert_array_equal(node.recent(), samples())  # Scaling never modifies counts.


def test_sct_uses_its_own_nominal_factors():
    calibration = deepcopy(CALIBRATION)
    calibration.update(ct_model='SCT013-030', slopes=[1.5664938885653974e-05] * 4 + CALIBRATION['slopes'][4:], reference_calibrated=[False] * 8)
    store, node = connected(calibration, selectors=1)
    summary = store.summary(node)
    assert summary['readings']['0'] == pytest.approx(0.015664938885653974)
    assert summary['units']['0'] == 'A' and summary['units']['4'] == 'V'
    assert not any(summary['calibrated'].values())


def test_selector_mismatch_does_not_reuse_cr_current_factors():
    store, node = connected(selectors=1)
    assert store.summary(node)['units']['0'] == 'counts'
    assert store.summary(node)['units']['4'] == 'V'
    assert 'does not match' in store.summary(node)['conversions']['0']['reason']
    node.accept(*unpack(waveform_packet(samples(), first=128, selectors=3)))
    assert set(store.summary(node)['units'].values()) == {'counts'}


@pytest.mark.parametrize('bad', [None, 0, True, '0.001', float('nan'), float('inf'), 1e30])
def test_invalid_factor_falls_back_only_for_affected_channel(bad):
    calibration = deepcopy(CALIBRATION)
    calibration['slopes'][0] = bad
    store, node = connected(calibration)
    summary = store.summary(node)
    assert summary['units']['0'] == 'counts'
    assert summary['readings']['0'] == pytest.approx(1000)
    assert not summary['calibrated']['0']
    assert summary['units']['1'] == 'A'


def test_missing_units_wrong_units_and_missing_reference_flags():
    for units in [None, 'A,V,A,A,V,V,V,mV', ['A']]:
        calibration = deepcopy(CALIBRATION)
        calibration['units'] = units
        store, node = connected(calibration)
        assert store.summary(node)['units']['1'] == 'counts'
    calibration = deepcopy(CALIBRATION)
    calibration.pop('reference_calibrated')
    store, node = connected(calibration)
    assert store.summary(node)['units']['0'] == 'A'
    assert not store.summary(node)['calibrated']['0']


def test_measured_override_wins_and_unverified_entries_use_node_factors(tmp_path):
    path = tmp_path / 'override.json'
    path.write_text(json.dumps({'devices': {DEVICE: {'profiles': {'0': {'channels': {
        '0': {'units_per_count': -0.002, 'offset_counts': 100, 'verified': True},
        '1': {'units_per_count': None, 'verified': False},
    }}}}}}))
    store, node = connected(store=LiveStore(load_calibration(path)))
    summary = store.summary(node)
    assert summary['readings']['0'] == pytest.approx(2)
    assert summary['conversions']['0']['source'] == 'override'
    assert summary['conversions']['1']['source'] == 'firmware'
    wave = store.waveform(DEVICE, [0], 200, 200, False)
    assert wave['channels']['0'][0][1] == pytest.approx(2)
    node.accept(*unpack(waveform_packet(samples(), first=128, selectors=2)))
    assert store.summary(node)['conversions']['0']['source'] == 'firmware'


def test_reconnection_refreshes_calibration():
    store, node = connected()
    node.connected = False
    calibration = deepcopy(CALIBRATION)
    calibration['slopes'][0] *= 2
    _, new_node = connected(calibration, store=store)
    assert store.summary(new_node)['readings']['0'] == pytest.approx(2000 * CALIBRATION['slopes'][0])


def test_tcp_to_api_converts_values_and_keeps_recording_exact(tmp_path):
    config = settings(tmp_path)
    app = create_app(config)
    wire = waveform_packet(samples())
    with TestClient(app) as client:
        with socket.create_connection(('127.0.0.1', config.tcp_port), timeout=2) as peer:
            peer.sendall(json.dumps(hello()).encode() + b'\n')
            assert peer.recv(3) == b'OK\n'
            peer.sendall(wire)
            for _ in range(100):
                summary = client.get('/api/live').json()['nodes'].get(DEVICE, {})
                if summary.get('samples') == 128:
                    break
                time.sleep(0.01)
            assert summary['readings']['0'] == pytest.approx(0.03719892123128429)
            assert summary['readings']['4'] == pytest.approx(2.059952385025099)
            assert summary['units']['4'] == 'V' and not summary['calibrated']['4']
            wave = client.get(f'/api/waveform/{DEVICE}?channels=0,4&center=false').json()
            assert wave['channels']['4'][0][1] == pytest.approx(-4900 * CALIBRATION['slopes'][4])
    assert next(config.recordings.glob('*.msac')).read_bytes() == wire
