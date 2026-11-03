# NEXORA v4.1 — Application Security Checklist Mapping

This implementation maps to the nine checks in the supplied `AppSecurityChecklist.pdf`. Security depends on correct environment settings, database migration, SMTP configuration, and deployment over HTTPS.

## 1. Login protection
- Login POST rate limit: 5 attempts per 15 minutes per client IP.
- Per-account failed-attempt tracking and temporary lock state are persisted.
- A simple arithmetic challenge appears after three failed attempts in the browser session.
- Failed and successful login events are logged without plaintext passwords or tokens.
- Note: a simple arithmetic challenge is not equivalent to a specialist CAPTCHA service; add a provider CAPTCHA for internet-facing deployments if threat levels require it.

## 2. Signup and verification
- Server-side username/email/password validation; password minimum is 12 characters.
- Database unique constraints remain responsible for unique usernames/emails.
- New local-password accounts require email verification, using a single-use hashed token that expires after 24 hours.
- Generic responses are used for duplicate registration.
- SMTP must be configured for verification mail; existing users are marked verified by the migration.

## 3. Session security
- Flask signed sessions use HttpOnly and SameSite cookies, one-hour permanent-session lifetime, and session rotation after authentication.
- HTTPS deployments must set `SESSION_COOKIE_SECURE=true`.
- A `session_version` invalidates old browser sessions after password reset.
- State-changing routes use Flask-WTF CSRF protection; logout is POST-only.

## 4. Error-message security
- Authentication/recovery responses avoid confirming whether an account exists.
- Unexpected HTTP 500 responses return a generic message; details remain in server logs.
- User-facing mail/queue failures no longer include raw exception strings.

## 5. Password reset
- Dual recovery options: secure 6-digit verification OTP and direct one-click reset link sent directly to the user's Gmail/email (expires in 15–30 minutes).
- Both reset tokens and OTP codes are stored securely as cryptographic hashes and marked single-use upon completion.
- Password reset revokes the Google OAuth token, stops monitoring, and increments the session version.
- Password reset request responses avoid user enumeration.

## 6. Multi-factor authentication
- TOTP authenticator setup and sign-in challenge are available in Settings.
- One-time backup codes are shown at enrollment and stored as hashes; successful backup-code use removes that code.
- Disabling MFA requires a current TOTP code.
- Administrator-only route decorators require MFA. There are no separate administrator management endpoints in this build.

## 7. Backend and API security
- Protected routes use login checks and email/notification records are scoped to the authenticated user.
- SQL access uses SQLAlchemy ORM/parameterized queries.
- Forms validate inputs on the server; profile uploads enforce size and file-signature checks.
- Global CSRF protection and a request-body size limit are enabled.

## 8. Logging and monitoring
- Login successes/failures and MFA events are written to the audit log/application log.
- Identity values in failed-login log lines are hashed; passwords, OAuth tokens, and backup codes are not logged.
- Configure central log retention/alerting in production; this package does not configure a hosted alerting provider.

## 9. Authorization (RBAC)
- User roles are stored server-side and users cannot self-promote.
- Resource queries filter by the active user's ID.
- Role-required routes enforce roles on the server; administrator access requires MFA.

## Deployment checklist
1. Back up PostgreSQL before upgrading.
2. Keep `.env` out of ZIPs and version control. Rotate any credentials that appeared in the previously uploaded archive.
3. Set a strong random `FLASK_SECRET_KEY`, PostgreSQL credentials, Redis, Google OAuth and Twilio credentials.
4. Configure SMTP settings and `APP_BASE_URL` so email verification links point to the correct host.
5. Install dependencies and run `flask --app run.py db upgrade`.
6. In production, serve only over HTTPS, set `SESSION_COOKIE_SECURE=true`, and set `NEXORA_ALLOW_INSECURE_COOKIES=false`.
7. Test registration/verification, login rate limit, reset token expiry/reuse, MFA enrollment/backup codes, cross-user resource access, CSRF rejection, and audit logs.
