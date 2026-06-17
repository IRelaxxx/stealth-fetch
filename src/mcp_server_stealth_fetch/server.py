from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Tuple
from urllib.parse import urlparse

import httpcloak
import markdownify
import readabilipy.simple_json
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from mcp.types import (
    ErrorData,
    GetPromptResult,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    Prompt,
    PromptArgument,
    PromptMessage,
    TextContent,
    Tool,
)
from pydantic import AnyUrl, BaseModel, Field, ValidationError

MIN_SECONDS_BETWEEN_FETCHES = 1.0


def create_httpcloak_session(**kwargs: Any) -> httpcloak.Session:
    return httpcloak.Session(**kwargs)


def _header_first(headers: Any, name: str) -> str:
    v = headers.get(name)
    if v is None:
        v = headers.get(name.lower())
    if v is None:
        return ""
    if isinstance(v, list):
        return str(v[0]) if v else ""
    return str(v)


def netloc_key_from_fetch_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError("URL must include a host (netloc)")
    return parsed.netloc.lower()


def netloc_key_from_domain_input(s: str) -> str:
    t = s.strip()
    if not t:
        raise ValueError("domain is required")
    if "://" in t:
        parsed = urlparse(t)
        if not parsed.netloc:
            raise ValueError("Could not parse host from URL")
        return parsed.netloc.lower()
    parsed = urlparse("https://" + t)
    if not parsed.netloc:
        raise ValueError("Could not parse host")
    return parsed.netloc.lower()


def extract_content_from_html(html: str) -> str:
    try:
        ret = readabilipy.simple_json.simple_json_from_html_string(
            html, use_readability=True
        )
        if not ret["content"]:
            return "<error>Page failed to be simplified from HTML</error>"
        raw_content = ret["content"]
        if not isinstance(raw_content, str):
            raw_content = str(raw_content)
        return markdownify.markdownify(
            raw_content,
            heading_style=markdownify.ATX,
        )
    except (IndexError, KeyError, TypeError) as e:
        return f"<error>Failed to simplify HTML: {e!r}</error>"


class Fetch(BaseModel):
    url: Annotated[AnyUrl, Field(description="URL to fetch")]
    max_length: Annotated[
        int,
        Field(
            default=5000,
            description="Maximum number of characters to return.",
            gt=0,
            lt=1_000_000,
        ),
    ]
    start_index: Annotated[
        int,
        Field(
            default=0,
            description=(
                "Character offset into the document. For start_index > 0 you must first "
                "fetch the same url with raw unchanged and start_index=0 so the full "
                "text is cached; later chunks slice that snapshot (no re-download)."
            ),
            ge=0,
        ),
    ]
    raw: Annotated[
        bool,
        Field(
            default=False,
            description="Return raw HTML without simplification to markdown.",
        ),
    ]


class EndSession(BaseModel):
    domain: Annotated[
        str,
        Field(
            description="Host, host:port, or URL identifying which origin's session to close.",
        ),
    ]


@dataclass
class ServerConfig:
    preset: str = "chrome-latest"
    proxy_url: str | None = None
    http_version: str = "auto"


class StealthFetchState:
    def __init__(
        self,
        config: ServerConfig,
        session_factory: Callable[..., httpcloak.Session] = create_httpcloak_session,
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._lock = asyncio.Lock()
        self._sessions: dict[str, httpcloak.Session] = {}
        self._last_fetch_end: float | None = None
        self._page_cache: dict[tuple[str, bool], tuple[str, str]] = {}

    def _session_kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "preset": self._config.preset,
            "http_version": self._config.http_version,
        }
        if self._config.proxy_url:
            kw["proxy"] = self._config.proxy_url
        return kw

    def _get_or_create_session(self, netloc_key: str) -> httpcloak.Session:
        if netloc_key not in self._sessions:
            self._sessions[netloc_key] = self._session_factory(**self._session_kwargs())
        return self._sessions[netloc_key]

    async def end_session(self, domain: str) -> str:
        try:
            key = netloc_key_from_domain_input(domain)
        except ValueError as e:
            raise McpError(
                ErrorData(code=INVALID_PARAMS, message=str(e))
            ) from e
        async with self._lock:
            sess = self._sessions.pop(key, None)
            if sess is not None:
                await asyncio.to_thread(sess.close)
            for ck in list(self._page_cache):
                u, _ = ck
                try:
                    if netloc_key_from_fetch_url(u) == key:
                        del self._page_cache[ck]
                except ValueError:
                    continue
            if sess is None:
                return f"No session stored for {key!r}."
        return f"Closed session for {key!r}."

    async def fetch(
        self,
        url: str,
        max_length: int,
        start_index: int,
        force_raw: bool,
    ) -> Tuple[str, str]:
        try:
            key = netloc_key_from_fetch_url(url)
        except ValueError as e:
            raise McpError(
                ErrorData(code=INVALID_PARAMS, message=str(e))
            ) from e

        async with self._lock:
            now = time.monotonic()
            if self._last_fetch_end is not None:
                wait = MIN_SECONDS_BETWEEN_FETCHES - (now - self._last_fetch_end)
                if wait > 0:
                    await asyncio.sleep(wait)

            try:
                cache_key = (url, force_raw)
                if start_index > 0:
                    if cache_key not in self._page_cache:
                        raise McpError(
                            ErrorData(
                                code=INVALID_PARAMS,
                                message=(
                                    "No cached document for this URL and raw flag. "
                                    "Call fetch with start_index=0 first (same url and raw), "
                                    "then use the suggested start_index for the next chunk."
                                ),
                            )
                        )
                    content, prefix = self._page_cache[cache_key]
                else:
                    session = self._get_or_create_session(key)
                    try:
                        response = await session.get_async(url)
                    except Exception as e:
                        raise McpError(
                            ErrorData(
                                code=INTERNAL_ERROR,
                                message=f"Failed to fetch {url}: {e!r}",
                            )
                        ) from e

                    if response.status_code >= 400:
                        raise McpError(
                            ErrorData(
                                code=INTERNAL_ERROR,
                                message=f"Failed to fetch {url} - status code {response.status_code}",
                            )
                        )

                    page_raw = response.text
                    content_type = _header_first(response.headers, "content-type")
                    is_page_html = (
                        "<html" in page_raw[:100].lower()
                        or "text/html" in content_type.lower()
                        or not content_type
                    )

                    if is_page_html and not force_raw:
                        content = extract_content_from_html(page_raw)
                        prefix = ""
                    else:
                        content = page_raw
                        prefix = (
                            f"Content type {content_type!r} cannot be simplified to markdown; raw body:\n"
                        )

                    self._page_cache[cache_key] = (content, prefix)

                original_length = len(content)
                if start_index >= original_length:
                    body = "<error>No more content available.</error>"
                else:
                    truncated = content[start_index : start_index + max_length]
                    if not truncated:
                        body = "<error>No more content available.</error>"
                    else:
                        body = truncated
                        actual = len(truncated)
                        remaining = original_length - (start_index + actual)
                        if actual == max_length and remaining > 0:
                            next_start = start_index + actual
                            body += (
                                f"\n\n<error>Content truncated. Call fetch with start_index={next_start} for more.</error>"
                            )
            finally:
                self._last_fetch_end = time.monotonic()

        return prefix, body


async def serve(config: ServerConfig | None = None) -> None:
    cfg = config or ServerConfig()
    state = StealthFetchState(cfg)
    server = Server("mcp-server-stealth-fetch")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="fetch",
                description="""Fetches a URL from the internet and optionally extracts its contents as markdown.

Uses a persistent browser-like session per host (httpcloak). At least 1 second passes between consecutive fetches (global).

Pagination: use start_index=0 first; continuation uses the cached full text for the same url and raw (no second download).""",
                inputSchema=Fetch.model_json_schema(),
            ),
            Tool(
                name="end_session",
                description="Close and discard the httpcloak session for a given host (or URL). The next fetch to that host opens a new session.",
                inputSchema=EndSession.model_json_schema(),
            ),
        ]

    @server.list_prompts()
    async def list_prompts() -> list[Prompt]:
        return [
            Prompt(
                name="fetch",
                description="Fetch a URL and extract its contents as markdown",
                arguments=[
                    PromptArgument(
                        name="url", description="URL to fetch", required=True
                    )
                ],
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == "fetch":
            try:
                args = Fetch(**arguments)
            except ValidationError as e:
                raise McpError(
                    ErrorData(code=INVALID_PARAMS, message=str(e))
                ) from e
            url = str(args.url)
            if not url:
                raise McpError(
                    ErrorData(code=INVALID_PARAMS, message="URL is required")
                )
            prefix, body = await state.fetch(
                url,
                args.max_length,
                args.start_index,
                args.raw,
            )
            return [
                TextContent(type="text", text=f"{prefix}Contents of {url}:\n{body}")
            ]
        if name == "end_session":
            try:
                args = EndSession(**arguments)
            except ValidationError as e:
                raise McpError(
                    ErrorData(code=INVALID_PARAMS, message=str(e))
                ) from e
            msg = await state.end_session(args.domain)
            return [TextContent(type="text", text=msg)]
        raise McpError(
            ErrorData(code=INVALID_PARAMS, message=f"Unknown tool: {name}")
        )

    @server.get_prompt()
    async def get_prompt(name: str, arguments: dict | None) -> GetPromptResult:
        if name != "fetch":
            raise McpError(
                ErrorData(code=INVALID_PARAMS, message=f"Unknown prompt: {name}")
            )
        if not arguments or "url" not in arguments:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="URL is required"))
        url = arguments["url"]
        try:
            prefix, body = await state.fetch(url, 5000, 0, False)
        except McpError as e:
            return GetPromptResult(
                description=f"Failed to fetch {url}",
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(type="text", text=str(e)),
                    )
                ],
            )
        return GetPromptResult(
            description=f"Contents of {url}",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(type="text", text=prefix + body),
                )
            ],
        )

    options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, options, raise_exceptions=False)
