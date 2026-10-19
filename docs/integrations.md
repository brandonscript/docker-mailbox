# Agent integrations

The [skill](../.agents/skills/docker-mailbox) works in any agent that reads `.agents/skills/` (such as [peen](https://github.com/psyb0t/peen)), and installs natively in the clients below. Give agents a token with the narrowest scopes they need (typically `read`, `draft`, `organize`).

## Claude Code

```bash
claude plugin marketplace add psyb0t/agents
claude plugin install mailbox@psyb0t
```

Configure the mailboxd URL and bearer token when the client prompts for them.

## Codex

```bash
codex plugin marketplace add psyb0t/agents
codex plugin add mailbox@psyb0t
```

Invoke the marketplace skill as `$mailbox:docker-mailbox`. In a repository containing `.agents/skills/`, Codex discovers the local skill as `$docker-mailbox` without installation.

## OpenClaw

The skill is published to ClawHub on every release:

```bash
openclaw skills install @psyb0t/docker-mailbox
```

For MCP clients that speak local stdio, the [`@psyb0t/mailbox`](../.agents/plugins/mailbox) plugin bridges to mailboxd's `/mcp` endpoint:

```bash
openclaw plugins install clawhub:@psyb0t/mailbox
```

Then set `MAILBOX_URL` and `MAILBOX_TOKEN` to your server URL and configured bearer token.


[Project overview](../README.md)
