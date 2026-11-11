from __future__ import annotations

from ..model import Sample


class DriverError(Exception):
    pass


class Driver:
    """One instance per machine. The collector calls connect() (and again after
    any exception), then read() every poll interval."""

    control = "generic"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def connect(self) -> None:
        raise NotImplementedError

    def read(self) -> Sample:
        raise NotImplementedError

    def read_alarms(self):
        """Active alarms / messages on the control, for display only (they never change states or cycles).
        Returns a list of {"code": str, "text": str}, or None when this control/driver can't read them."""
        return None

    def close(self) -> None:
        pass
