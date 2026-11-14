# Release checklist

The same checklist every one of my projects meets before publishing. Ticked only when verified;
anything checked only by CI (not yet run) says so.

Last checked: 2026-11-09

## A. Installation and usage

- [x] What it is, with a diagram
- [x] Prerequisites with versions and links
- [x] Install steps for Windows (PowerShell) and macOS/Linux; Docker
- [x] Usage with real output for the offline commands (index, search, files, missing-key message)
- [ ] Real output for `ask`: **not run, since it calls the paid API and needs your key.** The README shows the format and says so. The manual live smoke workflow runs it when you choose
- [x] One command for tests
- [x] Configuration table for every setting
- [x] Troubleshooting
- [x] How the spend cap works, design decisions, how it scales, performance, what's next
- [x] AI-assisted line
- [x] Relative links and anchors checked (0 broken); external links checked in CI by lychee

## B. No private data

- [x] Made-up "Acme Traders" sample documents written for this project
- [x] No employer, product or people names; no real hosts, IPs, account ids or keys
- [x] No work code, names, counts or costs
- [x] Pre-publish check: 0 hits locally against the private block-list; also runs in CI
- [ ] gitleaks: CI only

## C. Secure by default

- [x] API key only from the environment (SDK); never logged or stored; not in `.env.example`
- [x] Secret-looking files never indexed, by name and by content (tested)
- [x] Tools read only the index, with bounded ranges; arguments re-validated; unknown tools refused (tested)
- [x] Prompt-injection hygiene: file contents framed as data in the system prompt
- [x] Parameterised SQL only; FTS query built from quoted words only (tested against FTS syntax)
- [x] Hard spend caps per run and per day, safe across concurrent runs (tested; 15/15 repeated runs)
- [x] Dependencies pinned; the SDK held at the newest release that's at least two weeks old; **pip-audit: no known vulnerabilities** (2026-11-09)
- [x] Dependabot (pip, Actions, Docker); CodeQL workflow; gitleaks in CI
- [x] Docker: multi-stage, non-root, runs with `--read-only` (checked in CI)
- [x] `SECURITY.md`
- [ ] CodeQL results: first run on GitHub after the push
- [ ] Actions pinned to commit SHAs: major versions for now; Dependabot keeps them current
- [ ] Repo settings on GitHub after the push: secret scanning + push protection, private vulnerability reporting, branch protection

## D. Scalable

- [x] Incremental indexing (size + modified time, then hash); summaries paid for once per file content
- [x] Repo map capped, so the cached prefix doesn't grow with the folder
- [x] Concurrent summaries with a limit; stop on cap
- [x] Ledger safe across processes (SQLite `BEGIN IMMEDIATE` + re-check)
- [x] "How it scales" section, including the move to PostgreSQL for a shared service
- N/A Health endpoints and statelessness: a CLI, not a service (the agent-console project will be the service)

## E. Optimised

- [x] Model routing: cheap model for bulk summaries, strong model for answers; effort set per route
- [x] Prompt caching: explicit breakpoint on a byte-stable prefix (tested identical across questions) + automatic caching for the loop
- [x] `max_tokens` sized to the remaining budget on every call
- [x] Async client; tool calls in one turn run concurrently
- [x] Profiled the indexer; re-index made about 3× faster (2.47 s → 0.85 s on 2,000 files)
- [x] Multi-stage Docker image
- [x] Performance numbers in the README (offline). Per-question cost is shown by `cost-report` from real runs

## F. Repo basics

- [x] Tests: 64 passed, 97% coverage (gate 80% in CI); no network, no key, no cost
- [x] Lint: ruff check and format clean
- [x] Markdown: core rules checked locally (0 issues); full markdownlint in CI
- [x] `.gitignore` (including `data/` and `*.db`), `LICENSE` (MIT), `CHANGELOG.md`
