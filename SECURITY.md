# Security

Machine Monitoring is made for a **closed shop network that you trust**, behind a firewall. Never make it
reachable from the internet (no port forwarding, no public cloud VM) - not even with the admin password set.

## What is protected, and how

| What | Protection |
|---|---|
| Changing machines (⚙ Settings) | Admin password. Chosen on the first visit to the Settings page, stored only as a salted hash (PBKDF2-SHA256, Python standard library) in `data/admin.json`. Five wrong passwords from one computer make that computer wait 5 minutes (other computers can still log in). A login lasts 12 hours, is kept in that browser tab only and ends when the monitor restarts. Forgotten: `reset_admin_pi.bat` (or `python run.py --set-admin-password` on the monitor) sets a new one and logs out every browser. |
| Sending programs to a control | Off by default, switched on per machine (`transfer = "send"`). Optional PIN (`[transfer] pin`); five wrong PINs from one computer make that computer wait 5 minutes. The running/selected program and protected programs (FANUC O8xxx/O9xxx) are never replaced; a replaced program is first copied to `data/transfer_backup/`. |
| Stored passwords | Never sent back to the browser. The HEIDENHAIN PLC code number is used for one login and not stored or logged. |
| `config.toml`, `data/`, `certs/` | Kept out of Git (`.gitignore`); `data/` and `certs/` are also kept out of the deploy package. |

## Known limits

Read these before you install it. They are choices that keep the monitor simple to install, not oversights.

- **No HTTPS.** Everything - including the admin password, the transfer PIN and the PLC code number when they
  are typed - travels over the network as plain text. Anyone who can capture traffic on that network can read
  it. If you need HTTPS, put a reverse proxy (e.g. Caddy or nginx) in front of the monitor and only allow the
  proxy to reach port 8080.
- **Viewing needs no login.** Anyone who can reach port 8080 sees all machine data and history and can use the
  JSON API (`/api/...`, including `/api/about` with the version). On machines where program transfer is on,
  they can also browse the control's program folders and fetch programs - only *sending* is behind the PIN.
- **The first visitor sets the admin password.** Until it is set, anyone who opens ⚙ Settings can choose it.
  Set it right after installing.
- **The transfer PIN is simple.** It is stored in plain text in `config.toml` and is the same for everyone. It
  stops mistakes and casual use, not someone who can read `config.toml` or capture network traffic.
- **Wrong tries are counted per IP address.** Behind a reverse proxy every browser comes from the proxy's
  address, so all of them share one count.
- **Machine passwords are in plain text** in `config.toml` (e.g. the SINUMERIK OPC UA user), because the
  monitor must send them to the control. The same goes for the copies in `data/config_backup/`,
  `data/config.deployed.toml`, and `config.toml.before_deploy` on the PC you deploy from. Keep those files, and
  any copy or backup of them, private.
- **`certs/client_key.pem`** is the private key of the monitor's OPC UA certificate. Whoever has it can connect
  to a SINUMERIK that trusts the monitor. Keep it private.
- **Most control protocols have no login.** FANUC FOCAS (port 8193), HEIDENHAIN LSV2 (19000) and MTConnect
  agents can be read by anything on the network that can reach them - with or without this monitor. The monitor
  only reads, except for program transfer where it is switched on. OPC UA (SINUMERIK) uses a user and
  password, and encryption only when `security` is set.
- **Connection test from the Settings page.** An admin can make the monitor connect to any address and port on
  its network (that is what *Test connection* is for).
- **The FANUC FOCAS library runs as program code** inside the monitor. Only use library files from FANUC or
  your machine builder (the Raspberry Pi library in `FOCAS/` is FANUC's own, unchanged; its SHA-256 is in
  `NOTICE.md`).
- **Copied PLC files** (`data/plc_<id>/`, HEIDENHAIN "Copy from control") belong to the machine builder. Keep
  them on the monitor; don't share them.
- **Database backups** (`~/backups/` on the Pi) hold all production history. Keep them private.
- **The control logins you give the monitor** should have the least rights needed: read only, plus file write
  only on machines where sending is switched on.
- **No automatic updates.** Security fixes go into the newest version only; update with `deploy.bat` (Pi) or by
  pulling the repository.

## Reporting a problem

Please report security problems privately via GitHub's "Report a vulnerability" (Security tab of the
repository), not in a public issue.
