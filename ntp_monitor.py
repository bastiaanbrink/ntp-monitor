import ntplib
import time
import requests
import os
import logging
import socket
import subprocess
import statistics
import html

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
            f"<b>Dispersie:</b> <code>{root_disp:.4f}s</code>")


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
            send_telegram_alert(f"🔁 <b>[herinnering — al {mins} min actief]</b>\n{_resolve(alert_msg)}")
            st["last_notified"] = now
    else:
        st["good"] += 1
        st["bad"] = 0
        if st["active"] and st["good"] >= recover_after:
            mins = int((now - st["since"]) / 60)
            msg = _resolve(recover_msg)
            if mins >= 1:
                msg += f"\n⏱ <b>Duur van de storing:</b> {mins} min"
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
    dns_line = f"✅ OK — <code>{_esc(ip_address)}</code>" if dns_status else "❌ mislukt"
    ping_line = f"✅ OK — <code>{_esc(response_time)} ms</code>" if ping_status else "❌ mislukt"
    return build_msg("🚨", "NTP-server onbereikbaar", NTP_SERVER,
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
        return
    evaluate_condition("unreachable", False, "",
                       build_msg("✅", "NTP-server weer bereikbaar", NTP_SERVER,
                                 ["De server reageert weer normaal."]))

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
        build_msg("🧭", "Lokale klok verdacht", NTP_SERVER, [
            f"Offset naar deze server <b>én</b> naar onafhankelijke referentie zijn beide buiten bereik.",
            f"<b>Offset {_esc(NTP_SERVER)}:</b> <code>{offset:+.6f}s</code>",
            f"<b>Offset {_esc(REFERENCE_NTP)}:</b> <code>{(ref_offset if ref_offset is not None else 0):+.6f}s</code>",
            "➡️ Waarschijnlijk de klok van <b>deze host</b>, niet de server.",
        ]),
        build_msg("✅", "Lokale klok hersteld", NTP_SERVER,
                  [f"<b>Offset:</b> <code>{offset:+.6f}s</code> — weer binnen bereik.", ctx]),
    )
    evaluate_condition(
        "offset", offset_out and not local_clock_suspect,
        build_msg("⚠️", "NTP offset buiten bereik", NTP_SERVER, [
            f"<b>Offset:</b> <code>{offset:+.6f}s</code>  (drempel <code>±{OFFSET_THRESHOLD}s</code>)",
            ctx,
        ]),
        build_msg("✅", "NTP offset hersteld", NTP_SERVER,
                  [f"<b>Offset:</b> <code>{offset:+.6f}s</code> — terug binnen drempel <code>±{OFFSET_THRESHOLD}s</code>.", ctx]),
    )

    # ---- Sync-quality (absolute server properties; not affected by local clock) ----
    if STRATUM_MAX > 0:
        stratum_bad = stratum == 0 or stratum > STRATUM_MAX
        evaluate_condition(
            "stratum", stratum_bad,
            build_msg("🛰️", "NTP stratum verhoogd", NTP_SERVER, [
                f"<b>Stratum:</b> <code>{stratum}</code>  (max <code>{STRATUM_MAX}</code>) — server niet goed gesynchroniseerd.",
                ctx,
            ]),
            build_msg("✅", "NTP stratum hersteld", NTP_SERVER,
                      [f"<b>Stratum:</b> <code>{stratum}</code> — weer normaal.", ctx]),
        )
    if CHECK_LEAP:
        evaluate_condition(
            "leap", leap == 3,
            build_msg("⛔", "NTP leap = UNSYNC (alarm)", NTP_SERVER, [
                "De server meldt <b>leap = unsynchronized</b> — mogelijk GPS/PPS-verlies of holdover.",
                ctx,
            ]),
            build_msg("✅", "NTP leap hersteld", NTP_SERVER,
                      [f"<b>Leap:</b> <code>{_leap_str(leap)}</code> — weer normaal.", ctx]),
        )
    if ROOT_DISPERSION_MAX > 0:
        evaluate_condition(
            "rootdisp", root_disp > ROOT_DISPERSION_MAX,
            build_msg("📈", "NTP root-dispersie hoog", NTP_SERVER, [
                f"<b>Dispersie:</b> <code>{root_disp:.4f}s</code>  (drempel <code>{ROOT_DISPERSION_MAX}s</code>) — hoge sync-onzekerheid (holdover?).",
                ctx,
            ]),
            build_msg("✅", "NTP dispersie hersteld", NTP_SERVER,
                      [f"<b>Dispersie:</b> <code>{root_disp:.4f}s</code> — weer laag.", ctx]),
        )


def main():
    while True:
        check_ntp_server()
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
