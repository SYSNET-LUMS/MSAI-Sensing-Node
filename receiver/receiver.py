"""Windows/Linux/cloud VM receiver for the MSAI sensing-node firmware."""
import argparse
import asyncio
import datetime as dt
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import ssl
import struct
import time
from protocol import HEADER, MAX_PAYLOAD, unpack

SERVICE = 'd0be1001-63f0-4dd1-a28d-e0201450a001'
DATA = 'd0be1002-63f0-4dd1-a28d-e0201450a001'
AUTH = 'd0be1003-63f0-4dd1-a28d-e0201450a001'

def emit(event, **values):
    print(json.dumps(dict(time=dt.datetime.now(dt.timezone.utc).isoformat(), type=event, **values)), flush=True)

def initialize(folder, host, port):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    folder.mkdir(parents=True, exist_ok=True)
    if any((folder / name).exists() for name in ('server.key', 'server.crt', 'connection.json')):
        raise SystemExit('Identity already exists. Reuse it; do not silently rotate trust.')
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'MSAI Receiver')])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-dt.timedelta(days=1))
            .not_valid_after(now+dt.timedelta(days=3650)).sign(key, hashes.SHA256()))
    (folder/'server.key').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (folder/'server.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    settings = dict(protocol='MSAC1', host=host, port=port, pin=cert.fingerprint(hashes.SHA256()).hex(), token=secrets.token_hex(32))
    (folder/'connection.json').write_text(json.dumps(settings, indent=2), encoding='utf-8')
    for file in folder.iterdir():
        file.chmod(0o600)
    print('Created receiver identity. Import connection.json in the node setup page. Keep it private.')

class Session:
    def __init__(self, device, folder, calibration):
        if len(device) != 12 or any(c not in '0123456789ABCDEFabcdef' for c in device):
            raise ValueError('Invalid device ID')
        self.device, self.calibration = device, calibration
        self.epoch, self.next, self.samples, self.gaps, self.blocks = None, 0, 0, 0, 0
        self.last = time.monotonic()
        self.path = folder / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-')+device+'.msac')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.record = self.path.open('xb')
        self.path.with_suffix('.json').write_text(json.dumps(dict(device=device, calibration=calibration), indent=2), encoding='utf-8')

    def packet(self, wire):
        meta, samples = unpack(wire)
        self.record.write(wire)
        if isinstance(samples, dict):
            emit('STATUS', device=self.device, **samples)
            self.record.flush()
            return
        if self.epoch != meta['epoch']:
            self.epoch, self.next = meta['epoch'], 0
            emit('ACQUISITION_EPOCH', device=self.device, epoch=self.epoch)
        if meta['first'] != self.next:
            self.gaps += 1
            emit('DISCONTINUITY', device=self.device, expected=self.next, received=meta['first'])
        self.next = meta['first']+len(samples)
        self.samples += len(samples)
        self.blocks += 1
        if time.monotonic()-self.last >= 2:
            emit('WAVEFORMS', device=self.device, samples=self.samples, blocks=self.blocks, rate=meta['rate'], discontinuities=self.gaps)
            self.last = time.monotonic()

    def close(self):
        self.record.close()
        emit('SESSION_ENDED', device=self.device, samples=self.samples, discontinuities=self.gaps, recording=str(self.path))

async def serve(identity, bind, port, folder):
    settings = json.loads((identity/'connection.json').read_text())
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(identity/'server.crt', identity/'server.key')
    async def client(reader, writer):
        session = None
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            if len(line) > 2048:
                raise ValueError('Oversized hello')
            hello = json.loads(line)
            if hello.get('protocol') != 'MSAC1' or not isinstance(hello.get('token'), str) or not hmac.compare_digest(hello['token'], settings['token']):
                raise ValueError('Authentication failed')
            session = Session(hello['device'], folder, hello.get('calibration', {}))
            writer.write(b'OK\n')
            await writer.drain()
            emit('CONNECTED', device=session.device, transport='TLS', peer=writer.get_extra_info('peername'))
            while True:
                header = await asyncio.wait_for(reader.readexactly(HEADER.size), 10)
                size = HEADER.unpack(header)[-1]
                if size > MAX_PAYLOAD:
                    raise ValueError('Oversized packet')
                rest = await asyncio.wait_for(reader.readexactly(size+4), 5)
                session.packet(header+rest)
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError, ValueError, KeyError, TypeError) as error:
            emit('DISCONNECTED', reason=str(error))
        finally:
            if session:
                session.close()
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass
    server = await asyncio.start_server(client, bind, port, ssl=context, limit=4096, ssl_handshake_timeout=10)
    emit('LISTENING', bind=bind, port=port, mode='TLS')
    async with server:
        await server.serve_forever()

async def ble(identity, name, folder):
    from bleak import BleakClient, BleakScanner
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    settings = json.loads((identity/'connection.json').read_text())
    token = bytes.fromhex(settings['token'])
    device = await BleakScanner.find_device_by_filter(lambda d, a: (not name or d.name == name) and SERVICE in a.service_uuids, timeout=20)
    if device is None:
        raise SystemExit('No matching BLE sensing node found.')
    frames = asyncio.Queue(maxsize=256)
    def notification(_, value):
        try:
            frames.put_nowait(bytes(value))
        except asyncio.QueueFull:
            raise RuntimeError('BLE receiver queue overflow')
    async with BleakClient(device) as client:
        hello = json.loads(bytes(await client.read_gatt_char(AUTH)))
        challenge = bytes.fromhex(hello['challenge'])
        key = hmac.digest(token, challenge+b'MSAI-DATA', 'sha256')
        proof = hmac.digest(token, challenge+hello['device'].encode('ascii'), 'sha256')
        await client.start_notify(DATA, notification)
        await client.write_gatt_char(AUTH, proof, response=True)
        session = Session(hello['device'], folder, hello.get('calibration', {}))
        emit('CONNECTED', device=session.device, transport='BLE/AES-GCM', rate=hello['rate'])
        current, packet, total, expected_number = None, bytearray(), 0, 0
        try:
            while client.is_connected:
                fragment = await asyncio.wait_for(frames.get(), 10)
                if len(fragment) < 9:
                    raise ValueError('Truncated BLE fragment')
                number, offset, size = struct.unpack_from('<IHH', fragment)
                if not offset:
                    if packet:
                        raise ValueError('Incomplete BLE frame')
                    current, total = number, size
                if number != current or offset != len(packet) or size != total or not 32 <= size <= 3740 or len(packet)+len(fragment)-8 > total:
                    raise ValueError('BLE fragment discontinuity')
                packet.extend(fragment[8:])
                if len(packet) == total:
                    if packet[:4] != b'BLE1' or packet[4:8] != challenge[:4] or struct.unpack_from('<Q', packet, 8)[0] != expected_number:
                        raise ValueError('BLE replay/session mismatch')
                    plain = AESGCM(key).decrypt(bytes(packet[4:16]), bytes(packet[16:]), b'BLE1')
                    session.packet(plain)
                    ack = struct.pack('<Q', expected_number)
                    await client.write_gatt_char(AUTH, ack+hmac.digest(key, ack, 'sha256')[:16], response=True)
                    expected_number += 1
                    packet.clear()
        finally:
            session.close()

def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['init', 'serve', 'ble'])
    p.add_argument('--identity', type=Path, default=Path('identity'))
    p.add_argument('--host', default='receiver.local', help='Reachable address exported for node setup')
    p.add_argument('--bind', default='0.0.0.0')
    p.add_argument('--port', type=int, default=5443)
    p.add_argument('--recordings', type=Path, default=Path('recordings'))
    p.add_argument('--name', help='BLE node name, such as MSAI-ABCDEF')
    a = p.parse_args()
    if a.command == 'init':
        initialize(a.identity, a.host, a.port)
    else:
        try:
            asyncio.run(serve(a.identity, a.bind, a.port, a.recordings) if a.command == 'serve' else ble(a.identity, a.name, a.recordings))
        except KeyboardInterrupt:
            pass
if __name__ == '__main__':
    main()
