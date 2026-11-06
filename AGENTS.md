# Repository guidance

This file applies throughout the repository. Read [README.md](README.md) for supported
tools, setup, limitations, and recovery. This is an unofficial local Python MCP server
using Artistly's internal HTTP endpoints; browser automation is for interactive login.

## Project layout

- `src/artistly_mcp/server.py`: MCP tools, descriptions, and agent instructions.
- `models.py` and `workflows.py`: validated inputs, payloads, and workflow metadata.
- `client.py` and `uploads.py`: HTTP requests, chunked uploads, and image validation.
- `service.py` and `state.py`: job correlation, recovery, persistence, and locking.
- `cli.py`, `login.py`, and `config.py`: local commands, authentication, and settings.
- `tests/`: synthetic data, mocked HTTP, and real MCP stdio protocol tests.
- `evals/`: read-only evaluation fixtures; no executable harness. See its README.

## Development and checks

Use Python 3.11+ and uv. Install with `uv sync --extra dev --locked`.
For code changes, run `uv run pytest -q`, `uv run ruff check src tests`, and
`git diff --check`. For packaging/dependency changes, also run
`uv build --python .venv/bin/python`. Keep `uv.lock` aligned with dependency changes.
For documentation-only edits, validate affected examples against current schemas and
check links and whitespace. Do not run live generations just to verify documentation.

## Preserve these behaviors

- Persist job intent before submission and retain single-active-job protection across
  processes sharing a state directory. Resume uncertain submissions through get/poll;
  never automatically resubmit generation or restart an interrupted upload.
- Preserve old jobs without workflow markers. Match results using each workflow's
  existing metadata and prompt-rewrite rules; report ambiguity instead of guessing.
- Keep generation arguments inside `params`, validate unsupported fields, and keep
  stdout reserved for MCP protocol messages. Update tool descriptions and runtime
  instructions alongside changed behavior so agents can discover how to use it.
- Keep images private by default. Upload only user-provided or explicitly identified
  local references. Preserve image validation, CDN restrictions, download limits,
  owner-only permissions, and non-overwriting filenames.
- T-shirt Designs always uses High Quality and sends a positive artwork description
  unchanged. Do not add garment/mockup wording, exclusion lists, or negative prompts.
- Realistic and multi-character Consistent Characters remain unsupported. Verify new
  workflows from observed requests; do not invent endpoints or payload fields.

## Verification, documentation, and commits

- Use isolated temporary state and mocked requests in tests. Live generation/upload
  checks need a user-requested live task; avoid concurrent manual generation while
  tracking jobs. Report live coverage separately from mocked/schema coverage.
- Never commit cookies, session files, job databases, private images, or raw network
  captures containing credentials. Keep local evidence in ignored `test-output/`.
- Update the README and affected examples for tool, setup, recovery, or option changes.
  Keep configuration examples portable and clearly mark replaceable paths and IDs.
- When asked to commit, use Conventional Commits with a concise subject and body
  explaining what changed, why, and testing. Wrap body paragraphs at 72 columns without
  orphaned words or literal newline escapes. Push or rewrite history only when requested.
