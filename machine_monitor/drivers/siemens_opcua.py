"""Siemens SINUMERIK (840D sl / ONE / 828D) via the built-in OPC UA server.

Requires:  pip install asyncua
Control:   OPC UA option licensed + enabled (default port 4840), a user with
           read rights created in HMI -> Commissioning -> Network -> OPC UA.

All variables are BTSS paths exposed as  ns=2;s=<path>. The defaults below
are for channel 1. Check them once with UaExpert (free) - path names can
differ slightly per software version; any of them can be overridden in
config.toml under [machine.nodes].

Signals used:
  chanStatus             0 RESET, 1 INTERRUPTED, 2 ACTIVE      -> exec state
  opMode (BAG)           0 JOG, 1 MDA, 2 AUTO                  -> mode
  workPNameLong          the program selected/started (main)   -> main_program
  progName               program executing right now (may be a sub) -> current_program
  actParts               workpiece counter ($AC_ACTUAL_PARTS)  -> part_count
  oldProgNetTimeCounter  +1 every time a program ends regularly (M30/M2)

profile = "umati" (config) additionally uses the OPC UA for Machine Tools
companion standard (OPC 40501 / umati) that DN Solutions and others expose
under Objects/Machines/<machine>:
  Monitoring/<channel>/ChannelState   0 Active, 1 Interrupted, 2 Reset
  Monitoring/<channel>/ChannelMode    0 Automatic, 1 MDA, 2 Manual, 3 Other
  Production/ActiveProgram/Name       the MAIN program
  Production/ActiveProgram/State/CurrentState   Running / Ended / Interrupted / Aborted
Counters (actParts, oldProgNetTimeCounter) still come from the SINUMERIK paths.
                         -> program_end_count. This is the most reliable
                            "part finished" signal on SINUMERIK, even when the
                            part counter is not configured.
"""
from __future__ import annotations

import os
import threading
import time

from ..model import ExecState, Mode, Sample
from .base import Driver, DriverError

APP_URI = "urn:machine-monitoring:client"

# asyncua's sync Client normally starts its OWN event-loop thread (+ a few file handles) per Client.
# A connection attempt to a switched-off machine every minute then leaks one thread + files each time
# -> "Too many open files" after some hours. All Siemens machines share ONE loop thread instead.
_TLOOP = None
_TLOOP_LOCK = threading.Lock()


def _shared_tloop():
    global _TLOOP
    with _TLOOP_LOCK:
        if _TLOOP is None:
            from asyncua.sync import ThreadLoop
            t = ThreadLoop()
            t.daemon = True
            t.start()
            _TLOOP = t
        return _TLOOP


def _dispose(client) -> None:
    """Release everything a (possibly failed) client holds."""
    if client is None:
        return
    try:
        client.disconnect()
    except Exception:
        pass
    try:   # only stop the loop if this client owns it (never the shared one)
        if getattr(client, "close_tloop", False) and getattr(client, "tloop", None) is not _TLOOP:
            client.tloop.stop()
    except Exception:
        pass
CERT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "certs")


def ensure_client_certificate() -> tuple:
    """Create a self-signed OPC UA client certificate once (certs/client_cert.der + client_key.pem).
    The first encrypted connection is rejected by the SINUMERIK until this certificate is moved
    to 'trusted' on the control (HMI: Commissioning > Network > OPC UA > Certificates)."""
    cert_p = os.path.join(CERT_DIR, "client_cert.der")
    key_p = os.path.join(CERT_DIR, "client_key.pem")
    if os.path.exists(cert_p) and os.path.exists(key_p):
        return cert_p, key_p
    import datetime
    import socket
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    os.makedirs(CERT_DIR, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MachineMonitoring"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Machine Monitoring")])
    now = datetime.datetime.now(datetime.timezone.utc)
    host = socket.gethostname()
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(APP_URI),
                                                        x509.DNSName(host)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=True,
                                         data_encipherment=True, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH,
                                                  ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    with open(cert_p, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.DER))
    with open(key_p, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    return cert_p, key_p


# Verified on DN Solutions DNM6700 / NHP5000 (SINUMERIK, 2026-11):
#   chanStatus 0 RESET, 1 ACTIVE, 2 INTERRUPTED  (running machine reads 1)
#   progStatus 1 interrupted, 2 stopped, 3 in progress, 4 waiting, 5 aborted/ended
#   workPNameLong is only the workpiece FOLDER; selectedWorkPProg is the main program path
#   actParts and totalParts are enabled independently ($MC_PART_COUNTER) - both are read
DEFAULT_NODES = {
    "chan_status": "/Channel/State/chanStatus",
    "op_mode": "/Bag/State/opMode",
    "main_program": "/Channel/ProgramInfo/selectedWorkPProg",
    "current_program": "/Channel/ProgramInfo/progName",
    "part_count": "/Channel/State/actParts",
    "total_parts": "/Channel/State/totalParts",
    "program_end_count": "/Channel/State/oldProgNetTimeCounter",
    "prog_status": "/Channel/State/progStatus",
}


# extra SINUMERIK paths the probe tries, to find the right main-program variable on a control
PROBE_CANDIDATES = [
    "/Channel/ProgramInfo/selectedWorkPProg",
    "/Channel/ProgramInfo/workPandProgName",
    "/Channel/ProgramInfo/workPName",
    "/Channel/ProgramInfo/progName[u1,1]",
    "/Channel/ProgramInfo/progName[u1,2]",
    "/Channel/ProgramInfo/actLineNumber",
    "/Channel/State/acProg",
    "/Channel/State/actProgNetTime",
    "/Channel/State/totalParts",
    "/Channel/State/reqParts",
    "/Channel/State/feedRateIpoOvr",
    "/Nck/Configuration/numChannels",
    "/Channel/ProgramInfo/actBlock",
    "/Channel/ProgramInfo/lastBlock",
    "/Channel/ProgramInfo/actBlock[u1,1]",
    "/Channel/Parameter/R[u1,1]",
]


def _num(x):
    try:
        return int(getattr(x, "value", x)) if x is not None else None
    except (TypeError, ValueError):
        return None


def _text(x):
    if x is None:
        return None
    return str(getattr(x, "Text", x) or "").strip() or None


def _bname(node) -> str:
    try:
        return node.read_browse_name().Name
    except Exception:
        return "?"


def _child(node, name):
    if node is None:
        return None
    try:
        for ch in node.get_children():
            if _bname(ch) == name:
                return ch
    except Exception:
        pass
    return None


def split_alarm_text(msg: str):
    """'700209 | [M,Door] Now is door request ok' -> ('700209', '[M,Door] Now is door request ok').
    SINUMERIK sends the alarm number at the start of the message; ConditionName is just 'SystemCondition'."""
    import re
    m = re.match(r"\s*(\d{3,7})\s*(?:\||:|-)?\s*(.*)$", msg or "", re.S)
    if m:
        return m.group(1), m.group(2).strip()
    return "", (msg or "").strip()


class _AlarmHandler:
    """Receives OPC UA Alarms & Conditions events (called from the asyncua loop thread) and keeps the
    set of active alarms. Display only - nothing here influences the machine state or cycles."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = {}          # condition key -> {"code", "text"}

    @staticmethod
    def _get(ev, *names):
        for n in names:
            v = getattr(ev, n, None)
            if v is not None:
                return v
        return None

    @staticmethod
    def _txt(v) -> str:
        if v is None:
            return ""
        return str(getattr(v, "Text", v) or "").strip()

    def event_notification(self, ev):
        try:
            msg = self._txt(self._get(ev, "Message"))
            cname = self._txt(self._get(ev, "ConditionName"))
            src = self._txt(self._get(ev, "SourceName"))
            if not msg and not cname:
                return                     # RefreshStart/RefreshEnd and other non-alarm events
            cid = self._get(ev, "NodeId", "ConditionId")
            key = str(cid) if cid is not None else f"{src}|{cname}|{msg}"
            active = self._get(ev, "ActiveState/Id", "ActiveState.Id")
            if active is None:
                st = self._txt(self._get(ev, "ActiveState")).lower()
                active = (st not in ("inactive", "false", "0")) if st else True
            retain = self._get(ev, "Retain")
            on = bool(active) and retain is not False
            code, msg = split_alarm_text(msg)
            if not code:
                code = cname if cname and cname != msg and cname != "SystemCondition" else src
            with self.lock:
                if on:
                    self.active[key] = {"code": code, "text": msg or cname}
                else:
                    self.active.pop(key, None)
        except Exception:                  # never let a strange event break the subscription
            pass

    def status_change_notification(self, status):
        pass

    def datachange_notification(self, node, val, data):
        pass


class SiemensOpcUaDriver(Driver):
    control = "siemens"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.client = None
        nodes = dict(DEFAULT_NODES)
        nodes.update(cfg.get("nodes", {}))
        self.node_paths = {k: v for k, v in nodes.items() if v}
        self.extra_paths = dict(cfg.get("extra_nodes", {}))
        self._nodes = {}
        self._last_counters = {}
        self._parts = 0
        self._parts_delta_max = 0
        # optional end marker written by the NC program, e.g. PROGRAMSTATUS 1000 start / 4001 end
        cs = dict(cfg.get("cycle_signal", {}))
        self.sig_node = cs.get("node") or ""
        self.sig_name = cs.get("name") or (self.sig_node.rsplit("/", 1)[-1] if self.sig_node else "")
        self.sig_start = {float(x) for x in cs.get("start", [])}
        self.sig_end = {float(x) for x in cs.get("end", [])}
        self.sig_ignore = {float(x) for x in cs.get("ignore", [0])}
        self._sig_marker = None
        self._alarms = None             # _AlarmHandler when the A&C subscription works
        self._alarm_sub = None
        self._alarm_tried = False
        self._alarm_warned = False
        if self.sig_node:
            self.node_paths["cycle_signal"] = self.sig_node
        # alternative: end of cycle = a block in the program, e.g. M60 (pallet change / clearance wait).
        # Uses the block being executed right now (/Channel/ProgramInfo/actBlock).
        import re as _re
        self.sig_block_re = _re.compile(cs["block_regex"], _re.I) if cs.get("block_regex") else None
        if self.sig_block_re:
            self.node_paths["act_block"] = cs.get("block_node", "/Channel/ProgramInfo/actBlock")

    def _url(self) -> str:
        return self.cfg.get("endpoint") or f"opc.tcp://{self.cfg['host']}:{self.cfg.get('port', 4840)}"

    def _make_client(self, Client, security=None):
        try:
            c = Client(self._url(), timeout=self.cfg.get("timeout_s", 5), tloop=_shared_tloop())
        except (ImportError, TypeError):           # older asyncua without a shareable loop
            c = Client(self._url(), timeout=self.cfg.get("timeout_s", 5))
        aio = getattr(c, "aio_obj", c)
        aio.application_uri = APP_URI
        aio.name = "MachineMonitoring"
        # SINUMERIK allows only a few sessions. If a connection breaks without a clean logout,
        # the session stays occupied for this long (asyncua default: 1 hour) -> keep it short.
        aio.session_timeout = int(self.cfg.get("session_timeout_s", 60)) * 1000
        if self.cfg.get("username"):
            c.set_user(self.cfg["username"])
            c.set_password(self.cfg.get("password", ""))
        sec = self.cfg.get("security", "") if security is None else security
        if sec:
            # "Basic256Sha256,SignAndEncrypt" -> certificate is created automatically,
            # or give your own: "Basic256Sha256,SignAndEncrypt,cert.der,key.pem"
            parts = [x.strip() for x in sec.split(",")]
            if len(parts) == 2:
                cert_p, key_p = ensure_client_certificate()
                parts += [cert_p, key_p]
            c.set_security_string(",".join(parts))
        return c

    def _nid(self, path: str) -> str:
        return path if path.startswith("ns=") else f"ns=2;s={path}"

    def connect(self) -> None:
        try:
            from asyncua.sync import Client
        except ImportError as e:
            raise DriverError("asyncua not installed: pip install asyncua") from e
        self.close()                       # never keep an old client around
        c = self._make_client(Client)
        try:
            c.connect()
        except Exception:
            _dispose(c)                    # machine off / refused: clean up this attempt completely
            raise
        self.client = c
        allpaths = {**self.node_paths, **{"x:" + k: v for k, v in self.extra_paths.items()}}
        self._nodes = {k: c.get_node(self._nid(p)) for k, p in allpaths.items()}
        self.umati = {}
        if str(self.cfg.get("profile", "btss")).lower() == "umati":
            self.umati = self.discover_umati()
            if not self.umati:
                raise DriverError("profile = umati, but no Objects/Machines/<machine>/Monitoring channel found")
            for k in ("chan_status", "op_mode", "main_program", "prog_status"):
                self._nodes.pop(k, None)          # replaced by umati values
            for k, n in self.umati.items():
                if k != "_path":
                    self._nodes["u:" + k] = n
        self._alarms, self._alarm_sub, self._alarm_tried = None, None, False

    def discover_umati(self) -> dict:
        """Find the umati (OPC 40501 Machine Tools) nodes by browsing names."""
        c = self.client
        machines = _child(c.nodes.objects, "Machines")
        if machines is None:
            return {}
        want = self.cfg.get("umati_machine")
        cands = [m for m in machines.get_children() if _child(m, "Monitoring") is not None]
        if want:
            cands = [m for m in cands if _bname(m) == want] or cands
        if not cands:
            return {}
        m = cands[0]
        mon = _child(m, "Monitoring")
        chans = [ch for ch in mon.get_children() if _child(ch, "ChannelState") is not None]
        want_ch = self.cfg.get("umati_channel")
        if want_ch:
            chans = [ch for ch in chans if _bname(ch) == want_ch] or chans
        out = {"_path": f"Machines/{_bname(m)}"}
        if chans:
            ch = chans[0]
            out["_path"] += f" channel={_bname(ch)}"
            out["channel_state"] = _child(ch, "ChannelState")
            mode = _child(ch, "ChannelMode")
            if mode is not None:
                out["channel_mode"] = mode
        ap = _child(_child(m, "Production"), "ActiveProgram")
        if ap is not None:
            for name, key in (("Name", "program_name"),):
                n = _child(ap, name)
                if n is not None:
                    out[key] = n
            cur = _child(_child(ap, "State"), "CurrentState")
            if cur is not None:
                out["program_state"] = cur
        return out

    def probe(self, lines=None) -> list:
        """Diagnostics: endpoints/security offered by the control + every node read individually."""
        lines = lines if lines is not None else []
        from asyncua.sync import Client
        tok_names = {0: "Anonymous", 1: "UserName", 2: "Certificate", 3: "IssuedToken"}
        mode_names = {1: "None", 2: "Sign", 3: "SignAndEncrypt"}
        try:
            c = self._make_client(Client, "")
            eps = c.connect_and_get_server_endpoints()
            _dispose(c)
            for e in eps:
                toks = []
                for t in (e.UserIdentityTokens or []):
                    tt = getattr(t.TokenType, "value", t.TokenType)
                    pol = (t.SecurityPolicyUri or "").split("#")[-1]
                    toks.append(tok_names.get(tt, str(tt)) + (f"({pol})" if pol else ""))
                m = getattr(e.SecurityMode, "value", e.SecurityMode)
                lines.append(f"endpoint {e.SecurityPolicyUri.split('#')[-1]:<15} {mode_names.get(m, m):<15} logins: {', '.join(toks)}")
        except Exception as e:
            lines.append(f"could not list endpoints: {type(e).__name__}: {e}")

        attempts = [("as configured", None)] if self.cfg.get("security") else [
            ("unencrypted", ""), ("encrypted Basic256Sha256", "Basic256Sha256,SignAndEncrypt")]
        ok = None
        for label, sec in attempts:
            c = None
            try:
                c = self._make_client(Client, sec)
                c.connect()
                _dispose(c)
                lines.append(f"CONNECT {label}: OK - login accepted")
                ok = sec
                break
            except Exception as e:
                _dispose(c)
                lines.append(f"CONNECT {label}: FAILED - {type(e).__name__}: {e}")
        if ok is None:
            if any("Basic256Sha256" in a[0] for a in attempts):
                lines.append(f"client certificate: {os.path.join(CERT_DIR, 'client_cert.der')} - if the encrypted attempt "
                             "was rejected, trust it on the control (OPC UA > Certificates: rejected -> trusted) and run again")
            return lines
        if ok and not self.cfg.get("security"):
            lines.append(f'>>> add   security = "{ok}"   to this machine in config.toml')
        self.cfg = dict(self.cfg, security=ok if ok is not None else self.cfg.get("security"))
        try:
            self.connect()
        except Exception as e:
            lines.append(f"CONNECT FAILED: {type(e).__name__}: {e}")
            return lines
        lines.append("--- configured values")
        for k, n in self._nodes.items():
            try:
                lines.append(f"  OK    {k:<18} {n.nodeid.to_string():<55} = {n.read_value()!r}")
            except Exception as e:
                lines.append(f"  FAIL  {k:<18} {n.nodeid.to_string():<55} -> {type(e).__name__}: {e}")
        lines.append("--- other SINUMERIK candidates")
        for path in PROBE_CANDIDATES:
            try:
                val = self.client.get_node(self._nid(path)).read_value()
                lines.append(f"  OK    {path:<45} = {val!r}")
            except Exception as e:
                lines.append(f"  --    {path:<45} ({type(e).__name__})")
        if self.sig_name:
            lines.append(f"--- searching end-marker variable '{self.sig_name}' (can take up to 2 minutes)")
            self._find_variable(self.sig_name, lines)
        lines.append("--- OPC UA for Machine Tools (umati)")
        try:
            u = self.discover_umati()
            if not u:
                lines.append("  no Objects/Machines/<machine>/Monitoring found - umati not available")
            else:
                lines.append(f"  found {u.get('_path')}")
                for k, n in u.items():
                    if k == "_path":
                        continue
                    try:
                        lines.append(f"  OK    {k:<15} {n.nodeid.to_string():<45} = {n.read_value()!r}")
                    except Exception as e:
                        lines.append(f"  FAIL  {k:<15} -> {type(e).__name__}: {e}")
                lines.append("--- umati tree (variables)")
                lines += self._dump(_child(self.client.nodes.objects, "Machines"), "Machines", 0, [0])
        except Exception as e:
            lines.append(f"  umati browse failed: {type(e).__name__}: {e}")
        self.close()
        return lines

    def _find_variable(self, name: str, out=None) -> list:
        out = out if out is not None else []
        found = []
        guesses = []
        for area in ("SGUD", "MGUD", "UGUD", "GD4", "GD5", "GD6", "GD7", "GD8", "GD9"):
            guesses += [f"/NC/_N_NC_GUD_{area}/{name}",              # DEF NCK ...
                        f"/NC/_N_CH_GUD_{area}/{name}",              # DEF CHAN ...
                        f"/NC/_N_CH_GUD_{area}/{name}[u1]",
                        f"/NC/_N_CH_GUD_{area}/{name}[u1,1]",
                        f"/Channel/_N_CH_GUD_{area}/{name}",
                        f"/Channel/_N_CH_GUD_{area}/{name}[u1]"]
        for i, g in enumerate(guesses):
            try:
                val = self.client.get_node(self._nid(g)).read_value()
                found.append(g)
                out.append(f"  FOUND {g} = {val!r}")
                break
            except Exception as e:
                if i < 6:     # show WHY the SGUD paths fail (unknown node vs. no rights)
                    out.append(f"  --    {g:<45} {type(e).__name__}: {str(e)[:90]}")
        if not found:
            # browse the address space for a node with this name. One request per FOLDER/OBJECT
            # (browse names come with the reply) and variables are never expanded -> fast & light.
            import time as _t
            t0, seen, expanded = _t.time(), 0, 0
            queue = [(self.client.nodes.objects.nodeid, "", 0)]
            skip = {"Drive", "FileSystem", "Methods", "Server", "Alarms", "Tool", "TCTM", "Types", "Views"}
            while queue and expanded < 3000 and _t.time() - t0 < 60:
                nid, path, depth = queue.pop(0)
                try:
                    descs = self.client.get_node(nid).get_children_descriptions()
                except Exception:
                    continue
                expanded += 1
                if expanded % 200 == 0:
                    out.append(f"  ... searched {seen} nodes in {expanded} folders ({_t.time() - t0:.0f} s)")
                for d in descs:
                    seen += 1
                    nm = d.BrowseName.Name
                    p = f"{path}/{nm}"
                    ncls = getattr(d.NodeClass, "value", d.NodeClass)
                    if nm.upper() == name.upper():
                        try:
                            val = self.client.get_node(d.NodeId).read_value()
                        except Exception as e:
                            val = f"<{type(e).__name__}>"
                        out.append(f"  FOUND by browsing: {d.NodeId.to_string()}  ({p}) = {val!r}")
                        found.append(d.NodeId.to_string())
                    elif ncls == 1 and depth < 8 and nm not in skip:      # 1 = Object (folder)
                        queue.append((d.NodeId, p, depth + 1))
                if found:
                    break
            if not found:
                out.append(f"  not found (searched {seen} nodes). Is it a GUD visible to OPC UA, an R-parameter, "
                           "or a $AC_MARKER? Give the exact path as cycle_signal.node in config.toml")
        if found:
            out.append(f'>>> put   node = "{found[0]}"   under [machine.cycle_signal] in config.toml')
        return out

    def _dump(self, node, path, depth, count) -> list:
        """List variables under Machines/ (max depth 6, max 400 lines)."""
        out = []
        if node is None or depth > 6 or count[0] > 400:
            return out
        try:
            children = node.get_children()
        except Exception:
            return out
        for ch in children:
            if count[0] > 400:
                out.append("  ... (truncated)")
                break
            name = _bname(ch)
            p = f"{path}/{name}"
            try:
                nc = ch.read_node_class()
                is_var = getattr(nc, "value", nc) == 2
            except Exception:
                is_var = False
            if is_var:
                try:
                    val = repr(ch.read_value())
                    if len(val) > 120:
                        val = val[:117] + "..."
                except Exception as e:
                    val = f"<{type(e).__name__}>"
                out.append(f"  {p} = {val}")
                count[0] += 1
            if name in ("Equipment", "Tools", "Notification", "Messages", "Identification", "Components"):
                continue    # large / irrelevant subtrees
            out += self._dump(ch, p, depth + 1, count)
        return out

    def _read_all(self) -> dict:
        keys = list(self._nodes)
        nodes = [self._nodes[k] for k in keys]
        try:
            vals = self.client.read_values(nodes)
            return dict(zip(keys, vals))
        except Exception:
            out = {}
            for k, n in zip(keys, nodes):
                try:
                    out[k] = n.read_value()
                except Exception:
                    out[k] = None
            if all(v is None for v in out.values()):
                raise
            return out

    def read(self) -> Sample:
        v = self._read_all()
        finished = aborted = None
        if getattr(self, "umati", None):
            cs = _num(v.get("u:channel_state"))
            exec_state = {0: ExecState.RUNNING, 1: ExecState.PAUSED, 2: ExecState.IDLE}.get(cs, ExecState.IDLE)
            cm = _num(v.get("u:channel_mode"))
            mode = {0: Mode.AUTO, 1: Mode.MDI, 2: Mode.MANUAL}.get(cm, Mode.UNKNOWN) if cm is not None else Mode.UNKNOWN
            v["main_program"] = _text(v.get("u:program_name"))
            st = (_text(v.get("u:program_state")) or "").lower()
            if st:
                finished = st.startswith("ended")
                aborted = st.startswith("aborted")
            extra_u = {"umati_channel_state": cs, "umati_channel_mode": cm, "umati_program_state": st}
        else:
            chan = _num(v.get("chan_status"))
            exec_state = {0: ExecState.IDLE, 1: ExecState.RUNNING, 2: ExecState.PAUSED}.get(
                chan if chan is not None else -1, ExecState.IDLE)
            prog = _num(v.get("prog_status"))
            if exec_state == ExecState.RUNNING and prog in (1, 2):     # program interrupted/stopped
                exec_state = ExecState.PAUSED
            mode = {0: Mode.MANUAL, 1: Mode.MDI, 2: Mode.AUTO}.get(
                int(v["op_mode"]) if v.get("op_mode") is not None else -1, Mode.UNKNOWN)
            extra_u = {}

        def as_int(x):
            try:
                return int(x) if x is not None else None
            except (TypeError, ValueError):
                return None

        # NC-program end marker: end value -> cycle done; program still active -> WAITING (e.g. M60)
        end_marker = None
        wait_reason = None
        if self.sig_node:
            raw = v.get("cycle_signal")
            try:
                val = float(getattr(raw, "value", raw)) if raw is not None else None
            except (TypeError, ValueError):
                val = None
            if val is not None and val not in self.sig_ignore:
                if val in self.sig_end:
                    self._sig_marker = True
                elif val in self.sig_start:
                    self._sig_marker = False
            end_marker = self._sig_marker
            if end_marker and exec_state in (ExecState.RUNNING, ExecState.PAUSED):
                exec_state = ExecState.WAITING
                wait_reason = "operator"
            extra_u["cycle_signal"] = raw
        elif self.sig_block_re:
            blk = _text(v.get("act_block")) or ""
            if blk or exec_state != ExecState.IDLE:
                self._sig_marker = bool(self.sig_block_re.search(blk))
            end_marker = self._sig_marker
            if end_marker and exec_state in (ExecState.RUNNING, ExecState.PAUSED):
                exec_state = ExecState.WAITING
                wait_reason = "operator"
            extra_u["act_block"] = blk.strip()

        # parts: count up whenever EITHER counter rises (each can be enabled separately)
        parts = None
        for key in ("part_count", "total_parts"):
            val = as_int(v.get(key))
            if val is None:
                continue
            last = self._last_counters.get(key)
            self._last_counters[key] = val
            if last is not None and 0 < val - last <= 50:   # bigger jumps = counter set by hand
                self._parts_delta_max = max(self._parts_delta_max, val - last)
            parts = 0
        if parts is not None:
            self._parts += self._parts_delta_max
            self._parts_delta_max = 0
            parts = self._parts

        extra = {k[2:]: v[k] for k in v if k.startswith("x:")}
        extra["progStatus"] = v.get("prog_status")
        extra["actParts"] = v.get("part_count")
        extra["totalParts"] = v.get("total_parts")
        extra.update(extra_u)
        return Sample(
            ts=time.time(), online=True, exec_state=exec_state, mode=mode,
            main_program=(v.get("main_program") or None),
            current_program=(v.get("current_program") or None),
            part_count=parts,
            program_end_count=as_int(v.get("program_end_count")),
            program_finished=finished, program_aborted=aborted,
            end_marker=end_marker, wait_reason=wait_reason,
            extra=extra,
        )

    # ------------------------------------------------------------ alarms
    def _subscribe_alarms(self) -> None:
        """OPC UA Alarms & Conditions on the Server object + ConditionRefresh (sends the alarms that are
        already active). Best effort: if the control doesn't support it, alarms are simply not shown."""
        self._alarm_tried = True
        from asyncua import ua
        h = _AlarmHandler()
        sub = self.client.create_subscription(1000, h)
        try:
            sub.subscribe_events(self.client.get_node(ua.ObjectIds.Server),
                                 self.client.get_node(ua.ObjectIds.AlarmConditionType))
        except Exception:
            try:
                sub.delete()
            except Exception:
                pass
            raise
        self._alarm_sub, self._alarms = sub, h
        sid = getattr(sub, "subscription_id", None)
        if sid is None:
            sid = getattr(getattr(sub, "aio_obj", None), "subscription_id", None)
        if sid is not None:
            try:
                self.client.get_node(ua.ObjectIds.ConditionType).call_method(
                    ua.NodeId(ua.ObjectIds.ConditionType_ConditionRefresh),
                    ua.Variant(sid, ua.VariantType.UInt32))
            except Exception:
                pass                           # no refresh: alarms already active now show up when they change

    def read_alarms(self):
        if self.client is None or not self.cfg.get("alarms", True):
            return None
        if not self._alarm_tried:
            try:
                self._subscribe_alarms()
            except Exception as e:
                if not self._alarm_warned:
                    import logging
                    logging.getLogger("siemens").info("[%s] alarms not available via OPC UA: %s",
                                                      self.cfg.get("id"), e)
                    self._alarm_warned = True
                return None
        if self._alarms is None:
            return None
        with self._alarms.lock:
            return list(self._alarms.active.values())

    def close(self) -> None:
        self._alarms, self._alarm_sub, self._alarm_tried = None, None, False
        if self.client is not None:
            _dispose(self.client)
            self.client = None
