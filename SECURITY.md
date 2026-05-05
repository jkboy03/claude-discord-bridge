# Security policy

## Reporting a vulnerability

**Do not open a public GitHub issue for security problems.**

Use GitHub's **private vulnerability reporting** flow:

1. Go to the [**Security** tab](https://github.com/jkboy03/claude-discord-bridge/security) of this repository.
2. Click **Report a vulnerability**.
3. Fill in the form — include a description, reproduction steps (or a
   minimal proof of concept), the commit SHA you tested against, and
   whether you'd like credit in the release notes.

This creates a private advisory visible only to the maintainer; the
report cannot be seen by other users until you and the maintainer
agree to publish it.

You should expect an acknowledgement within 7 days. Once a fix is
ready, the patch will land on `master` and the published advisory
will reference the report.

## Scope

This bridge runs `claude` (Claude Code CLI) as a child process and pipes
output to a Discord DM. Reports about any of the following are in scope:

- **Authorization bypass** — anything that lets a Discord user *other than*
  `BRIDGE_ALLOWED_USER_ID` reach `run_claude_turn`, mutate `SessionState`,
  or invoke `_do_stop` on a live process.
- **Secrets handling** — any path through which `BRIDGE_DISCORD_BOT_TOKEN`
  or `.env` contents can leak into Discord output, logs, error messages,
  or stdout/stderr captured by users.
- **Code execution / sandbox escape** — any payload that, sent as a Discord
  DM, escapes the `claude -p` boundary (e.g., manipulates the Python
  process directly rather than being treated as a prompt).
- **Resource exhaustion / DoS** — patterns that crash the bot or hang it
  past `/stop` and require manual systemd intervention.
- **Dependency vulnerabilities** — known CVEs in pinned versions of
  `discord.py` or `python-dotenv` that affect the bridge's threat model.

## Out of scope

- Bugs that require an attacker to already control your machine, your
  Discord account, your `.env`, or the Anthropic account that issued the
  Claude Code login.
- Anything that's a property of `claude` (Claude Code CLI) or Discord
  itself, not the bridge — please report those upstream.
- Issues that depend on running with a misconfigured `BRIDGE_ALLOWED_USER_ID`
  (e.g., setting it to `0` or to a friend's ID and being surprised they
  can use the bot).

## Threat model assumptions

This bridge is designed for **single-user, self-hosted deployment**. It is
not multi-tenant. The user-ID gate is the *only* authorization layer.
Threats outside that model — e.g., "what if my friend gets my bot token"
— are documented in the README's security notes and are not bugs.
