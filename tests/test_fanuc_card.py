import socket
import struct
import threading

from machine_monitor.fanuc_card import CardStream, frame, start_body


def test_start_body_matches_the_recording():
    b = start_body("O7777.nc")
    assert len(b) == 516 and b[:14] == bytes.fromhex("8009 0001 303a 4f37 3737 372e 6e63") and b[14:] == b"\0" * 502


def test_prepare_matches_what_the_transfer_tool_sent():
    assert CardStream.prepare(b"%\r\nO7777 (TEST MONITOR)\r\nM30\r\n%\r\n") == b"\nO7777 (TEST MONITOR)\r\nM30\r\n%"
    assert CardStream.prepare(b"O1 (X)\r\nM30") == b"\nO1 (X)\r\nM30\r\n%"


def _fake_card(port_holder, log, files):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port_holder.append(srv.getsockname()[1])
    c, _ = srv.accept()
    buf = b""

    def rd():
        nonlocal buf
        while True:
            if len(buf) >= 10:
                ln = struct.unpack_from(">H", buf, 8)[0]
                if len(buf) >= 10 + ln:
                    t = struct.unpack_from(">H", buf, 6)[0]
                    b, buf = buf[10:10 + ln], buf[10 + ln:]
                    return t, b
            buf += c.recv(65536)
    up = None
    while True:
        t, b = rd()
        log.append(t)
        if t == 0x0101:
            c.sendall(frame(0x0102, b"\x00\x01"))
        elif t == 0x1501:
            name = b[4:].split(b"\0")[0].decode()[2:]
            c.sendall(frame(0x1502, b"\0" * 256))
            data = files[name]
            for i in range(0, len(data), 1450):
                c.sendall(frame(0x1604, data[i:i + 1450]))
            c.sendall(frame(0x1701))
        elif t == 0x1101:
            pre, nm = b[4:].split(b"\0")[0].decode().split(":", 1)
            if nm in files and pre != "10000":
                c.sendall(frame(0x1103, bytes.fromhex("2001000500030000")))
                continue
            up = [nm, b""]
            c.sendall(frame(0x1102, b"\0" * 256))
        elif t == 0x1204:
            up[1] += b
        elif t == 0x1301:
            files[up[0]] = up[1]
            c.sendall(frame(0x1302))
        elif t == 0x0201:
            c.sendall(frame(0x0202))
            c.close()
            return


def test_fetch_and_send_against_a_fake_card():
    port, log, files = [], [], {"0001.NC": b"\n<PART1>\r\nM30\r\n%" * 300}
    th = threading.Thread(target=_fake_card, args=(port, log, files), daemon=True)
    th.start()
    while not port:
        pass
    with CardStream("127.0.0.1", port[0], timeout=5) as cs:
        assert cs.fetch("0001.NC") == files["0001.NC"]
        cs.send("O7777.NC", b"%\r\nO7777 (TEST MONITOR)\r\nM30\r\n%\r\n")
    th.join(2)
    assert files["O7777.NC"] == b"\nO7777 (TEST MONITOR)\r\nM30\r\n%"
    assert log == [0x0101, 0x1501, 0x1702, 0x1101, 0x1204, 0x1301, 0x0201]


# ---- the card inside the transfer panel (FanucFiles routing)
import ctypes
import pytest
from machine_monitor.transfer import FanucFiles, TransferError


def _ff(**cfg):
    f = FanucFiles({"host": "127.0.0.1", "memory_card": "dnc", **cfg})
    f.C, f.drv = ctypes, None
    return f


def test_card_replaces_cf_text_drive_and_folder_functions_are_blocked():
    f = _ff()
    f.drives = lambda: ["CNC_MEM", "CF_TEXT", "DATA"]
    paths = [r["path"] for r in f.roots()]
    assert paths[:2] == ["//CNC_MEM/USER/PATH1/", "card:/"] and "//CF_TEXT/" not in paths
    for call in (lambda: f.list("//CF_TEXT/"), lambda: f.fetch("//CF_TEXT/0001.NC", "/tmp/x"),
                 lambda: f.send("/tmp/x", "//CF_TEXT/", "A.NC"), lambda: f.delete("//CF_TEXT/A.NC"),
                 lambda: f.delete("card:/A.NC")):
        with pytest.raises(TransferError):
            call()
    assert f.overwrites("card:/") and not f.overwrites("//CNC_MEM/USER/PATH1/")


def test_without_memory_card_setting_nothing_changes():
    f = _ff(memory_card="")
    f.drives = lambda: ["CNC_MEM", "MEMCARD"]
    assert [r["path"] for r in f.roots()] == ["//CNC_MEM/USER/PATH1/", "//MEMCARD/"]
    assert not f.is_card("card:/X.NC")


def test_card_fetch_and_send_through_the_session(tmp_path):
    port, log, files = [], [], {"0001.NC": b"\n<PART1>\r\nM30\r\n%"}
    th = threading.Thread(target=_fake_card, args=(port, log, files), daemon=True)
    th.start()
    while not port:
        pass
    f = _ff(port=port[0])
    src = tmp_path / "up"
    src.write_bytes(b"%\r\nO7779 (X)\r\nM30\r\n%\r\n")
    f.send(str(src), "card:/", "O7779.NC")
    th.join(3)
    assert files["O7779.NC"] == b"\nO7779 (X)\r\nM30\r\n%"
    th = threading.Thread(target=_fake_card, args=(port2 := [], [], files), daemon=True)
    th.start()
    while not port2:
        pass
    f.cfg["port"] = port2[0]
    assert f.fetch("card:/0001.NC", str(tmp_path / "got")) == len(files["0001.NC"])
    assert (tmp_path / "got").read_bytes() == files["0001.NC"]


def test_existing_file_is_refused_then_overwritten_with_10000():
    from machine_monitor.fanuc_card import CardExists
    port, log, files = [], [], {"O7777.NC": b"old"}
    th = threading.Thread(target=_fake_card, args=(port, log, files), daemon=True)
    th.start()
    while not port:
        pass
    with CardStream("127.0.0.1", port[0], timeout=5) as cs:
        with pytest.raises(CardExists):
            cs.send("O7777.NC", b"O7777 (NEW)\r\nM30")
        cs.send("O7777.NC", b"O7777 (NEW)\r\nM30", overwrite=True)
    th.join(2)
    assert files["O7777.NC"] == b"\nO7777 (NEW)\r\nM30\r\n%"
    assert start_body("O7777.nc", True)[:18] == b"\x80\x09\x00\x0110000:O7777.nc"
