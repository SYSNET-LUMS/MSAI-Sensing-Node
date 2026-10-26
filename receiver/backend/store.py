"""Bounded raw buffers; node-provided unit conversion; RMS uses every sample."""
from __future__ import annotations

from dataclasses import dataclass, field
from copy import copy
from threading import RLock
from functools import wraps
from datetime import datetime, timezone
import json
import math
import os
import tempfile
from pathlib import Path
import time
from typing import Any

import numpy as np


def synchronized(fn):
    @wraps(fn)
    def call(self, *args, **kwargs):
        with self.lock:
            return fn(self, *args, **kwargs)
    return call


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def firmware_conversion(metadata: dict, ch: int, selectors: int | None) -> dict:
    """MSAC1 hello slopes are engineering units per decoded integer count.

    Reference calibration is separate from the availability of a nominal scale.
    Never reuse a CR scale for SCT (or vice versa) after a selector change.
    """
    result = {'unit': 'counts', 'scale': 1.0, 'offset': 0.0,
              'verified': False, 'source': 'raw', 'reason': 'Missing or invalid node calibration'}
    expected = 'A' if ch < 4 else ('V' if ch < 7 else 'mV')
    if selectors not in (0, 1, 2):
        return {**result, 'reason': 'Waiting for a valid sensor selection'}
    if ch < 4 and metadata.get('ct_model') != ('SCT013-030' if selectors == 1 else 'CR3110-3000'):
        return {**result, 'reason': 'Sensor selection does not match node calibration'}
    slopes = metadata.get('slopes')
    units = metadata.get('units')
    units = [unit.strip() for unit in units.split(',')] if isinstance(units, str) else units
    if not isinstance(slopes, list) or len(slopes) != 8 or not isinstance(units, list) or len(units) != 8:
        return result
    scale = slopes[ch]
    if units[ch] != expected or type(scale) not in (int, float) or not math.isfinite(scale) or not 1e-15 <= abs(scale) <= 1e9:
        return result
    flags = metadata.get('reference_calibrated')
    verified = isinstance(flags, list) and len(flags) == 8 and flags[ch] is True
    return {'unit': expected, 'scale': scale, 'offset': 0.0,
            'verified': verified, 'source': 'firmware', 'reason': None}


def load_calibration(path: Path) -> dict:
    if not path.exists():
        raise ValueError(f'Calibration configuration missing: {path}')
    data = json.loads(path.read_text(encoding='utf-8'))
    devices = data.get('devices') if isinstance(data, dict) else None
    if not isinstance(devices, dict):
        raise ValueError('calibration.json must contain a devices object')
    normalized = {}
    for device, settings in devices.items():
        if len(device) != 12 or any(c not in '0123456789abcdefABCDEF' for c in device):
            raise ValueError(f'Invalid calibration device ID: {device}')
        if not isinstance(settings, dict) or not isinstance(settings.get('profiles', {}), dict):
            raise ValueError(f'Invalid settings for {device}')
        for mode, profile in settings.get('profiles', {}).items():
            if mode not in ('0', '1', '2', '3') or not isinstance(profile, dict):
                raise ValueError(f'Invalid selector profile for {device}')
            channels = profile.get('channels', {})
            if not isinstance(channels, dict):
                raise ValueError('channels must be an object')
            for ch, config in channels.items():
                if ch not in tuple(map(str, range(8))) or not isinstance(config, dict):
                    raise ValueError('Invalid calibration channel')
                expected = 'A' if int(ch) < 4 else ('V' if int(ch) < 7 else 'mV')
                if config.get('unit', expected) != expected:
                    raise ValueError(f'CH{ch} must use {expected}; verify the physical channel mapping')
                if type(config.get('verified', False)) is not bool:
                    raise ValueError('verified must be true or false')
                factor = config.get('units_per_count')
                offset = config.get('offset_counts', 0)
                if not isinstance(offset, (int, float)) or not math.isfinite(offset):
                    raise ValueError('offset_counts must be finite')
                if factor is not None and (type(factor) not in (int, float) or not math.isfinite(factor) or not 1e-15 <= abs(factor) <= 1e9):
                    raise ValueError('units_per_count must be a finite nonzero scale, or null')
                if config.get('verified') and factor is None:
                    raise ValueError('A verified calibration requires units_per_count')
        normalized[device.upper()] = settings
    return normalized


@dataclass
class Node:
    lock: Any = field(default_factory=RLock, init=False, repr=False)
    device: str
    calibration_meta: dict
    transport: str
    name: str
    session_id: str
    connected: bool = True
    rate: int = 0
    epoch: int | None = None
    selectors: int | None = None
    next_index: int = 0
    last_wave: float = 0.0
    last_status: float = 0.0
    last_seen: str | None = None
    status: dict = field(default_factory=dict)
    samples: int = 0
    blocks: int = 0
    discontinuities: int = 0
    receiver_errors: int = 0
    error: str | None = None
    recording: str | None = None
    recording_active: bool = False
    # Maximum two seconds at the highest supported rate; raw counts stay exact.
    buffer: np.ndarray = field(default_factory=lambda: np.zeros((20000, 8), dtype=np.int32))
    position: int = 0
    count: int = 0
    window_version: int = 0
    max_arrival_gap_ms: float = 0.0
    last_arrival_gap_ms: float = 0.0
    stream_delay_ms: float = 0.0
    clock_epoch: int | None = None
    clock_floor: float | None = None
    received_bytes: int = 0
    bandwidth_started: float = field(default_factory=time.monotonic)
    bandwidth_bytes: int = 0
    bandwidth_bps: float | None = None
    device_uptime_seconds: float | None = None
    boot_time_estimate: float | None = None
    processing_ms_max: float = 0.0

    @synchronized
    def clear_window(self) -> None:
        self.position = self.count = 0
        self.window_version += 1

    @synchronized
    def accept(self, meta: dict, payload: dict | list) -> None:
        self.last_seen = utc_now()
        if 'time_us' in meta:
            uptime = meta['time_us'] / 1e6
            estimate = time.time() - uptime
            if self.device_uptime_seconds is None:
                self.boot_time_estimate = estimate
            else:
                self.boot_time_estimate = min(self.boot_time_estimate, estimate)
            # Status may overtake queued waveform samples. A reconnect creates
            # a new Node, so older packets must not move this session backwards.
            self.device_uptime_seconds = max(self.device_uptime_seconds or 0, uptime)
        if isinstance(payload, dict):
            # Preserve firmware status verbatim for diagnosis. Do not manufacture RMS.
            self.status = payload
            self.last_status = time.monotonic()
            return
        changed = (self.epoch, self.rate, self.selectors) != (meta['epoch'], meta['rate'], meta['selectors'])
        if changed:
            self.clear_window()
            if self.epoch != meta['epoch']:
                self.next_index = 0
        if meta['first'] != self.next_index:
            self.discontinuities += 1
            self.clear_window()  # Never calculate or draw across a discontinuity.
        self.epoch, self.rate, self.selectors = meta['epoch'], meta['rate'], meta['selectors']
        values = np.asarray(payload, dtype=np.int32)
        size = len(values)
        capacity = min(len(self.buffer), self.rate * 2)
        take = min(size, capacity - self.position)
        self.buffer[self.position:self.position + take] = values[:take]
        if take < size:
            self.buffer[:size - take] = values[take:]
        self.position = (self.position + size) % capacity
        self.count = min(capacity, self.count + size)
        self.next_index = meta['first'] + size
        self.samples += size
        self.blocks += 1
        now = time.monotonic()
        self.last_arrival_gap_ms = (now - self.last_wave) * 1000 if self.last_wave else 0.0
        self.max_arrival_gap_ms = max(self.max_arrival_gap_ms, self.last_arrival_gap_ms)
        # Relative delay uses device uptime, not an assumed synchronized UTC clock.
        transit = now - (meta.get('time_us', 0) / 1e6 + size / self.rate)
        if self.clock_epoch != self.epoch:
            self.clock_floor = transit
            self.clock_epoch = self.epoch
        self.clock_floor = min(self.clock_floor, transit)
        self.stream_delay_ms = max(0.0, (transit - self.clock_floor) * 1000)
        self.last_wave = now

    @synchronized
    def recent(self, n: int | None = None) -> np.ndarray:
        count = self.count if n is None else min(self.count, n)
        if not count:
            return np.empty((0, 8), dtype=np.int32)
        capacity = min(len(self.buffer), self.rate * 2)
        start = (self.position - count) % capacity
        if start + count <= capacity:
            return self.buffer[start:start + count].copy()
        return np.concatenate((self.buffer[start:capacity], self.buffer[:(start + count) % capacity]))


    @synchronized
    def frozen(self, samples: int | None = None):
        """Copy only a bounded contiguous window; never hold a lock during FFT/JSON."""
        result = copy(self)
        result.lock = RLock()
        result.buffer = self.recent(samples)
        result.position = 0
        result.count = len(result.buffer)
        return result


class LiveStore:
    def __init__(self, calibration: dict | None = None, stale_seconds: float = 3.0, max_nodes: int = 32, names_path: Path | None = None):
        self.lock = RLock()
        self.names_path = names_path
        self.names = json.loads(names_path.read_text(encoding='utf-8')) if names_path and names_path.exists() else {}
        if not isinstance(self.names, dict) or any(not isinstance(v, str) for v in self.names.values()):
            raise ValueError('Node names file must contain device/name pairs')
        self.nodes: dict[str, Node] = {}
        self.calibration = calibration or {}
        self.stale_seconds = stale_seconds
        self.max_nodes = max_nodes

    @synchronized
    def connect(self, hello: dict, transport: str, session_id: str) -> Node:
        device = hello['device'].upper()
        old = self.nodes.get(device)
        if old and old.connected:
            raise ValueError(f'Device {device} already has an active connection')
        if old is None and len(self.nodes) >= self.max_nodes:
            inactive = next((key for key, node in self.nodes.items() if not node.connected), None)
            if inactive is None:
                raise ValueError('Maximum active device count reached')
            del self.nodes[inactive]
        node = Node(device, hello.get('calibration', {}), transport,
                    self.names.get(device) or self.calibration.get(device, {}).get('name', f'MSAI-{device[-6:]}'), session_id)
        self.nodes[device] = node
        return node

    @synchronized
    def rename(self, device: str, name: str) -> str:
        device = device.upper()
        name = name.strip()
        if not 1 <= len(name) <= 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError('Use 1â€“80 characters without control characters')
        node = self.nodes[device]
        names = {**self.names, device: name}
        if self.names_path:
            self.names_path.parent.mkdir(parents=True, exist_ok=True)
            # Replace only after a complete write; failed saves leave the old name intact.
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.names_path.parent,
                                                 delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(names, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(self.names_path)
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()
        self.names = names
        node.name = name
        return name

    def view(self, device: str | None = None, window_ms: int | None = None):
        with self.lock:
            nodes = [self.nodes[device.upper()]] if device else list(self.nodes.values())
        result = LiveStore(self.calibration, self.stale_seconds, self.max_nodes)
        for node in nodes:
            with node.lock:
                samples = max(8, node.rate * window_ms // 1000) if window_ms else None
                result.nodes[node.device] = node.frozen(samples)
        return result

    def current_frame(self, frame: dict) -> bool:
        with self.lock:
            node = self.nodes.get(frame['device'])
        if node is None:
            return False
        with node.lock:
            return (self.is_live(node) and node.session_id == frame['session_id']
                    and node.epoch == frame['epoch'] and node.window_version == frame['window_version'])

    def spectrum(self, device: str, channel: int, window_ms: int) -> dict:
        node = self.nodes[device.upper()]
        count = max(8, node.rate * window_ms // 1000)
        result = {'device': node.device, 'online': self.is_live(node), 'channel': channel,
                  'rate': node.rate, 'session_id': node.session_id, 'epoch': node.epoch,
                  'window_version': node.window_version, 'next_index': str(node.next_index),
                  'source_samples': 0, 'required_samples': count, 'window_ms': window_ms,
                  'frequencies': [], 'amplitudes': [], 'window': 'periodic Hann',
                  'amplitude': 'single-sided peak', 'dc_removed': True}
        # Require a complete contiguous window after reconnect, gap or mode change.
        if not result['online'] or node.count < count:
            return result
        values = node.recent(count)[:, channel].astype(np.float64)
        conversion = self.conversions(node)[channel]
        values = (values - values.mean()) * conversion['scale']
        window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(count) / count)
        amplitudes = np.abs(np.fft.rfft(values * window)) * (2 / window.sum())
        amplitudes[0] *= 0.5
        if count % 2 == 0:
            amplitudes[-1] *= 0.5
        frequencies = np.fft.rfftfreq(count, 1 / node.rate)
        peak = int(np.argmax(amplitudes[1:])) + 1
        result.update(source_samples=count, window_ms=count * 1000 / node.rate,
                      bin_hz=node.rate / count, unit=conversion['unit'], conversion=conversion,
                      frequencies=frequencies.tolist(), amplitudes=amplitudes.tolist(),
                      peak_hz=float(frequencies[peak]), peak_amplitude=float(amplitudes[peak]))
        return result

    def is_live(self, node: Node) -> bool:
        return node.connected and node.last_wave > 0 and time.monotonic() - node.last_wave < self.stale_seconds

    def conversions(self, node: Node) -> list[dict]:
        configs = self.calibration.get(node.device, {}).get('profiles', {}).get(str(node.selectors), {}).get('channels', {})
        result = []
        for ch in range(8):
            config = configs.get(str(ch), {})
            conversion = firmware_conversion(node.calibration_meta, ch, node.selectors)
            # Existing explicit, measured device/profile overrides remain supported.
            if node.selectors in (0, 1, 2) and config.get('verified') is True and config.get('units_per_count') is not None:
                conversion = {
                    'unit': config.get('unit', 'A' if ch < 4 else ('V' if ch < 7 else 'mV')),
                    'scale': config['units_per_count'], 'offset': config.get('offset_counts', 0),
                    'verified': True, 'source': 'override', 'reason': None,
                }
            result.append(conversion)
        return result

    def summary(self, node: Node) -> dict:
        online = self.is_live(node)
        values = node.recent().astype(np.float64)
        # AC RMS = sqrt(mean((x-mean(x))**2)). DC offset is intentionally removed.
        rms = np.std(values, axis=0) if len(values) else None
        conversion = self.conversions(node)
        status_fresh = node.last_status > 0 and time.monotonic() - node.last_status < self.stale_seconds
        return {
            'name': node.name, 'online': online, 'connected': node.connected,
            'session_id': node.session_id, 'window_version': node.window_version,
            'received_bytes': node.received_bytes, 'processing_ms_max': round(node.processing_ms_max, 2),
            'bandwidth_bps': node.bandwidth_bps if online else 0,
            'uptime_seconds': node.device_uptime_seconds,
            'boot_time_estimate': node.boot_time_estimate,
            'arrival_gap_ms': round(node.last_arrival_gap_ms, 2),
            'max_arrival_gap_ms': round(node.max_arrival_gap_ms, 2),
            'stream_delay_ms': round(node.stream_delay_ms, 2),
            'rate': node.rate, 'transport': node.transport, 'epoch': node.epoch,
            'selectors': node.selectors, 'last_seen': node.last_seen,
            'waveform_age_ms': round((time.monotonic() - node.last_wave) * 1000) if node.last_wave else None,
            'status_fresh': status_fresh,
            'running': node.status.get('running') if status_fresh else None,
            'fault': node.status.get('fault'), 'status': node.status,
            'readings': {str(ch): float(rms[ch] * abs(c['scale'])) if online and rms is not None else None for ch, c in enumerate(conversion)},
            'raw_rms': {str(ch): float(rms[ch]) if online and rms is not None else None for ch in range(8)},
            'units': {str(ch): c['unit'] for ch, c in enumerate(conversion)},
            'calibrated': {str(ch): c['verified'] for ch, c in enumerate(conversion)},
            'conversions': {str(ch): c for ch, c in enumerate(conversion)},
            'rms_window_samples': len(values),
            'rms_window_ms': round(len(values) * 1000 / node.rate, 2) if node.rate else 0,
            'samples': node.samples, 'blocks': node.blocks,
            'discontinuities': node.discontinuities, 'receiver_errors': node.receiver_errors,
            'error': node.error, 'recording': node.recording, 'recording_active': node.recording_active,
            'calibration_metadata': node.calibration_meta,
        }

    def snapshot(self) -> dict:
        return {'server_time': utc_now(), 'nodes': {key: self.summary(node) for key, node in self.nodes.items()}}

    def waveform(self, device: str, channels: list[int], window_ms: int, max_points: int, center: bool) -> dict:
        node = self.nodes[device.upper()]
        values = node.recent(max(1, node.rate * window_ms // 1000)).astype(np.float64)
        online = self.is_live(node)
        result = {'device': node.device, 'online': online, 'rate': node.rate,
                  'epoch': node.epoch, 'session_id': node.session_id, 'window_version': node.window_version,
                  'next_index': str(node.next_index), 'source_samples': len(values),
                  'window_ms': window_ms, 'centered': center, 'channels': {}, 'units': {}, 'conversions': {},
                  'preview_only': True, 'decimation': 'none'}
        if not online or not len(values):
            return result
        conversions = self.conversions(node)
        # The server returns ordered min/max points per time bucket, not every nth
        # sample. This bounds browser work and retains visible peaks. NOT for FFT.
        buckets = max(1, max_points // 2)
        edges = np.linspace(0, len(values), buckets + 1, dtype=int) if len(values) > max_points else None
        for ch in channels:
            c = conversions[ch]
            series = values[:, ch] - (float(values[:, ch].mean()) if center else c['offset'])
            series *= c['scale']
            if edges is None:
                indices = np.arange(len(values))
            else:
                # Vectorized buckets avoid hundreds of Python/NumPy calls while
                # holding the GIL. Include both extrema, in original time order.
                starts, ends = edges[:-1], edges[1:]
                grid = starts[:, None] + np.arange(int(np.max(ends - starts)))
                valid = grid < ends[:, None]
                parts = series[np.minimum(grid, len(series) - 1)]
                minima = starts + np.argmin(np.where(valid, parts, np.inf), axis=1)
                maxima = starts + np.argmax(np.where(valid, parts, -np.inf), axis=1)
                indices = np.unique(np.concatenate((minima, maxima)))
                result['decimation'] = 'min-max-envelope'
            times = np.round((indices - len(values) + 1) * (1000 / node.rate), 4)
            result['channels'][str(ch)] = np.column_stack((times, series[indices])).tolist()
            result['units'][str(ch)] = c['unit']
            result['conversions'][str(ch)] = c
        return result
