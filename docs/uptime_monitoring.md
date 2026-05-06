# Uptime Monitoring (XeanVI)

## External monitor recommendation
True uptime monitoring should run **off-server**. A local VPS cron job cannot alert if the VPS itself is offline.

Recommended external monitor targets:
- `https://xeanvi.com/`
- `https://xeanvi.com/healthz`
- `https://xeanvi.com/readyz`

Recommended settings:
- Check interval: 1 minute or 5 minutes
- Timeout: 10 seconds
- Alert after: 2 consecutive failures
- Alert channels: email, SMS, Telegram, Discord, Slack

## Uptime Kuma option
Example monitor configuration:
- Type: HTTP(s)
- URL: `https://xeanvi.com/readyz`
- Expected status code: 200
- Optional keyword: `ok`
- Interval: 60 seconds
- Retry count: 2

## Local cron fallback
```cron
*/5 * * * * cd /var/www/stock/trader/stock && /var/www/stock/trader/stock/venv/bin/python scripts/uptime_check.py >> /var/log/xeanvi-uptime-check.log 2>&1
```

## Manual test commands
```bash
python scripts/uptime_check.py
python scripts/uptime_check.py --json
```

## Environment variables
- `UPTIME_BASE_URL`: Base URL used for checks. Default `https://xeanvi.com`.
- `UPTIME_CHECK_PATHS`: Comma-separated paths to check. Default `/healthz,/readyz`.
- `UPTIME_TIMEOUT_SECONDS`: Request timeout in seconds. Default `10`.
- `UPTIME_FAIL_WEBHOOK_URL`: Optional webhook called when any check fails.
- `UPTIME_SUCCESS_WEBHOOK_URL`: Optional webhook called when all checks pass.
