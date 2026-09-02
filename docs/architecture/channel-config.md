# Channel configuration, membership, lifecycle and external identity commands (P2-02, P2-09, P2-13)

Companion to `channels.md`. Covers per-channel configuration and membership, the channel
lifecycle (archive → soft delete), the external identity command path shared by all providers,
and the Mattermost `link start` / `link confirm` challenge.

## Per-channel configuration and membership (P2-02, spec §9.3, plan §7G)

Every setting lives on the channel row and every membership on `channel_members`; nothing is
inherited from a sibling channel (validation V-P2-19).

| Command (`server/application/channel_members.py`) | Permission | Effect |
| --- | --- | --- |
| `AddChannelMember(channel_id, account_id, permissions)` | `channel.manage` | Upsert an active membership; Agents join through their Account like humans. |
| `RemoveChannelMember(channel_id, account_id)` | `channel.manage` | Membership status `removed` (row kept for history). |
| `SetMemberPermissions(channel_id, account_id, permissions)` | `channel.manage` | Replace the member's channel permissions (`read`, `write`, `moderate`). |
| `SetChannelDocumentTemplate(channel_id, documentation_template)` | `channel.manage` | Per-channel documentation template identifier. |
| `ConfigureChannel(...)` (P2-01) | `channel.manage` | Policy, language, retention, legal hold, template id. |

Each write appends one `CHANNEL_CONFIGURED` Event whose payload carries a `change` object
(`member_add`, `member_remove`, `member_permissions`, `document_template`), updates the row in the
same transaction and writes an audit entry. Read: `members_of(ctx, channel_id)`.
REST: `POST/DELETE /api/v1/channels/{id}/members[...]`, `PUT .../members/{account}/permissions`,
`PUT .../document-template`, `GET .../members` (`server/api/v1/channel_members.py`).

Errors: `CHANNEL_NOT_FOUND` (404), `CHANNEL_DELETED` (409), `MEMBER_PERMISSIONS_INVALID` (422),
`MEMBER_NOT_FOUND` (404).

## Lifecycle: archive → soft delete (P2-09, spec §9.3, validation V-P2-18)

`ArchiveChannel` (P2-01) sets `status = archived` and appends `CHANNEL_ARCHIVED`.
`DeleteChannel(channel_id, reason_code)` (`server/channels/lifecycle.py`) is only accepted for an
archived channel (`CHANNEL_NOT_ARCHIVED`) and refuses with `CHANNEL_DELETE_BLOCKED` while the
channel still has an enabled Telegram bridge or an open Task (`deletion_blockers`). Deletion is a
**soft delete**: `status = deleted`, `deleted_at` set, an audit entry `channel.delete` with the
counts of kept references. No row of Tasks, Events, documents, artifact links, thread bindings or
message mappings is touched; `references(session, channel_uuid)` reports them and `channel_view`
still resolves a deleted channel for history. The baseline defines no deletion Event, so the
deletion is recorded as status + audit only (the archive Event remains the last Event of the
channel aggregate). A replayed `DeleteChannel` is idempotent.

## External identity commands (P2-13, spec §8.4, validation V-P2-20/21/22)

`server/identity/external_commands.py`:

- `resolve_external_principal(session, provider_instance_id, external_user_id)` returns the
  command `Principal` of the **active** link only; suspended, revoked, pending and unlinked users
  raise `EXTERNAL_IDENTITY_NOT_ACTIVE`. Resolution has no side effects. A principal's permissions
  are those of the linked Account; a link is scoped to one provider instance, so the same external
  user id on another instance resolves independently (or not at all).
- `list_links`, `admin_transition(kind=approve|suspend|revoke)` back the Administrator REST
  routes in `server/api/v1/identity_admin.py` (`GET /api/v1/identity/admin/links`,
  `POST /api/v1/identity/admin/links/{id}/approve|suspend|revoke`; permission `admin.accounts`).
- A second link for the same (instance, external user) is refused by the service
  (`EXTERNAL_IDENTITY_DUPLICATE`, audited `DENY`) and by the unique index of the table.

## Mattermost link challenge (`server/identity/mattermost_link.py`, validation V-P2-27)

`link start` issues the 8-digit challenge through `ExternalLinkService.start_challenge` and sends
the code by **direct message** only; the channel gets an ephemeral "check your DMs" reply. The
code is never posted to the channel. `link confirm <code>` confirms on the command path, which
always lands in `pending_admin`; an Administrator approves it (REST above) before the link is
active and commands execute. Wrong, expired and reused codes return the service's error codes;
five failures lock the user for 15 minutes (`EXTERNAL_IDENTITY_LOCKED`), which also blocks a
correct code and a new `link start` until the lockout expires.

The command path binds to the Account prepared by the Administrator for that Mattermost user
(`accounts.auth_subject = mattermost:<username>`, or account id `acct-<username>`); without such
an Account the reply is `ACCOUNT_NOT_FOUND` guidance. Events of unlinked users use the
workspace's system service Account as actor (`SYSTEM_ACCOUNT_MISSING` if Setup has not created
one). Handlers are mounted explicitly: `server.identity.mattermost_link.register()` during app
wiring (`LINK_HANDLERS["start"|"confirm"]`); the command grammar accepts 6–8 digit codes.
