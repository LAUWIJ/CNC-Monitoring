"""Control-independent data model.

Every driver (Siemens, Heidenhain, Fanuc, simulator) translates whatever its
control reports into a `Sample`. The cycle engine only ever sees Samples, so
the "is a part being made right now?" logic is written once for all controls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ExecState(str, Enum):
    """What the NC channel is doing right now."""
    RUNNING = "RUNNING"      # program/MDI block is executing (cycle start lit)
    PAUSED = "PAUSED"        # program active but stopped: feed hold, M0/M1, NC-stop, single block wait
    WAITING = "WAITING"      # part finished, program still active but waiting (e.g. M60 pallet clearance)
    IDLE = "IDLE"            # reset state, no program active
    ALARM = "ALARM"          # alarm / e-stop / error, not executing
    OFFLINE = "OFFLINE"      # no connection to the control


class Mode(str, Enum):
    AUTO = "AUTO"            # memory/program run (incl. single block, DNC/remote)
    MDI = "MDI"
    MANUAL = "MANUAL"        # jog, handwheel, ref, edit, ...
    UNKNOWN = "UNKNOWN"


class MachineState(str, Enum):
    """What the dashboard timeline shows."""
    RUNNING = "RUNNING"      # executing a part program in AUTO
    SETUP = "SETUP"          # executing in MDI/manual (setup, probing, warm-up)
    PAUSED = "PAUSED"        # inside a cycle but stopped (feed hold, M0, operator)
    WAITING = "WAITING"      # waiting for the operator (e.g. NHP5000 M60 pallet clearance)
    WAIT_ROBOT = "WAIT_ROBOT"  # waiting for the robot (load/unload handshake)
    IDLE = "IDLE"            # stationary, no program running
    ALARM = "ALARM"
    OFFLINE = "OFFLINE"


@dataclass
class Sample:
    ts: float                                  # unix time (seconds)
    online: bool = True
    exec_state: ExecState = ExecState.IDLE
    mode: Mode = Mode.UNKNOWN
    alarm: bool = False

    # Program identification ------------------------------------------------
    # main_program: the program the operator started, as reported by the
    #   control (Fanuc mdata / pdf main, Heidenhain program stack "main",
    #   Siemens workPNameLong). None if the control can't tell us.
    # current_program: whatever is executing *right now* - may be a sub-program,
    #   an M-code/G-code macro (O9xxx), a cycle, a tool-change program, ...
    main_program: Optional[str] = None
    current_program: Optional[str] = None

    # Strong "a part was finished" signals (any that the control offers) -----
    part_count: Optional[int] = None           # Fanuc #3901, Siemens $AC_ACTUAL_PARTS
    program_end_count: Optional[int] = None    # Siemens oldProgNetTimeCounter (+1 on M30/M2)
    program_finished: Optional[bool] = None    # Heidenhain PgmState.FINISHED
    program_aborted: Optional[bool] = None     # Heidenhain PgmState.CANCELLED / error
    end_marker: Optional[bool] = None          # True while an NC-program end marker is set (e.g. PROGRAMSTATUS=4001)
    wait_reason: Optional[str] = None          # with exec_state WAITING: "operator" (default) or "robot"
    current_block: Optional[str] = None        # NC block being executed, if the driver can read it

    extra: dict = field(default_factory=dict)  # raw values for debugging / dashboard


def display_name(path: Optional[str]) -> Optional[str]:
    """Turn control-specific program paths into something readable.

    Siemens: /_N_WKS_DIR/_N_BRACKET_WPD/_N_BRACKET_MPF -> BRACKET.MPF
    Heidenhain: TNC:\\nc_prog\\Bracket.h            -> Bracket.h
    Fanuc 30i: //CNC_MEM/USER/PATH1/O1234          -> O1234
    """
    if not path:
        return path
    p = str(path).strip().rstrip("\x00").strip()
    if not p:
        return None
    for sep in ("\\", "/"):
        if sep in p:
            p = p.rsplit(sep, 1)[-1] or p
    if p.startswith("_N_"):
        p = p[3:]
        for ext in ("_MPF", "_SPF"):
            if p.endswith(ext):
                p = p[: -len(ext)] + "." + ext[1:]
    return p
