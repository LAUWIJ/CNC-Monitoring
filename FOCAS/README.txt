Put the FANUC FOCAS2 library files here (not in the repository - licensed by FANUC).
FANUC part number of the FOCAS2 library: A02B-0207-K737 (order from FANUC or your machine builder).
Get them from FANUC or your machine builder.

- Windows, 64-bit Python: Fwlib64.dll + ALL fwlib*64.dll files (fwlibe64.dll, fwlib30i64.dll, fwlib0iD64.dll, ...)
  and set fwlib_path in config.toml to ...\FOCAS\Fwlib64.dll
- Raspberry Pi (Raspberry Pi OS 32-bit): libfwlib32-linux-armv7.so.* - picked up automatically.

See AI_HANDOVER.md, section 4 (FANUC).
