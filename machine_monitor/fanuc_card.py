"""FANUC memory card (DNC card, 'CF_TEXT' in FANUC's Program Transfer Tool) - fetch and send.

The FOCAS library has no documented function for copying a file between the PC and this card. FANUC's own
Program Transfer Tool does it with the normal FOCAS transfer messages on the program-transfer stream, with a
'0:' device prefix in the file name (recorded with Wireshark on the Puma 2600LY, 0i-D, 2026-11-11):

  open      a0a0a0a0 0001 0101 0002  0001                      -> 0102
  fetch     a0a0a0a0 0001 1501 0204  80 09 00 01 '0:0001.NC' 00..  -> 1502, then 1604 blocks .. 1701, we send 1702
  send      a0a0a0a0 0001 1101 0204  80 09 00 01 '0:O7777.nc' 00.. -> 1102, we send 1204 blocks, 1301 -> 1302
  overwrite the same send, but '10000:' instead of '0:' (recorded 2026-11-11: '0:' for an existing file is
            answered 1103, FANUC's tool then asks the user and sends again with '10000:' -> 1102, file replaced)
  close     a0a0a0a0 0001 0201 0000                            -> 0202

This module sends exactly those messages over its own TCP connection - nothing else. The file list comes
from cnc_rdmcdfinfo (FOCAS library, verified on the same control).
"""
from __future__ import annotations

import socket
import struct

SIG = b"\xa0\xa0\xa0\xa0"
NAME_FIELD = 512                  # 516-byte body = 4 bytes + 512 bytes name, zero padded
BLOCK = 1400


class CardError(Exception):
    pass


def frame(typ: int, body: bytes = b"") -> bytes:
    return SIG + struct.pack(">HHH", 1, typ, len(body)) + body


class CardExists(CardError):
    pass


def start_body(name: str, overwrite: bool = False) -> bytes:
    path = (("10000:" if overwrite else "0:") + name).encode("ascii")
    if len(path) >= NAME_FIELD:
        raise CardError("file name too long")
    return b"\x80\x09\x00\x01" + path + b"\0" * (NAME_FIELD - len(path))


class CardStream:
    def __init__(self, host: str, port: int = 8193, timeout: float = 20.0):
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.s.settimeout(timeout)
        self.buf = b""
        self.s.sendall(frame(0x0101, b"\x00\x01"))
        typ, _ = self.recv()
        if typ != 0x0102:
            raise CardError(f"control did not accept the transfer connection (answer {typ:04x})")

    def recv(self):
        while True:
            if len(self.buf) >= 10:
                if self.buf[:4] != SIG:
                    raise CardError("unexpected data from the control")
                ln = struct.unpack_from(">H", self.buf, 8)[0]
                if len(self.buf) >= 10 + ln:
                    typ = struct.unpack_from(">H", self.buf, 6)[0]
                    body, self.buf = self.buf[10:10 + ln], self.buf[10 + ln:]
                    return typ, body
            d = self.s.recv(65536)
            if not d:
                raise CardError("the control closed the connection")
            self.buf += d

    def close(self):
        try:
            self.s.sendall(frame(0x0201))
            self.s.settimeout(3)
            self.recv()
        except Exception:
            pass
        try:
            self.s.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def fetch(self, name: str, progress=None) -> bytes:
        self.s.sendall(frame(0x1501, start_body(name)))
        typ, _ = self.recv()
        if typ != 0x1502:
            raise CardError(f"control refused to start sending {name} (answer {typ:04x})")
        data = b""
        while True:
            typ, body = self.recv()
            if typ == 0x1604:
                if not body and not data:
                    pass                        # pyfanuc: an empty first block = file does not exist
                data += body
                if progress:
                    progress(len(data), None)
            elif typ == 0x1701:
                self.s.sendall(frame(0x1702))
                break
            else:
                raise CardError(f"unexpected answer {typ:04x} while receiving {name}")
        if not data:
            raise CardError(f"{name} not found on the memory card (or empty)")
        return data

    @staticmethod
    def prepare(data: bytes) -> bytes:
        """Same shape as the Program Transfer Tool sent: starts with LF, ends with '%', no leading '%' line."""
        text = data.lstrip(b"\r\n")
        if text.startswith(b"%"):
            text = text[1:].lstrip(b"\r\n")
        text = text.rstrip(b"\r\n \t")
        if not text.endswith(b"%"):
            text += b"\r\n%"
        return b"\n" + text

    def send(self, name: str, data: bytes, progress=None, overwrite: bool = False) -> None:
        payload = self.prepare(data)
        self.s.sendall(frame(0x1101, start_body(name, overwrite)))
        typ, body = self.recv()
        if typ == 0x1103 and not overwrite:
            # 1103 = the control won't create the file. Seen for an existing name (overwrite test 2026-11-11) AND
            # for a new name while the machine was in MDI with a held program (2026-11-12; FANUC's own tool then
            # says "wrong mode"). The caller tells the two apart with the card list.
            raise CardExists(f"{name}: control answered 1103 {bytes(body[:16]).hex()}")
        if typ != 0x1102:
            raise CardError(f"control refused to receive {name} (answer {typ:04x})")
        for i in range(0, len(payload), BLOCK):
            self.s.sendall(frame(0x1204, payload[i:i + BLOCK]))
            if progress:
                progress(min(i + BLOCK, len(payload)), len(payload))
        self.s.sendall(frame(0x1301))
        typ, _ = self.recv()
        if typ != 0x1302:
            raise CardError(f"control did not confirm {name} (answer {typ:04x})")
