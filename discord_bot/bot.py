from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import discord
import httpx
import uvicorn
from discord import app_commands
from discord.ext import commands
from fastapi import FastAPI

LOG = logging.getLogger("bypassx.discord")
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
MAX_AUTO_LINKS = 3
MAX_MESSAGE_LENGTH = 1900


def env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


API_URL = os.getenv("BYPASS_API_URL", "https://bypassx-bpzt.onrender.com").rstrip("/")
API_TIMEOUT = max(5.0, float(os.getenv("BYPASS_API_TIMEOUT", "75")))
PREFIX = os.getenv("DISCORD_PREFIX", "+")
CONFIG_PATH = Path(os.getenv("AUTO_BYPASS_CONFIG", "data/auto_channels.json"))
PORT = env_int("PORT", 10000)


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


def premium_embed(title: str, description: str = "", color: discord.Color = BRAND_COLOR) -> discord.Embed:
    embed = discord.Embed(title=f"✦ {title}", description=description, color=color)
    embed.set_footer(text="BypassX • Fast. Clean. Reliable.")
    return embed


class AutoChannelStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._channels: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            self._channels = {str(key): int(value) for key, value in data.items()}
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            self._channels = {}

    async def reload(self) -> None:
        async with self._lock:
            self._load()

    async def get(self, guild_id: int) -> int | None:
        async with self._lock:
            return self._channels.get(str(guild_id))

    async def set(self, guild_id: int, channel_id: int) -> None:
        async with self._lock:
            self._channels[str(guild_id)] = channel_id
            await self._save()

    async def remove(self, guild_id: int) -> None:
        async with self._lock:
            self._channels.pop(str(guild_id), None)
            await self._save()

    async def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="auto-channels-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._channels, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


class BypassXBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=PREFIX, intents=intents, help_command=None)
        self.store = AutoChannelStore(CONFIG_PATH)
        self.api_client = httpx.AsyncClient(timeout=httpx.Timeout(API_TIMEOUT, connect=15.0), follow_redirects=False)
        self._guild_locks: dict[int, asyncio.Lock] = {}

    async def setup_hook(self) -> None:
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            LOG.info("Synced slash commands to guild %s", guild_id)
        else:
            await self.tree.sync()
            LOG.info("Synced global slash commands")

    async def close(self) -> None:
        await self.api_client.aclose()
        await super().close()

    def guild_lock(self, guild_id: int) -> asyncio.Lock:
        return self._guild_locks.setdefault(guild_id, asyncio.Lock())

    async def resolve(self, url: str) -> dict[str, Any]:
        try:
            response = await self.api_client.post(f"{API_URL}/bypass", json={"url": url})
            data = response.json()
            if response.is_success and data.get("success") and data.get("destination"):
                return data
            return {"success": False, "error": data.get("error", "Unable to resolve this URL")}
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            LOG.warning("Bypass API unavailable: %s", type(exc).__name__)
            return {"success": False, "error": "Bypass API timed out or is unavailable"}
        except (httpx.HTTPError, ValueError):
            LOG.exception("Invalid Bypass API response")
            return {"success": False, "error": "Bypass API returned an invalid response"}

    async def send_result(self, destination: discord.abc.Messageable, user: discord.abc.User, url: str) -> None:
        result = await self.resolve(url)
        mention = user.mention
        allowed = discord.AllowedMentions(users=True, everyone=False, roles=False, replied_user=False)
        if result.get("success"):
            target = str(result["destination"])
            embed = premium_embed("Link resolved", "Your destination is ready.", SUCCESS_COLOR)
            embed.add_field(name="Original", value=url[:1024], inline=False)
            embed.add_field(name="Destination", value=target[:1024], inline=False)
            embed.set_footer(text=f"Service: {result.get('service', 'unknown')} • Method: {result.get('method', 'unknown')}")
            await destination.send(f"{mention}", embed=embed, allowed_mentions=allowed)
        else:
            embed = premium_embed("Unable to resolve", result.get("error", "Unknown resolver error"), ERROR_COLOR)
            embed.add_field(name="Original", value=url[:1024], inline=False)
            await destination.send(f"{mention}", embed=embed, allowed_mentions=allowed)

    async def on_ready(self) -> None:
        if self.user:
            LOG.info("Logged in as %s (%s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message) -> None:
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
            for link in links:
                await self.send_result(message.channel, message.author, link.rstrip(".,!?)]}"))


bot = BypassXBot()
web_app = FastAPI(title="BypassXBot", version="1.0.0")


@web_app.get("/")
async def web_root() -> dict[str, Any]:
    return {"name": "BypassXBot", "status": "online", "discord_ready": bot.is_ready()}


@web_app.get("/health")
async def web_health() -> dict[str, Any]:
    return {"status": "ok", "discord_ready": bot.is_ready()}


async def require_manage_guild(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(embed=premium_embed("Permission required", "You need the **Manage Server** permission for this command.", ERROR_COLOR), ephemeral=True)
        return False
    return True


async def require_owner(interaction: discord.Interaction) -> bool:
    if interaction.user.id not in OWNER_IDS:
        await interaction.response.send_message(embed=premium_embed("Owner only", "This command is restricted to the configured bot owner.", ERROR_COLOR), ephemeral=True)
        return False
    return True


async def require_dev_owner(interaction: discord.Interaction) -> bool:
    if not await require_owner(interaction):
        return False
    if not DEV_GUILD_ID or not interaction.guild or interaction.guild.id != DEV_GUILD_ID:
        await interaction.response.send_message(embed=premium_embed("Developer guild only", "This maintenance command is available only in the configured developer server.", ERROR_COLOR), ephemeral=True)
        return False
    return True


async def prefix_owner_check(ctx: commands.Context) -> bool:
    if ctx.author.id not in OWNER_IDS:
        raise commands.CheckFailure("owner_only")
    return True


async def prefix_dev_check(ctx: commands.Context) -> bool:
    if ctx.author.id not in OWNER_IDS or not DEV_GUILD_ID or not ctx.guild or ctx.guild.id != DEV_GUILD_ID:
        raise commands.CheckFailure("developer_guild_only")
    return True


def help_embed() -> discord.Embed:
    embed = premium_embed("Command center", "Resolve links, automate channels, and manage your server with BypassX.")
    embed.add_field(
        name="◆ Resolver",
        value=f"`{PREFIX}bypass <url>`\n`/bypass` — resolve a shortlink instantly",
        inline=False,
    )
    embed.add_field(
        name="◆ Automation",
        value=f"`{PREFIX}autobypass on [#channel]`\n`{PREFIX}autobypass off`\n`/autobypass` — configure automatic link detection",
        inline=False,
    )
    embed.add_field(
        name="◆ Server tools",
        value=f"`{PREFIX}status` • `{PREFIX}ping` • `{PREFIX}help`",
        inline=False,
    )
    embed.set_thumbnail(url="https://cdn.simpleicons.org/discord/5865F2")
    return embed


@bot.command(name="bypass")
async def bypass_command(ctx: commands.Context, url: str | None = None) -> None:
    if not url:
        await ctx.reply(f"Usage: `{PREFIX}bypass <url>`")
        return
    await ctx.typing()
    await bot.send_result(ctx.channel, ctx.author, url)


@bot.command(name="setautochannel")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def set_auto_channel_command(ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
    target = channel or ctx.channel
    await bot.store.set(ctx.guild.id, target.id)
    await ctx.reply(f"Auto-bypass is enabled in {target.mention}.")


@bot.command(name="disableautobypass")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def disable_auto_channel_command(ctx: commands.Context) -> None:
    await bot.store.remove(ctx.guild.id)
    await ctx.reply("Auto-bypass is disabled for this server.")


@bot.command(name="autobypass")
async def auto_bypass_command(ctx: commands.Context, action: str | None = None, channel: discord.TextChannel | None = None) -> None:
    if not ctx.guild or not isinstance(ctx.author, discord.Member) or not ctx.author.guild_permissions.manage_guild:
        await ctx.reply("You need the Manage Server permission for this command.")
        return
    if action and action.lower() in {"off", "disable", "disabled"}:
        await bot.store.remove(ctx.guild.id)
        await ctx.reply("Auto-bypass is disabled for this server.")
        return
    target = channel or ctx.channel
    await bot.store.set(ctx.guild.id, target.id)
    await ctx.reply(f"Auto-bypass is enabled in {target.mention}.")


@bot.command(name="status")
async def status_command(ctx: commands.Context) -> None:
    channel_id = await bot.store.get(ctx.guild.id) if ctx.guild else None
    channel_text = f"<#{channel_id}>" if channel_id else "disabled"
    embed = premium_embed("Server status", "Your BypassX configuration at a glance.")
    embed.add_field(name="API", value="Online endpoint", inline=True)
    embed.add_field(name="Auto-bypass", value=channel_text, inline=True)
    embed.add_field(name="Prefix", value=f"`{PREFIX}`", inline=True)
    await ctx.reply(embed=embed)


@bot.command(name="ping")
async def ping_command(ctx: commands.Context) -> None:
    await ctx.reply(embed=premium_embed("Pong", f"Gateway latency: **{round(bot.latency * 1000)}ms**"))


@bot.command(name="help")
async def help_command(ctx: commands.Context) -> None:
    await ctx.reply(embed=help_embed())


@bot.command(name="ownerstatus")
@commands.check(prefix_owner_check)
async def owner_status_command(ctx: commands.Context) -> None:
    embed = premium_embed("Owner console", "Private runtime information.")
    embed.add_field(name="Discord user", value=f"`{ctx.author.id}`", inline=True)
    embed.add_field(name="Servers", value=f"`{len(bot.guilds)}`", inline=True)
    embed.add_field(name="Developer guild", value=f"`{DEV_GUILD_ID or 'not configured'}`", inline=False)
    await ctx.reply(embed=embed)


@bot.command(name="reload")
@commands.check(prefix_dev_check)
async def reload_command(ctx: commands.Context) -> None:
    await bot.store.reload()
    await ctx.reply(embed=premium_embed("Configuration reloaded", "Auto-bypass settings were reloaded from disk.", SUCCESS_COLOR))


@bot.command(name="sync")
@commands.check(prefix_dev_check)
async def sync_command(ctx: commands.Context) -> None:
    synced = await bot.tree.sync()
    await ctx.reply(embed=premium_embed("Commands synced", f"Synchronized **{len(synced)}** slash commands globally.", SUCCESS_COLOR))


@bot.command(name="debug")
@commands.check(prefix_dev_check)
async def debug_command(ctx: commands.Context) -> None:
    embed = premium_embed("Developer diagnostics", "Runtime details for the configured developer guild.")
    embed.add_field(name="Ready", value=str(bot.is_ready()), inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="API", value=API_URL, inline=False)
    await ctx.reply(embed=embed)


@bot.tree.command(name="bypass", description="Resolve a shortlink with BypassX")
@app_commands.describe(url="The HTTP(S) shortlink to resolve")
async def bypass_slash(interaction: discord.Interaction, url: str) -> None:
    await interaction.response.defer()
    result = await bot.resolve(url)
    if result.get("success"):
        embed = premium_embed("Link resolved", "Your destination is ready.", SUCCESS_COLOR)
        embed.add_field(name="Destination", value=str(result["destination"])[:1024], inline=False)
        embed.set_footer(text=f"Service: {result.get('service', 'unknown')} • Method: {result.get('method', 'unknown')}")
        await interaction.followup.send(content=interaction.user.mention, embed=embed, allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False))
    else:
        await interaction.followup.send(content=interaction.user.mention, embed=premium_embed("Unable to resolve", result.get("error", "Unable to resolve this URL"), ERROR_COLOR), allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False))


@bot.tree.command(name="autobypass", description="Enable or disable automatic bypass in a channel")
@app_commands.describe(enabled="Whether auto-bypass should be enabled", channel="The channel to monitor")
async def autobypass_slash(interaction: discord.Interaction, enabled: bool, channel: discord.TextChannel | None = None) -> None:
    if not await require_manage_guild(interaction):
        return
    target = channel or interaction.channel
    if enabled:
        await bot.store.set(interaction.guild.id, target.id)
        await interaction.response.send_message(f"Auto-bypass enabled in {target.mention}.")
    else:
        await bot.store.remove(interaction.guild.id)
        await interaction.response.send_message("Auto-bypass disabled for this server.")


@bot.tree.command(name="status", description="Show BypassX server settings")
async def status_slash(interaction: discord.Interaction) -> None:
    channel_id = await bot.store.get(interaction.guild.id) if interaction.guild else None
    embed = premium_embed("Server status", "Your BypassX configuration at a glance.")
    embed.add_field(name="API", value="Online endpoint", inline=True)
    embed.add_field(name="Auto-bypass", value=f"<#{channel_id}>" if channel_id else "disabled", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ping", description="Check BypassX latency")
async def ping_slash(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(embed=premium_embed("Pong", f"Gateway latency: **{round(bot.latency * 1000)}ms**"))


@bot.tree.command(name="help", description="Open the BypassX command center")
async def help_slash(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(embed=help_embed(), ephemeral=True)


@bot.tree.command(name="ownerstatus", description="Show private bot owner diagnostics")
async def owner_status_slash(interaction: discord.Interaction) -> None:
    if not await require_owner(interaction):
        return
    embed = premium_embed("Owner console", "Private runtime information.")
    embed.add_field(name="Servers", value=f"`{len(bot.guilds)}`", inline=True)
    embed.add_field(name="Ready", value=str(bot.is_ready()), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="reload", description="Reload bot configuration (developer guild only)")
async def reload_slash(interaction: discord.Interaction) -> None:
    if not await require_dev_owner(interaction):
        return
    await bot.store.reload()
    await interaction.response.send_message(embed=premium_embed("Configuration reloaded", "Auto-bypass settings were reloaded from disk.", SUCCESS_COLOR), ephemeral=True)


@bot.tree.command(name="sync", description="Sync slash commands (developer guild only)")
async def sync_slash(interaction: discord.Interaction) -> None:
    if not await require_dev_owner(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    synced = await bot.tree.sync()
    await interaction.followup.send(embed=premium_embed("Commands synced", f"Synchronized **{len(synced)}** slash commands globally.", SUCCESS_COLOR), ephemeral=True)


@bot.tree.command(name="debug", description="Show private diagnostics (developer guild only)")
async def debug_slash(interaction: discord.Interaction) -> None:
    if not await require_dev_owner(interaction):
        return
    embed = premium_embed("Developer diagnostics", "Runtime details for the configured developer guild.")
    embed.add_field(name="Ready", value=str(bot.is_ready()), inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="API", value=API_URL, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        message = "This command is restricted to the bot owner."
        if str(error) == "developer_guild_only":
            message = "This maintenance command is restricted to the bot owner in the developer guild."
        await ctx.reply(embed=premium_embed("Access denied", message, ERROR_COLOR))
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply(embed=premium_embed("Permission required", "You need the **Manage Server** permission for that command.", ERROR_COLOR))
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(embed=premium_embed("Missing argument", f"Use `{PREFIX}help` for command usage.", ERROR_COLOR))
        return
    LOG.warning("Command error: %s", type(error).__name__)
    await ctx.reply(embed=premium_embed("Command error", "That command could not be completed.", ERROR_COLOR))


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is required")

    async def main() -> None:
        server = uvicorn.Server(
            uvicorn.Config(web_app, host="0.0.0.0", port=PORT, log_level="info", log_config=None)
        )
        bot_task = asyncio.create_task(bot.start(token, reconnect=True), name="discord-gateway")
        web_task = asyncio.create_task(server.serve(), name="health-server")
        done, pending = await asyncio.wait(
            {bot_task, web_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if not task.cancelled() and task.exception():
                raise task.exception()
        await bot.close()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
