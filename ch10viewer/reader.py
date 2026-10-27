"""Streaming reader for the Chapter 10 primary packet envelope.

Payload offsets are relative to the data region, INCLUDING channel-specific data.
No assumption is made about Ethernet packet boundaries or absolute UTC time.
"""
from dataclasses import dataclass
from pathlib import Path
import math
import struct

PRIMARY = struct.Struct('<HHIIBBBB6sH')
SYNC = 0xEB25
TICK_SECONDS = 1e-7
FORMATS = {'uint8': 'B', 'int8': 'b', 'uint16': 'H', 'int16': 'h',
           'uint32': 'I', 'int32': 'i', 'uint64': 'Q', 'int64': 'q',
           'float32': 'f', 'float64': 'd'}


class FormatError(ValueError):
    pass


@dataclass(frozen=True)
class Packet:
    index: int
    offset: int
    channel: int
    length: int
    data_length: int
    version: int
    sequence: int
    flags: int
    data_type: int
    rtc: int
    checksum_ok: bool

    @property
    def header_length(self):
        return 36 if self.flags & 0x80 else 24

    @property
    def data_offset(self):
        return self.offset + self.header_length


def scan(path, progress=None):
    """Index packets without loading payloads; reject truncation and bad lengths.

    Strict boundaries deliberately avoid mistaking payload bytes for packet sync.
    A bad primary checksum is exposed as a warning, not silently repaired.
    """
    size = Path(path).stat().st_size
    packets = []
    with open(path, 'rb') as stream:
        offset = 0
        while offset < size:
            raw = stream.read(24)
            if len(raw) != 24:
                raise FormatError(f'Offset 0x{offset:X}: truncated primary header')
            sync, channel, length, data_length, version, seq, flags, dtype, rtc, checksum = PRIMARY.unpack(raw)
            header_length = 36 if flags & 0x80 else 24
            checksum_bytes = (0, 1, 2, 4)[flags & 3]
            if sync != SYNC:
                raise FormatError(f'Offset 0x{offset:X}: expected CH10 sync 0xEB25; raw CH10 packet stream required')
            if length < header_length or length % 4:
                raise FormatError(f'Offset 0x{offset:X}: invalid packet length {length}')
            if data_length + checksum_bytes > length - header_length:
                raise FormatError(f'Offset 0x{offset:X}: data exceeds packet boundary')
            if offset + length > size:
                raise FormatError(f'Offset 0x{offset:X}: truncated packet ({length} bytes expected)')
            valid = (sum(struct.unpack('<11H', raw[:22])) & 0xFFFF) == checksum
            packets.append(Packet(len(packets), offset, channel, length, data_length,
                                  version, seq, flags, dtype, int.from_bytes(rtc, 'little'), valid))
            offset += length
            stream.seek(offset)
            if progress and len(packets) % 10000 == 0:
                progress(offset, size)
    if not packets:
        raise FormatError('Empty file: no CH10 packets')
    return packets


def read_data(path, packet, limit=65536):
    with open(path, 'rb') as stream:
        stream.seek(packet.data_offset)
        data = stream.read(min(packet.data_length, limit))
    if len(data) != min(packet.data_length, limit):
        raise FormatError('File changed or was truncated after indexing')
    return data


def extract(path, packets, *, offset, kind, endian='little', scale=1., bias=0., channel=None):
    """Read one configured scalar per CH10 packet (not per Ethernet frame)."""
    if offset < 0 or kind not in FORMATS or endian not in ('little', 'big'):
        raise ValueError('Invalid offset, type, or byte order')
    if not math.isfinite(scale) or not math.isfinite(bias):
        raise ValueError('Scale and bias must be finite')
    decoder = struct.Struct(('<' if endian == 'little' else '>') + FORMATS[kind])
    rows = []
    skipped = 0
    with open(path, 'rb') as stream:
        for packet in packets:
            if channel is not None and packet.channel != channel:
                continue
            if offset + decoder.size > packet.data_length:
                skipped += 1
                continue
            stream.seek(packet.data_offset + offset)
            raw = stream.read(decoder.size)
            if len(raw) != decoder.size:
                raise FormatError('File changed after indexing')
            value = decoder.unpack(raw)[0] * scale + bias
            if not math.isfinite(value):
                skipped += 1
                continue
            rows.append((packet.index, packet.channel, packet.rtc, value))
    return rows, skipped
