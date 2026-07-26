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

# Per-condition state: name -> {bad, good, active, last_notified, since}
conditions = {}


# ---------------- Read-only HTTP status server (optional) ----------------
# Enabled only when HTTP_PORT is set; otherwise behaviour is unchanged.
HTTP_PORT = os.getenv("HTTP_PORT", "").strip()

_status_lock = threading.Lock()
_latest_status = {
    "server": NTP_SERVER,
    "location": LOCATION,
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
}


def _any_alert_active():
    return any(st.get("active") for st in conditions.values())


def update_status(reachable, stratum=None, leap=None,
                  offset=None, offset_median=None, root_disp=None):
    """Publish the latest computed values for the HTTP status server (non-blocking)."""
    with _status_lock:
        _latest_status.update({
            "server": NTP_SERVER,
            "location": LOCATION,
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


def _render_metrics():
    with _status_lock:
        s = dict(_latest_status)
    labels = 'server="%s",location="%s"' % (s["server"], s["location"])
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
        httpd = ThreadingHTTPServer(("0.0.0.0", port), _StatusHandler)
    except Exception as e:
        logging.error(f"Failed to start status server on port {port}: {e}")
        return
    threading.Thread(target=httpd.serve_forever, name="status-http", daemon=True).start()
    logging.info(f"Status HTTP server listening on 0.0.0.0:{port} (/status, /metrics)")


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
    loc = f"  ·  <b>{_esc(LOCATION)}</b>" if LOCATION else ""
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

def check_dns_resolution(server):
    try:
        return True, socket.gethostbyname(server)
    except socket.error:
        return False, None


def check_ping(server):
    try:
        result = subprocess.run(["ping", "-c", "1", server],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "time=" in line:
                    return True, line.split("time=")[1].split(" ")[0]
        return False, None
    except Exception as e:
        logging.error(f"Ping check failed: {e}")
        return False, None


def query_once(server):
    """One NTP request with NTP_RETRY_COUNT attempts. Returns an NTPStats response or None."""
    for attempt in range(NTP_RETRY_COUNT):
        try:
            return ntplib.NTPClient().request(server, version=3, timeout=NTP_TIMEOUT)
        except Exception as e:
            logging.debug(f"{server} attempt {attempt + 1}/{NTP_RETRY_COUNT} failed: {e}")
            time.sleep(2)
    return None


def sample_server(server):
    """Collect up to NTP_SAMPLE_COUNT responses from a server."""
    responses = []
    for i in range(NTP_SAMPLE_COUNT):
        r = query_once(server)
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
    dns_line = f"✅ OK — <code>{_esc(ip_address)}</code>" if dns_status else "❌ failed"
    ping_line = f"✅ OK — <code>{_esc(response_time)} ms</code>" if ping_status else "❌ failed"
    return build_msg("🚨", "NTP server unreachable", NTP_SERVER,
                     [f"<b>DNS:</b> {dns_line}", f"<b>Ping:</b> {ping_line}"])


def reset_streaks(*names):
    for n in names:
        st = conditions.get(n)
        if st:
            st["bad"] = 0
            st["good"] = 0


# ---------------- Main check ----------------

def check_ntp_server():
    responses = sample_server(NTP_SERVER)

    # ---- Reachability ----
    if not responses:
        evaluate_condition("unreachable", True, unreachable_message, "")
        reset_streaks("offset", "localclock", "stratum", "leap", "rootdisp")
        update_status(reachable=False)
        return
    evaluate_condition("unreachable", False, "",
                       build_msg("✅", "NTP server back online", NTP_SERVER,
                                 ["The server is responding normally again."]))

    offset = median_offset(responses)
    stratum = max(r.stratum for r in responses)
    leap = 3 if any(r.leap == 3 for r in responses) else responses[-1].leap
    root_disp = statistics.median([r.root_dispersion for r in responses])
    detail = "" if len(responses) <= 1 else f" (median of {len(responses)})"
    logging.info(f"NTP Server: {NTP_SERVER}, Offset: {offset:.6f} seconds, "
                 f"stratum={stratum}, leap={leap}, root_disp={root_disp:.4f}s{detail}")

    ctx = _context_line(stratum, leap, root_disp)

    # ---- Offset, with local-clock disambiguation ----
    offset_out = abs(offset) > OFFSET_THRESHOLD
    local_clock_suspect = False
    ref_offset = None
    if offset_out and REFERENCE_NTP:
        ref = sample_server(REFERENCE_NTP)
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
                  root_disp=root_disp)


def main():
    start_http_server()
    while True:
        check_ntp_server()
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
