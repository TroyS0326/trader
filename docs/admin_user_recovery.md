# Admin User Recovery

Purpose: secure admin-only page for account recovery and support.

Route: `/admin/user-recovery`

Requires `ADMIN_EMAIL` in `/etc/xeanvi/xeanvi.env` and authenticated admin session.

Admins can:
- Search users by email/user id/Stripe customer id/Alpaca account id.
- View safe account summary.
- Send password reset emails.
- Clear onboarding flags.
- Mark onboarding complete safely.
- Update local `subscription_status` only.

Admins cannot:
- View passwords.
- View Alpaca token values.
- Directly modify Stripe billing state.
- Impersonate users.

Security notes:
- All routes are login + admin gated.
- Sensitive values are masked or omitted.
- Admin actions are audit-logged via `UserEvent`.

Deployment:
- `pip install -r requirements.txt`
- `sudo systemctl restart xeanvi`
