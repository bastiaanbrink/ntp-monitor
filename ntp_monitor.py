import ntplib
import time
import requests
import os
import logging
import socket
import subprocess
import statistics
import html
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ----- Configuration -----
NTP_SERVER = os.getenv("NTP_SERVER", "pool.ntp.org")
OFFSET_THRESHOLD = float(os.getenv("OFFSET_THRESHOLD", "0.5"))  # in seconds
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))  # in seconds
NTP_RETRY_COUNT = int(os.getenv("NTP_RETRY_COUNT", "1"))  # attempts per sample before it counts as failed
NTP_TIMEOUT = float(os.getenv("NTP_TIMEOUT", "5"))       # per-request socket timeout (seconds)
LOCATION = os.getenv("NTP_MONITOR_LOCATION", "").strip()

# Address family: monitor IPv4 and IPv6 as separate targets.
#   auto -> let the resolver pick (original behaviour)
#   4    -> force IPv4 (A record) only
#   6    -> force IPv6 (AAAA record) only
# Run two containers (one with 4, one with 6) to watch both paths independently.
NTP_IP_VERSION = os.getenv("NTP_IP_VERSION", "auto").strip().lower()
_FAMILY = {"4": socket.AF_INET, "6": socket.AF_INET6}.get(NTP_IP_VERSION, socket.AF_UNSPEC)
_PROTO_LABEL = {"4": "IPv4", "6": "IPv6"}.get(NTP_IP_VERSION, "auto")

# Noise reduction (defaults preserve the original single-sample behaviour)
NTP_SAMPLE_COUNT = int(os.getenv("NTP_SAMPLE_COUNT", "1"))   # samples per check; the MEDIAN offset is evaluated
NTP_SAMPLE_DELAY = float(os.getenv("NTP_SAMPLE_DELAY", "1"))  # seconds between samples within one check
ALERT_AFTER = int(os.getenv("ALERT_AFTER", "1"))    # consecutive bad checks before alerting
RECOVER_AFTER = int(os.getenv("RECOVER_AFTER", "1"))  # consecutive good checks before recovery

# Sync-quality checks (0 / false = disabled -> drop-in compatible)
STRATUM_MAX = int(os.getenv("STRATUM_MAX", "0"))                 # >0: alert if stratum==0 (kiss-o-death) or stratum>STRATUM_MAX
CHECK_LEAP = os.getenv("CHECK_LEAP", "false").lower() in ("1", "true", "yes", "on")  # alert on leap==3 (unsynchronised)
ROOT_DISPERSION_MAX = float(os.getenv("ROOT_DISPERSION_MAX", "0"))  # >0: alert if root dispersion exceeds this many seconds

# Re-notification & delivery robustness
RENOTIFY_INTERVAL = int(os.getenv("RENOTIFY_INTERVAL", "0"))  # >0: re-send a still-active alert every N seconds
TELEGRAM_RETRY = int(os.getenv("TELEGRAM_RETRY", "3"))        # attempts per Telegram message

# Local-clock disambiguation: on an offset breach, cross-check an INDEPENDENT reference.
REFERENCE_NTP = os.getenv("REFERENCE_NTP", "").strip()  # empty = disabled

# NTS (RFC 8915) key-establishment check (disabled by default -> drop-in compatible).
# Each check opens TLS 1.3 to NTS_PORT with ALPN "ntske/1" (pinned to NTP_IP_VERSION like the NTP query),
# verifies the certificate against NTP_SERVER, performs a real NTS-KE exchange (NTPv4 + AES-SIV-CMAC-256)
# and requires at least one cookie back. Optionally alerts when the certificate is about to expire.
NTS_CHECK = os.getenv("NTS_CHECK", "false").lower() in ("1", "true", "yes", "on")
NTS_PORT = int(os.getenv("NTS_PORT", "4460"))
NTS_CERT_WARN_DAYS = int(os.getenv("NTS_CERT_WARN_DAYS", "0"))  # >0: alert when the cert expires within N days

# Per-condition state: name -> {bad, good, active, last_notified, since}
conditions = {}


# ---------------- Read-only HTTP status server (optional) ----------------
# Enabled only when HTTP_PORT is set; otherwise behaviour is unchanged.
HTTP_PORT = os.getenv("HTTP_PORT", "").strip()
# Address to bind the status server to. Default 0.0.0.0 (original behaviour); with --network host that exposes
# /status and /metrics on every interface, so set e.g. 127.0.0.1 (or a VPN address) on internet-facing hosts.
HTTP_BIND = os.getenv("HTTP_BIND", "0.0.0.0").strip() or "0.0.0.0"

_status_lock = threading.Lock()
_latest_status = {
    "server": NTP_SERVER,
    "location": LOCATION,
    "proto": _PROTO_LABEL,
    "target_ip": None,
    "reachable": None,
    "stratum": None,
    "leap": None,
    "offset_ms": None,
    "offset_median_ms": None,
    "root_dispersion_ms": None,
    "alerting": False,
    "last_check": None,
    "threshold_ms": round(OFFSET_THRESHOLD * 1000.0, 6),
    "interval_s": CHECK_INTERVAL,
    "nts_ok": None,
    "nts_cookies": None,
    "nts_cert_days": None,
    "nts_error": None,
}


def _any_alert_active():
    return any(st.get("active") for st in conditions.values())


def update_status(reachable, stratum=None, leap=None,
                  offset=None, offset_median=None, root_disp=None,
                  proto=None, target_ip=None):
    """Publish the latest computed values for the HTTP status server (non-blocking)."""
    with _status_lock:
        _latest_status.update({
            "server": NTP_SERVER,
            "location": LOCATION,
            "proto": proto if proto is not None else _PROTO_LABEL,
            "target_ip": target_ip,
            "reachable": bool(reachable),
            "stratum": stratum,
            "leap": leap,
            "offset_ms": None if offset is None else round(offset * 1000.0, 6),
            "offset_median_ms": None if offset_median is None else round(offset_median * 1000.0, 6),
            "root_dispersion_ms": None if root_disp is None else round(root_disp * 1000.0, 6),
            "alerting": _any_alert_active(),
            "last_check": datetime.now(timezone.utc).isoformat(),
            "threshold_ms": round(OFFSET_THRESHOLD * 1000.0, 6),
            "interval_s": CHECK_INTERVAL,
        })


def update_nts_status(ok, cookies=None, cert_days=None, error=None):
    with _status_lock:
        _latest_status.update({"nts_ok": ok, "nts_cookies": cookies,
                               "nts_cert_days": cert_days, "nts_error": error})


def _render_metrics():
    with _status_lock:
        s = dict(_latest_status)
    labels = 'server="%s",location="%s",proto="%s"' % (s["server"], s["location"], s["proto"])
    out = []

    def gauge(name, val, help_text):
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} gauge")
        if val is not None:
            out.append(f"{name}{{{labels}}} {val}")

    gauge("ntp_reachable", None if s["reachable"] is None else int(s["reachable"]),
          "1 if the NTP server responded on the last check, else 0")
    gauge("ntp_stratum", s["stratum"], "NTP stratum reported on the last check")
    gauge("ntp_leap", s["leap"], "NTP leap indicator (0 ok, 3 unsync)")
    gauge("ntp_offset_ms", s["offset_ms"], "Last-sample clock offset in milliseconds")
    gauge("ntp_offset_median_ms", s["offset_median_ms"], "Median clock offset in milliseconds")
    gauge("ntp_root_dispersion_ms", s["root_dispersion_ms"], "Root dispersion in milliseconds")
    gauge("ntp_alerting", int(bool(s["alerting"])), "1 if any alert condition is currently active")
    if NTS_CHECK:
        gauge("nts_ok", None if s["nts_ok"] is None else int(s["nts_ok"]),
              "1 if the last NTS-KE exchange succeeded (TLS, ALPN ntske/1, cookies received)")
        gauge("nts_cookies", s["nts_cookies"], "Number of NTS cookies received on the last check")
        gauge("nts_cert_days", s["nts_cert_days"], "Days until the NTS-KE TLS certificate expires")
    return "\n".join(out) + "\n"


class _StatusHandler(BaseHTTPRequestHandler):
    server_version = "ntp-monitor-status/1.0"

    def log_message(self, *args):  # silence per-request logging
        pass

    def _send(self, code, body, content_type):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/status":
            with _status_lock:
                body = json.dumps(_latest_status)
            self._send(200, body, "application/json")
        elif path == "/metrics":
            self._send(200, _render_metrics(), "text/plain; version=0.0.4; charset=utf-8")
        elif path in ("/", "/health", "/healthz"):
            self._send(200, "ok\n", "text/plain; charset=utf-8")
        else:
            self._send(404, "not found\n", "text/plain; charset=utf-8")

    do_HEAD = do_GET


def start_http_server():
    """Start the read-only status server in a daemon thread if HTTP_PORT is set."""
    if not HTTP_PORT:
        return
    try:
        port = int(HTTP_PORT)
    except ValueError:
        logging.error(f"Invalid HTTP_PORT={HTTP_PORT!r}; status server disabled")
        return
    try:
        family = socket.AF_INET6 if ":" in HTTP_BIND else socket.AF_INET
        server_cls = type("_StatusServer", (ThreadingHTTPServer,), {"address_family": family})
        httpd = server_cls((HTTP_BIND, port), _StatusHandler)
    except Exception as e:
        logging.error(f"Failed to start status server on port {port}: {e}")
        return
    threading.Thread(target=httpd.serve_forever, name="status-http", daemon=True).start()
    logging.info(f"Status HTTP server listening on {HTTP_BIND}:{port} (/status, /metrics)")


# ---------------- Telegram ----------------

def send_telegram_alert(message, parse_mode="HTML"):
    """Send a Telegram message (HTML formatted), retrying a few times on failure."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
        "disable_notification": False,
    }
    for attempt in range(max(1, TELEGRAM_RETRY)):
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                return True
            logging.error(f"Telegram send failed (HTTP {response.status_code}): {response.text}")
        except Exception as e:
            logging.error(f"Telegram send error (attempt {attempt + 1}/{TELEGRAM_RETRY}): {e}")
        time.sleep(2)
    return False


# ---------------- Message formatting ----------------

def _esc(value):
    return html.escape(str(value))


def _leap_str(leap):
    return {0: "OK", 1: "+1s", 2: "-1s", 3: "UNSYNC ⛔"}.get(leap, str(leap))


def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")


def _context_line(stratum, leap, root_disp):
    return (f"<b>Stratum:</b> <code>{stratum}</code>  ·  "
            f"<b>Leap:</b> <code>{_leap_str(leap)}</code>  ·  "
            f"<b>Dispersion:</b> <code>{root_disp:.4f}s</code>")


def build_msg(emoji, title, server, body_lines):
    """Assemble a consistent, HTML-formatted Telegram message."""
    tags = []
    if LOCATION:
        tags.append(_esc(LOCATION))
    if _PROTO_LABEL != "auto":
        tags.append(_PROTO_LABEL)
    loc = f"  ·  <b>{' / '.join(tags)}</b>" if tags else ""
    parts = [f"{emoji} <b>{_esc(title)}</b>{loc}",
             f"<b>Server:</b> <code>{_esc(server)}</code>"]
    parts.extend(body_lines)
    parts.append(f"🕐 <code>{_now_str()}</code>")
    return "\n".join(parts)


# ---------------- Condition state machine ----------------

def _resolve(msg):
    return msg() if callable(msg) else msg


def evaluate_condition(name, is_bad, alert_msg, recover_msg,
                       alert_after=None, recover_after=None):
    """Edge-triggered alerting with debounce, periodic re-notification, and duration tracking.

    alert_msg / recover_msg may be strings or zero-arg callables (built lazily so
    expensive work like DNS/ping only runs when a message is actually sent).
    """
    alert_after = ALERT_AFTER if alert_after is None else alert_after
    recover_after = RECOVER_AFTER if recover_after is None else recover_after
    st = conditions.setdefault(name, {"bad": 0, "good": 0, "active": False,
                                      "last_notified": 0.0, "since": 0.0})
    now = time.time()

    if is_bad:
        st["bad"] += 1
        st["good"] = 0
        if not st["active"] and st["bad"] >= alert_after:
            send_telegram_alert(_resolve(alert_msg))
            st["active"] = True
            st["last_notified"] = now
            st["since"] = now
        elif st["active"] and RENOTIFY_INTERVAL > 0 and (now - st["last_notified"]) >= RENOTIFY_INTERVAL:
            mins = int((now - st["since"]) / 60)
            send_telegram_alert(f"🔁 <b>[reminder — active for {mins} min]</b>\n{_resolve(alert_msg)}")
            st["last_notified"] = now
    else:
        st["good"] += 1
        st["bad"] = 0
        if st["active"] and st["good"] >= recover_after:
            mins = int((now - st["since"]) / 60)
            msg = _resolve(recover_msg)
            if mins >= 1:
                msg += f"\n⏱ <b>Outage duration:</b> {mins} min"
            send_telegram_alert(msg)
            st["active"] = False


# ---------------- NTP / diagnostics ----------------

def resolve_target(server):
    """Resolve `server` to a single literal IP of the configured address family.

    Returns (ip, proto) with proto in {"IPv4", "IPv6"}, or (None, None) when the
    server has no address of the requested family (missing A/AAAA record or a
    DNS failure). Passing the literal on to ntplib pins the query to that family.
    """
    try:
        infos = socket.getaddrinfo(server, 123, _FAMILY, socket.SOCK_DGRAM)
    except socket.gaierror:
        return None, None
    if not infos:
        return None, None
    family = infos[0][0]
    ip = infos[0][4][0]
    return ip, ("IPv6" if family == socket.AF_INET6 else "IPv4")


def check_dns_resolution(server):
    """Resolve within the configured family; returns (ok, literal_ip)."""
    ip, _ = resolve_target(server)
    return (ip is not None), ip


def check_ping(server):
    try:
        cmd = ["ping", "-c", "1", "-w", "2"]
        if _FAMILY == socket.AF_INET6:
            cmd.append("-6")
        elif _FAMILY == socket.AF_INET:
            cmd.append("-4")
        cmd.append(server)
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "time=" in line:
                    return True, line.split("time=")[1].split(" ")[0]
        return False, None
    except Exception as e:
        logging.error(f"Ping check failed: {e}")
        return False, None


def query_once(address):
    """One NTP request with NTP_RETRY_COUNT attempts. Returns an NTPStats response or None.

    `address` is a resolved literal IP so the query stays pinned to the chosen family.
    """
    for attempt in range(NTP_RETRY_COUNT):
        try:
            return ntplib.NTPClient().request(address, version=3, timeout=NTP_TIMEOUT)
        except Exception as e:
            logging.debug(f"{address} attempt {attempt + 1}/{NTP_RETRY_COUNT} failed: {e}")
            time.sleep(2)
    return None


def sample_server(address):
    """Collect up to NTP_SAMPLE_COUNT responses from a resolved literal IP."""
    responses = []
    for i in range(NTP_SAMPLE_COUNT):
        r = query_once(address)
        if r is not None:
            responses.append(r)
        if i < NTP_SAMPLE_COUNT - 1 and NTP_SAMPLE_DELAY > 0:
            time.sleep(NTP_SAMPLE_DELAY)
    return responses


def median_offset(responses):
    return statistics.median([r.offset for r in responses])


def same_sign(a, b):
    return (a >= 0) == (b >= 0)


def unreachable_message():
    dns_status, ip_address = check_dns_resolution(NTP_SERVER)
    ping_status, response_time = check_ping(NTP_SERVER)
    dns_line = f"✅ OK — <code>{_esc(ip_address)}</code>" if dns_status else f"❌ no {_PROTO_LABEL} address"
    ping_line = f"✅ OK — <code>{_esc(response_time)} ms</code>" if ping_status else "❌ failed"
    return build_msg("🚨", "NTP server unreachable", NTP_SERVER,
                     [f"<b>DNS ({_PROTO_LABEL}):</b> {dns_line}", f"<b>Ping:</b> {ping_line}"])


def reset_streaks(*names):
    for n in names:
        st = conditions.get(n)
        if st:
            st["bad"] = 0
            st["good"] = 0


# ---------------- NTS-KE (RFC 8915) ----------------

_NTSKE_EOM, _NTSKE_NEXTPROTO, _NTSKE_ERROR, _NTSKE_WARNING, _NTSKE_AEAD, _NTSKE_COOKIE = 0, 1, 2, 3, 4, 5
_AEAD_AES_SIV_CMAC_256 = 15


def _ntske_record(rtype, body=b"", critical=True):
    return ((0x8000 if critical else 0) | rtype).to_bytes(2, "big") + len(body).to_bytes(2, "big") + body


def nts_ke_check(server, address):
    """Real NTS-KE exchange against `address` (literal IP, pinned family) with SNI/verification for `server`.

    Returns dict(ok, cookies, cert_days, error). Uses only the stdlib (ssl); the authenticated NTP exchange
    itself is not performed (needs AES-SIV), but a server that hands out cookies for NTPv4/AES-SIV-CMAC-256
    over a verified TLS 1.3 session is a working NTS server.
    """
    import ssl
    res = {"ok": False, "cookies": 0, "cert_days": None, "error": None}
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.set_alpn_protocols(["ntske/1"])
    try:
        with socket.create_connection((address, NTS_PORT), timeout=NTP_TIMEOUT) as raw:
            with ctx.wrap_socket(raw, server_hostname=server) as tls:
                if tls.selected_alpn_protocol() != "ntske/1":
                    res["error"] = f"ALPN not negotiated ({tls.selected_alpn_protocol()})"
                    return res
                cert = tls.getpeercert()
                if cert and cert.get("notAfter"):
                    res["cert_days"] = int((ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) // 86400)
                tls.sendall(_ntske_record(_NTSKE_NEXTPROTO, (0).to_bytes(2, "big"))
                            + _ntske_record(_NTSKE_AEAD, _AEAD_AES_SIV_CMAC_256.to_bytes(2, "big"))
                            + _ntske_record(_NTSKE_EOM))
                buf = b""
                while True:
                    chunk = tls.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    if _ntske_has_eom(buf):
                        break
    except ssl.SSLCertVerificationError as e:
        res["error"] = f"certificate: {e.verify_message}"
        return res
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        return res
    proto = aead = None
    i = 0
    while i + 4 <= len(buf):
        rtype = int.from_bytes(buf[i:i + 2], "big") & 0x7FFF
        ln = int.from_bytes(buf[i + 2:i + 4], "big")
        body = buf[i + 4:i + 4 + ln]
        i += 4 + ln
        if rtype == _NTSKE_EOM:
            break
        if rtype == _NTSKE_ERROR:
            res["error"] = f"server error record {int.from_bytes(body, 'big')}"
            return res
        if rtype == _NTSKE_NEXTPROTO and len(body) >= 2:
            proto = int.from_bytes(body[:2], "big")
        elif rtype == _NTSKE_AEAD and len(body) >= 2:
            aead = int.from_bytes(body[:2], "big")
        elif rtype == _NTSKE_COOKIE:
            res["cookies"] += 1
    if proto != 0:
        res["error"] = f"no NTPv4 next-protocol in response ({proto})"
    elif aead != _AEAD_AES_SIV_CMAC_256:
        res["error"] = f"AEAD AES-SIV-CMAC-256 not accepted ({aead})"
    elif res["cookies"] < 1:
        res["error"] = "no cookies received"
    else:
        res["ok"] = True
    return res


def _ntske_has_eom(buf):
    i = 0
    while i + 4 <= len(buf):
        rtype = int.from_bytes(buf[i:i + 2], "big") & 0x7FFF
        ln = int.from_bytes(buf[i + 2:i + 4], "big")
        if i + 4 + ln > len(buf):
            return False
        if rtype == _NTSKE_EOM:
            return True
        i += 4 + ln
    return False


def check_nts(target_ip):
    r = nts_ke_check(NTP_SERVER, target_ip)
    days = r["cert_days"]
    if r["ok"]:
        logging.info(f"NTS-KE {NTP_SERVER} [{target_ip}]:{NTS_PORT} OK, cookies={r['cookies']}, cert expires in {days} d")
    else:
        logging.warning(f"NTS-KE {NTP_SERVER} [{target_ip}]:{NTS_PORT} FAILED: {r['error']}")
    evaluate_condition(
        "nts", not r["ok"],
        build_msg("🔐", "NTS key establishment failed", NTP_SERVER, [
            f"<b>NTS-KE:</b> <code>{_esc(target_ip)}:{NTS_PORT}</code> — {_esc(r['error'])}",
            "Plain NTP may still work, but NTS clients cannot authenticate.",
        ]),
        build_msg("✅", "NTS key establishment recovered", NTP_SERVER,
                  [f"<b>NTS-KE:</b> OK — {r['cookies']} cookies, certificate valid for {days} days."]),
    )
    if NTS_CERT_WARN_DAYS > 0 and days is not None:
        evaluate_condition(
            "ntscert", days < NTS_CERT_WARN_DAYS,
            build_msg("📜", "NTS certificate expiring", NTP_SERVER, [
                f"<b>Certificate</b> expires in <code>{days}</code> days (warn below <code>{NTS_CERT_WARN_DAYS}</code>) — renewal failing?",
            ]),
            build_msg("✅", "NTS certificate renewed", NTP_SERVER,
                      [f"<b>Certificate</b> now valid for <code>{days}</code> days."]),
            alert_after=1, recover_after=1,
        )
    update_nts_status(r["ok"], r["cookies"], days, r["error"])


# ---------------- Main check ----------------

def check_ntp_server():
    target_ip, proto = resolve_target(NTP_SERVER)

    # ---- No address of the requested family (missing A/AAAA or DNS failure) ----
    if target_ip is None:
        evaluate_condition("unreachable", True, unreachable_message, "")
        reset_streaks("offset", "localclock", "stratum", "leap", "rootdisp")
        update_status(reachable=False, proto=_PROTO_LABEL, target_ip=None)
        return

    if NTS_CHECK:
        check_nts(target_ip)   # independent of the UDP NTP result (TCP path / filters can differ, cf. IPv4 TCP 4460)

    responses = sample_server(target_ip)

    # ---- Reachability ----
    if not responses:
        evaluate_condition("unreachable", True, unreachable_message, "")
        reset_streaks("offset", "localclock", "stratum", "leap", "rootdisp")
        update_status(reachable=False, proto=proto, target_ip=target_ip)
        return
    evaluate_condition("unreachable", False, "",
                       build_msg("✅", "NTP server back online", NTP_SERVER,
                                 ["The server is responding normally again."]))

    offset = median_offset(responses)
    stratum = max(r.stratum for r in responses)
    leap = 3 if any(r.leap == 3 for r in responses) else responses[-1].leap
    root_disp = statistics.median([r.root_dispersion for r in responses])
    detail = "" if len(responses) <= 1 else f" (median of {len(responses)})"
    logging.info(f"NTP Server: {NTP_SERVER} [{proto} {target_ip}], Offset: {offset:.6f} seconds, "
                 f"stratum={stratum}, leap={leap}, root_disp={root_disp:.4f}s{detail}")

    ctx = _context_line(stratum, leap, root_disp)

    # ---- Offset, with local-clock disambiguation ----
    offset_out = abs(offset) > OFFSET_THRESHOLD
    local_clock_suspect = False
    ref_offset = None
    if offset_out and REFERENCE_NTP:
        ref_ip, _ = resolve_target(REFERENCE_NTP)
        ref = sample_server(ref_ip) if ref_ip else []
        if ref:
            ref_offset = median_offset(ref)
            if abs(ref_offset) > OFFSET_THRESHOLD and same_sign(ref_offset, offset):
                local_clock_suspect = True
                logging.warning(f"Local clock suspect: {NTP_SERVER} offset {offset:.6f}s and "
                                f"reference {REFERENCE_NTP} offset {ref_offset:.6f}s both out of range.")

    evaluate_condition(
        "localclock", local_clock_suspect,
        build_msg("🧭", "Local clock suspect", NTP_SERVER, [
            f"Offset to this server <b>and</b> to an independent reference are both out of range.",
            f"<b>Offset {_esc(NTP_SERVER)}:</b> <code>{offset:+.6f}s</code>",
            f"<b>Offset {_esc(REFERENCE_NTP)}:</b> <code>{(ref_offset if ref_offset is not None else 0):+.6f}s</code>",
            "➡️ Likely <b>this host's</b> clock, not the server.",
        ]),
        build_msg("✅", "Local clock recovered", NTP_SERVER,
                  [f"<b>Offset:</b> <code>{offset:+.6f}s</code> — back within range.", ctx]),
    )
    evaluate_condition(
        "offset", offset_out and not local_clock_suspect,
        build_msg("⚠️", "NTP offset out of range", NTP_SERVER, [
            f"<b>Offset:</b> <code>{offset:+.6f}s</code>  (threshold <code>±{OFFSET_THRESHOLD}s</code>)",
            ctx,
        ]),
        build_msg("✅", "NTP offset recovered", NTP_SERVER,
                  [f"<b>Offset:</b> <code>{offset:+.6f}s</code> — back within threshold <code>±{OFFSET_THRESHOLD}s</code>.", ctx]),
    )

    # ---- Sync-quality (absolute server properties; not affected by local clock) ----
    if STRATUM_MAX > 0:
        stratum_bad = stratum == 0 or stratum > STRATUM_MAX
        evaluate_condition(
            "stratum", stratum_bad,
            build_msg("🛰️", "NTP stratum raised", NTP_SERVER, [
                f"<b>Stratum:</b> <code>{stratum}</code>  (max <code>{STRATUM_MAX}</code>) — server not properly synced.",
                ctx,
            ]),
            build_msg("✅", "NTP stratum recovered", NTP_SERVER,
                      [f"<b>Stratum:</b> <code>{stratum}</code> — back to normal.", ctx]),
        )
    if CHECK_LEAP:
        evaluate_condition(
            "leap", leap == 3,
            build_msg("⛔", "NTP leap = UNSYNC (alarm)", NTP_SERVER, [
                "The server reports <b>leap = unsynchronized</b> — possible GPS/PPS loss or holdover.",
                ctx,
            ]),
            build_msg("✅", "NTP leap recovered", NTP_SERVER,
                      [f"<b>Leap:</b> <code>{_leap_str(leap)}</code> — back to normal.", ctx]),
        )
    if ROOT_DISPERSION_MAX > 0:
        evaluate_condition(
            "rootdisp", root_disp > ROOT_DISPERSION_MAX,
            build_msg("📈", "NTP root dispersion high", NTP_SERVER, [
                f"<b>Dispersion:</b> <code>{root_disp:.4f}s</code>  (threshold <code>{ROOT_DISPERSION_MAX}s</code>) — high sync uncertainty (holdover?).",
                ctx,
            ]),
            build_msg("✅", "NTP dispersion recovered", NTP_SERVER,
                      [f"<b>Dispersion:</b> <code>{root_disp:.4f}s</code> — back to normal.", ctx]),
        )

    update_status(reachable=True, stratum=stratum, leap=leap,
                  offset=responses[-1].offset, offset_median=offset,
                  root_disp=root_disp, proto=proto, target_ip=target_ip)


def main():
    start_http_server()
    while True:
        check_ntp_server()
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
