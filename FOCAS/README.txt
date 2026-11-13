FANUC FOCAS2 library files.

- Raspberry Pi (Raspberry Pi OS 32-bit): libfwlib32-linux-armv7.so.1.0.5 is INCLUDED in this folder
  (FANUC allows the Linux library to be redistributed freely - see NOTICE.md). Picked up automatically.

- Windows: NOT included (licensed by FANUC). FANUC part number of the FOCAS2 library: A02B-0207-K737,
  order from FANUC or your machine builder. With 64-bit Python put Fwlib64.dll + ALL fwlib*64.dll files
  (fwlibe64.dll, fwlib30i64.dll, fwlib0iD64.dll, ...) here and set fwlib_path in config.toml to
  ...\FOCAS\Fwlib64.dll

See AI_HANDOVER.md, section 4 (FANUC).
