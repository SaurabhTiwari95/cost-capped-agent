# Security policy

This is a personal demonstration project. Please report a security problem privately:
**Security** tab → **Report a vulnerability**. Please don't open a public issue.
I aim to reply within 7 days.

## What's in place

- The API key is read from the environment by the Anthropic SDK; it's never logged, stored or put in `.env.example`.
- Secret-looking files (`.env`, keys, certificates) and files containing credential patterns are never indexed, so
  they can't be sent to the model.
- The agent's tools read only from the index (never the disk), with bounded line ranges: no path traversal, no
  writes, no shell.
- Tool arguments are re-validated even though the tool schemas are strict.
- The system prompt tells the model that file contents are data, not instructions (prompt-injection hygiene).
- Hard spend caps per run and per day, checked before every call.
- Pinned dependencies, pip-audit, Dependabot, CodeQL, gitleaks; a non-root container that runs with a read-only
  filesystem.
