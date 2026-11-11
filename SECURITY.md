# Security

- **The dashboard has no login.** Anyone who can reach port 8080 can see all machine data and - if program
  transfer is switched on - download programs from the machines and, with `transfer = "send"`, write programs
  to them. Run it only on a closed shop network. Never forward the port to the internet.
- Program transfer is off by default. Switch it on per machine (`transfer = "read"` or `"send"`) and set a
  PIN for sending (`[transfer] pin = "..."`) if others can reach the dashboard.
- `config.toml` holds IP addresses and passwords (e.g. the OPC UA user). It is in `.gitignore` - keep it out of
  any repository, zip or ticket.
- The control logins used (OPC UA user, LSV2) should have the least rights needed: read only, plus file
  write only on machines where you use sending.

## Reporting a problem

Please report security problems privately via GitHub's "Report a vulnerability" (Security tab of the
repository), not in a public issue.
