# Changelog

## Unreleased

### Hardened

- Added per-agent backend watchdogs for both Codex and Claude workers:
  - `<PREFIX>_IDLE_TIMEOUT_SECONDS` defaults to 600 seconds of stdout silence.
  - `<PREFIX>_TURN_TIMEOUT_SECONDS` optionally caps total turn wall time.
- Timeout cleanup now terminates stuck backend subprocesses, clears the active worker lock, preserves known session/thread IDs, and sends a final manager mention with a warning.
- Documented final-report mention behavior, markdown spillover convention, timeout config, and stop behavior for public deployments.
- Added regression tests for idle-timeout behavior on both backends.
