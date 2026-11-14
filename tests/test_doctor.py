"""--doctor: read-only checks, problems as FAIL, exit code."""
import json
import socket

from machine_monitor import doctor


def test_doctor_reports_config_and_machines(tmp_path, capsys):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'''[server]
port = 1

[[machine]]
id = "m1"
name = "Up"
control = "mtconnect"
host = "127.0.0.1"
port = {port}

[[machine]]
id = "m2"
name = "Down"
control = "mtconnect"
host = "http://127.0.0.1:1/"
''')
    rc = doctor.main(str(tmp_path), str(cfg), as_json=True)
    out = json.loads(capsys.readouterr().out)
    by = {c["name"]: c for c in out["checks"]}
    assert rc == 0 and out["ok"]
    assert by["config"]["status"] == "OK" and "2 machine(s)" in by["config"]["detail"]
    assert by["machine m1"]["status"] == "OK"
    assert by["machine m2"]["status"] == "WARN" and "fix" in by["machine m2"]
    assert not (tmp_path / "admin.json").exists()                  # nothing created but data/
    srv.close()


def test_doctor_fails_on_bad_config(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[machine]]\nid = "m1"\ncontrol = "okuma"\n[[machine]]\nid = "m1"\ncontrol = "fanuc"\nhost = "x"\n')
    assert doctor.main(str(tmp_path), str(cfg), connect=False) == 1
    out = capsys.readouterr().out
    assert "unknown control" in out and "duplicate id m1" in out
    cfg.write_text("[[machine]\n")
    assert doctor.main(str(tmp_path), str(cfg), connect=False) == 1
