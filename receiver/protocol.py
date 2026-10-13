"""MSAC1: lossless 24-bit waveform blocks; no third-party codec dependency."""
import struct
import zlib

HEADER = struct.Struct('<4sBBHHHIIQQI')
MAX_PAYLOAD = 3600

def decode(data, count, channels):
    if not 1 <= count <= 128 or not 1 <= channels <= 8:
        raise ValueError('Invalid dimensions')
    bits = int.from_bytes(data, 'big')
    remaining = len(data) * 8
    def get(n):
        nonlocal remaining
        if n > remaining:
            raise ValueError('Truncated Rice stream')
        remaining -= n
        return (bits >> remaining) & ((1 << n) - 1)
    out = [[0] * channels for _ in range(count)]
    for c in range(channels):
        k = get(5)
        if k > 24:
            raise ValueError('Invalid Rice parameter')
        value = get(24)
        if value & 0x800000:
            value -= 1 << 24
        out[0][c] = value
        for i in range(1, count):
            q = 0
            while not get(1):
                q += 1
                if q > (0x1fffffe >> k):
                    raise ValueError('Rice quotient overflow')
            z = (q << k) | get(k)
            if z > 0x1fffffe:
                raise ValueError('Delta overflow')
            value += -((z >> 1) + 1) if z & 1 else z >> 1
            if not -8388608 <= value <= 8388607:
                raise ValueError('Sample overflow')
            out[i][c] = value
    if remaining >= 8 or get(remaining):
        raise ValueError('Noncanonical padding')
    return out

def encode(samples):
    n, channels = len(samples), len(samples[0])
    if not 1 <= n <= 128 or not 1 <= channels <= 8 or any(len(r) != channels for r in samples):
        raise ValueError('Invalid dimensions')
    if any(not -8388608 <= v <= 8388607 for row in samples for v in row):
        raise ValueError('Invalid sample')
    bits, length = 0, 0
    def put(v, n):
        nonlocal bits, length
        bits = (bits << n) | v
        length += n
    for c in range(channels):
        deltas = [samples[i][c] - samples[i-1][c] for i in range(1, n)]
        zs = [-d*2-1 if d < 0 else d*2 for d in deltas]
        k = min(range(25), key=lambda k: sum((z >> k)+1+k for z in zs))
        put(k, 5)
        put(samples[0][c] & 0xffffff, 24)
        for z in zs:
            put(1, (z >> k)+1)
            put(z & ((1 << k)-1), k)
    padding = -length % 8
    return (bits << padding).to_bytes((length+padding)//8, 'big')

def unpack(wire):
    if len(wire) < HEADER.size + 4:
        raise ValueError('Truncated packet')
    magic, version, codec, channels, count, mode, epoch, rate, first, stamp, size = HEADER.unpack_from(wire)
    if magic != b'MSC1' or version != 1 or size > MAX_PAYLOAD or len(wire) != HEADER.size+size+4:
        raise ValueError('Invalid packet header')
    if zlib.crc32(wire[:-4]) != struct.unpack_from('<I', wire, len(wire)-4)[0]:
        raise ValueError('Packet CRC mismatch')
    if rate not in (1000, 2000, 10000) or mode not in (0, 1, 2, 3):
        raise ValueError('Invalid acquisition metadata')
    payload = wire[HEADER.size:-4]
    meta = dict(epoch=epoch, rate=rate, first=first, time_us=stamp, selectors=mode)
    if codec == 1 and channels == 8 and count == 128:
        return meta, decode(payload, count, channels)
    if codec == 0 and channels == 0 and count == 0:
        import json
        status = json.loads(payload)
        if not isinstance(status, dict):
            raise ValueError('Invalid status payload')
        return meta, status
    raise ValueError('Unsupported codec or dimensions')
