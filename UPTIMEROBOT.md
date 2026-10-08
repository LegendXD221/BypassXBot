# UptimeRobot setup

UptimeRobot is an external monitor. It is not part of the Discord bot process.

## Create the monitor

1. Open [UptimeRobot](https://uptimerobot.com/) and create or sign in to an account.
2. Choose **Add New Monitor**.
3. Use these settings:

```text
Monitor Type: HTTP(s)
Friendly Name: BypassXBot Health
URL: https://YOUR-RENDER-SERVICE.onrender.com/health
Monitoring Interval: 5 minutes
```

Replace `YOUR-RENDER-SERVICE` with the actual public URL shown by Render. If the service name is available, it may be:

```text
https://bypassx-discord-bot.onrender.com/health
```

4. Select your email alert contact.
5. Save the monitor.

The endpoint should return HTTP 200 with a response like:

```json
{"status":"ok","discord_ready":true}
```

The `discord_ready` field may be `false` for a short time while the Discord gateway is connecting; the HTTP health endpoint still confirms that the Render process is responding.

## Important limitation

UptimeRobot can keep a free Render Web Service receiving traffic and alert you when it stops responding, but it cannot guarantee Discord gateway uptime. Render free services may still restart, sleep, or be limited. UptimeRobot checks every 5 minutes on its free plan, so alerts are not instant.

For stronger 24/7 Discord uptime, use an always-on VM or a paid worker. Never put your Discord token in UptimeRobot; only monitor the public `/health` URL.
