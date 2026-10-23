# Configuration

## Container listener and host exposure

The image listens on `0.0.0.0:8000` inside the container so Docker port forwarding and other containers can reach it. The standalone `mailboxd http` CLI defaults to `127.0.0.1`. If you replace the image command with `mailboxd http`, add `--host 0.0.0.0`.

Publish host-local access with `-p 127.0.0.1:8000:8000`. `-p 8000:8000` publishes on all host interfaces: do that only for deliberate network access, behind HTTPS. The Compose file publishes on loopback by default.

## The config file

One YAML file, found via `--config`, `MAILBOXD_CONFIG`, or `/etc/mailboxd/config.yaml`. See [`config.example.yaml`](../config.example.yaml) for every option with comments. Any string may contain `${NAME}`; a variable that is not set is a startup error.

```yaml
log_level: INFO

auth:
  trusted_authserv_id: mx.cgs.example   # see "Authentication-Results trust"
  tokens:
    - name: mila
      token: ${MAILBOXD_TOKEN_MILA}
      scopes: [read, draft, organize]
      mailboxes: [contact]
      dry_run: false

mailboxes:
  - name: contact                # URL-safe handle, unique; passed as the `mailbox` argument
    description: "Contact inbox"
    imap: { host: imap.example.com, username: contact@cgs.example, password: "${PW}" }
    smtp:
      host: smtp.example.com
      username: contact@cgs.example
      password: ${PW}
      from_address: "L'équipe CGS <contact@cgs.example>"
      identities:
        - { address: contact@cgs.example, name: "L'équipe CGS", default: true }
        - { address: support@cgs.example, name: "Support CGS" }
      save_sent: true
      per_hour: 20
    folders: { sent: null, drafts: null, trash: null, junk: null }   # null = auto-detect
```

- **At least one mailbox**, and each needs at least one of `imap` / `smtp`.
- **`name`** matches `[a-zA-Z0-9_-]+` and is unique. It is a URL path segment and the value of the `mailbox` argument of MCP tools. It is not a tool prefix.
- **Defaults**: IMAP `993/ssl`, SMTP `587/starttls`.
- **Special folders** (`sent`, `drafts`, `trash`, `junk`): auto-detected from IMAP `SPECIAL-USE` flags, then from usual names (`Sent`, `Sent Items`, `Drafts`, `Trash`, `Corbeille`, `[Gmail]/Trash`, ...). Pin a name to override.
- **The config file holds plaintext passwords and bearer tokens** unless you use `${ENV}`. Treat it like a credential vault: gitignore it, `chmod 600`, mount it read-only.

## Tokens and scopes

Every request except `GET /health` needs `Authorization: Bearer <token>`. Tokens are compared in constant time. The same gate covers `/mcp`.

A token entry is either a mapping `{name, token, scopes, mailboxes, dry_run}` or, for backwards compatibility, a bare string. A bare string is named `legacy-N`, holds **every scope on every mailbox** and logs a warning at startup. `name` is what shows up in the audit log and in draft headers; it must be unique.

| Scope      | Allows                                                                                                              |
| ---------- | ------------------------------------------------------------------------------------------------------------------- |
| `read`     | List, search, read, raw source, attachments, threads, identities, stats, list drafts.                               |
| `draft`    | Create, update and delete drafts; prepare a reply as a draft.                                                       |
| `organize` | Mark read/unread, labels, move, create folder, delete to Trash.                                                     |
| `send`     | Send, reply directly, send a draft.                                                                                 |
| `delete`   | Permanent deletion (only from the Trash folder).                                                                    |
| `admin`    | Audit records, deep health, webhook delivery inspection and replay, within the mailbox allowlist. |

`mailboxes` is a list of mailbox names or `"*"`. A mailbox outside the list behaves exactly like one that does not exist (`404`), so a restricted token cannot even learn its name. `dry_run: true` forces every send by that token to be previewed only; a request can switch dry-run on but never off.

## Identities and signatures

`smtp.identities` lists the addresses a mailbox may send as, each with an optional display `name`, `signature_text` and `signature_html`. Without `identities`, one is derived from `from_address`. Sending, replying and drafting take an `identity` argument (an address); the legacy `from_address` field is still accepted but must match an identity. Anything else is refused with `422 invalid_identity` and the list of allowed addresses. Signatures are appended to the text (`-- ` separator) and HTML bodies unless the request passes `"signature": false`.

## State volume

`state.path` (env `MAILBOXD_STATE_PATH`, default `/var/lib/mailboxd/state.db`) is a SQLite database in WAL mode, shared by all uvicorn workers and by `mailboxd watch`. It stores audit metadata, send counters, named checkpoints, draft send claims and webhook delivery records. Queued webhooks include message headers, but never bodies or attachments. Mount a volume on `/var/lib/mailboxd` to preserve state when recreating containers. The image creates the directory owned by the unprivileged `mailboxd` user. See [persistence and recovery](operations.md#persistence).

## Limits, labels and send guard

```yaml
send_guard: { dry_run: false, per_hour: 30 }   # rolling hour, per mailbox AND per token; 0 = unlimited
limits:
  incoming_message_max_bytes: 33554432         # raw IMAP source before parsing (32 MiB)
  request_max_bytes: 33554432                 # maximum HTTP/MCP request body
  attachment_max_bytes: 15728640               # largest attachment served
  outgoing_attachments_max_bytes: 20971520     # total attachments on one outgoing message
  mcp_inline_max_bytes: 524288                 # raw source / base64 attachment returned inline over MCP
labels: { suspect: $Suspect, a_traiter: $ATraiter, fait: $Fait }   # friendly name -> IMAP keyword
```

`limits.incoming_message_max_bytes` caps raw RFC822 messages before MIME parsing,
body conversion, or attachment decoding. Reads, raw source, attachment retrieval,
threads, replies, and existing-draft operations share this limit. Oversized messages
return `413 too_large` (the same code in MCP). The client checks `RFC822.SIZE`
before downloading a full message, then requests at most the limit plus one byte with `BODY.PEEK`
to detect an incorrect size. Missing sizes and inconsistent/truncated responses
fail closed. Header-only reads and watcher notifications apply the budget to
the fetched headers, allowing messages with large attachments. Header-only listings use capped partial fetches too; they remain available for
oversized messages whose selected headers fit the budget.
The setting must be positive; increasing it increases memory use. This bounds
requests to a conforming IMAP server; it does not cap imaplib's socket buffering
if a malicious server ignores the requested partial range. The watcher retries
pathological oversized headers with backoff and retains its cursor; operators must
raise the budget or remove the offending message to resume notifications.

`smtp.per_hour` overrides `send_guard.per_hour` for one mailbox. Labels are plain IMAP keywords: the friendly names are what you pass to `set_labels` and the `label` search filter (plus the built-in `flagged`).


[Project overview](../README.md)
