# Changelog

## 0.1.0 (2026-11-09)

- Index a folder into SQLite full-text search; skip secret-looking files; incremental by SHA-256.
- One-line file summaries with the cheap model (claude-haiku-5-5), concurrent, within a spend cap.
- Answers with claude-opus-5-5 (effort medium, server-side refusal fallbacks) through a tool loop:
  search_index, read_file, list_files.
- Prompt caching: explicit breakpoint on the system prompt + repo map, automatic caching for the loop.
- Hard spend caps per run and per day: worst-case reservation before each call, max_tokens shrunk to fit,
  settled from real usage, safe across concurrent runs.
- cost-report: spend per day, model and purpose, with cache hit rate.
- CLI, Docker image, CI (no API calls), manual live smoke workflow.
