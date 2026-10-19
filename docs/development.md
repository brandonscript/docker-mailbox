# Architecture and development

## Architecture

```
┌────────────┐    ┌──────────────────────────┐    ┌──────────────┐
│  HTTP CLI  │───▶│  FastAPI                 │    │  IMAP server │
│  / curl    │    │  /inbox, /mailboxes, …   │───▶│  SMTP server │
└────────────┘    │                          │    └──────────────┘
┌────────────┐    │  bearer gate → Principal │
│  MCP host  │───▶│  ─── Service (scopes) ── │───▶ state.db (SQLite volume)
│ (Claude…)  │    │  /mcp (streamable HTTP)  │      audit · send limits · checkpoints
└────────────┘    └──────────────────────────┘
                              ▲
        mailboxd watch ───────┘  IMAP IDLE → signed webhooks
```

The HTTP routes and the MCP tools are thin layers over one `Service`, which resolves the mailbox for the caller's `Principal`, checks the scope, acts, and audits. SQLite holds audit records, quotas, checkpoints, draft send claims and notification delivery metadata. Mail bodies remain on IMAP. Share the database on a local filesystem on one host; SQLite WAL is not for multi-host or network-filesystem deployments.

## Development

The CLI defaults to loopback; Docker and `make run` explicitly listen on `0.0.0.0` inside the container. `make run` publishes on all host interfaces. Use the [host-local Docker example](../README.md#quick-start) for local access. Bearer auth protects the API and MCP in every mode; `/health` is public.

```bash
make help          # list all targets
make dev-image     # build the sandboxed dev container
make shell         # drop into it
make run           # boot the server (REST + /mcp); mount CONFIG=path/to/config.yaml
make test          # full suite (unit + docker-in-docker integration)
make test-coverage # full suite with an 85 percent coverage floor
make test-unit     # in-process only, fast feedback loop
make lint          # flake8 + mypy
make sec           # semgrep + bandit + pip-audit -> sec.sarif (reports, never fails)
make audit         # check the frozen runtime and dev dependency graph
make audit-compose # check production Compose defaults
make format        # isort + black
```

Tests run against an in-process IMAP mock (`tests/imap_mock.py`) that can emulate a modern server (`MOVE`, `UIDPLUS`, `SPECIAL-USE`, several folders, flags, `APPENDUID`) and records every command it receives, so tests can assert that no folder-wide `EXPUNGE` is ever issued.

The suite also uses real SQLite files, local SMTP and HTTP servers, and live TLS connections with trusted, untrusted and wrong-hostname certificates. Production-container tests check startup, authentication, filesystem permissions, persistent state and Compose rendering. These tests use local fixtures, not real provider accounts; provider-specific behavior still needs checking on the target deployment.

### Package management

`uv`'s `exclude-newer` rejects package versions newer than a fixed cutoff. Dependency changes set that cutoff to UTC midnight seven days ago. `pkg-lock` keeps the existing cutoff.

```bash
make pkg-add PKG=foo==1.2.3
make pkg-remove PKG=foo
make pkg-update PKG=foo
make pkg-lock
make pkg-upgrade
```

Don't hand-edit `[tool.uv].exclude-newer`. Let the targets do it.


[Project overview](../README.md)
