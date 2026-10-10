from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import discord
import httpx
import uvicorn
from discord import app_commands
from discord.ext import commands
from fastapi import FastAPI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN", "")
API_URL = os.getenv("API_URL", "https://bypassx-bpzt.onrender.com").rstrip("/")
FALLBACK_API_URL = os.getenv(
    "FALLBACK_API_URL", "https://usebypass.com/api/v1/bypass"
)
TRW_API_URL = os.getenv("TRW_API_URL", "https://trw.lat/api/bypass")
OMEGATECH_API_URL = os.getenv(
    "OMEGATECH_API_URL", "https://api.omegatech.app/api/tools/All-bypass"
)
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/bypassxbot.sqlite3"))
PORT = int(os.getenv("PORT", "10000"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "18"))
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))
USER_COOLDOWN = float(os.getenv("USER_COOLDOWN", "4"))
OWNER_IDS = {int(value.strip()) for value in os.getenv("OWNER_IDS", "").split(",") if value.strip().isdigit()}
AUTO_BYPASS_LINKS = os.getenv("AUTO_BYPASS_LINKS", "true").lower() in {
    "1", "true", "yes", "on"
}

LOG = logging.getLogger("bypassxbot")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def valid_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except (TypeError, ValueError):
        return False


def clean_url(value: str) -> str:
    return value.strip().rstrip(".,!?;:)]}\u300b\u3009")


class SQLiteStore:
    """Small SQLite cache and request-history store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS cache (
                    source_url TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS requests (
                    user_id INTEGER PRIMARY KEY,
                    last_request REAL NOT NULL
                )"""
            )
            db.commit()

    async def cache_get(self, url: str) -> dict[str, Any] | None:
        async with self._lock:
            def read() -> tuple[str, float] | None:
                with sqlite3.connect(self.path) as db:
                    return db.execute(
                        "SELECT payload, created_at FROM cache WHERE source_url=?",
                        (url,),
                    ).fetchone()
            row = await asyncio.to_thread(read)
        if not row or time.time() - float(row[1]) > CACHE_TTL:
            return None
        import json
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) and value.get("success") else None
        except (ValueError, TypeError):
            return None

    async def cache_set(self, url: str, payload: dict[str, Any]) -> None:
        import json
        async with self._lock:
            def write() -> None:
                with sqlite3.connect(self.path) as db:
                    db.execute(
                        "INSERT OR REPLACE INTO cache(source_url,payload,created_at) VALUES(?,?,?)",
                        (url, json.dumps(payload), time.time()),
                    )
                    db.commit()
            await asyncio.to_thread(write)

    async def cooldown_remaining(self, user_id: int) -> float:
        async with self._lock:
            def read() -> float | None:
                with sqlite3.connect(self.path) as db:
                    row = db.execute(
                        "SELECT last_request FROM requests WHERE user_id=?", (user_id,)
                    ).fetchone()
                    return float(row[0]) if row else None
            last = await asyncio.to_thread(read)
        if last is None:
            return 0.0
        return max(0.0, USER_COOLDOWN - (time.time() - last))

    async def clear_cache(self) -> int:
        async with self._lock:
            def write() -> int:
                with sqlite3.connect(self.path) as db:
                    cursor = db.execute("DELETE FROM cache")
                    db.commit()
                    return cursor.rowcount
            return await asyncio.to_thread(write)

    async def counts(self) -> tuple[int, int]:
        async with self._lock:
            def read() -> tuple[int, int]:
                with sqlite3.connect(self.path) as db:
                    cache_count = int(db.execute("SELECT COUNT(*) FROM cache").fetchone()[0])
                    user_count = int(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
                    return cache_count, user_count
            return await asyncio.to_thread(read)

    async def mark_request(self, user_id: int) -> None:
        async with self._lock:
            def write() -> None:
                with sqlite3.connect(self.path) as db:
                    db.execute(
                        "INSERT OR REPLACE INTO requests(user_id,last_request) VALUES(?,?)",
                        (user_id, time.time()),
                    )
                    db.commit()
            await asyncio.to_thread(write)


class BypassXBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.store = SQLiteStore(DATABASE_PATH)
        self.api_client = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT),
            headers={"User-Agent": "BypassXBot/1.0"},
            follow_redirects=True,
        )
        self._last_auto_bypass: dict[tuple[int, int], float] = {}
        self.maintenance = False
        self.started_at = time.monotonic()

    async def setup_hook(self) -> None:
        try:
            synced = await self.tree.sync()
            LOG.info("Synced %d application command(s)", len(synced))
        except Exception:
            LOG.exception("Failed to sync application commands")
        asyncio.create_task(run_health_server())

    async def close(self) -> None:
        await self.api_client.aclose()
        await super().close()

    @staticmethod
    def _destination_from_payload(payload: Any, original_url: str) -> str | None:
        """Extract a final HTTP(S) destination from common API response shapes."""
        candidates: list[Any] = []
        if isinstance(payload, dict):
            for key in ("destination", "result", "url", "link", "bypassed", "target", "destination_url"):
                if key in payload:
                    candidates.append(payload[key])
            for key in ("data", "result", "destination", "response"):
                nested = payload.get(key)
                if isinstance(nested, dict):
                    for subkey in ("destination", "result", "url", "link", "target"):
                        if subkey in nested:
                            candidates.append(nested[subkey])
        elif isinstance(payload, str):
            candidates.append(payload)

        for candidate in candidates:
            if isinstance(candidate, str):
                candidate = candidate.strip()
                if valid_http_url(candidate) and candidate.rstrip("/") != original_url.rstrip("/"):
                    return candidate
        return None

    async def _resolve_usebypas(self, url: str) -> dict[str, Any] | None:
        try:
            response = await self.api_client.get(FALLBACK_API_URL, params={"url": url})
            if not response.is_success:
                LOG.info("UseBypass returned HTTP %s", response.status_code)
                return None
            try:
                payload = response.json()
            except ValueError:
                return None
            destination = self._destination_from_payload(payload, url)
            if destination:
                return {"success": True, "destination": destination, "service": "usebypas", "method": "usebypas-api"}
        except httpx.HTTPError as exc:
            LOG.info("UseBypass unavailable: %s", type(exc).__name__)
        return None

    async def _resolve_trw(self, url: str) -> dict[str, Any] | None:
        try:
            response = await self.api_client.get(
                TRW_API_URL,
                params={"url": url, "mode": "stream", "verbose": "true"},
            )
            if not response.is_success:
                LOG.info("TRW returned HTTP %s", response.status_code)
                return None
            try:
                payload = response.json()
            except ValueError:
                payload = response.text
            destination = self._destination_from_payload(payload, url)
            if destination:
                return {"success": True, "destination": destination, "service": "trw", "method": "trw-api"}
        except httpx.HTTPError as exc:
            LOG.info("TRW unavailable: %s", type(exc).__name__)
        return None

    async def _resolve_omegatech(self, url: str) -> dict[str, Any] | None:
        try:
            response = await self.api_client.get(OMEGATECH_API_URL, params={"url": url})
            if not response.is_success:
                LOG.info("OmegaTech returned HTTP %s", response.status_code)
                return None
            try:
                payload = response.json()
            except ValueError:
                LOG.warning("OmegaTech returned invalid JSON")
                return None
            if not isinstance(payload, dict) or payload.get("success") is not True:
                return None
            destination = self._destination_from_payload(payload, url)
            if destination:
                return {"success": True, "destination": destination, "service": "omegatech", "method": "omegatech-api"}
        except httpx.HTTPError as exc:
            LOG.info("OmegaTech unavailable: %s", type(exc).__name__)
        return None

    async def resolve(self, url: str) -> dict[str, Any]:
        cached = await self.store.cache_get(url)
        if cached:
            LOG.info("Resolution cache hit")
            return cached

        result: dict[str, Any] | None = None
        # Provider 1: primary BypassX API.
        try:
            response = await self.api_client.post(f"{API_URL}/bypass", json={"url": url})
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if response.is_success and isinstance(payload, dict) and payload.get("success") and payload.get("destination"):
                destination = self._destination_from_payload(payload, url)
                if destination:
                    result = {**payload, "success": True, "destination": destination}
                    result.setdefault("service", "bypassx")
                    result.setdefault("method", "primary")
            if result is None:
                LOG.info("Primary API did not resolve URL; trying UseBypass")
        except httpx.HTTPError as exc:
            LOG.info("Primary API unavailable: %s", type(exc).__name__)

        if result is None:
            result = await self._resolve_usebypas(url)
        if result is None:
            LOG.info("UseBypass did not resolve URL; trying TRW")
            result = await self._resolve_trw(url)
        if result is None:
            LOG.info("TRW did not resolve URL; trying OmegaTech")
            result = await self._resolve_omegatech(url)

        if result and result.get("success") and valid_http_url(str(result.get("destination", ""))):
            await self.store.cache_set(url, result)
            return result
        return {"success": False, "error": "Unable to resolve this URL through the configured providers"}

    async def send_bypass_result(
        self,
        *,
        user: discord.abc.User,
        channel: discord.abc.Messageable,
        url: str,
        status_message: discord.Message | None = None,
        interaction: discord.Interaction | None = None,
    ) -> None:
        """Resolve a URL, remove only the temporary status message, then post result."""
        try:
            result = await self.resolve(url)
        except Exception:
            LOG.exception("Unexpected bypass failure")
            result = {"success": False, "error": "Unexpected error"}
        finally:
            if status_message is not None:
                with suppress(discord.HTTPException, discord.Forbidden):
                    await status_message.delete()

        if result.get("success"):
            destination = str(result.get("destination", ""))
            embed = premium_embed("Link resolved", "Your destination is ready.")
            embed.add_field(name="Destination", value=f"[Open destination]({destination})", inline=False)
            service = str(result.get("service", "BypassX")).replace("_", " ").title()
            embed.add_field(name="Resolver", value=service, inline=True)
            if interaction is not None:
                await interaction.followup.send(embed=embed, ephemeral=False, allowed_mentions=discord.AllowedMentions.none())
            else:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        else:
            embed = premium_embed("Could not resolve link", "None of the configured providers returned a valid destination. Please try again later.")
            if interaction is not None:
                await interaction.followup.send(embed=embed, ephemeral=False)
            else:
                await channel.send(embed=embed)

    async def on_ready(self) -> None:
        LOG.info("Logged in as %s (ID: %s)", self.user, self.user.id if self.user else "unknown")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        await self.process_commands(message)
        if not AUTO_BYPASS_LINKS or not message.guild or (self.maintenance and message.author.id not in OWNER_IDS):
            return
        urls = [clean_url(match.group(0)) for match in URL_RE.finditer(message.content)]
        urls = [url for url in urls if valid_http_url(url)]
        if not urls:
            return
        # Handle only the first URL in a message to avoid spam/multiple posts.
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if now - self._last_auto_bypass.get(key, 0.0) < USER_COOLDOWN:
            return
        self._last_auto_bypass[key] = now
        status = await message.channel.send(
            embed=premium_embed("Resolving link", "Checking available providers…"),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.send_bypass_result(
            user=message.author,
            channel=message.channel,
            url=urls[0],
            status_message=status,
        )


bot = BypassXBot()


@bot.tree.command(name="bypass", description="Bypass a supported short link")
@app_commands.describe(url="The link you want to bypass")
async def bypass_command(interaction: discord.Interaction, url: str) -> None:
    if bot.maintenance and interaction.user.id not in OWNER_IDS:
        await interaction.response.send_message(
            embed=premium_embed("Maintenance mode", "Bypass is temporarily unavailable. Please try again soon."),
            ephemeral=True,
        )
        return
    if not valid_http_url(url):
        await interaction.response.send_message(
            embed=premium_embed("Invalid URL", "Enter a valid link beginning with `https://` or `http://`."),
            ephemeral=True,
        )
        return

    remaining = await bot.store.cooldown_remaining(interaction.user.id)
    if remaining > 0:
        await interaction.response.send_message(
            embed=premium_embed("Slow down", f"You can use `/bypass` again in **{remaining:.1f}s**."),
            ephemeral=True,
        )
        return
    await bot.store.mark_request(interaction.user.id)
    await interaction.response.defer(thinking=False)
    status = await interaction.followup.send(
        embed=premium_embed("Resolving link", "Checking available providers…"),
        wait=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await bot.send_bypass_result(
        user=interaction.user,
        channel=interaction.channel or interaction.user,
        url=url.strip(),
        status_message=status,
        interaction=interaction,
    )


@bot.tree.command(name="ping", description="Check the bot's response time")
async def ping_command(interaction: discord.Interaction) -> None:
    embed = premium_embed("Pong", "BypassX is online and responding.")
    embed.add_field(name="Gateway latency", value=f"{round(bot.latency * 1000)} ms", inline=True)
    embed.add_field(name="Status", value="Operational", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


def premium_embed(title: str, description: str = "") -> discord.Embed:
    embed = discord.Embed(title=title, description=description, colour=discord.Colour.from_rgb(124, 92, 255))
    embed.set_footer(text="BypassX • Minimal. Fast. Reliable.")
    return embed


async def owner_only(interaction: discord.Interaction) -> bool:
    if not is_owner(interaction.user.id):
        await interaction.response.send_message(
            embed=premium_embed("Access denied", "This control is restricted to the bot owner."),
            ephemeral=True,
        )
        return False
    return True


@bot.tree.command(name="help", description="View BypassX commands")
async def help_command(interaction: discord.Interaction) -> None:
    embed = premium_embed("BypassX", "A clean, fast link resolver with automatic link detection.")
    embed.add_field(name="/bypass <url>", value="Resolve a supported short link.", inline=False)
    embed.add_field(name="/ping", value="Check bot latency.", inline=True)
    embed.add_field(name="/status", value="View resolver providers.", inline=True)
    embed.add_field(name="Automatic mode", value="Post a supported link in a server channel to try automatic resolution.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="sync", description="Owner: sync slash commands")
async def sync_command(interaction: discord.Interaction) -> None:
    if not await owner_only(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    synced = await bot.tree.sync()
    embed = premium_embed("Commands synced", f"Successfully synced **{len(synced)}** application commands.")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="clearcache", description="Owner: clear cached bypass results")
async def clearcache_command(interaction: discord.Interaction) -> None:
    if not await owner_only(interaction):
        return
    deleted = await bot.store.clear_cache()
    await interaction.response.send_message(
        embed=premium_embed("Cache cleared", f"Removed **{deleted}** cached result(s)."), ephemeral=True
    )


@bot.tree.command(name="stats", description="Owner: view bot statistics")
async def stats_command(interaction: discord.Interaction) -> None:
    if not await owner_only(interaction):
        return
    cache_count, tracked_users = await bot.store.counts()
    uptime = int(time.monotonic() - bot.started_at)
    hours, remainder = divmod(uptime, 3600)
    minutes, seconds = divmod(remainder, 60)
    embed = premium_embed("Bot overview", "Live operational snapshot.")
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)} ms", inline=True)
    embed.add_field(name="Uptime", value=f"{hours}h {minutes}m {seconds}s", inline=True)
    embed.add_field(name="Cached results", value=str(cache_count), inline=True)
    embed.add_field(name="Tracked users", value=str(tracked_users), inline=True)
    embed.add_field(name="Auto-bypass", value="Enabled" if AUTO_BYPASS_LINKS else "Disabled", inline=True)
    embed.add_field(name="Maintenance", value="On" if bot.maintenance else "Off", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="setcooldown", description="Owner: set the user cooldown in seconds")
@app_commands.describe(seconds="Cooldown from 1 to 120 seconds")
async def setcooldown_command(interaction: discord.Interaction, seconds: app_commands.Range[int, 1, 120]) -> None:
    if not await owner_only(interaction):
        return
    global USER_COOLDOWN
    USER_COOLDOWN = float(seconds)
    await interaction.response.send_message(
        embed=premium_embed("Cooldown updated", f"New cooldown: **{seconds} seconds**."), ephemeral=True
    )


@bot.tree.command(name="maintenance", description="Owner: toggle public bypass access")
@app_commands.describe(enabled="Turn maintenance mode on or off")
async def maintenance_command(interaction: discord.Interaction, enabled: bool) -> None:
    if not await owner_only(interaction):
        return
    bot.maintenance = enabled
    state = "enabled" if enabled else "disabled"
    await interaction.response.send_message(
        embed=premium_embed("Maintenance updated", f"Public bypass access is now **{state}**."), ephemeral=True
    )


@bot.tree.command(name="announce", description="Owner: post an announcement in a channel")
@app_commands.describe(channel="Destination channel", message="Announcement text")
async def announce_command(interaction: discord.Interaction, channel: discord.TextChannel, message: str) -> None:
    if not await owner_only(interaction):
        return
    if len(message) > 1800:
        await interaction.response.send_message("Keep announcements under 1,800 characters.", ephemeral=True)
        return
    embed = premium_embed("Announcement", message)
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        await interaction.response.send_message("Could not post there. Check the bot's channel permissions.", ephemeral=True)
        return
    await interaction.response.send_message(
        embed=premium_embed("Announcement sent", f"Posted successfully in {channel.mention}."), ephemeral=True
    )


@bot.tree.command(name="reload", description="Owner: reload runtime settings from environment")
async def reload_command(interaction: discord.Interaction) -> None:
    if not await owner_only(interaction):
        return
    global API_URL, FALLBACK_API_URL, TRW_API_URL, OMEGATECH_API_URL, USER_COOLDOWN, AUTO_BYPASS_LINKS, REQUEST_TIMEOUT
    API_URL = os.getenv("API_URL", "https://bypassx-bpzt.onrender.com").rstrip("/")
    FALLBACK_API_URL = os.getenv("FALLBACK_API_URL", "https://usebypass.com/api/v1/bypass")
    TRW_API_URL = os.getenv("TRW_API_URL", "https://trw.lat/api/bypass")
    OMEGATECH_API_URL = os.getenv("OMEGATECH_API_URL", "https://api.omegatech.app/api/tools/All-bypass")
    USER_COOLDOWN = float(os.getenv("USER_COOLDOWN", "4"))
    REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "18"))
    AUTO_BYPASS_LINKS = os.getenv("AUTO_BYPASS_LINKS", "true").lower() in {"1", "true", "yes", "on"}
    await bot.api_client.aclose()
    bot.api_client = httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT),
        headers={"User-Agent": "BypassXBot/1.0"},
        follow_redirects=True,
    )
    await interaction.response.send_message(
        embed=premium_embed("Runtime reloaded", "Environment-based provider URLs, timeout, cooldown and auto-bypass settings have been refreshed."),
        ephemeral=True,
    )


@bot.tree.command(name="shutdown", description="Owner: gracefully stop the bot process")
@app_commands.describe(confirm="Set true to confirm shutdown")
async def shutdown_command(interaction: discord.Interaction, confirm: bool) -> None:
    if not await owner_only(interaction):
        return
    if not confirm:
        await interaction.response.send_message("Shutdown cancelled. Run `/shutdown confirm:true` to confirm.", ephemeral=True)
        return
    await interaction.response.send_message(
        embed=premium_embed("Shutting down", "The bot is closing gracefully. Your hosting platform may restart it."),
        ephemeral=True,
    )
    await bot.close()


@bot.tree.command(name="ownerpanel", description="Owner: view available management controls")
async def ownerpanel_command(interaction: discord.Interaction) -> None:
    if not await owner_only(interaction):
        return
    embed = premium_embed("Owner controls", "Private management commands")
    embed.add_field(name="Operations", value="`/sync` · `/stats` · `/clearcache` · `/reload`", inline=False)
    embed.add_field(name="Runtime", value="`/setcooldown` · `/maintenance` · `/shutdown`", inline=False)
    embed.add_field(name="Community", value="`/announce`", inline=False)
    embed.add_field(name="Security", value="Owner commands require an ID listed in `OWNER_IDS`.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Show the bypass provider chain")
async def status_command(interaction: discord.Interaction) -> None:
    embed = premium_embed("Resolver status", "Providers are attempted in order until a valid destination is found.")
    embed.add_field(name="01 · Primary", value="BypassX API", inline=True)
    embed.add_field(name="02 · Fallback", value="UseBypass", inline=True)
    embed.add_field(name="03 · Fallback", value="TRW", inline=True)
    embed.add_field(name="04 · Fallback", value="OmegaTech", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Optional HTTP health endpoint for Render
# ---------------------------------------------------------------------------
web_app = FastAPI(title="BypassXBot Health")


@web_app.get("/")
async def root() -> dict[str, Any]:
    return {"ok": True, "service": "BypassXBot", "discord_ready": bot.is_ready()}


@web_app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "BypassXBot", "discord_ready": bot.is_ready()}


async def run_health_server() -> None:
    config = uvicorn.Config(web_app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Set DISCORD_TOKEN (or TOKEN) in the environment.")
    bot.run(TOKEN, log_handler=None)
