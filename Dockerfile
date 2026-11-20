# Use a lightweight Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install required system dependencies (including ping)
RUN apt-get update && apt-get install -y iputils-ping && rm -rf /var/lib/apt/lists/*

# Install required Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the script
COPY ntp_monitor.py .

# Log to stdout unbuffered so `docker logs` reflects checks in real time
# (block-buffered stdout otherwise delays output by minutes/hours).
ENV PYTHONUNBUFFERED="1"

# Expose environment variables
ENV NTP_SERVER="pool.ntp.org"
ENV OFFSET_THRESHOLD="0.5"
ENV TELEGRAM_BOT_TOKEN=""
ENV TELEGRAM_CHAT_ID=""
ENV CHECK_INTERVAL="60"
ENV NTP_RETRY_COUNT="1"
ENV NTP_MONITOR_LOCATION=""

# Address family: auto (default, resolver picks) | 4 (IPv4/A only) | 6 (IPv6/AAAA only).
# Run one container per family to monitor IPv4 and IPv6 paths separately.
ENV NTP_IP_VERSION="auto"

# Noise-reduction knobs (defaults keep the original single-sample behaviour)
ENV NTP_SAMPLE_COUNT="1"
ENV NTP_SAMPLE_DELAY="1"
ENV ALERT_AFTER="1"
ENV RECOVER_AFTER="1"

# Sync-quality checks (0 / false = disabled, so defaults stay a drop-in)
ENV NTP_TIMEOUT="5"
ENV STRATUM_MAX="0"
ENV CHECK_LEAP="false"
ENV ROOT_DISPERSION_MAX="0"

# Re-notification, delivery robustness, and local-clock disambiguation
ENV RENOTIFY_INTERVAL="0"
ENV TELEGRAM_RETRY="3"
ENV REFERENCE_NTP=""

# Optional read-only HTTP status server (empty/unset = disabled = original behaviour)
ENV HTTP_PORT=""
# Bind address for the status server (0.0.0.0 = all interfaces; use 127.0.0.1 on internet-facing hosts)
ENV HTTP_BIND="0.0.0.0"

# Optional NTS (RFC 8915) key-establishment check on TCP NTS_PORT (TLS 1.3, ALPN ntske/1, cookies),
# pinned to NTP_IP_VERSION like the NTP query; NTS_CERT_WARN_DAYS>0 also alerts on certificate expiry.
ENV NTS_CHECK="false"
ENV NTS_PORT="4460"
ENV NTS_CERT_WARN_DAYS="0"

# Run the script
CMD ["python", "ntp_monitor.py"]
