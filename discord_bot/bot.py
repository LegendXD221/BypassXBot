
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import discord
import httpx
import uvicorn
from discord import app_commands
from discord.ext import commands
from fastapi import FastAPI

LOG = logging.getLogger("bypassx.discord")
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
MAX_AUTO_LINKS = 1
MAX_MESSAGE_LENGTH = 1900


def env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def env_float(
    name: str,
    default: float,
    minimum: float = 0.0,
) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


API_URL = os.getenv(
    "BYPASS_API_URL",
    "https://bypassx-bpzt.onrender.com",
).rstrip("/")

FALLBACK_API_URL = os.getenv(
    "BYPASS_FALLBACK_API_URL",
    "https://usebypass.com/api/v1/bypass",
).rstrip("/")

CROWD_API_URL = os.getenv(
    "BYPASS_CROWD_API_URL",
    "https://crowd.fastforward.team/crowd/query_v1",
).rstrip("/")

API_TIMEOUT = max(
    5.0,
    env_float("BYPASS_API_TIMEOUT", 75.0),
)

FALLBACK_TIMEOUT = min(
    120.0,
    env_float("BYPASS_FALLBACK_TIMEOUT", 120.0, minimum=5.0),
)

PREFIX = os.getenv("DISCORD_PREFIX", "+")

CONFIG_PATH = Path(
    os.getenv("AUTO_BYPASS_CONFIG", "data/auto_channels.json")
)

DATABASE_PATH = Path(
    os.getenv("BOT_DATABASE_PATH", "data/bypassx.sqlite3")
)

CACHE_TTL_SECONDS = env_float(
    "BYPASS_CACHE_TTL",
    300.0,
    minimum=0.0,
)

PORT = env_int("PORT", 10000)

USER_COOLDOWN_SECONDS = env_float(
    "BYPASS_USER_COOLDOWN",
    5.0,
)

GUILD_COOLDOWN_SECONDS = env_float(
    "BYPASS_GUILD_COOLDOWN",
    2.0,
)

MAX_CONCURRENT_BYPASSES = env_int(
    "BYPASS_MAX_CONCURRENT",
    3,
)

STARTED_AT = time.time()


def parse_ids(name: str) -> set[int]:
    values: set[int] = set()

    for raw in os.getenv(name, "").split(","):
        try:
            if raw.strip():
                values.add(int(raw.strip()))
        except ValueError:
            LOG.warning("Ignoring invalid %s value", name)

    return values


OWNER_IDS = parse_ids("BOT_OWNER_IDS")

try:
    DEV_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0")) or None
except ValueError:
    DEV_GUILD_ID = None


BRAND_COLOR = discord.Color.from_rgb(104, 89, 222)
SUCCESS_COLOR = discord.Color.from_rgb(46, 204, 113)
ERROR_COLOR = discord.Color.from_rgb(231, 76, 60)


def premium_embed(
    title: str,
    description: str = "",
    color: discord.Color = BRAND_COLOR,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"✦ {title}",
        description=description,
        color=color,
    )
    embed.set_footer(text="BypassX • Fast. Clean. Reliable.")
    return embed


class SQLiteStore:
    def __init__(
        self,
        db_path: Path,
        legacy_path: Path | None = None,
    ) -> None:
        self.db_path = db_path
        self.legacy_path = legacy_path
        self._lock = asyncio.Lock()

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auto_channels (
                    guild_id TEXT PRIMARY KEY,
                    channel_id INTEGER NOT NULL
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS resolution_cache (
                    original_url TEXT PRIMARY KEY,
                    destination TEXT NOT NULL,
                    service TEXT,
                    method TEXT,
                    expires_at REAL NOT NULL
                )
                """
            )

            if self.legacy_path and self.legacy_path.exists():
                try:
                    data = json.loads(
                        self.legacy_path.read_text()
                    )

                    for guild_id, channel_id in data.items():
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO auto_channels
                            (guild_id, channel_id)
                            VALUES (?, ?)
                            """,
                            (str(guild_id), int(channel_id)),
                        )

                    self.legacy_path.rename(
                        self.legacy_path.with_suffix(
                            self.legacy_path.suffix + ".migrated"
                        )
                    )

                except (
                    OSError,
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                ) as exc:
                    LOG.warning(
                        "Could not migrate legacy auto-bypass settings: %s",
                        type(exc).__name__,
                    )

    async def reload(self) -> None:
        async with self._lock:
            self._initialize()

    async def get(self, guild_id: int) -> int | None:
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT channel_id FROM auto_channels
                    WHERE guild_id = ?
                    """,
                    (str(guild_id),),
                ).fetchone()

                return int(row[0]) if row else None

    async def set(
        self,
        guild_id: int,
        channel_id: int,
    ) -> None:
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO auto_channels(guild_id, channel_id)
                    VALUES (?, ?)
                    ON CONFLICT(guild_id)
                    DO UPDATE SET channel_id = excluded.channel_id
                    """,
                    (str(guild_id), channel_id),
                )

    async def remove(self, guild_id: int) -> None:
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM auto_channels WHERE guild_id = ?",
                    (str(guild_id),),
                )

    async def cache_get(
        self,
        original_url: str,
    ) -> dict[str, Any] | None:
        async with self._lock:
            now = time.time()

            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT destination, service, method, expires_at
                    FROM resolution_cache
                    WHERE original_url = ?
                    """,
                    (original_url,),
                ).fetchone()

                if not row:
                    return None

                if row[3] <= now:
                    connection.execute(
                        "DELETE FROM resolution_cache WHERE original_url = ?",
                        (original_url,),
                    )
                    return None

                return {
                    "success": True,
                    "destination": row[0],
                    "service": row[1],
                    "method": row[2],
                    "cached": True,
                }

    async def cache_set(
        self,
        original_url: str,
        result: dict[str, Any],
    ) -> None:
        if CACHE_TTL_SECONDS <= 0:
            return

        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO resolution_cache
                    (original_url, destination, service, method, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(original_url)
                    DO UPDATE SET
                        destination = excluded.destination,
                        service = excluded.service,
                        method = excluded.method,
                        expires_at = excluded.expires_at
                    """,
                    (
                        original_url,
                        str(result["destination"]),
                        result.get("service"),
                        result.get("method"),
                        time.time() + CACHE_TTL_SECONDS,
                    ),
                )

    async def cache_clear(self) -> None:
        async with self._lock:
            with self._connect() as connection:
                connection.execute("DELETE FROM resolution_cache")

    async def cache_count(self) -> int:
        async with self._lock:
            with self._connect() as connection:
                return int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM resolution_cache
                        WHERE expires_at > ?
                        """,
                        (time.time(),),
                    ).fetchone()[0]
                )


class BypassLimiter:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._user_last: dict[int, float] = {}
        self._guild_last: dict[int, float] = {}
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_BYPASSES)

    async def reserve(
        self,
        user_id: int,
        guild_id: int | None,
    ) -> float:
        now = asyncio.get_running_loop().time()

        async with self._lock:
            user_remaining = USER_COOLDOWN_SECONDS - (
                now - self._user_last.get(user_id, 0.0)
            )

            guild_remaining = 0.0

            if guild_id is not None:
                guild_remaining = GUILD_COOLDOWN_SECONDS - (
                    now - self._guild_last.get(guild_id, 0.0)
                )

            remaining = max(
                user_remaining,
                guild_remaining,
                0.0,
            )

            if remaining > 0:
                return remaining

            self._user_last[user_id] = now

            if guild_id is not None:
                self._guild_last[guild_id] = now

            cutoff = now - max(
                USER_COOLDOWN_SECONDS,
                GUILD_COOLDOWN_SECONDS,
            ) * 2

            self._user_last = {
                key: value
                for key, value in self._user_last.items()
                if value >= cutoff
            }

            self._guild_last = {
                key: value
                for key, value in self._guild_last.items()
                if value >= cutoff
            }

            return 0.0

    async def __aenter__(self) -> "BypassLimiter":
        await self._slots.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: Any,
        exc: Any,
        traceback: Any,
    ) -> None:
        self._slots.release()


class BypassXBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix=PREFIX,
            intents=intents,
            help_command=None,
        )

        self.store = SQLiteStore(DATABASE_PATH, CONFIG_PATH)
        self.limiter = BypassLimiter()

        self.api_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                API_TIMEOUT,
                connect=15.0,
            ),
            follow_redirects=False,
        )

        self._guild_locks: dict[int, asyncio.Lock] = {}

    async def setup_hook(self) -> None:
        guild_id = os.getenv("DISCORD_GUILD_ID")

        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

            LOG.info(
                "Synced slash commands to guild %s",
                guild_id,
            )
        else:
            await self.tree.sync()
            LOG.info("Synced global slash commands")

    async def close(self) -> None:
        await self.api_client.aclose()
        await super().close()

    def guild_lock(self, guild_id: int) -> asyncio.Lock:
        return self._guild_locks.setdefault(
            guild_id,
            asyncio.Lock(),
        )

    # ---------------------------------------------------------
    # FIXED: Destination extraction
    # ---------------------------------------------------------

    @staticmethod
    def _destination_from_payload(
        payload: Any,
        original_url: str,
    ) -> str | None:
        """
        Extract destination URLs from direct or nested API responses.

        Supports:
        {"url": "https://example.com"}

        {"result": {"url": "https://example.com"}}

        {"data": {"destination": "https://example.com"}}
        """

        original = original_url.rstrip("/")

        def find_url(value: Any) -> str | None:
            if isinstance(value, str):
                candidate = value.strip().strip("\"'")

                if candidate.startswith("//"):
                    candidate = "https:" + candidate

                parsed = urlparse(candidate)

                if (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname
                    and candidate.rstrip("/") != original
                ):
                    return candidate

                return None

            if isinstance(value, dict):
                for key in (
                    "destination",
                    "url",
                    "target",
                    "link",
                ):
                    if key in value:
                        result = find_url(value[key])

                        if result:
                            return result

                for key in (
                    "result",
                    "data",
                    "response",
                ):
                    if key in value:
                        result = find_url(value[key])

                        if result:
                            return result

            return None

        return find_url(payload)

    # ---------------------------------------------------------
    # FIXED: UseBypass with HTTP 202 polling
    # ---------------------------------------------------------

    async def _resolve_usebypas(
        self,
        url: str,
    ) -> dict[str, Any] | None:
        """Resolve through UseBypass, including asynchronous polling."""

        try:
            response = await self.api_client.get(
                FALLBACK_API_URL,
                params={"url": url},
                timeout=FALLBACK_TIMEOUT,
            )

            if response.status_code not in (200, 202):
                LOG.warning(
                    "UseBypass submission returned HTTP %s",
                    response.status_code,
                )
                return None

            try:
                payload = response.json()
            except ValueError:
                LOG.warning("UseBypass returned invalid JSON")
                return None

            if not isinstance(payload, dict):
                return None

            def make_result(
                destination: str,
            ) -> dict[str, Any]:
                return {
                    "success": True,
                    "destination": destination,
                    "service": "usebypas",
                    "method": "usebypas",
                }

            # First, check for an immediate destination.
            destination = self._destination_from_payload(
                payload,
                url,
            )

            if destination:
                LOG.info("UseBypass resolved URL immediately")
                return make_result(destination)

            status = str(
                payload.get("status", "")
            ).upper()

            entry_id = (
                payload.get("id")
                or payload.get("entry_id")
            )

            pending_statuses = {
                "PENDING",
                "PROCESSING",
                "RESOLVING",
            }

            # Do not poll if the response isn't pending.
            if (
                response.status_code != 202
                and status not in pending_statuses
            ):
                LOG.info(
                    "UseBypass returned no destination; status=%s",
                    status or "unknown",
                )
                return None

            if not entry_id:
                LOG.warning(
                    "UseBypass returned pending status without a request ID"
                )
                return None

            result_endpoint = (
                f"{FALLBACK_API_URL.rstrip('/')}"
                f"/result/{entry_id}"
            )

            loop = asyncio.get_running_loop()
            deadline = loop.time() + FALLBACK_TIMEOUT

            while loop.time() < deadline:
                remaining = deadline - loop.time()

                if remaining <= 0:
                    break

                # Poll every two seconds.
                await asyncio.sleep(min(2.0, remaining))

                remaining = deadline - loop.time()

                if remaining <= 0:
                    break

                poll_timeout = min(10.0, remaining)

                poll = await self.api_client.get(
                    result_endpoint,
                    timeout=httpx.Timeout(
                        poll_timeout,
                        connect=min(5.0, poll_timeout),
                    ),
                )

                if poll.status_code not in (200, 202):
                    LOG.warning(
                        "UseBypass polling returned HTTP %s",
                        poll.status_code,
                    )
                    return None

                try:
                    result_payload = poll.json()
                except ValueError:
                    LOG.warning(
                        "UseBypass polling returned invalid JSON"
                    )
                    return None

                if not isinstance(result_payload, dict):
                    return None

                destination = self._destination_from_payload(
                    result_payload,
                    url,
                )

                if destination:
                    LOG.info(
                        "UseBypass resolved URL through polling"
                    )
                    return make_result(destination)

                result_status = str(
                    result_payload.get("status", "")
                ).upper()

                if result_status in {
                    "FAILED",
                    "ERROR",
                    "NOT_FOUND",
                    "BLOCKED",
                    "IGNORED",
                }:
                    LOG.info(
                        "UseBypass resolution failed: %s",
                        result_status,
                    )
                    return None

            LOG.warning("UseBypass polling timed out")
            return None

        except httpx.TimeoutException:
            LOG.warning("UseBypass request timed out")

        except httpx.HTTPError as exc:
            LOG.warning(
                "UseBypass HTTP error: %s",
                type(exc).__name__,
            )

        except (ValueError, TypeError) as exc:
            LOG.warning(
                "UseBypass response error: %s",
                type(exc).__name__,
            )

        return None

    # ---------------------------------------------------------
    # FastForward community lookup
    # ---------------------------------------------------------

    async def _resolve_crowd(
        self,
        url: str,
    ) -> dict[str, Any] | None:
        parsed = urlparse(url)

        if not parsed.hostname:
            return None

        path = parsed.path.lstrip("/")

        if parsed.query:
            path = f"{path}?{parsed.query}"

        try:
            response = await self.api_client.post(
                CROWD_API_URL,
                data={
                    "domain": parsed.hostname,
                    "path": path,
                },
            )

            if (
                response.status_code == 204
                or not response.is_success
            ):
                LOG.info(
                    "FastForward crowd provider returned status=%s",
                    response.status_code,
                )
                return None

            try:
                payload: Any = response.json()
            except ValueError:
                payload = response.text

            destination = self._destination_from_payload(
                payload,
                url,
            )

            if destination:
                return {
                    "success": True,
                    "destination": destination,
                    "service": "fastforward-crowd",
                    "method": "fastforward-crowd",
                }

        except (httpx.HTTPError, ValueError) as exc:
            LOG.info(
                "FastForward crowd provider unavailable: %s",
                type(exc).__name__,
            )

        return None

    # ---------------------------------------------------------
    # Resolver chain
    # ---------------------------------------------------------

    async def resolve(self, url: str) -> dict[str, Any]:
        cached = await self.store.cache_get(url)

        if cached:
            LOG.info("Resolution cache hit")
            return cached

        result: dict[str, Any] | None = None

        # Provider 1: Primary BypassX API.
        try:
            response = await self.api_client.post(
                f"{API_URL}/bypass",
                json={"url": url},
            )

            try:
                data = response.json()
            except ValueError:
                data = {}

            if (
                response.is_success
                and isinstance(data, dict)
                and data.get("success")
                and data.get("destination")
            ):
                result = data
                result.setdefault("service", "bypassx")
                result.setdefault("method", "primary")

            else:
                LOG.info(
                    "Primary API did not resolve URL; trying UseBypass"
                )

        except (httpx.HTTPError, ValueError) as exc:
            LOG.info(
                "Primary API unavailable: %s; trying UseBypass",
                type(exc).__name__,
            )

        # Provider 2: UseBypass.
        if result is None:
            result = await self._resolve_usebypas(url)

        if result is None:
            LOG.info(
                "UseBypass did not resolve URL; trying FastForward"
            )

        # Provider 3: FastForward community lookup.
        if result is None:
            result = await self._resolve_crowd(url)

        if result:
            await self.store.cache_set(url, result)
            return result

        return {
            "success": False,
            "error": (
                "Unable to resolve this URL through "
                "the configured providers"
            ),
        }

    # ---------------------------------------------------------
    # Discord response handling
    # ---------------------------------------------------------

    async def send_result(
        self,
        destination: discord.abc.Messageable,
        user: discord.abc.User,
        url: str,
        guild_id: int | None = None,
        ephemeral: bool = False,
    ) -> bool:
        remaining = await self.limiter.reserve(
            user.id,
            guild_id,
        )

        send_options: dict[str, Any] = {
            "allowed_mentions": discord.AllowedMentions(
                users=True,
                everyone=False,
                roles=False,
                replied_user=False,
            )
        }

        if ephemeral:
            send_options["ephemeral"] = True

        if remaining > 0:
            embed = premium_embed(
                "Slow down",
                (
                    f"Please wait **{remaining:.1f}s** "
                    "before sending another bypass request."
                ),
                ERROR_COLOR,
            )

            await destination.send(
                content=user.mention,
                embed=embed,
                **send_options,
            )

            return False

        async with self.limiter:
            result = await self.resolve(url)

        if result.get("success"):
            target = str(result["destination"])

            embed = premium_embed(
                "Link resolved",
                "Your destination is ready.",
                SUCCESS_COLOR,
            )

            embed.add_field(
                name="Original",
                value=url[:1024],
                inline=False,
            )

            embed.add_field(
                name="Destination",
                value=target[:1024],
                inline=False,
            )

            embed.set_footer(
                text=(
                    f"Service: {result.get('service', 'unknown')} "
                    f"• Method: {result.get('method', 'unknown')}"
                )
            )

            await destination.send(
                content=user.mention,
                embed=embed,
                **send_options,
            )

        else:
            embed = premium_embed(
                "Unable to resolve",
                result.get(
                    "error",
                    "Unknown resolver error",
                ),
                ERROR_COLOR,
            )

            embed.add_field(
                name="Original",
                value=url[:1024],
                inline=False,
            )

            await destination.send(
                content=user.mention,
                embed=embed,
                **send_options,
            )

        return True

    async def on_ready(self) -> None:
        if self.user:
            LOG.info(
                "Logged in as %s (%s)",
                self.user,
                self.user.id,
            )

    async def on_message(
        self,
        message: discord.Message,
    ) -> None:
        if message.author.bot or not message.guild:
            return

        await self.process_commands(message)

        if message.content.startswith(PREFIX):
            return

        channel_id = await self.store.get(message.guild.id)

        if channel_id != message.channel.id:
            return

        links = URL_RE.findall(message.content)[:MAX_AUTO_LINKS]

        if not links:
            return

        async with self.guild_lock(message.guild.id):
            await self.send_result(
                message.channel,
                message.author,
                links[0].rstrip(".,!?)]}"),
                guild_id=message.guild.id,
            )


bot = BypassXBot()

web_app = FastAPI(
    title="BypassXBot",
    version="1.0.0",
)


@web_app.get("/")
async def web_root() -> dict[str, Any]:
    return {
        "name": "BypassXBot",
        "status": "online",
        "discord_ready": bot.is_ready(),
        "uptime_seconds": round(time.time() - STARTED_AT),
        "cache_entries": await bot.store.cache_count(),
    }


@web_app.get("/health")
async def web_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "discord_ready": bot.is_ready(),
        "uptime_seconds": round(time.time() - STARTED_AT),
        "cache_entries": await bot.store.cache_count(),
    }


async def require_manage_guild(
    interaction: discord.Interaction,
) -> bool:
    if (
        not interaction.guild
        or not isinstance(interaction.user, discord.Member)
        or not interaction.user.guild_permissions.manage_guild
    ):
        await interaction.response.send_message(
            embed=premium_embed(
                "Permission required",
                "You need the **Manage Server** permission for this command.",
                ERROR_COLOR,
            ),
            ephemeral=True,
        )
        return False

    return True


async def require_owner(
    interaction: discord.Interaction,
) -> bool:
    if interaction.user.id not in OWNER_IDS:
        await interaction.response.send_message(
            embed=premium_embed(
                "Owner only",
                "This command is restricted to the configured bot owner.",
                ERROR_COLOR,
            ),
            ephemeral=True,
        )
        return False

    return True


async def require_dev_owner(
    interaction: discord.Interaction,
) -> bool:
    if not await require_owner(interaction):
        return False

    if (
        not DEV_GUILD_ID
        or not interaction.guild
        or interaction.guild.id != DEV_GUILD_ID
    ):
        await interaction.response.send_message(
            embed=premium_embed(
                "Developer guild only",
                "This maintenance command is available only in the configured developer server.",
                ERROR_COLOR,
            ),
            ephemeral=True,
        )
        return False

    return True


async def prefix_owner_check(
    ctx: commands.Context,
) -> bool:
    if ctx.author.id not in OWNER_IDS:
        raise commands.CheckFailure("owner_only")

    return True


async def prefix_dev_check(
    ctx: commands.Context,
) -> bool:
    if (
        ctx.author.id not in OWNER_IDS
        or not DEV_GUILD_ID
        or not ctx.guild
        or ctx.guild.id != DEV_GUILD_ID
    ):
        raise commands.CheckFailure("developer_guild_only")

    return True


def help_embed() -> discord.Embed:
    embed = premium_embed(
        "Command center",
        "Resolve links, automate channels, and manage your server with BypassX.",
    )

    embed.add_field(
        name="◆ Resolver",
        value=(
            f"`{PREFIX}bypass <url>`\n"
            "`/bypass` — resolve a shortlink instantly"
        ),
        inline=False,
    )

    embed.add_field(
        name="◆ Automation",
        value=(
            f"`{PREFIX}autobypass on [#channel]`\n"
            f"`{PREFIX}autobypass off`\n"
            "`/autobypass` — configure automatic link detection"
        ),
        inline=False,
    )

    embed.add_field(
        name="◆ Server tools",
        value=(
            f"`{PREFIX}status` • "
            f"`{PREFIX}ping` • "
            f"`{PREFIX}help`"
        ),
        inline=False,
    )

    embed.set_thumbnail(
        url="https://cdn.simpleicons.org/discord/5865F2"
    )

    return embed


@bot.command(name="bypass")
async def bypass_command(
    ctx: commands.Context,
    url: str | None = None,
) -> None:
    if not url:
        await ctx.reply(
            f"Usage: `{PREFIX}bypass <url>`"
        )
        return

    await ctx.typing()

    await bot.send_result(
        ctx.channel,
        ctx.author,
        url,
        guild_id=ctx.guild.id if ctx.guild else None,
    )


@bot.command(name="setautochannel")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def set_auto_channel_command(
    ctx: commands.Context,
    channel: discord.TextChannel | None = None,
) -> None:
    target = channel or ctx.channel

    await bot.store.set(
        ctx.guild.id,
        target.id,
    )

    await ctx.reply(
        f"Auto-bypass is enabled in {target.mention}."
    )


@bot.command(name="disableautobypass")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def disable_auto_channel_command(
    ctx: commands.Context,
) -> None:
    await bot.store.remove(ctx.guild.id)
    await ctx.reply("Auto-bypass is disabled for this server.")


@bot.command(name="autobypass")
async def auto_bypass_command(
    ctx: commands.Context,
    action: str | None = None,
    channel: discord.TextChannel | None = None,
) -> None:
    if (
        not ctx.guild
        or not isinstance(ctx.author, discord.Member)
        or not ctx.author.guild_permissions.manage_guild
    ):
        await ctx.reply(
            "You need the Manage Server permission for this command."
        )
        return

    if action and action.lower() in {
        "off",
        "disable",
        "disabled",
    }:
        await bot.store.remove(ctx.guild.id)
        await ctx.reply("Auto-bypass is disabled for this server.")
        return

    target = channel or ctx.channel

    await bot.store.set(
        ctx.guild.id,
        target.id,
    )

    await ctx.reply(
        f"Auto-bypass is enabled in {target.mention}."
    )


@bot.command(name="status")
async def status_command(
    ctx: commands.Context,
) -> None:
    channel_id = (
        await bot.store.get(ctx.guild.id)
        if ctx.guild
        else None
    )

    channel_text = (
        f"<#{channel_id}>"
        if channel_id
        else "disabled"
    )

    embed = premium_embed(
        "Server status",
        "Your BypassX configuration at a glance.",
    )

    embed.add_field(
        name="API",
        value="Online endpoint",
        inline=True,
    )

    embed.add_field(
        name="Auto-bypass",
        value=channel_text,
        inline=True,
    )

    embed.add_field(
        name="Prefix",
        value=f"`{PREFIX}`",
        inline=True,
    )

    embed.add_field(
        name="Cache",
        value=f"`{await bot.store.cache_count()}` active entries",
        inline=True,
    )

    await ctx.reply(embed=embed)


@bot.command(name="ping")
async def ping_command(
    ctx: commands.Context,
) -> None:
    await ctx.reply(
        embed=premium_embed(
            "Pong",
            f"Gateway latency: **{round(bot.latency * 1000)}ms**",
        )
    )


@bot.command(name="help")
async def help_command(
    ctx: commands.Context,
) -> None:
    await ctx.reply(embed=help_embed())


@bot.command(name="ownerstatus")
@commands.check(prefix_owner_check)
async def owner_status_command(
    ctx: commands.Context,
) -> None:
    embed = premium_embed(
        "Owner console",
        "Private runtime information.",
    )

    embed.add_field(
        name="Discord user",
        value=f"`{ctx.author.id}`",
        inline=True,
    )

    embed.add_field(
        name="Servers",
        value=f"`{len(bot.guilds)}`",
        inline=True,
    )

    embed.add_field(
        name="Developer guild",
        value=f"`{DEV_GUILD_ID or 'not configured'}`",
        inline=False,
    )

    embed.add_field(
        name="Cache entries",
        value=f"`{await bot.store.cache_count()}`",
        inline=True,
    )

    embed.add_field(
        name="Database",
        value=f"`{DATABASE_PATH}`",
        inline=False,
    )

    await ctx.reply(embed=embed)


@bot.command(name="reload")
@commands.check(prefix_dev_check)
async def reload_command(
    ctx: commands.Context,
) -> None:
    await bot.store.reload()

    await ctx.reply(
        embed=premium_embed(
            "Configuration reloaded",
            "Auto-bypass settings were reloaded from disk.",
            SUCCESS_COLOR,
        )
    )


@bot.command(name="sync")
@commands.check(prefix_dev_check)
async def sync_command(
    ctx: commands.Context,
) -> None:
    synced = await bot.tree.sync()

    await ctx.reply(
        embed=premium_embed(
            "Commands synced",
            f"Synchronized **{len(synced)}** slash commands globally.",
            SUCCESS_COLOR,
        )
    )


@bot.command(name="debug")
@commands.check(prefix_dev_check)
async def debug_command(
    ctx: commands.Context,
) -> None:
    embed = premium_embed(
        "Developer diagnostics",
        "Runtime details for the configured developer guild.",
    )

    embed.add_field(
        name="Ready",
        value=str(bot.is_ready()),
        inline=True,
    )

    embed.add_field(
        name="Latency",
        value=f"{round(bot.latency * 1000)}ms",
        inline=True,
    )

    embed.add_field(
        name="API",
        value=API_URL,
        inline=False,
    )

    await ctx.reply(embed=embed)


@bot.command(name="cacheclear")
@commands.check(prefix_dev_check)
async def cache_clear_command(
    ctx: commands.Context,
) -> None:
    await bot.store.cache_clear()

    await ctx.reply(
        embed=premium_embed(
            "Cache cleared",
            "All stored successful resolutions were removed.",
            SUCCESS_COLOR,
        )
    )


@bot.tree.command(
    name="bypass",
    description="Resolve a shortlink with BypassX",
)
@app_commands.describe(
    url="The HTTP(S) shortlink to resolve"
)
async def bypass_slash(
    interaction: discord.Interaction,
    url: str,
) -> None:
    await interaction.response.defer()

    await bot.send_result(
        interaction.followup,
        interaction.user,
        url,
        guild_id=interaction.guild.id if interaction.guild else None,
        ephemeral=True,
    )


@bot.tree.command(
    name="autobypass",
    description="Enable or disable automatic bypass in a channel",
)
@app_commands.describe(
    enabled="Whether auto-bypass should be enabled",
    channel="The channel to monitor",
)
async def autobypass_slash(
    interaction: discord.Interaction,
    enabled: bool,
    channel: discord.TextChannel | None = None,
) -> None:
    if not await require_manage_guild(interaction):
        return

    target = channel or interaction.channel

    if enabled:
        await bot.store.set(
            interaction.guild.id,
            target.id,
        )

        await interaction.response.send_message(
            f"Auto-bypass enabled in {target.mention}."
        )
    else:
        await bot.store.remove(interaction.guild.id)

        await interaction.response.send_message(
            "Auto-bypass disabled for this server."
        )


@bot.tree.command(
    name="status",
    description="Show BypassX server settings",
)
async def status_slash(
    interaction: discord.Interaction,
) -> None:
    channel_id = (
        await bot.store.get(interaction.guild.id)
        if interaction.guild
        else None
    )

    embed = premium_embed(
        "Server status",
        "Your BypassX configuration at a glance.",
    )

    embed.add_field(
        name="API",
        value="Online endpoint",
        inline=True,
    )

    embed.add_field(
        name="Auto-bypass",
        value=f"<#{channel_id}>" if channel_id else "disabled",
        inline=True,
    )

    embed.add_field(
        name="Cache",
        value=f"`{await bot.store.cache_count()}` active entries",
        inline=True,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="ping",
    description="Check BypassX latency",
)
async def ping_slash(
    interaction: discord.Interaction,
) -> None:
    await interaction.response.send_message(
        embed=premium_embed(
            "Pong",
            f"Gateway latency: **{round(bot.latency * 1000)}ms**",
        )
    )


@bot.tree.command(
    name="help",
    description="Open the BypassX command center",
)
async def help_slash(
    interaction: discord.Interaction,
) -> None:
    await interaction.response.send_message(
        embed=help_embed(),
        ephemeral=True,
    )


@bot.tree.command(
    name="ownerstatus",
    description="Show private bot owner diagnostics",
)
async def owner_status_slash(
    interaction: discord.Interaction,
) -> None:
    if not await require_owner(interaction):
        return

    embed = premium_embed(
        "Owner console",
        "Private runtime information.",
    )

    embed.add_field(
        name="Servers",
        value=f"`{len(bot.guilds)}`",
        inline=True,
    )

    embed.add_field(
        name="Ready",
        value=str(bot.is_ready()),
        inline=True,
    )

    embed.add_field(
        name="Cache entries",
        value=f"`{await bot.store.cache_count()}`",
        inline=True,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="reload",
    description="Reload bot configuration (developer guild only)",
)
async def reload_slash(
    interaction: discord.Interaction,
) -> None:
    if not await require_dev_owner(interaction):
        return

    await bot.store.reload()

    await interaction.response.send_message(
        embed=premium_embed(
            "Configuration reloaded",
            "Auto-bypass settings were reloaded from disk.",
            SUCCESS_COLOR,
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="sync",
    description="Sync slash commands (developer guild only)",
)
async def sync_slash(
    interaction: discord.Interaction,
) -> None:
    if not await require_dev_owner(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    synced = await bot.tree.sync()

    await interaction.followup.send(
        embed=premium_embed(
            "Commands synced",
            f"Synchronized **{len(synced)}** slash commands globally.",
            SUCCESS_COLOR,
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="debug",
    description="Show private diagnostics (developer guild only)",
)
async def debug_slash(
    interaction: discord.Interaction,
) -> None:
    if not await require_dev_owner(interaction):
        return

    embed = premium_embed(
        "Developer diagnostics",
        "Runtime details for the configured developer guild.",
    )

    embed.add_field(
        name="Ready",
        value=str(bot.is_ready()),
        inline=True,
    )

    embed.add_field(
        name="Latency",
        value=f"{round(bot.latency * 1000)}ms",
        inline=True,
    )

    embed.add_field(
        name="API",
        value=API_URL,
        inline=False,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="cacheclear",
    description="Clear cached resolutions (developer guild only)",
)
async def cache_clear_slash(
    interaction: discord.Interaction,
) -> None:
    if not await require_dev_owner(interaction):
        return

    await bot.store.cache_clear()

    await interaction.response.send_message(
        embed=premium_embed(
            "Cache cleared",
            "All stored successful resolutions were removed.",
            SUCCESS_COLOR,
        ),
        ephemeral=True,
    )


@bot.event
async def on_command_error(
    ctx: commands.Context,
    error: commands.CommandError,
) -> None:
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.CheckFailure):
        message = "This command is restricted to the bot owner."

        if str(error) == "developer_guild_only":
            message = (
                "This maintenance command is restricted to "
                "the bot owner in the developer guild."
            )

        await ctx.reply(
            embed=premium_embed(
                "Access denied",
                message,
                ERROR_COLOR,
            )
        )
        return

    if isinstance(error, commands.MissingPermissions):
        await ctx.reply(
            embed=premium_embed(
                "Permission required",
                "You need the **Manage Server** permission for that command.",
                ERROR_COLOR,
            )
        )
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(
            embed=premium_embed(
                "Missing argument",
                f"Use `{PREFIX}help` for command usage.",
                ERROR_COLOR,
            )
        )
        return

    LOG.warning(
        "Command error: %s",
        type(error).__name__,
    )

    await ctx.reply(
        embed=premium_embed(
            "Command error",
            "That command could not be completed.",
            ERROR_COLOR,
        )
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO")
    )

    token = os.getenv("DISCORD_TOKEN")

    if not token:
        raise SystemExit("DISCORD_TOKEN is required")

    async def main() -> None:
        server = uvicorn.Server(
            uvicorn.Config(
                web_app,
                host="0.0.0.0",
                port=PORT,
                log_level="info",
                log_config=None,
            )
        )

        bot_task = asyncio.create_task(
            bot.start(token, reconnect=True),
            name="discord-gateway",
        )

        web_task = asyncio.create_task(
            server.serve(),
            name="health-server",
        )

        done, pending = await asyncio.wait(
            {bot_task, web_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()

        await asyncio.gather(
            *pending,
            return_exceptions=True,
        )

        for task in done:
            if not task.cancelled() and task.exception():
                raise task.exception()

        await bot.close()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass