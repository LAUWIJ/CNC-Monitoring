import os
import time

import pytest

from machine_monitor.store import Store
from machine_monitor.transfer import FanucFiles, TransferService, base_name


def _svc(tmp_path, **mextra):
    st = Store(str(tmp_path / "t.db"))
    m = {"id": "f1", "name": "F", "control": "simulator", "flavor": "fanuc", **mextra}
    st.register_machine("f1", "F", "simulator")
    return TransferService([m], st, str(tmp_path), mextra.pop("_settings", {})), st


def _wait(svc, jid):
    for _ in range(100):
        j = svc.job(jid)
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _upload(svc, data=b"O1234\nG0X0\nM30\n"):
    p = svc.new_upload_path()
    with open(p, "wb") as f:
        f.write(data)
    return p


def test_base_name_normalises_paths_and_o_numbers():
    assert base_name("//CNC_MEM/USER/PATH1/O0123") == "O123"
    assert base_name("TNC:\\nc_prog\\Clean_Vise.h") == "CLEAN_VISE.H"
    assert base_name("Sinumerik/FileSystem/Part Program/A.MPF") == "A.MPF"


def test_send_new_then_exists_then_replace_keeps_backup(tmp_path):
    svc, st = _svc(tmp_path)
    folder = "//CNC_MEM/USER/PATH1/"
    j = _wait(svc, svc.start_send("f1", folder, "O5555", _upload(svc))["id"])
    assert j["state"] == "done"
    j = _wait(svc, svc.start_send("f1", folder, "O5555", _upload(svc, b"O5555\nNEW\n"))["id"])
    assert j["state"] == "exists"
    j = _wait(svc, svc.replace(j["id"])["id"])
    assert j["state"] == "done" and j["backup"]
    assert os.listdir(tmp_path / "transfer_backup" / "f1")
    h = st.transfers("f1")
    assert [r["result"] for r in h] == ["ok", "ok"] and h[0]["detail"] == "replaced"


def test_protected_and_in_use_programs_are_refused(tmp_path):
    svc, st = _svc(tmp_path)
    with pytest.raises(ValueError, match="protected"):
        svc.start_send("f1", "//CNC_MEM/USER/PATH1/", "O9022", _upload(svc))
    st.update_live("f1", {"state": "RUNNING", "program": "O1001", "current_program": "O1001"}, False)
    with pytest.raises(ValueError, match="running"):
        svc.start_send("f1", "//CNC_MEM/USER/PATH1/", "O1001", _upload(svc))
    assert st.transfers("f1")[0]["result"] == "refused"


def test_send_off_and_pin(tmp_path):
    svc, _ = _svc(tmp_path, transfer="read")
    assert svc.info("f1")["send"] is False
    with pytest.raises(ValueError, match="switched off"):
        svc.start_send("f1", "//CF_TEXT/", "A.NC", _upload(svc))
    svc2, _ = _svc(tmp_path / "b")
    svc2.pin = "1234"
    with pytest.raises(PermissionError):
        svc2.start_send("f1", "//CF_TEXT/", "A.NC", _upload(svc2), pin="0000")
    j = _wait(svc2, svc2.start_send("f1", "//CF_TEXT/", "A.NC", _upload(svc2), pin="1234")["id"])
    assert j["state"] == "done"


def test_fetch_from_memory_card(tmp_path):
    svc, _ = _svc(tmp_path)
    names = [e["name"] for e in svc.list("f1", "//CF_TEXT/")]
    assert "BIGPART.NC" in names
    j = _wait(svc, svc.start_fetch("f1", "//CF_TEXT/BIGPART.NC")["id"])
    fp, name = svc.job_file(j["id"])
    assert name == "BIGPART.NC" and open(fp, "rb").read().startswith(b"%")


def test_fanuc_download_format():
    out = FanucFiles.prepare(b"%\r\nO0012 (OLD NAME)\r\nG0X0\r\nM30\r\n%\r\n", "O1234")
    assert out == b"\nO1234 (OLD NAME)\nG0X0\nM30\n%"
    out = FanucFiles.prepare(b"G0X0\nM30\n", "BIGPART.NC")
    assert out.startswith(b"\n<BIGPART.NC>\nG0X0") and out.endswith(b"%")


def test_heidenhain_folders_reported_as_files_open_as_folders():
    from machine_monitor.transfer import HeidenhainFiles

    class E:
        def __init__(self, name, size, d=False):
            self.name, self.size, self.is_directory, self.is_drive = name, size, d, False

    class Con:
        dirs = {"TNC:\\", "TNC:\\PROGRAM\\", "TNC:\\nc_prog\\"}
        cwd = "TNC:\\"

        def change_directory(self, p):
            if p in self.dirs:
                self.cwd = p
                return True
            return False

        def directory_content(self):
            return [E("PROGRAM", 4096), E("nc_prog", 4096), E("Ballbar.h", 341), E("README", 120)]

    h = HeidenhainFiles({"host": "x"})
    h.con = Con()
    got = {e["name"]: e["dir"] for e in h.list("TNC:\\")}
    assert got == {"PROGRAM": True, "nc_prog": True, "Ballbar.h": False, "README": False}


def test_program_folder_from_selected_program():
    pf = TransferService.program_folder
    assert pf("heidenhain", "TNC:\\PROGRAM\\PART_1190_OP10.h") == "TNC:\\PROGRAM\\"
    assert pf("fanuc", "//CF_TEXT/BIGPART.NC") == "//CF_TEXT/"
    assert pf("fanuc", "O1234") is None
    assert pf("siemens", "/_N_MPF_DIR/_N_PART_MPF") == "Sinumerik/FileSystem/Part Program/"
    assert pf("siemens", "/_N_WKS_DIR/_N_HOUSING_WPD/_N_A_MPF") == "Sinumerik/FileSystem/Work Pieces/HOUSING.WPD/"


def test_roots_open_at_program_folder(tmp_path):
    svc, st = _svc(tmp_path)
    st.update_live("f1", {"state": "RUNNING", "program_path": "//CF_TEXT/BIGPART.NC"}, False)
    roots = svc.roots("f1")
    assert [r["label"] for r in roots] == ["CNC memory", "Memory card"]      # no extra button on Fanuc
    assert roots[1].get("default") is True


def test_backups_are_limited(tmp_path):
    svc, _ = _svc(tmp_path)
    svc.backup_keep, svc.backup_days, svc.backup_max = 2, 30, 10_000
    d = tmp_path / "transfer_backup" / "f1"
    d.mkdir(parents=True)
    now = time.time()
    for i, (name, age, size) in enumerate([("O1234", 0, 100), ("O1234", 60, 100), ("O1234", 120, 100),
                                           ("O5555", 40 * 86400, 100), ("BIG.NC", 10, 20_000)]):
        p = d / f"2026111{i}-00000{i}_{name}"
        p.write_bytes(b"x" * size)
        os.utime(p, (now - age, now - age))
    assert svc.prune_backups() == 3          # 3rd O1234, 40-day-old O5555, BIG.NC over the size cap
    left = sorted(f.split("_", 1)[1] for f in os.listdir(d))
    assert left == ["O1234", "O1234"]


def test_fanuc_roots_follow_the_drives_the_control_reports():
    from machine_monitor.transfer import FanucFiles

    f = FanucFiles({"host": "x"})
    f.drives = lambda: ["CNC_MEM", "DATA", "CF_TEXT"]
    roots = f.roots()
    assert [(r["path"], r["label"]) for r in roots] == [
        ("//CNC_MEM/USER/PATH1/", "CNC memory"), ("//CF_TEXT/", "Memory card"), ("//DATA/", "Data server")]
    f.drives = lambda: ["CNC_MEM", "XYZ"]
    assert [r["label"] for r in f.roots()] == ["CNC memory", "XYZ"]
    # drive names the control doesn't report are NEVER opened (that crashed a 0i-D, SYS_ALM198)
    opened = []
    f.list = lambda path: opened.append(path) or []
    f.drives = lambda: ["CNC_MEM"]
    assert [r["path"] for r in f.roots()] == ["//CNC_MEM/USER/PATH1/"] and opened == []


def test_fanuc_transfer_is_off_unless_switched_on():
    assert not TransferService.supported({"control": "fanuc"})
    assert TransferService.supported({"control": "fanuc", "transfer": "read"})
    assert TransferService.supported({"control": "fanuc", "transfer": "send"})


def test_drives_seen_before_stay_listed_when_they_disappear(tmp_path):
    svc, _ = _svc(tmp_path)
    a = svc._remember_roots("f1", [{"path": "//CNC_MEM/USER/PATH1/", "label": "CNC memory", "note": ""},
                                   {"path": "//CF_TEXT/", "label": "Memory card", "note": ""}])
    assert all(r["available"] for r in a)
    b = svc._remember_roots("f1", [{"path": "//CNC_MEM/USER/PATH1/", "label": "CNC memory", "note": ""}])
    assert [(r["label"], r["available"]) for r in b] == [("CNC memory", True), ("Memory card", False)]
    svc2, _ = _svc(tmp_path)                                    # survives a restart of the monitor
    c = svc2._remember_roots("f1", [{"path": "//CNC_MEM/USER/PATH1/", "label": "CNC memory", "note": ""}])
    assert c[-1]["label"] == "Memory card" and c[-1]["available"] is False


def test_siemens_listing_skips_methods_and_tells_files_from_folders():
    import types
    from machine_monitor.transfer import SiemensFiles
    try:
        from asyncua import ua
        OBJ, METH = ua.NodeClass.Object, ua.NodeClass.Method
    except ImportError:                                   # asyncua not installed here: fake the enum
        ua = types.SimpleNamespace(NodeClass=types.SimpleNamespace(Object=1, Method=4))
        import sys
        sys.modules["asyncua"] = types.SimpleNamespace(ua=ua)
        OBJ, METH = 1, 4

    class N:
        def __init__(self, name, cls=OBJ, kids=(), typ=999):
            self.name, self.cls, self.kids, self.typ = name, cls, list(kids), typ

        def read_browse_name(self):
            return types.SimpleNamespace(Name=self.name)

        def read_node_class(self):
            return self.cls

        def read_type_definition(self):
            return types.SimpleNamespace(Identifier=self.typ)

        def get_children(self):
            return self.kids

        def get_child(self, path):
            for k in self.kids:
                if k.name == path[0].split(":")[-1]:
                    return types.SimpleNamespace(read_value=lambda: 1234)
            raise KeyError(path)

    file_kids = [N("Open", METH), N("Read", METH), N("Close", METH), N("Size", 2)]
    dir_kids = [N("CreateFile", METH), N("CreateDirectory", METH), N("Delete", METH), N("MoveOrCopy", METH)]
    folder = N("3D", kids=[N("5701004.MPF", kids=file_kids), N("MEASURE_LOG.TXT", kids=file_kids),
                           N("SUB.WPD", kids=dir_kids)] + dir_kids)
    s = SiemensFiles({"host": "x"})
    s._node = lambda path: folder
    got = {e["name"]: e["dir"] for e in s.list("Sinumerik/FileSystem/Work Pieces/3D/")}
    assert got == {"5701004.MPF": False, "MEASURE_LOG.TXT": False, "SUB.WPD": True}


def test_siemens_fetch_uses_standard_open_read_close(tmp_path):
    import sys
    import types
    from machine_monitor.transfer import SiemensFiles
    try:
        import asyncua  # noqa: F401  (real library: nothing to fake)
        real = hasattr(sys.modules["asyncua"], "__file__")
    except ImportError:
        real = False
    if not real:                      # asyncua not installed here: a minimal stand-in for ua.Variant & co
        ua = types.SimpleNamespace(NodeClass=types.SimpleNamespace(Object=1, Method=4),
                                   NodeId=type("NodeId", (), {}),
                                   Variant=lambda v, t=None: types.SimpleNamespace(Value=v),
                                   VariantType=types.SimpleNamespace(Byte=1, UInt32=2, Int32=3, String=4,
                                                                     Boolean=5, ByteString=6))
        sys.modules["asyncua"] = types.SimpleNamespace(ua=ua)
    data = b"%_N_WARMLOOP_MPF\nG0 X0\nM30\n" * 2000
    calls = []

    class M:
        def __init__(self, name):
            self.name = name

        def read_browse_name(self):
            return types.SimpleNamespace(Name=self.name)

    class F:
        pos = 0
        kids = [M("Open"), M("Read"), M("Close")]

        def get_children(self):
            return self.kids

        def call_method(self, m, *args):
            calls.append(m.name)
            if m.name == "Open":
                return 7
            if m.name == "Read":
                n = getattr(args[1], "Value", args[1])
                chunk = data[self.pos:self.pos + n]
                self.pos += len(chunk)
                return chunk
            return None

    s = SiemensFiles({"host": "x"})
    s._node = lambda path: F()
    out = tmp_path / "w.mpf"
    assert s.fetch("Sinumerik/FileSystem/Work Pieces/WARMLOOP/WARMLOOP.MPF", str(out)) == len(data)
    assert out.read_bytes() == data and calls[0] == "Open" and calls[-1] == "Close"



def test_siemens_send_finds_the_new_file_by_name_when_createfile_gives_no_node_id(tmp_path):
    import sys
    import types
    test_siemens_fetch_uses_standard_open_read_close(tmp_path)          # installs the ua stand-in if needed
    from machine_monitor.transfer import SiemensFiles
    calls, written = [], []

    class M:
        def __init__(self, name):
            self.name = name

        def read_browse_name(self):
            return types.SimpleNamespace(Name=self.name)

    class FileNode(M):
        nodeid = "file-id"

        def get_children(self):
            return [M("Open"), M("Write"), M("Close")]

        def call_method(self, m, *args):
            calls.append(m.name)
            if m.name == "Open":
                return 9
            if m.name == "Write":
                written.append(getattr(args[1], "Value", args[1]))

    new = FileNode("TEST.MPF")

    class Dir(M):
        created = False

        def get_children(self):
            return [M("CreateFile"), M("Delete")] + ([new] if self.created else [])

        def call_method(self, m, *args):
            calls.append(m.name)
            if m.name == "CreateFile":
                self.created = True
                return [types.SimpleNamespace(Identifier="Sinumerik/FileSystem/Work Pieces/3D/TEST.mpf"), 4]

    src = tmp_path / "TEST.mpf"
    src.write_bytes(b"; TEST MONITOR\nM30\n")
    s = SiemensFiles({"host": "x"})
    s._node = lambda path: Dir("3D")
    s.send(str(src), "Sinumerik/FileSystem/Work Pieces/3D/", "TEST.mpf")           # lower case in, TEST.MPF out
    assert b"".join(written) == src.read_bytes()
    assert calls == ["CreateFile", "Write", "Close"]                 # the CreateFile handle is used


def test_memory_card_replace_keeps_a_backup_and_overwrites_without_delete(tmp_path):
    card, calls = {"O7777.NC": b"\nO7777 (OLD)\r\nM30\r\n%"}, []

    class CardFiles(FanucFiles):
        def open(self):
            self.drv = None

        def card_list(self):
            return [{"name": n, "dir": False, "size": len(d), "mtime": None, "comment": ""} for n, d in card.items()]

        def card_fetch(self, path, local, progress=None):
            calls.append("fetch")
            with open(local, "wb") as f:
                f.write(card[self._card_name(path)])
            return len(card[self._card_name(path)])

        def card_send(self, local, name, progress=None, overwrite=False):
            calls.append(("send", overwrite))
            if name in card and not overwrite:
                raise AssertionError("would be refused by the control")
            card[name] = open(local, "rb").read()

    svc, st = _svc(tmp_path, control="fanuc", transfer="send", memory_card="dnc", host="x")
    svc.session = lambda m: CardFiles(m)
    j = _wait(svc, svc.start_send("f1", "card:/", "O7777.NC", _upload(svc, b"O7777 (NEW)\nM30\n"))["id"])
    assert j["state"] == "exists"
    j = _wait(svc, svc.replace(j["id"])["id"])
    assert j["state"] == "done", j
    assert calls == ["fetch", ("send", True)] and b"NEW" in card["O7777.NC"]
    assert os.listdir(tmp_path / "transfer_backup" / "f1")
