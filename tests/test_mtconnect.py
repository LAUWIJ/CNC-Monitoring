"""MTConnect: parsing a /current answer into a Sample (made-up agent output in the standard format)."""
from machine_monitor.drivers.mtconnect import parse_current, sample_from_rows
from machine_monitor.model import ExecState, Mode

CURRENT = """<?xml version="1.0" encoding="UTF-8"?>
<MTConnectStreams xmlns="urn:mtconnect.org:MTConnectStreams:1.3">
 <Header creationTime="2026-11-12T12:00:00Z" sender="agent" instanceId="1" version="1.3.0"/>
 <Streams>
  <DeviceStream name="LATHE1" uuid="lathe-1">
   <ComponentStream component="Device" name="LATHE1" componentId="dev">
    <Events><Availability dataItemId="avail" timestamp="t">AVAILABLE</Availability></Events>
   </ComponentStream>
   <ComponentStream component="Controller" name="controller" componentId="cont">
    <Events><EmergencyStop dataItemId="estop" timestamp="t">ARMED</EmergencyStop></Events>
    <Condition><Normal dataItemId="system" timestamp="t" type="SYSTEM"/></Condition>
   </ComponentStream>
   <ComponentStream component="Path" name="path1" componentId="p1">
    <Events>
     <Execution dataItemId="exec" timestamp="t">ACTIVE</Execution>
     <ControllerMode dataItemId="mode" timestamp="t">AUTOMATIC</ControllerMode>
     <Program dataItemId="pgm" subType="MAIN" timestamp="t">LONG_WORKPIECE_NAME_0001.MPF</Program>
     <Program dataItemId="apgm" subType="ACTIVE" timestamp="t">L6</Program>
     <PartCount dataItemId="pc" subType="ALL" timestamp="t">17</PartCount>
     <ToolNumber dataItemId="tool" timestamp="t">12</ToolNumber>
     <Block dataItemId="blk" timestamp="t">G1 X10 F200</Block>
    </Events>
    <Condition><Warning dataItemId="logic" timestamp="t" type="LOGIC_PROGRAM" nativeCode="700207">door is open</Warning></Condition>
   </ComponentStream>
   <ComponentStream component="Path" name="path2" componentId="p2">
    <Events><Execution dataItemId="exec2" timestamp="t">READY</Execution>
     <ControllerMode dataItemId="mode2" timestamp="t">MANUAL</ControllerMode></Events>
   </ComponentStream>
  </DeviceStream>
 </Streams>
</MTConnectStreams>"""


def test_sample_from_current():
    s = sample_from_rows(parse_current(CURRENT))
    assert s.online and s.exec_state == ExecState.RUNNING and s.mode == Mode.AUTO
    assert s.main_program == "LONG_WORKPIECE_NAME_0001.MPF" and s.current_program == "L6"
    assert s.part_count == 17 and s.tool == "12" and s.current_block == "G1 X10 F200" and not s.alarm


def test_second_path_and_states():
    s = sample_from_rows(parse_current(CURRENT), path="path2")
    assert s.exec_state == ExecState.IDLE and s.mode == Mode.MANUAL
    x = CURRENT.replace(">ACTIVE<", ">FEED_HOLD<")
    assert sample_from_rows(parse_current(x)).exec_state == ExecState.PAUSED
    x = CURRENT.replace(">ACTIVE<", ">PROGRAM_COMPLETED<")
    s = sample_from_rows(parse_current(x))
    assert s.exec_state == ExecState.IDLE and s.program_finished
    x = CURRENT.replace(">AVAILABLE<", ">UNAVAILABLE<")
    assert not sample_from_rows(parse_current(x)).online


def test_alarms_from_conditions():
    from machine_monitor.drivers.mtconnect import MTConnectDriver
    d = MTConnectDriver({"id": "m13", "host": "x"})
    d._rows = parse_current(CURRENT)
    assert d.read_alarms() == [{"code": "700207", "text": "(warning) door is open"}]
