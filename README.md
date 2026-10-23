# Artistly MCP

Local MCP server for Artistly's **Fast AI Image Designer → Create From Prompt** workflow.
It submits and tracks images using Artistly's internal HTTP endpoints and saves original
images directly from the CDN. Browser automation is used only for interactive sign-in.

This is an unofficial integration. Internal endpoints can change. The working MVP covers
Create From Prompt; illustration, upscaling, editing, and other Artistly functions are not
implemented. Artistly's [terms](https://artistly.ai/terms/terms.php) apply to use of the service.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
cd /Users/john/Documents/ChatGPT/artistly-to-api
uv sync --extra dev --locked
uv run playwright install chromium
uv run artistly-mcp login
uv run artistly-mcp status
```

Alternatively, use an installed Google Chrome for sign-in:

```sh
uv run artistly-mcp login --channel chrome
```

The login command opens a separate browser window. Sign in normally, including any MFA.
It checks authentication, saves session cookies locally, and closes its window automatically.
Passwords are never requested in chat or stored by the server. Login has a five-minute
timeout; increase with `--timeout 900` if needed. Run the same command when the session expires.
Subsequent MCP calls reload the saved session, so reconnecting does not require restarting
the MCP server. Existing local jobs remain resumable after reconnecting.

## Connect an MCP client

Use this local stdio server definition in a client that supports MCP:

```json
{
  "mcpServers": {
    "artistly": {
      "command": "/Users/john/Documents/ChatGPT/artistly-to-api/.venv/bin/artistly-mcp",
      "args": ["serve"]
    }
  }
}
```

`examples/mcp.json` contains that configuration. Client-specific configuration formats may
differ. The server has no HTTP listener. It writes only MCP protocol messages to stdout.
No credentials belong in the MCP configuration.

## Tools

- **artistly_session_status**: Check saved authentication and get reconnect instructions.
- **artistly_generate_image**: Submit Create From Prompt. Inputs are nested in `params`.
  Returns a local `job_id`, status, and any identified designs.
- **artistly_list_designs**: List one upstream page. Accepts `folder_id`, `tool_used`, and the
  opaque `cursor` from a previous `next_cursor`. Results include metadata and original URLs.
- **artistly_get_design**: Resume a `job_id`, or retrieve a design UUID. `wait_seconds` is
  optional (0–45); waiting applies to local jobs. No generation is submitted by this tool.
- **artistly_download_design**: Save all originals for a completed job or ready design UUID.
  Returns absolute local paths and total bytes. Repeating it creates new copies.

Example generation arguments:

```json
{
  "params": {
    "prompt": "A dog wearing a top hat, studio portrait",
    "quality": "fast",
    "aspect_ratio": "1:1",
    "quantity": 1,
    "visibility": "private",
    "wait_seconds": 15
  }
}
```

Example follow-up arguments:

```json
{"reference": "job_REPLACE_WITH_RETURNED_ID", "wait_seconds": 15}
```

Example download arguments:

```json
{"reference": "REPLACE_WITH_DESIGN_UUID_OR_JOB_ID"}
```

Generation supports `fast` and `high_quality`, quantities 1–4, optional existing `folder_id`,
and explicit `private`/`public` visibility. Defaults are private, Fast, square, one image.
Aspect ratios: `1:1`, `16:9`, `21:9`, `3:2`, `2:3`, `4:5`, `5:4`, `3:4`, `4:3`, `9:16`, `9:21`.
Only Fast, square, quantity-one generation has been verified live; other input mappings
follow the observed frontend and have automated coverage, not live generation coverage.

Try these requests in your assistant:

- “Check my Artistly session, generate a private square fox illustration, then save it.”
- “Show my recent Artistly designs and download the first completed design.”
- “Check the status of this Artistly job; resume it without creating another image.”

## Sessions, jobs, and downloads

Defaults:

- Session and SQLite job database: `~/.local/share/artistly-mcp/`
- Original images: `~/Downloads/Artistly/`

Override with `ARTISTLY_STATE_DIR` and `ARTISTLY_DOWNLOAD_DIR`. Use the same state directory
for login and server processes. State directories are owner-only (0700); session files and
the job database are 0600. Session cookies are sensitive bearer credentials stored as
protected plaintext, not encrypted. They are never tool arguments, tool results, or logs.
Use a private state directory outside a shared checkout. Download paths cannot be selected
through MCP tool input, files are never overwritten, and only HTTPS URLs on Artistly's CDN
are downloaded. Each image has a 50 MiB limit and must match its declared image format.

The server records intent **before** submitting. A timed-out call, interrupted server, or
uncertain HTTP response must be followed by `artistly_get_design` using its job ID. Submission
is never automatically retried. If the call was interrupted before returning an ID, run:

```sh
uv run artistly-mcp jobs
```

Only one local job can be active across server processes sharing a state directory.
The observed submission redirects to Personal Designs rather than returning a design ID.
Correlation compares new design UUIDs with the pre-submission list, then checks prompt,
dimensions, and category. Concurrent manual generations using the same settings can make
correlation ambiguous; avoid generating manually while a local job is active. Ambiguous
jobs remain blocked rather than guessing. Discovery checks at most five list pages.

If a job cannot be resolved, inspect Personal Designs before explicitly releasing it:

```sh
uv run artistly-mcp abandon-job job_ACTUAL_JOB_ID --confirm
```

This releases only the local lock. It **does not cancel** Artistly work. Check existing
results before submitting again. Upstream `processing` and final visibility statuses such
as `private` are normalized into local job states; `private` alone is not an error.

## Development and verification

```sh
uv run pytest -q
uv run ruff check src tests
```

Tests use synthetic data and HTTP mocks: complete generation/download, session expiry and
reconnection, fresh CSRF metadata, uncertain submissions, restart recovery, cross-process
serialization, ambiguous and multi-image results, pagination, download validation, file
permissions, and real MCP stdio handshake/tool calls. Tests never submit paid/live generations.
`evals/` includes a frozen fixture and ten read-only agent evaluation questions.

The original live HTTP smoke test and downloaded images are in ignored `test-output/`.
These are local evidence, not required runtime files. Source, dependency lock, and tests
contain no Artistly credentials or private account data.
