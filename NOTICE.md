# Notices

## License and additional permission

Copyright (C) 2026 Laurens Wijnschenk

Licensed under the GNU Affero General Public License, version 3 or (at your option) any later version.
See `LICENSE`.

**Additional permission under GNU AGPL version 3 section 7**

If you modify this Program, or any covered work, by linking or combining it with the FANUC FOCAS2 library
(or a modified version of that library), containing parts covered by the terms of FANUC's license for that
library, the licensors of this Program grant you additional permission to convey the resulting work.
Corresponding Source for a non-source form of such a combination shall not include the source code for the
parts of the FANUC FOCAS2 library used as well as that of the covered work.

## FANUC FOCAS2

The FOCAS2 library (FANUC part number A02B-0207-K737) is proprietary software of FANUC CORPORATION. It is
**not** covered by this project's license (AGPL) and is only loaded at run time from the `FOCAS/` folder.

- **Windows DLLs: not included.** Each user obtains them from FANUC or their machine builder under FANUC's
  license agreement.
- **Raspberry Pi: `FOCAS/libfwlib32-linux-armv7.so.1.0.5` is included, unchanged.** This is FANUC's
  FOCAS2/Ethernet library for Linux on ARM (32-bit), version 1.0.5. FANUC's *FOCAS2/Ethernet for Linux
  Operator's Manual*, section "Redistribution and Licenses", states: "The FOCAS2/Ethernet library for Linux can
  be redistributed freely." The README of the FOCAS2 library CD (A02B-0207-K737), section "Redistributable
  software", likewise lists `*.so.*.*.*` run-time modules as redistributable with an application.
  SHA-256: `48254b95d21a3f21fcca26bf4936c1e5dea2b65c7427732ae9d5db84f1b0d03a`.
  Copyright (C) FANUC CORPORATION. All rights to it remain with FANUC; FANUC's manual itself is not included.

`machine_monitor/fanuc_card.py` (memory card transfer on some FANUC controls) sends the network messages that
were observed between FANUC's own Program Transfer Tool and a control on the user's own network. It contains
no code from FANUC. Check that this use fits your own FOCAS license before using that part.

## Third-party Python packages (installed separately with pip, not included)

| package | used for | license |
|---|---|---|
| asyncua (FreeOpcUa / opcua-asyncio) | Siemens SINUMERIK, OPC UA | LGPL-3.0-or-later |
| pyLSV2 | HEIDENHAIN TNC, LSV2 | see the package |

Check the current license of each package before you redistribute them.

## Trademarks

FANUC, FOCAS, SINUMERIK, Siemens, HEIDENHAIN, TNC, DN Solutions and other names are trademarks of their
owners. They are used only to describe compatibility. This project is not affiliated with or endorsed by them.
