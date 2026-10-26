"""One bounded USB-serial BLE receiver per port, sharing the TCP node pipeline."""
from __future__ import annotations

import json
import threading
import time
from contextlib import suppress

from .serial_wire import Parser, OrderedReceiver


def list_serial_ports():
    try:
        from serial.tools import list_ports
        ports = sorted(list_ports.comports(), key=lambda p: p.device)
        return {'available': True, 'error': None, 'ports': [
            {'port': p.device, 'description': p.description or p.device, 'hwid': p.hwid}
            for p in ports]}
    except (ImportError, OSError) as error:
        return {'available': False, 'error': str(error), 'ports': []}


class SerialBase:
    def __init__(self, bridge, port, serial_factory=None):
        self.bridge, self.port = bridge, port
        self.serial_factory = serial_factory
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.node = self.recording = None
        self.session = self.legacy_sequence = None
        self.state = 'starting'
        self.error = None
        self.connected = False
        self.base = {}
        self.serial_crc_errors = self.discarded_bytes = 0
        self.packet_errors = 0
        self.last_frame = 0.0

    def snapshot(self):
        with self.lock:
            return {'port': self.port, 'state': self.state, 'error': self.error,
                    'connected': self.connected,
                    'device': self.node.device if self.node else None,
                    'base': dict(self.base), 'serial_crc_errors': self.serial_crc_errors,
                    'discarded_bytes': self.discarded_bytes, 'packet_errors': self.packet_errors,
                    'frame_age_s': time.monotonic()-self.last_frame if self.last_frame else None}

    def start(self):
        self.thread = threading.Thread(target=self.run, name=f'ble-usb-{self.port}', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        else:
            self.close_node()
        with self.lock:
            self.connected = False
            self.state = 'stopped'

    def close_node(self):
        with self.lock:
            node, recording = self.node, self.recording
            self.node = self.recording = None
        if node:
            with node.lock:
                node.connected = False
        if recording:
            recording.close()

    def consume(self, kind, sequence, session, payload):
        # Executed serially by this port's worker. Recording completes before ACK.
        from .server import validate_hello
        if session != self.session:
            self.close_node()
            self.session = session
            self.legacy_sequence = None
            with self.lock:
                self.base = {}
        if kind == 2:
            hello = validate_hello(json.loads(payload))
            if hello.get('transport') != 'ble-plain-v1':
                raise ValueError('Base station node metadata has an incompatible BLE protocol')
            if self.node:
                raise ValueError('Duplicate node metadata within a USB session')
            node, recording = self.bridge.begin(hello, f'BLE/USB ({self.port})')
            with self.lock:
                self.node, self.recording = node, recording
                self.state, self.error = 'connected', None
        elif kind == 1:
            if self.node is None or self.recording is None:
                raise ValueError('BLE data arrived before node metadata')
            self.bridge.packet(self.node, self.recording, payload)
            with self.lock:
                self.state, self.error = 'streaming', None
        elif kind == 3:
            status = json.loads(payload)
            if not isinstance(status, dict):
                raise ValueError('Invalid base station status')
            json.dumps(status, allow_nan=False)
            with self.lock:
                self.base = status
                self.state = str(status.get('state', 'connected'))
        else:
            raise ValueError('Unknown USB envelope type')
        with self.lock:
            self.last_frame = time.monotonic()

    def run(self):
        while not self.stop_event.is_set():
            ser = None
            parser = Parser()
            try:
                if self.serial_factory is None:
                    import serial
                    factory = serial.Serial
                else:
                    factory = self.serial_factory
                ser = factory(port=None, baudrate=2000000, timeout=.1, write_timeout=1)
                ser.dtr = ser.rts = False
                ser.port = self.port
                ser.open()
                if hasattr(ser, 'set_buffer_size'):
                    ser.set_buffer_size(rx_size=262144, tx_size=4096)
                self.session = self.legacy_sequence = None
                ordered = OrderedReceiver()
                ser.write(b'HELLO\n')
                opened = heartbeat = time.monotonic()
                with self.lock:
                    self.connected, self.error = True, None
                    self.state = 'waiting for base station'
                    self.last_frame = 0
                while not self.stop_event.is_set():
                    now = time.monotonic()
                    if now-heartbeat >= 1:
                        ser.write(b'PING\n')
                        heartbeat = now
                    if now-(self.last_frame or opened) > 8:
                        raise TimeoutError('No valid base station frames. Check firmware and USB port.')
                    raw = ser.read(min(65536, ser.in_waiting or 1))
                    for frame in parser.feed(raw):
                        if self.stop_event.is_set():
                            break
                        if parser.version == 2:
                            acknowledged = ordered.accept(frame, self.consume)
                            if acknowledged is not None:
                                ser.write(f'ACK {frame[2]} {acknowledged}\n'.encode())
                        else:
                            kind, sequence, session, payload = frame
                            if session != self.session or sequence != self.legacy_sequence:
                                self.consume(*frame)
                                self.legacy_sequence = sequence
                            if kind in (1, 2):
                                ser.write(f'ACK {session} {sequence}\n'.encode())
                    with self.lock:
                        self.serial_crc_errors = parser.crc_errors
                        self.discarded_bytes = parser.discarded_bytes
            except Exception as error:
                # No ACK for invalid metadata/data or failed recording. Closing USB
                # stops heartbeats, and the base disconnects BLE rather than losing data.
                if not self.stop_event.is_set():
                    with self.lock:
                        self.error = str(error) or type(error).__name__
                        self.state = 'disconnected; retrying'
                        self.packet_errors += 1
                    if self.node:
                        with self.node.lock:
                            self.node.error = self.error
                            self.node.receiver_errors += 1
            finally:
                if ser:
                    with suppress(OSError):
                        ser.close()
                try:
                    self.close_node()
                except OSError as error:
                    with self.lock:
                        self.error = f'Recording close failed: {error}'
                        self.state = 'disconnected; retrying'
                with self.lock:
                    self.connected = False
            self.stop_event.wait(2)
