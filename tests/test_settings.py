"""Settings page: config.toml edits keep comments and touch nothing else; admin password; wait rule names."""
import os
import tomllib

import pytest

from machine_monitor import config_edit as E
from machine_monitor.admin import Admin, set_password
from machine_monitor.settings import SettingsService, name_to_pattern, pattern_to_name

CFG = """# my shop
[server]
port = 8080

[defaults]
subprogram_patterns = [
  '^O?9\\d{3}$',   # macros
]

# ---- lathes
[[machine]]
id = "m1"
name = "Puma 2600LY"     # the old one
control = "fanuc"
host = "192.168.1.21"
roots = [
  "//CNC_MEM/",
]
# comment that belongs to m1
[machine.nodes]
x = "y"

# ---- mills
[[machine]]
id = "m2"
name = "DVF"
control = "heidenhain"
host = "192.168.1.22"   # keep me
"""


def test_update_keeps_comments_and_other_machines():
    new = E.update_machine(CFG, 0, {"name": "Puma 2600LY robot", "port": 8193}, set(),
                           [{"program": "^O?9021$", "reason": "robot", "action": "pause"}])
    assert 'name = "Puma 2600LY robot" # the old one' in new
    assert "# comment that belongs to m1" in new and "# ---- mills" in new and "# my shop" in new
    cfg = tomllib.loads(new)
    m1 = cfg["machine"][0]
    assert m1["port"] == 8193 and m1["nodes"] == {"x": "y"} and m1["roots"] == ["//CNC_MEM/"]
    assert m1["wait"] == [{"program": "^O?9021$", "reason": "robot", "action": "pause"}]
    assert cfg["machine"][1] == tomllib.loads(CFG)["machine"][1]
    E.checked(CFG, new, "m1", m1)


def test_multiline_value_and_remove_and_crlf():
    crlf = CFG.replace("\n", "\r\n")
    new = E.update_machine(crlf, 0, {"roots": ["//MEMCARD/"]}, {"host"})
    assert "\r\n" in new and "\n" not in new.replace("\r\n", "")
    m1 = tomllib.loads(new)["machine"][0]
    assert m1["roots"] == ["//MEMCARD/"] and "host" not in m1


def test_wait_rules_replaced_and_removed():
    one = E.update_machine(CFG, 1, {}, None, [{"program": "^A$", "reason": "robot", "action": "end"}])
    two = E.update_machine(one, 1, {}, None, [{"program": "^B$", "reason": "operator", "action": "pause"},
                                             {"program": "^C\\.H$", "reason": "robot", "action": "pause"}])
    assert [w["program"] for w in tomllib.loads(two)["machine"][1]["wait"]] == ["^B$", "^C\\.H$"]
    assert "'^C\\.H$'" in two                     # regex written as a literal string, readable as typed
    none = E.update_machine(two, 1, {}, None, [])
    assert "wait" not in tomllib.loads(none)["machine"][1]
    assert 'host = "192.168.1.22"   # keep me' in none


def test_add_and_remove_machine():
    added = E.add_machine(CFG, {"id": "m3", "name": "CLX", "control": "mtconnect", "host": "1.2.3.4"})
    assert [m["id"] for m in tomllib.loads(added)["machine"]] == ["m1", "m2", "m3"]
    gone = E.remove_machine(added, 0)
    cfg = tomllib.loads(gone)
    assert [m["id"] for m in cfg["machine"]] == ["m2", "m3"]
    assert cfg["defaults"] == tomllib.loads(CFG)["defaults"] and "# ---- mills" in gone


def test_checked_refuses_side_effects():
    bad = CFG.replace('host = "192.168.1.22"', 'host = "10.0.0.1"')
    with pytest.raises(E.ConfigError):
        E.checked(CFG, bad, "m1", tomllib.loads(CFG)["machine"][0])


def test_wait_names():
    assert name_to_pattern("9021") == "^O?9021$" and name_to_pattern("O9021") == "^O?9021$"
    assert pattern_to_name("^O?9021$") == "9021"
    p = name_to_pattern("LOAD_PRODUCT_0.H")
    assert pattern_to_name(p) == "LOAD_PRODUCT_0.H"
    assert name_to_pattern("^LOAD") == "^LOAD" and pattern_to_name("^LOAD") == "^LOAD"
    with pytest.raises(ValueError):
        name_to_pattern("(bad")


def test_service_save_test_values(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(CFG, encoding="utf-8")
    seen = []
    svc = SettingsService(str(cfg), str(tmp_path / "data"), None, seen.append)
    r = svc.save({"id": "m1", "control": "fanuc", "values": {"name": "Puma", "host": "192.168.1.21", "port": "",
                  "transfer": "read", "part_count_macro": "3901", "read_m00_signals": True},
                  "wait": [{"on": "program", "value": "9021", "reason": "robot", "action": "pause"}]})
    assert r["changed"] and seen == ["m1"]
    m1 = tomllib.loads(cfg.read_text())["machine"][0]
    assert m1["name"] == "Puma" and m1["transfer"] == "read" and "part_count_macro" not in m1   # default not added
    assert m1["wait"][0]["program"] == "^O?9021$"
    assert os.path.exists(tmp_path / "data" / "config.deployed.toml")       # the pre-edit version, for deploy.bat
    assert os.listdir(tmp_path / "data" / "config_backup")
    assert svc.save({"id": "m1", "control": "fanuc", "values": {"name": "Puma"},
                     "wait": [{"on": "program", "value": "9021", "reason": "robot", "action": "pause"}]})["changed"] is False
    with pytest.raises(ValueError):
        svc.save({"id": "m2", "new": True, "control": "fanuc", "values": {"name": "x", "host": "h"}})
    svc.save({"id": "m9", "new": True, "control": "siemens",
              "values": {"name": "840D", "host": "10.0.0.9", "username": "mon", "password": "pw"}})
    ms = svc.machines()["machines"]
    assert ms[-1]["values"]["password"] == {"set": True}                   # never sent back to the browser
    svc.delete("m9")
    assert [m["id"] for m in tomllib.loads(cfg.read_text())["machine"]] == ["m1", "m2"]


def test_admin_password(tmp_path):
    a = Admin(str(tmp_path))
    assert not a.is_set()
    with pytest.raises(ValueError):
        a.setup("123")
    tok = a.setup("secret1")
    a.check(tok)
    with pytest.raises(PermissionError):
        a.setup("other12")                        # only while none is set
    with pytest.raises(PermissionError):
        a.login("wrong!!")
    set_password(str(tmp_path), "newpass1")       # reset from the command line logs everyone out
    with pytest.raises(PermissionError):
        a.check(tok)
    a.check(a.login("newpass1"))


def test_lockout_is_per_computer(tmp_path):
    a = Admin(str(tmp_path))
    a.setup("secret1", "10.0.0.1")
    for _ in range(5):
        with pytest.raises(PermissionError, match="wrong password"):
            a.login("guess!!", "10.0.0.66")
    with pytest.raises(PermissionError, match="this computer"):
        a.login("secret1", "10.0.0.66")       # the guessing computer waits, even with the right password
    a.check(a.login("secret1", "10.0.0.1"))   # the admin's own computer is not locked out


def test_pin_lockout_per_computer():
    from machine_monitor.admin import Attempts
    t = Attempts(limit=3, window=60)
    for _ in range(3):
        t.check("pc-a")
        t.failed("pc-a")
    with pytest.raises(PermissionError):
        t.check("pc-a", "PINs")
    t.check("pc-b", "PINs")
    t.failed("pc-b")
    t.ok("pc-b")
    assert "pc-b" not in t.fails


def test_warmup_programs_field(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(CFG, encoding="utf-8")
    svc = SettingsService(str(cfg), str(tmp_path / "data"), None)
    svc.save({"id": "m2", "control": "heidenhain", "values": {"name": "DVF", "host": "192.168.1.22",
              "warmup_programs": "9000, WARMUP.H"}, "wait": []})
    m2 = tomllib.loads(cfg.read_text())["machine"][1]
    assert m2["warmup_programs"] == ["^O?9000$", "^WARMUP\\.H$"]
    assert svc.machines()["machines"][1]["values"]["warmup_programs"] == "9000, WARMUP.H"
    svc.save({"id": "m2", "control": "heidenhain", "values": {"name": "DVF", "host": "192.168.1.22",
              "warmup_programs": ""}, "wait": []})
    assert "warmup_programs" not in tomllib.loads(cfg.read_text())["machine"][1]
