"""Explicit synthetic test source; NEVER starts automatically with the dashboard."""
from __future__ import annotations
import argparse
import asyncio
import json
import math
import struct
import zlib

from .vendor.protocol import HEADER, encode

# Deliberately illustrative factors for testing automatic unit conversion.
# These are not calibration values for any physical board.
SYNTHETIC_CALIBRATION = {
    'source': 'SYNTHETIC TEST DATA — NOT REAL MOTOR MEASUREMENTS',
    'slopes': [0.001] * 4 + [0.01] * 3 + [0.1],
    'units': 'A,A,A,A,V,V,V,mV',
    'ct_model': 'CR3110-3000',
    'reference_calibrated': [False] * 8,
}


def waveform_packet(samples, first=0, rate=10000, epoch=1, selectors=0):
    payload = encode(samples)
    header = HEADER.pack(b'MSC1', 1, 1, 8, 128, selectors, epoch, rate, first, first * 1000000 // rate, len(payload))
    wire = header + payload
    return wire + struct.pack('<I', zlib.crc32(wire))


def status_packet(first=0, rate=10000, epoch=1, status=None):
    status = status or {'fault': 0, 'running': True, 'blocks': first // 128, 'overflows': 0, 'adc_crc': 0, 'missed_adc': 0}
    payload = json.dumps(status).encode()
    wire = HEADER.pack(b'MSC1', 1, 0, 0, 0, 0, epoch, rate, first, first * 1000000 // rate, len(payload)) + payload
    return wire + struct.pack('<I', zlib.crc32(wire))


async def simulate(host: str, port: int, seconds: float, rate: int, device: str):
    reader, writer = await asyncio.open_connection(host, port)
    hello = {'protocol': 'MSAC1', 'device': device, 'rate': rate,
             'calibration': SYNTHETIC_CALIBRATION}
    writer.write(json.dumps(hello).encode() + b'\n')
    await writer.drain()
    if await asyncio.wait_for(reader.readline(), 5) != b'OK\n':
        raise RuntimeError('Receiver did not acknowledge MSAC1 handshake')
    print(f'SYNTHETIC TEST ONLY: {device}, {rate} samples/s/channel, {seconds:g} seconds')
    loop = asyncio.get_running_loop()
    start, first, last_status = loop.time(), 0, -1
    try:
        while first < seconds * rate:
            rows = [[round((4000 + ch * 500) * math.sin(2 * math.pi * 50 * (first + i) / rate + (ch % 3) * 2 * math.pi / 3) + 123)
                     for ch in range(8)] for i in range(128)]
            writer.write(waveform_packet(rows, first, rate))
            if first // rate != last_status:
                writer.write(status_packet(first, rate))
                last_status = first // rate
            await writer.drain()
            first += 128
            await asyncio.sleep(max(0, start + first / rate - loop.time()))
    finally:
        writer.close()
        await writer.wait_closed()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5443)
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--rate', type=int, choices=[1000, 2000, 10000], default=10000)
    parser.add_argument('--device', default='DE0000000001')
    args = parser.parse_args()
    if not 0 < args.seconds <= 86400 or not 1 <= args.port <= 65535:
        parser.error('Use positive seconds (up to 86400) and a valid port')
    if len(args.device) != 12 or any(c not in '0123456789abcdefABCDEF' for c in args.device):
        parser.error('Device ID must be 12 hexadecimal characters')
    try:
        asyncio.run(simulate(args.host, args.port, args.seconds, args.rate, args.device))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
