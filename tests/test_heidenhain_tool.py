"""HEIDENHAIN tool in the spindle: address from the PLC compiler output, PLC double word, names from tool.t."""
import os
import tempfile
import unittest

from machine_monitor import plc_messages
from machine_monitor.drivers.heidenhain_lsv2 import HeidenhainLsv2Driver

TABLE = """BEGIN TOOL.T MM
T       NAME                            L           R           TIME1 TIME2 CUR_TIME
9       R390VLAKFREES-ROND-44           +98.9895    +21.9351    32    0     1722.03
59      LANGFAN                         +120        +5          0     0     0
[END]
"""


class FakeCon:
    def __init__(self, value, table=TABLE):
        self.value, self.table, self.reads, self.table_reads = value, table, [], 0

    def read_plc_memory(self, first, mem_type, count):
        self.reads.append(first)
        return [self.value]

    def recive_file(self, remote_path, local_path, override_file=True, binary_mode=False):
        self.table_reads += 1
        with open(local_path, "w", encoding="latin-1") as f:
            f.write(self.table)
        return True


class SymbolTest(unittest.TestCase):
    def test_find_in_map(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "plc", "program"))
            with open(os.path.join(d, "plc", "program", "MarkerByte.def"), "w") as f:
                f.write("DG_tool_in_spindle                       D   ; see B01\nMG_tool_in_spindle_index   D\n")
            with open(os.path.join(d, "plc", "program", "Other.src"), "w") as f:
                f.write("L DG_TOOL_IN_SPINDLE  ; compare with B01 and W20\n")
            with open(os.path.join(d, "plc", "program", "MainPgm.map"), "w") as f:
                f.write("<Global>\\SPINDLE[1].DG_TOOL_IN_SPINDLE D17040 D=0\n"
                        "<Global>\\SPINDLE[0].MG_TOOL_IN_SPINDLE_INDEX D17036 D=3\n"
                        "<Global>\\SPINDLE[0].DG_TOOL_IN_SPINDLE D17032 D=37\n")
            self.assertEqual(plc_messages.build_symbols(d), {"tool_in_spindle": "D17032"})
            self.assertEqual(plc_messages.load_symbols(d)["tool_in_spindle"]["source"],
                             os.path.join("plc", "program", "MainPgm.map"))

    def test_not_found(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(plc_messages.build_symbols(d), {})


class DriverToolTest(unittest.TestCase):
    def drv(self, value, **cfg):
        d = HeidenhainLsv2Driver({"id": "test_no_such_machine", "host": "x", **cfg})
        d.con = FakeCon(value)
        return d

    def test_tool_from_setting_with_name(self):
        d = self.drv(59, tool_in_spindle="D17032")
        self.assertEqual(d._read_tool(), ("59", "LANGFAN"))
        self.assertEqual(d.con.reads, [17032 // 4])
        d._read_tool()
        self.assertEqual(d.con.table_reads, 1)          # tool table not read again for a known tool

    def test_empty_spindle_and_off(self):
        self.assertEqual(self.drv(0, tool_in_spindle="D17032")._read_tool(), (None, None))
        self.assertEqual(self.drv(59, tool_in_spindle=False)._read_tool(), (None, None))
        self.assertEqual(self.drv(59)._read_tool(), (None, None))    # no address known: nothing read

    def test_life_kept(self):
        d = self.drv(9, tool_in_spindle="17032")
        self.assertEqual(d._read_tool(), ("9", "R390VLAKFREES-ROND-44"))
        self.assertEqual(d._tool_life["9"]["time1"], 32.0)


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------------------ tool life per tool + when tool.t is read
LIFE_TABLE = """BEGIN TOOL.T MM
T       NAME                            L           R           TL RT      TIME1 TIME2 CUR_TIME
9       FACEMILL-44                     +98.9895    +21.9351               60    0     20.5
59      LANGFAN                         +120        +5                     0     0     0
[END]
"""


def test_read_tool_life_per_tool_as_minus_t_group():
    d = HeidenhainLsv2Driver({"id": "test_no_such_machine", "host": "x", "tool_in_spindle": "D17032"})
    d.con = FakeCon(9, LIFE_TABLE)
    rows = d.read_tool_life(None)
    assert len(rows) == 1 and rows[0]["grp"] == -9 and rows[0]["life"] == 60 and rows[0]["count"] == 20.5
    from machine_monitor.tool_life import group_left
    assert group_left(rows[0]) == (39, "min")
    # right after: a very short part does not read the table again
    assert d.read_tool_life({"9"}) == [] and d.con.table_reads == 1


def test_table_read_at_part_start_only_after_setup():
    from types import SimpleNamespace as NS
    d = HeidenhainLsv2Driver({"id": "test_no_such_machine", "host": "x", "tool_in_spindle": "D17032"})
    d.con = FakeCon(9, LIFE_TABLE)
    state = {"ex": "AUTOMATIC", "pg": "STARTED", "main": "TNC:\\A.H"}
    d.con.execution_state = lambda: state["ex"]
    d.con.program_status = lambda: state["pg"]
    d.con.program_stack = lambda: NS(main=state["main"], current=state["main"], line_no=1)

    def step(ex, pg, main="TNC:\\A.H", wait=True):
        state.update(ex=ex, pg=pg, main=main)
        if wait:
            d._tool_table_ts -= 100          # pretend time passed (min gap between reads)
        return d.read()

    s = step("AUTOMATIC", "STARTED")           # first part after connecting: read (setup flag starts on)
    n = d.con.table_reads
    assert n == 1 and s.tool == "9" and s.tool_name == "FACEMILL-44"
    step("AUTOMATIC", "IDLE"); step("AUTOMATIC", "STARTED")       # next part, same program, no setup
    assert d.con.table_reads == n
    step("MANUAL", "IDLE"); step("AUTOMATIC", "STARTED")          # setup in between -> read at the start
    assert d.con.table_reads == n + 1
    step("AUTOMATIC", "IDLE"); step("AUTOMATIC", "STARTED", main="TNC:\\B.H")   # other program -> read
    assert d.con.table_reads == n + 2


def test_card_shows_heidenhain_tool_life_in_parts(tmp_path):
    from machine_monitor.engine import Cycle
    from machine_monitor.store import Store
    st = Store(str(tmp_path / "t.db"))
    g = {"grp": -9, "count_type": "minutes", "life": 60, "count": 20.5, "ntool": 1, "nfree": 1, "use_tool": 1,
         "tools": [{"num": 9, "status": 1}]}
    assert st.add_tool_life("m", 1000.0, g)
    for i in range(3):     # T9 runs 5 min per part
        st.add_cycle(Cycle("m", "A", None, "control", 100 + i * 400, end_ts=400 + i * 400, result="COMPLETED",
                           tool_times={"9": 300, "59": 60},
                           tool_info={"9": {"name": "FACEMILL-44", "group": None}, "59": {"name": "LANGFAN", "group": None}}))
    r = st.tool_status("m", "A", {"tool": "59", "tool_name": "LANGFAN", "cycle_start": 2000,
                                  "cycle_tool_times": {"9": 120}})
    # 39 min left, 2 min of it already used in this part -> 37 min / 5 min per part = 7 parts
    assert r["tool_left"] == 7 and r["tool_left_unit"] == "parts" and r["tool_left_min"] == 37
    assert r["tool_left_group"] is None and r["tool_left_tool"] == 9 and r["tool_left_name"] == "FACEMILL-44"


def test_address_from_binary_map_file():
    """Layout as in the DVF BMO's MainPgm.map (PLCCOMP V 4.10), with made-up neighbours."""
    import struct
    data = (b"PLCCOMP V 4.10 MAPFILE   \x00" + b"\x00" * 40
            + b"DG_TEMPERATURE_SPINDLE\x00\x00\x00D\x00" + struct.pack("<I", 8220) + b"\x00" * 30
            + b"SPINDLE" + b"\x00" * 18 + b"s\x00" + struct.pack("<I", 16632) + b"SPINDLESTRUKTUR\x00" + b"\x00" * 20
            + b"\xc6\x85\x02\x00D\x00\x00\x00" + struct.pack("<I", 400) + b"\x00" * 4 + b"DG_TOOL_IN_SPINDLE\x00OVER\x00")
    assert plc_messages.map_address(data, "DG_TOOL_IN_SPINDLE", ["SPINDLE"]) == "D17032"
    assert plc_messages.map_address(data, "DG_TEMPERATURE_SPINDLE", []) == "D8220"
    assert plc_messages.map_address(data, "DG_TOOL_IN_SPINDLE", ["OTHER"]) is None
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "plc", "program"))
        with open(os.path.join(d, "plc", "program", "MainPgm.map"), "wb") as f:
            f.write(data)
        with open(os.path.join(d, "plc", "program", "Spindle.src"), "w") as f:
            f.write("L Spindle[KG_S1].DG_tool_in_spindle  ; B01\n")
        assert plc_messages.build_symbols(d) == {"tool_in_spindle": "D17032"}



def test_address_from_compiler_listing_text():
    """The form HEIDENHAIN's example lists use: '<Global>\\SPINDLE[0].DG_TOOL_IN_SPINDLE D16356 D=12'."""
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "plc", "program"))
        with open(os.path.join(d, "plc", "program", "MainPgm.lst"), "w") as f:
            f.write("  L Spindle[KG_S1].DG_tool_in_spindle   ; B01\n"
                    "<Global>\\SPINDLE[0].DG_TOOL_IN_SPINDLE D16356 D=12\n")
        assert plc_messages.build_symbols(d) == {"tool_in_spindle": "D16356"}


LISTING = """PLCCOMP V6.21  MainPgm.src
                            12    DG_tool_in_spindle                       D
001000 11 22 33 44 55 66    20     PS     K&SPINDLE[0].DG_TOOL_IN_SPINDLE
       00 00 00 00 00 00 
       00 00 00 00 00 C7 
       04 02 E4 3F 00 00 
001020 11 22 33 44 55 66    20     PS     K+1
       04 02 01 00 00 00 
001040 11 22 33 44 55 66    30   L      Spindle[KG_S1].DG_tool_in_spindle
"""


def test_address_from_compiler_listing_code(tmp_path):
    """DVF Erowa: the listing has no address for the member, but the code that pushes K&SPINDLE[0].DG_TOOL_IN_SPINDLE
    holds it as a constant (C7 04 02 E4 3F 00 00 -> 0x3FE4 = 16356)."""
    p = tmp_path / "plc" / "program"
    p.mkdir(parents=True)
    (p / "MainPgm.lst").write_text(LISTING, encoding="latin-1")
    assert plc_messages.listing_addresses(str(p / "MainPgm.lst"), "DG_TOOL_IN_SPINDLE") == ["D16356"]
    assert plc_messages.build_symbols(str(tmp_path)) == {"tool_in_spindle": "D16356"}


def test_several_candidates_keep_the_one_with_the_last_called_tool(tmp_path, monkeypatch):
    import json
    import machine_monitor.drivers.heidenhain_lsv2 as h
    monkeypatch.setattr(h, "PROJECT_DIR", str(tmp_path))
    dest = tmp_path / "data" / "plc_m2"
    dest.mkdir(parents=True)
    (dest / "plc_symbols.json").write_text(json.dumps({"tool_in_spindle": {"candidates": ["D17032", "D16356"]}}))
    table = """BEGIN TOOL.T MM
T       NAME                            L           LAST_USE
2       VF-6.0-UNI                      +113.4      09:15:15 06.11.2026
3       VF-8.0-UNI                      +119.8      11:36:53 12.11.2026
[END]
"""

    class Con(FakeCon):
        def read_plc_memory(self, first, mem_type, count):
            return [{16356 // 4: 3, 17032 // 4: 0}.get(first, 0)]

    d = HeidenhainLsv2Driver({"id": "m2", "host": "x"})
    d.con = Con(0, table)
    assert d._read_tool() == ("3", "VF-8.0-UNI")


def test_itnc530_uses_spindle_status_and_single_fields():
    """VC630 (iTNC 530): tool number from spindle_tool_status, name and life per field - no table, no PLC."""
    from types import SimpleNamespace as NS
    fields = {(8, "NAME"): "VLAKFREES-44.0-ALU", (8, "TIME1"): 60, (8, "TIME2"): 0, (8, "CUR_TIME"): None,
              (8, "CUR.TIME"): 12.5, (8, "TL"): "", (5, "TIME1"): 0, (5, "TIME2"): 0}

    class Con:
        reads = []

        def spindle_tool_status(self):
            return NS(number=8, index=0, axis="Z", length=120.0, radius=22.0)

        def read_data_path(self, path):
            self.reads.append(path)
            _, _, _, _, t, col = path.split("/")
            return fields.get((int(t), col))

    d = HeidenhainLsv2Driver({"id": "m3", "host": "x"})
    d.con, d._itnc, d._tool_names = Con(), True, {}
    assert d._read_tool() == ("8", "VLAKFREES-44.0-ALU")
    d._read_tool()
    assert d.con.reads.count("/TABLE/TOOL/T/8/NAME") == 1            # name asked once per tool
    rows = d.read_tool_life({"8", "5"})
    assert len(rows) == 1 and rows[0]["grp"] == -8 and rows[0]["life"] == 60 and rows[0]["count"] == 12.5


def test_route_chosen_by_what_the_control_answers():
    from types import SimpleNamespace as NS
    from machine_monitor.drivers.heidenhain_lsv2 import _spindle_status_works
    assert _spindle_status_works(NS(spindle_tool_status=lambda: NS(number=8, axis="Z")))      # iTNC 530
    assert _spindle_status_works(NS(spindle_tool_status=lambda: NS(number=0, axis="Z")))      # empty spindle
    assert not _spindle_status_works(NS(spindle_tool_status=lambda: None))                    # TNC 640
    assert not _spindle_status_works(NS())                                                    # old pyLSV2

    def boom():
        raise OSError("refused")
    assert not _spindle_status_works(NS(spindle_tool_status=boom))
