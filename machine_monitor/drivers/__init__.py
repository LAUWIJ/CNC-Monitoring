from .base import Driver, DriverError


def make_driver(cfg: dict) -> Driver:
    control = cfg.get("control", "").lower()
    if control in ("siemens", "sinumerik", "opcua"):
        from .siemens_opcua import SiemensOpcUaDriver
        return SiemensOpcUaDriver(cfg)
    if control in ("heidenhain", "tnc", "lsv2", "dnc"):
        from .heidenhain_lsv2 import HeidenhainLsv2Driver
        return HeidenhainLsv2Driver(cfg)
    if control in ("fanuc", "focas"):
        from .fanuc_focas import FanucFocasDriver
        return FanucFocasDriver(cfg)
    if control in ("simulator", "sim"):
        from .simulator import SimulatedDriver
        return SimulatedDriver(cfg)
    raise ValueError(f"unknown control type '{control}' for machine {cfg.get('id')}")
