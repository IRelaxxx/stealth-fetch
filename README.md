# mcp-server-stealth-fetch

MCP server that fetches web pages with [httpcloak](https://github.com/sardanioss/httpcloak) (browser-like TLS/HTTP fingerprinting), **one persistent session per host**, a **global minimum 1 second** between fetch operations, and an **`end_session`** tool to drop a host’s session.

There is **no `robots.txt` enforcement**—you are responsible for policy and rate limits.

## Security

This server can request **arbitrary URLs**, including private or internal addresses (**SSRF**). Run only in environments you trust. The 1s global spacing is a light throttle, not network isolation.

## Install (reproducible)

Uses pinned direct dependencies and a lockfile:

```bash
uv sync --frozen
```

## Run

```bash
uv run mcp-server-stealth-fetch
```

Options:

- `--preset` — httpcloak preset (default: `chrome-latest`)
- `--proxy-url` — proxy URL for httpcloak
- `--http-version` — `auto` | `h1` | `h2` | `h3` (default: `auto`)

On Windows, if stdio encoding causes issues, set `PYTHONIOENCODING=utf-8` in the MCP server `env` block.

## MCP client configuration (uv)

```json
{
  "mcpServers": {
    "stealth-fetch": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "akrahl/mcp-server-stealth-fetch"]
    }
  }
}
```

Adjust `--directory` to your clone path. For a global install, use `command` / `args` that invoke `mcp-server-stealth-fetch` on your `PATH`.

## Tools

| Tool | Purpose |
|------|---------|
| `fetch` | `url`, `max_length`, `start_index`, `raw` — HTML simplified to markdown when possible (same idea as the reference [fetch MCP server](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch)). For `start_index > 0`, call `fetch` with `start_index=0` first using the **same** `url` and `raw`; chunks slice a cached snapshot (no second download). |
| `end_session` | `domain` — host, `host:port`, or URL; clears **page cache** for that host; closes the httpcloak session if present (idempotent message if no session). |

## Prompts

- `fetch` — same behavior as the `fetch` tool (single `url` argument).

## License

MIT. See [LICENSE](LICENSE).
