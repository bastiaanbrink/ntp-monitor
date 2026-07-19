# NTP Monitor (noise-reduced fork)

Monitors an NTP server and sends Telegram alerts when the server is offline or the
time offset is out of range. This is a fork of
[reulnl/ntp-monitor](https://github.com/reulnl/ntp-monitor) that reduces false-positive
alerts caused by single-sample network jitter.

## What's different from upstream

A single UDP NTP measurement is noisy: an occasional 100+ ms reading can appear even
when the server's clock is perfectly fine. Upstream alerts on every such blip (and
sends a recovery message the next cycle), which produces alert spam. This fork adds two
optional, env-driven filters. **Defaults keep the original single-sample behaviour**, so
it is a drop-in replacement — you only get the new behaviour once you set the new
variables.

- **Median-of-N sampling** (`NTP_SAMPLE_COUNT`): take N measurements per check and
  evaluate their median offset, so a lone outlier is filtered out within a single check.
- **Consecutive-strikes debounce** (`ALERT_AFTER` / `RECOVER_AFTER`): require N
  consecutive out-of-range checks before alerting, and N in-range checks before
  recovering. A momentary spike no longer trips an alert.

## How to Build and Run

### Build the Docker Image
```bash
docker build -t ntp-monitor .
```

### Run the Docker Image

```bash
docker run -d \
  --restart unless-stopped \
  -e NTP_SERVER="your_ntp_server" \
  -e OFFSET_THRESHOLD="0.05" \
  -e TELEGRAM_BOT_TOKEN="your_telegram_bot_token" \
  -e TELEGRAM_CHAT_ID="your_chat_id" \
  -e CHECK_INTERVAL="60" \
  -e NTP_RETRY_COUNT="2" \
  -e NTP_SAMPLE_COUNT="3" \
  -e ALERT_AFTER="3" \
  -e RECOVER_AFTER="2" \
  -e NTP_MONITOR_LOCATION="CASA" \
  ghcr.io/OWNER/ntp-monitor:latest
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `NTP_SERVER` | `pool.ntp.org` | Hostname/IP of the NTP server to check. |
| `OFFSET_THRESHOLD` | `0.5` | Max absolute offset (seconds) before out-of-range. |
| `CHECK_INTERVAL` | `60` | Seconds between checks. |
| `NTP_RETRY_COUNT` | `1` | Attempts per sample before it counts as failed (reachability). |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token. |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID. |
| `NTP_MONITOR_LOCATION` | `""` | Free-text label prefixed to every alert. |
| `NTP_SAMPLE_COUNT` | `1` | **New.** Samples per check; the **median** offset is evaluated. |
| `NTP_SAMPLE_DELAY` | `1` | **New.** Seconds between samples within one check. |
| `ALERT_AFTER` | `1` | **New.** Consecutive out-of-range checks required before alerting. |
| `RECOVER_AFTER` | `1` | **New.** Consecutive in-range checks required before recovery. |

With the defaults (`NTP_SAMPLE_COUNT=1`, `ALERT_AFTER=1`, `RECOVER_AFTER=1`) the behaviour
is identical to upstream.

## License

MIT — see [LICENSE](LICENSE). Original work © 2025 reulnl.
