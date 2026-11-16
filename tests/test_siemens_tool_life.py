"""SINUMERIK 840D tool monitoring -> one-tool groups (values from the NHP5000 probe, 2026-11-16)."""
from machine_monitor.drivers.siemens_opcua import SiemensOpcUaDriver
from machine_monitor.tool_life import group_left

VALUES = {
    "/Tool/Catalogue/numTools": 4,
    "/Tool/Catalogue/toolNo[u1,1]": 1, "/Tool/Catalogue/toolNo[u1,2]": 11,
    "/Tool/Catalogue/toolNo[u1,3]": 17, "/Tool/Catalogue/toolNo[u1,4]": 69,
    "/Tool/Data/toolMon[u1,1]": 0, "/Tool/Data/toolMon[u1,11]": 2,
    "/Tool/Data/toolMon[u1,17]": 2, "/Tool/Data/toolMon[u1,69]": 1,
    "/Tool/Data/toolState[u1,11]": 16515, "/Tool/Data/toolState[u1,17]": 16515, "/Tool/Data/toolState[u1,69]": 3,
    # T11 WPB-11.0: 12 of 1550 left, T17 CENTERBOOR-10: 294 of 650; T69 made a time tool for the test
    **{f"/Tool/Supervision/data[u1,c11,{k}]": v for k, v in {2: 0.0, 4: 12.0, 5: 200.0, 6: 1550.0}.items()},
    **{f"/Tool/Supervision/data[u1,c17,{k}]": v for k, v in {2: 0.0, 4: 294.0, 5: 0.0, 6: 650.0}.items()},
    **{f"/Tool/Supervision/data[u1,c69,{k}]": v for k, v in {2: 42.5, 4: 0.0, 5: 60.0, 6: 0.0}.items()},
}


class Node:
    def __init__(self, path, values):
        self.path, self.values = path, values

    def read_value(self):
        if self.path not in self.values:
            raise RuntimeError("BadNodeIdUnknown")
        return self.values[self.path]


class Client:
    def __init__(self, values):
        self.values = values

    def get_node(self, nid):
        return Node(nid.split(";s=", 1)[1], self.values)

    def read_values(self, nodes):
        return [n.read_value() for n in nodes]      # raises like a real batch read -> per node fallback


def driver(values=VALUES):
    d = SiemensOpcUaDriver({"id": "m9", "host": "x"})
    d.client = Client(dict(values))
    return d


def test_count_and_time_tools():
    d = driver()
    g = {x["grp"]: x for x in d.read_tool_life(None)}
    assert set(g) == {-11, -17, -69}
    assert g[-17]["count_type"] == "cycles" and group_left(g[-17]) == (294, "parts")
    assert group_left(g[-11]) == (12, "parts")
    assert g[-69]["count_type"] == "minutes" and g[-69]["count"] == 17.5 and group_left(g[-69]) == (42, "min")
    assert d.tools_without_life() == {1}


def test_used_up_and_locked():
    v = dict(VALUES)
    v["/Tool/Supervision/data[u1,c11,4]"] = 0.0
    v["/Tool/Data/toolState[u1,17]"] = 16515 | 4
    g = {x["grp"]: x for x in driver(v).read_tool_life({"11", "17"})}
    assert g[-11]["tools"][0]["status"] == 2 and group_left(g[-11]) == (0, "parts")
    assert g[-17]["tools"][0]["status"] == 2


def test_monitoring_switched_off_is_reported():
    v = dict(VALUES)
    v["/Tool/Data/toolMon[u1,17]"] = 0
    d = driver(v)
    assert d.read_tool_life({"17"}) == [] and d.tools_without_life() == {17}


def test_reread_while_waiting_uses_known_tools():
    d = driver()
    d.read_tool_life(None)
    d.client.values["/Tool/Catalogue/numTools"] = 0        # a second full scan would find nothing
    assert {x["grp"] for x in d.read_tool_life(None)} == {-11, -17, -69}
