"""Camera firmware loader: ELF parsing and the FX3 RAM-write sequence, without real USB."""

import struct

import pytest

from beamctl import fx3load


def make_elf(entry, segments):
    """A minimal 32-bit little-endian ELF with one PT_LOAD per (address, data, memsize)."""
    phoff, phentsize = 52, 32
    data_off = phoff + phentsize * len(segments)
    header = bytearray(52)
    header[:6] = b"\x7fELF\x01\x01"
    struct.pack_into("<II", header, 24, entry, phoff)
    struct.pack_into("<HH", header, 42, phentsize, len(segments))
    phdrs, blobs, off = b"", b"", data_off
    for addr, blob, memsz in segments:
        phdrs += struct.pack("<8I", 1, off, addr, addr, len(blob), memsz, 5, 4)
        blobs += blob
        off += len(blob)
    return bytes(header) + phdrs + blobs


def test_elf_segments_skip_bss_only_segments():
    elf = make_elf(0x4001EC00, [(0x0, b"\x01" * 16, 16), (0x40003000, b"\x02" * 5000, 5000),
                                (0x40030000, b"", 0x400)])
    entry, segs = fx3load.elf_segments(elf)
    assert entry == 0x4001EC00
    assert [(a, len(b)) for a, b in segs] == [(0x0, 16), (0x40003000, 5000)]


def test_rejects_non_elf():
    with pytest.raises(ValueError):
        fx3load.elf_segments(b"not an elf file at all" * 4)


def test_load_writes_2k_chunks_then_jumps(monkeypatch):
    calls = []
    monkeypatch.setattr(fx3load.os, "open", lambda *a: 99)
    monkeypatch.setattr(fx3load.os, "close", lambda fd: None)
    monkeypatch.setattr(fx3load, "_control", lambda fd, addr, payload=b"": calls.append((addr, len(payload))))
    elf = make_elf(0x40001234, [(0x40003000, bytes(5000), 5000)])
    entry, size = fx3load.load("/dev/bus/usb/001/003", elf)
    assert (entry, size) == (0x40001234, 5000)
    # 5000 bytes -> 2048 + 2048 + 904, at consecutive addresses, then an empty request at the entry point.
    assert calls == [(0x40003000, 2048), (0x40003800, 2048), (0x40004000, 904), (0x40001234, 0)]


def test_control_request_layout():
    # Vendor OUT request 0xA0, address split into wValue (low) / wIndex (high), like Cypress's loader.
    x = fx3load._CtrlTransfer(fx3load.VENDOR_OUT, fx3load.FX3_REQUEST, 0x3000, 0x4000, 16, 5000, None)
    assert (x.bRequestType, x.bRequest, x.wValue, x.wIndex) == (0x40, 0xA0, 0x3000, 0x4000)
    assert fx3load.USBDEVFS_CONTROL == 0xC0185500
