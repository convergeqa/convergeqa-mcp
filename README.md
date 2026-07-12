# convergeqa-mcp

<!-- mcp-name: io.github.convergeqa/reviews -->
<!-- mcp-name: io.github.convergeqa/compare -->

ConvergeQA is a multi-model document review system with public verification receipts. Its panels challenge thinking, surface disagreements, and document review evidence, while the responsible individual decides what to accept, reject, or revise.

This package exposes two stdio MCP servers:

- `convergeqa-mcp reviews` (or `convergeqa-mcp-reviews`): Critique and Iterate review tools.
- `convergeqa-mcp compare` (or `convergeqa-mcp-compare`): Compare due-diligence review tools.

Both servers delegate to the same CLI client contracts used by ConvergeQA agent tooling. Reviews consume paid credits. The servers read a ConvergeQA Developer API key or service-account key from environment variables at runtime, and the tool never stores keys.

## Links

- API keys: https://convergeqa.net/developers
- Pricing and credits: https://convergeqa.net/pricing
- Agent overview: https://convergeqa.net/agents
- Trust: https://convergeqa.net/trust
- LLM reference: https://convergeqa.net/llms.txt

## Install

From PyPI:

```bash
uvx convergeqa-mcp reviews
uvx convergeqa-mcp compare
```

```bash
pipx run convergeqa-mcp reviews
pipx run convergeqa-mcp compare
```

Or directly from Git:

```bash
uvx --from git+https://github.com/convergeqa/convergeqa-mcp.git convergeqa-mcp-reviews
uvx --from git+https://github.com/convergeqa/convergeqa-mcp.git convergeqa-mcp-compare
```

## Claude Code

Set `CONVERGEQA_API_KEY` or `CONVERGEQA_SERVICE_ACCOUNT_KEY` before starting your MCP client. The default key names are uppercase environment variables.

```bash
claude mcp add convergeqa-reviews --env CONVERGEQA_API_KEY=$CONVERGEQA_API_KEY -- uvx convergeqa-mcp reviews
```

Add Compare as a second server:

```bash
claude mcp add convergeqa-compare --env CONVERGEQA_API_KEY=$CONVERGEQA_API_KEY -- uvx convergeqa-mcp compare
```

## Claude Desktop

Example `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "convergeqa-reviews": {
      "command": "uvx",
      "args": ["convergeqa-mcp", "reviews"],
      "env": {
        "CONVERGEQA_API_KEY": "${CONVERGEQA_API_KEY}"
      }
    },
    "convergeqa-compare": {
      "command": "uvx",
      "args": ["convergeqa-mcp", "compare"],
      "env": {
        "CONVERGEQA_API_KEY": "${CONVERGEQA_API_KEY}"
      }
    }
  }
}
```

If your MCP client does not expand environment placeholders, set the key in the client environment by the method that client supports.

## Other MCP Clients

These are standard stdio JSON-RPC MCP servers, so any MCP-capable client can run them: Codex CLI, OpenCode, Cursor, OpenClaw, Hermes Agent, and others. The pattern is always the same: launch the command, pass the API key in the environment.

Codex CLI (`~/.codex/config.toml`):

```toml
[mcp_servers.convergeqa-reviews]
command = "uvx"
args = ["convergeqa-mcp", "reviews"]
env = { "CONVERGEQA_API_KEY" = "<your key>" }

[mcp_servers.convergeqa-compare]
command = "uvx"
args = ["convergeqa-mcp", "compare"]
env = { "CONVERGEQA_API_KEY" = "<your key>" }
```

For clients without a `uvx` runtime, install once with `pipx install convergeqa-mcp` and point the client at the installed `convergeqa-mcp-reviews` and `convergeqa-mcp-compare` commands. Python 3.10 or newer; no other runtime dependencies.

## Tools

`convergeqa-mcp reviews` (16 tools):

- `convergeqa_critique_start`: Start a private authenticated Critique agent review.
- `convergeqa_critique_status`: Fetch owner-scoped Critique job status.
- `convergeqa_critique_packet`: Fetch owner-authenticated Critique packet JSON and optionally write it to a path.
- `convergeqa_critique_session`: Fetch owner-authenticated Critique session packet.
- `convergeqa_critique_sessions`: List owner-authenticated Critique sessions.
- `convergeqa_critique_templates`: List API-visible ConvergeQA templates for the configured credential.
- `convergeqa_critique_decide`: Submit delegated Critique decisions.
- `convergeqa_critique_export`: Download owner-authenticated Critique export ZIP to a caller-selected path.
- `convergeqa_iterate_start`: Start a private authenticated Iterate agent review.
- `convergeqa_iterate_status`: Fetch owner-scoped Iterate job status.
- `convergeqa_iterate_packet`: Fetch owner-authenticated Iterate packet JSON and optionally write it to a path.
- `convergeqa_iterate_session`: Fetch owner-authenticated Iterate session packet.
- `convergeqa_iterate_sessions`: List owner-authenticated Iterate sessions.
- `convergeqa_iterate_templates`: List API-visible ConvergeQA templates for the configured credential.
- `convergeqa_iterate_decide`: Submit delegated Iterate decisions.
- `convergeqa_iterate_export`: Download owner-authenticated Iterate export ZIP to a caller-selected path.

`convergeqa-mcp compare` (5 tools):

- `convergeqa_compare_submit`: Submit a private authenticated Compare due-diligence job.
- `convergeqa_compare_status`: Fetch owner-scoped Compare due-diligence job status.
- `convergeqa_compare_packet`: Fetch owner-authenticated packet JSON and optionally write it to a caller-selected path.
- `convergeqa_compare_bundle`: Download owner-authenticated Compare due-diligence bundle to a caller-selected path.
- `convergeqa_compare_run`: Submit, poll, fetch packet, and optionally download packet/bundle artifacts.

## Credentials

The review servers accept these default environment variable names:

- `CONVERGEQA_API_KEY`
- `CONVERGEQA_SERVICE_ACCOUNT_KEY`

Tool arguments may override the environment variable name, but literal credential values are rejected.

## Protocol

The current servers speak stdio JSON-RPC and report MCP protocol version `2024-11-05`, matching the existing ConvergeQA MCP server behavior.
