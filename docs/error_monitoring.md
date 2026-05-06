# Error Monitoring (Sentry)

## What Sentry monitors
- Unhandled Flask web app exceptions.
- Celery/background worker exceptions (when Celery integration is available).
- ERROR-level logging events through logging integration.
- Events tagged with release/environment/service name for production triage.

## Create a Sentry project
1. Sign in to Sentry and create a Python project.
2. Copy the DSN from project settings.
3. Set environment variables in `/etc/xeanvi/xeanvi.env`.

## Environment variables (`/etc/xeanvi/xeanvi.env`)
Recommended production values:

```bash
SENTRY_DSN=<your-sentry-dsn>
SENTRY_ENVIRONMENT=production
SENTRY_RELEASE=<git-sha-or-version>
SENTRY_TRACES_SAMPLE_RATE=0.0
SENTRY_PROFILES_SAMPLE_RATE=0.0
SENTRY_SEND_DEFAULT_PII=false
```

## Deploy
```bash
pip install -r requirements.txt
sudo systemctl restart xeanvi
```

## Verify
```bash
python -m py_compile sentry_setup.py app.py tasks.py
sudo journalctl -u xeanvi -n 80 -l --no-pager
```

For manual testing, use a local one-off shell script (not a public route), or trigger a Sentry dashboard test event.

## Security note
- Do **not** enable `SENTRY_SEND_DEFAULT_PII` unless legal/compliance review approves it.
- Sentry events can include stack traces and request context, so secret/PII scrubbing is mandatory.
