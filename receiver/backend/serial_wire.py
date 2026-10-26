"""Resynchronizing, CRC-protected frames from the USB base station."""
import struct
import zlib

HEADER = struct.Struct('<4sBBHII')
MAX_PAYLOAD = 3644


def pack(kind, payload, sequence=0, session=0, version=1):
    header = HEADER.pack(b'BSU1', version, kind, len(payload), sequence, session)
    raw = header + payload
    return raw + struct.pack('<I', zlib.crc32(raw))


class Parser:
    def __init__(self):
        self.buffer = bytearray()
        self.crc_errors = 0
        self.discarded_bytes = 0
        self.bad_frames = []
        self.version = None

    def feed(self, data):
        self.buffer.extend(data)
        result = []
        while len(self.buffer) >= HEADER.size:
            pos = self.buffer.find(b'BSU1')
            if pos < 0:
                self.discarded_bytes += len(self.buffer) - 3
                del self.buffer[:-3]
                break
            if pos:
                self.discarded_bytes += pos
                del self.buffer[:pos]
            if len(self.buffer) < HEADER.size:
                break
            _, version, kind, size, seq, session = HEADER.unpack_from(self.buffer)
            if version not in (1, 2) or kind not in (1, 2, 3) or size > MAX_PAYLOAD:
                self.discarded_bytes += 1
                del self.buffer[0]
                continue
            length = HEADER.size + size + 4
            if len(self.buffer) < length:
                break
            raw = self.buffer[:length]
            if zlib.crc32(raw[:-4]) != struct.unpack_from('<I', raw, length-4)[0]:
                self.crc_errors += 1
                if len(self.bad_frames) < 8:
                    self.bad_frames.append(bytes(raw))
                del self.buffer[0]
                continue
            if self.version is not None and version != self.version:
                raise ValueError('Serial protocol changed without reconnecting')
            self.version = version
            result.append((kind, seq, session, bytes(raw[HEADER.size:-4])))
            del self.buffer[:length]
        return result


class OrderedReceiver:
    """Version 2: retain later frames until the missing frame is validated."""
    def __init__(self):
        self.session = None
        self.expected = 0
        self.pending = {}

    def accept(self, frame, consume):
        kind, sequence, session, payload = frame
        if session != self.session:
            self.session = session
            self.expected = 0
            self.pending.clear()
        distance = (sequence-self.expected) & 0xffffffff
        if distance >= 0x80000000:
            return (self.expected-1) & 0xffffffff  # ACK lost; already delivered.
        if distance >= 16:
            raise ValueError('Serial receive window exceeded')
        old = self.pending.get(sequence)
        if old is not None and old != frame:
            raise ValueError('Conflicting serial retransmission')
        self.pending[sequence] = frame
        advanced = False
        while self.expected in self.pending:
            current = self.pending.pop(self.expected)
            consume(*current)  # Never acknowledge invalid inner CRC/codec data.
            self.expected = (self.expected+1) & 0xffffffff
            advanced = True
        return ((self.expected-1) & 0xffffffff) if advanced else None
