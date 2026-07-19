# NTP Monitor (noise-reduced fork)

[![Publish Docker Image](https://github.com/bastiaanbrink/ntp-monitor/actions/workflows/docker-image.yml/badge.svg)](https://github.com/bastiaanbrink/ntp-monitor/actions/workflows/docker-image.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Image: ghcr.io](https://img.shields.io/badge/image-ghcr.io%2Fbastiaanbrink%2Fntp--monitor-blue)](https://github.com/bastiaanbrink/ntp-monitor/pkgs/container/ntp-monitor)

Monitors an NTP server and sends **Telegram** alerts when the server goes offline, its
time offset drifts out of range, or its sync quality degrades. One lightweight Python
container watches one server; run several to watch several.

This is a fork of [reulnl/ntp-monitor](https://github.com/reulnl/ntp-monitor) that
eliminates the false-positive alerts caused by single-sample network jitter and adds
sync-quality checks, re-notification, and richer alert messages.

## Quick start

Pull the pre-built multi-arch image (`linux/amd64` + `linux/arm64`) — no build needed:

```bash
docker run -d \
  --name NTPmonitor-ntp \
  --restart unless-stopped \
  --network host \
  --log-opt max-size=10m --log-opt max-file=3 \
  -e NTP_SERVER="ntp.example.net" \
  -e TELEGRAM_BOT_TOKEN="your_bot_token" \
  -e TELEGRAM_CHAT_ID="your_chat_id" \
  -e NTP_MONITOR_LOCATION="SITE" \
  -e OFFSET_THRESHOLD="0.05" \
  -e NTP_SAMPLE_COUNT="5" -e ALERT_AFTER="2" -e RECOVER_AFTER="2" \
  -e STRATUM_MAX="1" -e CHECK_LEAP="true" -e ROOT_DISPERSION_MAX="0.5" \
  -e RENOTIFY_INTERVAL="1800" -e REFERENCE_NTP="time.cloudflare.com" \
  ghcr.io/bastiaanbrink/ntp-monitor:latest
```

Prefer declarative config? See [`docker-compose.yml`](docker-compose.yml).

## What's different from upstream

A single UDP NTP measurement is noisy: an occasional 100+ ms reading can appear even when
the server's clock is perfectly fine. Upstream alerts on every such blip (and sends a
recovery the next cycle), which produces alert spam. This fork adds the following, all
**env-driven with defaults that reproduce the original behaviour** — a safe drop-in.

| Improvement | Variables | What it does |
|---|---|---|
| **Median-of-N sampling** | `NTP_SAMPLE_COUNT`, `NTP_SAMPLE_DELAY` | Take N measurements per check and evaluate their **median**, so a lone outlier is filtered out within one check. |
| **Consecutive-strikes debounce** | `ALERT_AFTER`, `RECOVER_AFTER` | Require N consecutive bad checks before alerting (and N good before recovery). A momentary spike no longer trips an alert. |
| **Sync-quality checks** | `STRATUM_MAX`, `CHECK_LEAP`, `ROOT_DISPERSION_MAX` | A server can be reachable and low-offset yet unsynced (stratum 16, leap alarm, dispersion growing after GPS/holdover loss). These catch it early. |
| **Re-notification + retry** | `RENOTIFY_INTERVAL`, `TELEGRAM_RETRY` | Re-send a still-active alert periodically so a sustained problem isn't announced once and forgotten; retry Telegram delivery on failure. |
| **Local-clock disambiguation** | `REFERENCE_NTP` | On an offset breach, cross-check an independent reference. If the offset to it is out-of-range in the same direction, the alert blames *this host's* clock, not the monitored server. |
| **Richer alerts** | — | Structured HTML messages: headline, server, the triggering metric vs. threshold, a stratum/leap/dispersion context line, a timestamp, and outage duration on reminders/recovery. |

## How it behaves

- The monitor loops every `CHECK_INTERVAL` seconds. Each check samples the server
  `NTP_SAMPLE_COUNT` times and evaluates the **median** offset.
- Alerts are **edge-triggered with hysteresis**: one message when a condition goes bad
  (after `ALERT_AFTER` consecutive checks) and one when it recovers (after
  `RECOVER_AFTER`) — not one per cycle.
- **Time to detect** a sustained problem ≈ `ALERT_AFTER × CHECK_INTERVAL`. With
  `CHECK_INTERVAL=60, ALERT_AFTER=2` that's ~2 minutes.
- While a problem persists, `RENOTIFY_INTERVAL` re-sends the alert so it isn't forgotten.
- Each condition (unreachable, offset, stratum, leap, dispersion, local-clock) has its own
  independent state, so they alert and recover separately.

### Example alert

```
⚠️ NTP offset out of range  ·  SRV2
Server: ntp2.as215248.net
Offset: -0.134624s  (threshold ±0.05s)
Stratum: 1  ·  Leap: OK  ·  Dispersion: 0.0001s
🕐 2026-07-19 15:12:03 UTC
```

## Recommended profile

Sensible values for watching a stratum-1 server over a jittery/WAN path:

```
OFFSET_THRESHOLD=0.05      NTP_SAMPLE_COUNT=5     NTP_SAMPLE_DELAY=1
NTP_RETRY_COUNT=2          ALERT_AFTER=2          RECOVER_AFTER=2
STRATUM_MAX=1              CHECK_LEAP=true        ROOT_DISPERSION_MAX=0.5
RENOTIFY_INTERVAL=1800     REFERENCE_NTP=time.cloudflare.com
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `NTP_SERVER` | `pool.ntp.org` | Hostname/IP of the NTP server to check. |
| `OFFSET_THRESHOLD` | `0.5` | Max absolute median offset (seconds) before out-of-range. |
| `CHECK_INTERVAL` | `60` | Seconds between checks. |
| `NTP_RETRY_COUNT` | `1` | Attempts per sample before it counts as failed (reachability). |
| `NTP_TIMEOUT` | `5` | Per-request socket timeout (seconds). |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token. |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID. |
| `TELEGRAM_RETRY` | `3` | Attempts per Telegram message. |
| `NTP_MONITOR_LOCATION` | `""` | Free-text label shown in every alert. |
| `NTP_SAMPLE_COUNT` | `1` | Samples per check; the **median** offset is evaluated. |
| `NTP_SAMPLE_DELAY` | `1` | Seconds between samples within one check. |
| `ALERT_AFTER` | `1` | Consecutive out-of-range checks required before alerting. |
| `RECOVER_AFTER` | `1` | Consecutive in-range checks required before recovery. |
| `STRATUM_MAX` | `0` | If >0, alert when stratum is 0 (kiss-o'-death) or exceeds this. |
| `CHECK_LEAP` | `false` | If true, alert when the leap indicator is 3 (unsynchronised). |
| `ROOT_DISPERSION_MAX` | `0` | If >0 (seconds), alert when root dispersion exceeds it. |
| `RENOTIFY_INTERVAL` | `0` | If >0 (seconds), re-send a still-active alert this often. |
| `REFERENCE_NTP` | `""` | Independent reference for local-clock disambiguation (empty = off). |

With the defaults (`NTP_SAMPLE_COUNT=1`, `ALERT_AFTER=1`, `RECOVER_AFTER=1`, all quality
checks off, `REFERENCE_NTP` empty) the behaviour is identical to upstream.

## Notes

- **`--network host`** lets the container's clock match the host's, which is what you want
  when measuring offset.
- **Log rotation:** the `--log-opt` flags above apply to the `json-file` driver. If your
  Docker daemon uses the **journald** driver, those options are invalid — drop them and let
  journald/systemd handle rotation.
- **Build from source** instead of pulling: `docker build -t ntp-monitor .`

## License

MIT — see [LICENSE](LICENSE). Original work © 2025 reulnl.
