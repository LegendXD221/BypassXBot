# BypassXBot

A Discord bot that uses the public **BypassX API** to resolve supported shortlinks.

## Features

- Prefix commands using `+`.
- Slash commands using Discord's `/` command menu.
- `+bypass <url>` and `/bypass` for manual resolution.
- Server-configurable auto-bypass channel.
- Automatically detects up to three HTTP(S) links in configured channels.
- Pings the user who posted the link.
- `+status`, `+ping`, and `+help` utilities.
- Admin-only auto-bypass configuration.
- Safe API timeout handling and no token/API secrets in source code.

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

Auto-bypass configuration is stored in `data/auto_channels.json`. On Render, the default filesystem is ephemeral; attach persistent storage or re-run the setup command after a worker restart if you need settings to survive restarts.

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
export BYPASS_API_URL='https://bypassx-bpzt.onrender.com'
python -m discord_bot.bot
```

## Render Web Service

The included `render.yaml` defines a Web Service. The bot runs its Discord gateway and a small health server in the same process:

```text
Build Command: pip install -r requirements-bot.txt
Start Command: python -m discord_bot.bot
Health Check: /health
```

Render provides free Web Services with usage limitations. Free services may sleep when inactive, which can disconnect the Discord gateway. If the bot must remain online continuously, use an always-on VM or a paid worker.

Set `DISCORD_TOKEN` in Render's environment settings. The bot uses the existing public API by default:

```text
https://bypassx-bpzt.onrender.com
```
