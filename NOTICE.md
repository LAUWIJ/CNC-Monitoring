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

The FOCAS2 library (FANUC part number A02B-0207-K737) is licensed by FANUC and is **not** part of this
repository. Each user obtains it from FANUC or their machine builder under FANUC's terms. This program only
loads it at run time if the user has put it in the `FOCAS/` folder.

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
