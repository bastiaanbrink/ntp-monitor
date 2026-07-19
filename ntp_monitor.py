import ntplib
import time
import requests
import os
import logging
import socket
import subprocess
import statistics

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Configuration
NTP_SERVER = os.getenv("NTP_SERVER", "pool.ntp.org")
OFFSET_THRESHOLD = float(os.getenv("OFFSET_THRESHOLD", "0.5"))  # in seconds
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))  # in seconds
NTP_RETRY_COUNT = int(os.getenv("NTP_RETRY_COUNT", "1"))  # attempts per sample before it counts as failed

# Noise-reduction knobs (defaults preserve the original single-sample behaviour)
NTP_SAMPLE_COUNT = int(os.getenv("NTP_SAMPLE_COUNT", "1"))   # samples per check; the MEDIAN offset is evaluated
NTP_SAMPLE_DELAY = float(os.getenv("NTP_SAMPLE_DELAY", "1"))  # seconds between samples within one check
ALERT_AFTER = int(os.getenv("ALERT_AFTER", "1"))    # consecutive out-of-range checks before alerting
RECOVER_AFTER = int(os.getenv("RECOVER_AFTER", "1"))  # consecutive in-range checks before recovery

server_unreachable = False
last_offset_out_of_range = False
oor_streak = 0       # consecutive out-of-range checks
inrange_streak = 0   # consecutive in-range checks

def send_telegram_alert(message):
    """Send alert to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_notification": False}
    try:
        response = requests.post(url, json=payload)
        if response.status_code != 200:
            logging.error(f"Failed to send Telegram alert: {response.text}")
    except Exception as e:
        logging.error(f"Error sending Telegram alert: {e}")

def check_dns_resolution(server):
    try:
        ip_address = socket.gethostbyname(server)
        return True, ip_address
    except socket.error:
        return False, None

def check_ping(server):
    try:
        result = subprocess.run(["ping", "-c", "1", server],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "time=" in line:
                    response_time = line.split("time=")[1].split(" ")[0]
                    return True, response_time
        return False, None
    except Exception as e:
        logging.error(f"Ping check failed: {e}")
        return False, None

def query_offset_once():
    """Single NTP request with NTP_RETRY_COUNT attempts. Returns offset or None."""
    for attempt in range(NTP_RETRY_COUNT):
        try:
            client = ntplib.NTPClient()
            response = client.request(NTP_SERVER, version=3)
            return response.offset
        except Exception as e:
            logging.debug(f"Attempt {attempt + 1}/{NTP_RETRY_COUNT}: Error querying {NTP_SERVER}: {e}")
            time.sleep(2)
    return None

def sample_offset():
    """Collect up to NTP_SAMPLE_COUNT offsets and return their median (None if all fail)."""
    offsets = []
    for i in range(NTP_SAMPLE_COUNT):
        offset = query_offset_once()
        if offset is not None:
            offsets.append(offset)
        if i < NTP_SAMPLE_COUNT - 1 and NTP_SAMPLE_DELAY > 0:
            time.sleep(NTP_SAMPLE_DELAY)
    if not offsets:
        return None, []
    return statistics.median(offsets), offsets

def check_ntp_server():
    global server_unreachable, last_offset_out_of_range, oor_streak, inrange_streak

    offset, samples = sample_offset()
    location = os.getenv("NTP_MONITOR_LOCATION", "").strip()

    # ---- Reachability handling ----
    if offset is None:
        if not server_unreachable:
            dns_status, ip_address = check_dns_resolution(NTP_SERVER)
            ping_status, response_time = check_ping(NTP_SERVER)
            message = (
                f"[{location}] 🚨 Alert: NTP server {NTP_SERVER} unreachable.\n"
                f"DNS Resolution: {'Successful, IP: ' + ip_address if dns_status else 'Failed'}\n"
                f"Ping: {'Successful, Response Time: ' + str(response_time) + ' ms' if ping_status else 'Failed'}"
            )
            send_telegram_alert(message)
            server_unreachable = True
        # Reset offset streaks while unreachable so we re-confirm after recovery
        oor_streak = 0
        inrange_streak = 0
        return

    # Reachable again after being down
    if server_unreachable:
        send_telegram_alert(f"[{location}] ✅ Recovery: NTP server {NTP_SERVER} is back online.")
        server_unreachable = False

    detail = "" if len(samples) <= 1 else f" (median of {len(samples)}: {[round(s, 6) for s in samples]})"
    logging.info(f"NTP Server: {NTP_SERVER}, Offset: {offset:.6f} seconds{detail}")

    # ---- Offset threshold handling with debounce ----
    if abs(offset) > OFFSET_THRESHOLD:
        oor_streak += 1
        inrange_streak = 0
        if not last_offset_out_of_range and oor_streak >= ALERT_AFTER:
            message = (f"[{location}] ⚠️ Alert: NTP offset for {NTP_SERVER} out-of-range: {offset:.6f} seconds "
                       f"(Threshold: {OFFSET_THRESHOLD} seconds, {oor_streak} consecutive checks)")
            send_telegram_alert(message)
            last_offset_out_of_range = True
    else:
        inrange_streak += 1
        oor_streak = 0
        if last_offset_out_of_range and inrange_streak >= RECOVER_AFTER:
            message = (f"[{location}] ✅ Recovery: NTP offset for {NTP_SERVER} back within threshold: {offset:.6f} seconds.")
            send_telegram_alert(message)
            last_offset_out_of_range = False

def main():
    while True:
        check_ntp_server()
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
