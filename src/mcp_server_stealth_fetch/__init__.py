import argparse
import asyncio

from .server import ServerConfig, serve


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MCP server: fetch URLs via httpcloak (per-host sessions, global fetch spacing).",
    )
    parser.add_argument(
        "--preset",
        default="chrome-latest",
        help="httpcloak browser preset (default: chrome-latest)",
    )
    parser.add_argument(
        "--proxy-url",
        default=None,
        help="Proxy URL for httpcloak (e.g. socks5://... or http://...)",
    )
    parser.add_argument(
        "--http-version",
        default="auto",
        choices=("auto", "h1", "h2", "h3"),
        help="HTTP version preference for httpcloak (default: auto)",
    )
    args = parser.parse_args()
    cfg = ServerConfig(
        preset=args.preset,
        proxy_url=args.proxy_url,
        http_version=args.http_version,
    )
    asyncio.run(serve(cfg))
