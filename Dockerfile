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

# Expose environment variables
ENV NTP_SERVER="pool.ntp.org"
ENV OFFSET_THRESHOLD="0.5"
ENV TELEGRAM_BOT_TOKEN=""
ENV TELEGRAM_CHAT_ID=""
ENV CHECK_INTERVAL="60"
ENV NTP_RETRY_COUNT="1"
ENV NTP_MONITOR_LOCATION=""

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

# Run the script
CMD ["python", "ntp_monitor.py"]
