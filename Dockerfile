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

# Run the script
CMD ["python", "ntp_monitor.py"]
