# Contributing

Thanks for considering a contribution. Quick guide:

## Before you start

- Open an issue describing the change. For non-trivial work, get rough
  agreement on direction before you write code.
- Security-sensitive findings: see [SECURITY.md](SECURITY.md) — email
  the maintainer privately, do **not** file a public issue.

## Local setup

```bash
git clone https://github.com/jkboy03/claude-discord-bridge
cd claude-discord-bridge
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio pytest-cov ruff
```

You do **not** need a real Discord bot token or a working `claude` install
to run the test suite — every external dependency is stubbed.

## Running tests

```bash
# Full suite + coverage report
pytest --cov=bot --cov-report=term-missing

# Lint
ruff check bot.py tests/
```

CI runs both on every push and PR (Python 3.10 / 3.11 / 3.12). PRs cannot
merge with a red build.

## Coverage policy

Current coverage is ~96%. The remaining lines are:

- Module-load `sys.exit()` branches for missing env vars.
- The `client.run()` entry point.
- Discord's `on_ready` lifecycle handler.

These require a real Discord connection or subprocess-isolated import to
exercise; they're documented as intentionally uncovered. Don't lower
coverage on the *covered* surface — CI fails the build below 90%.

## Test conventions

Tests live in `tests/test_bridge.py`, organized into classes by risk
class (auth gating, pre-lock interception, subprocess streaming, etc.).

When you add a feature:

1. Add a test that fails without your change.
2. Implement the change.
3. Make sure the new test and all existing tests pass.
4. Run `ruff check` and `pytest --cov=bot --cov-fail-under=90`.

When you fix a bug:

1. Add a regression test that reproduces the bug.
2. Confirm the test fails on `master`.
3. Apply the fix; the test should now pass.
4. Reference the regression test in the PR description.

## What we're cautious about

This is a single-user authorization bridge. Two changes get extra scrutiny
in review:

- **Anything that touches `_is_authorized`, `on_message`'s gate sequence,
  or the pre-lock interceptor block (`/stop`, `/exit`, `/quit`).**
  These are the bot's safety boundary. Tests for them live in
  `TestOnMessageGating`, `TestPreLockInterceptors`, and
  `TestSlashCommandAuth` — they must stay green and grow in lockstep
  with the gate logic.
- **Anything that adds a way for Discord input to reach `subprocess` or
  `os.system` without going through `state.claude_args`.** The current
  shape — input is *always* the last arg of an explicit argv list — is
  what makes this bridge safe to expose. Don't widen that surface.

## Style

- Code: ruff defaults, type hints where they aid clarity, comments only
  when the *why* isn't obvious from a good name.
- Commits: imperative subject ("Add /context support" not "Added"). Body
  optional but appreciated for non-trivial changes — explain the *why*.
- PRs: link the issue, describe the change in 1-3 bullets, include a
  test plan checklist if there's anything reviewer should verify by hand.
