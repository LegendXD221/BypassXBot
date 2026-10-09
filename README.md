# BypassXBot

A Discord bot that uses the public **BypassX API** to resolve supported shortlinks.

## Features

- Prefix commands using `+`.
- Slash commands using Discord's `/` command menu.
- `+bypass <url>` and `/bypass` for manual resolution.
- Server-configurable auto-bypass channel.
- Automatically detects one HTTP(S) link per message in configured channels.
- Pings the user who posted the link.
- `+status`, `+ping`, and `+help` utilities.
- Premium branded embeds and command center.
- Admin-only auto-bypass configuration.
- Owner-only console commands and developer-guild-only maintenance commands.
- Safe API timeout handling and no token/API secrets in source code.
- Anti-abuse protection with per-user cooldowns, per-server cooldowns, and a global concurrency cap.
- Sequential resolver fallback: BypassX API, usebypas, then FastForward crowd query.
- SQLite-backed auto-bypass settings that survive process restarts when persistent storage is attached.
- Successful-resolution cache to reduce duplicate upstream requests.

## Commands

| Prefix | Slash | Purpose |
|---|---|---|
| `+bypass <url>` | `/bypass url:<url>` | Resolve one link |
| `+autobypass on [#channel]` | `/autobypass enabled:true channel:#channel` | Enable auto-bypass |
| `+autobypass off` | `/autobypass enabled:false` | Disable auto-bypass |
| `+setautochannel [#channel]` | — | Set the monitored channel |
| `+disableautobypass` | — | Disable auto-bypass |
| `+status` | `/status` | Show bot/API configuration |
| `+ping` | `/ping` | Show bot latency |
| `+help` | — | Show command help |

### Owner and developer commands

Set `BOT_OWNER_IDS` to one or more comma-separated Discord user IDs. These commands are denied to everyone else:

| Prefix | Slash | Access |
|---|---|---|
| `+ownerstatus` | `/ownerstatus` | Bot owner only |
| `+reload` | `/reload` | Bot owner + developer guild |
| `+sync` | `/sync` | Bot owner + developer guild |
| `+debug` | `/debug` | Bot owner + developer guild |
| `+cacheclear` | `/cacheclear` | Bot owner + developer guild |

`DISCORD_GUILD_ID` is used as the developer guild for fast slash-command sync and maintenance-command access.

Auto-bypass configuration and the resolution cache are stored in `BOT_DATABASE_PATH` (default `data/bypassx.sqlite3`). The old `data/auto_channels.json` file is migrated automatically once. On Render, attach persistent storage if settings and cache entries must survive service restarts.

## Rate limits and cooldowns

Every manual or automatic bypass request is protected by:

- `BYPASS_USER_COOLDOWN` — per-user cooldown in seconds; default `5`.
- `BYPASS_GUILD_COOLDOWN` — per-server cooldown in seconds; default `2`.
- `BYPASS_MAX_CONCURRENT` — maximum simultaneous API requests; default `3`.

Automatic bypass processes one link per message. Cooldown responses do not call the API.

## Discord application setup

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable the **Message Content Intent** under Bot settings.
3. Invite the bot with the `bot` and `applications.commands` scopes.
4. Grant only the permissions it needs: View Channels, Send Messages, Embed Links, Read Message History, and Use Application Commands.
5. Set `DISCORD_TOKEN` as a secret environment variable. Never commit it.

For faster slash-command registration during development, set `DISCORD_GUILD_ID` to one server ID. Omit it for global command sync.

## Run locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-bot.txt
export DISCORD_TOKEN='your-token'
export BOT_OWNER_IDS='your-discord-user-id'
export BYPASS_API_URL='https://bypassx-bpzt.onrender.com'
export BYPASS_FALLBACK_API_URL='https://usebypas.com/api/v1/bypass'
export BYPASS_CROWD_API_URL='https://crowd.fastforward.team/crowd/query_v1'
export BYPASS_USER_COOLDOWN='5'
export BYPASS_GUILD_COOLDOWN='2'
export BYPASS_MAX_CONCURRENT='3'
export BOT_DATABASE_PATH='data/bypassx.sqlite3'
export BYPASS_CACHE_TTL='300'
python -m discord_bot.bot
```

### Resolver fallback order

When a bypass request does not produce a valid destination, the bot tries providers in this order:

1. `BYPASS_API_URL` — the primary BypassX API.
2. `BYPASS_FALLBACK_API_URL` — `usebypas.com/api/v1/bypass?url=...`.
3. `BYPASS_CROWD_API_URL` — FastForward’s documented form-encoded crowd query using the link hostname and path.

Every provider response is validated as an HTTP(S) destination. Provider failures are logged by name without logging tokens or sensitive request data. The fallback chain remains subject to the bot’s cooldowns, concurrency cap, and API timeouts.

The usebypas provider has a separate `BYPASS_FALLBACK_TIMEOUT` setting, defaulting to `120` seconds to support resolutions that take from one second up to two minutes. The primary BypassX API keeps its shorter timeout.

## Render Web Service

The included `render.yaml` defines a Web Service. The bot runs its Discord gateway and a small health server in the same process:

```text
Build Command: pip install -r requirements-bot.txt
Start Command: python -m discord_bot.bot
Health Check: /health
```

Render provides free Web Services with usage limitations. Free services may sleep when inactive, which can disconnect the Discord gateway. If the bot must remain online continuously, use an always-on VM or a paid worker.

For external monitoring, see [`UPTIMEROBOT.md`](UPTIMEROBOT.md). Configure UptimeRobot to check the public `/health` endpoint every five minutes. This can keep the web service receiving traffic and alert you if it stops responding, but it cannot guarantee Discord gateway uptime on Render's free tier.

Set `DISCORD_TOKEN` in Render's environment settings. The bot uses the existing public API by default:

```text
https://bypassx-bpzt.onrender.com
```
