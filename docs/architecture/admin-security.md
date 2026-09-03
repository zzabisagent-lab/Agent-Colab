# Admin security (P4-08/P4-09/P4-10/P4-14)

## MFA (P4-09)

- TOTP per RFC 6238 (30 s step, 6 digits, SHA-1, ±1 step) in `server/security/totp.py`
  (standard library only). Enrollment `POST /api/v1/auth/mfa/enroll` returns the otpauth URI and
  8 single-use recovery codes **once**; the secret is envelope-encrypted with a per-account DEK
  (`mfa_enrollments.secret_ciphertext` + `key_ref`), recovery codes are stored as SHA-256 hashes
  (`recovery_codes`). Storage is shared with Setup through `server/security/mfa_store.py`.
- `POST /mfa/confirm {code}` activates the enrollment; `POST /mfa/verify {code}` and
  `POST /mfa/recovery {recovery_code}` write a **re-authentication proof** (`session_mfa`):
  session-bound for cookie sessions (also sets `account_sessions.mfa_verified_at/reauth_at`),
  account-bound (`session_id NULL`) for Bearer API clients. `GET /mfa` reports status.
- Policy (`server/security/policy.py`, env `AGENT_COLAB_SECURITY_*` overrides, settings reader
  seam for P4-04): MFA is mandatory for `role-system-owner` / `role-administrator` or any role
  granting an `admin.*` permission; Members follow `security.mfa_members` (default off);
  Agent/service Accounts are excluded (`MFA_NOT_APPLICABLE`).
- A cookie session of an MFA-required Account without a proof is limited to safe methods and the
  MFA endpoints: other writes get `403 MFA_REQUIRED` (session policy middleware).
- OIDC: `server/security/oidc.py` defines the provider interface (authorization URL, code
  exchange, id-token validation) with a fake provider for tests; **not enabled by default**
  (`AGENT_COLAB_OIDC_PROVIDER` unset). A provider assertion counts as MFA only with `amr` ∋ `mfa`.

## Re-authentication for critical actions

`server/security/reauth.py::require_recent_mfa` fails closed; `create_app` installs the real
verifier (`server/security/reauth_verifier.py`) that reads the latest unexpired `session_mfa`
proof for the account + session (max age `security.reauth_max_age_s`, default 300 s). Critical
actions: HIGH+ approval decisions, break-glass, setup reconfiguration, hard delete. Client claims
(`reauth_verified` in bodies or headers) are ignored everywhere; the Phase 1
`POST /api/v1/approvals/{id}/decide` now derives re-authentication from the proof only.

## Admin web security (P4-08)

Middleware order (outermost first): `SecurityHeadersMiddleware` (CSP `default-src 'self'`,
`X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`,
HSTS when `base_url` is https) → `SessionPolicyMiddleware` (idle expiry
`security.session_idle_s` via `account_sessions.last_seen_at`, absolute expiry from the session
row, MFA gate, break-glass action recording) → `CsrfMiddleware` (double-submit:
`GET /api/v1/auth/csrf` sets `agent_colab_csrf` and returns the token; state-changing requests
authenticated by the session cookie must send `X-CSRF-Token`; Bearer requests are exempt;
`CSRF_TOKEN_INVALID` 403 otherwise). Session cookies are HttpOnly, SameSite=Strict, Secure off
loopback. Rate limits (`server/security/ratelimit.py`, `auth_rate_limits`): 6 failures within 15
minutes per IP and per credential fingerprint block for 15 minutes with `429 RATE_LIMITED` and one
redacted audit row (`<action>.rate_limited`) per rejection; applied to MFA confirm/verify/recovery
and break-glass activation. Authorization is always server-side: cookie and Bearer principals
get identical decisions (V-P4-08 API parity test).

## Break-glass (P4-10)

`POST /api/v1/breakglass/activate {recovery_code, totp_code, scope, reason}`: System Owner only
(`admin.break_glass`, denials normalized to 404), a single-use recovery code **and** a fresh TOTP
(both proofs, rate-limited). Opens `breakglass_sessions` (TTL `security.breakglass_ttl_s`, default
60 min), appends `BREAK_GLASS_STARTED` (aggregate `break_glass`), announces immediately in the ops
channel through the notification outbox, audits `breakglass.activate`. Requests carrying
`X-Break-Glass-Session` are recorded in `breakglass_actions` and audited (`breakglass.action`).
`POST /{id}/terminate` and the expiry sweep (`POST /sweep`, gateway maintenance tick) append
`BREAK_GLASS_ENDED`, announce, and open the automatic post-hoc verification Task (HIGH, criterion
"justification and every action reviewed") with a VerificationRun assigned by the §7D.2 engine
(Owner = implementer, excluded) — in a separate transaction so independent audit writes never
wait on the ending transaction's audit-chain lock. Break-glass changes no authority: Event rows
stay append-only (DB triggers) and no plaintext secret read path exists.

## Approvals queue (P4-14)

`GET /api/v1/approvals/queue` lists pending grants with risk, `quorum_required`/`quorum_current`/
`quorum_remaining`, decision path, `reauth_required`/`reauth_satisfied`, escalation role and
whether the caller already decided. `POST /api/v1/approvals/{id}/queue-decide {decision, reason}`
decides with the server-side proof: HIGH+ without a recent proof → `403 REAUTH_REQUIRED`; CRITICAL
needs two distinct Humans (`APPROVAL_DUPLICATE_APPROVER` for the same Human twice); Agents and
services get `APPROVAL_HUMAN_ONLY`. The Phase 1 approval service (eligibility, quorum ledger,
escalation events) is reused unchanged.

## Error codes

`MFA_REQUIRED`, `MFA_NOT_ENROLLED`, `MFA_ALREADY_ENROLLED`, `MFA_CODE_INVALID`,
`MFA_RECOVERY_CODE_INVALID`, `MFA_NOT_APPLICABLE`, `MFA_CRYPTO_UNAVAILABLE`, `REAUTH_REQUIRED`,
`CSRF_TOKEN_INVALID`, `SESSION_IDLE_EXPIRED`, `RATE_LIMITED`, `BREAK_GLASS_NOT_FOUND`,
`BREAK_GLASS_ALREADY_ACTIVE`, `BREAK_GLASS_ENDED`, `BREAK_GLASS_NOT_OWNER`,
`BREAK_GLASS_HUMAN_ONLY`, `BREAK_GLASS_SCOPE_REQUIRED`, `APPROVAL_DUPLICATE_APPROVER`,
`APPROVAL_HUMAN_ONLY`.
