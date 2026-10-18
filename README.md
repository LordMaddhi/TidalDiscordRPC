# TidalDiscordRPC

A lightweight Windows tray app that shows what you're playing in **TIDAL** as a Discord **Rich Presence** ("Listening to …") with the correct album cover.

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Lightweight:** runs silently in the tray, no window, a few dozen MB of RAM
- **No TIDAL login or API key:** track info comes straight from the Windows media session
- **Correct covers:** album art from the iTunes Search API, matched by artist and album name instead of "first search result"
- **Progress bar:** start and end time are shown in Discord, and the presence clears on pause
- **Tray menu:** current track, presence on/off, *Start with Windows*, open config, open debug log
- **Single instance, rate-limit friendly, local cache** for covers

## How it works

```
Windows media session (GSMTC)  ->  track, artist, album, position
            |
            v
iTunes Search API  ->  cover URL (artist ID lookup + fuzzy album matching)
            |
            v
Discord Rich Presence (local IPC via pypresence)
```

### Cover matching

Wrong covers are worse than no cover, so a cover is only used when it passes an artist and album check. Per iTunes store (default `de`, then `us`), the app tries:

1. **Artist lookup:** find the artist ID, fetch all albums of that artist, match the album name (normalized: accents, brackets like *(Deluxe)* and *- Single* are ignored; similarity score >= 0.85)
2. **Album search:** search by album name only, then filter by artist
3. **Song search:** verify title, artist and album of the result

If nothing passes, a fallback image is shown. Found covers are cached in `%APPDATA%\TidalDiscordRPC\covers.json`; misses and network errors are never written to disk.

## Setup

### 1. Create a Discord application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create an application. Its **name is shown after "Listening to"** (e.g. `TIDAL`).
2. Copy the **Application ID**.
3. Under *Rich Presence -> Art Assets*, upload a fallback image named `tidal` (shown when no cover is found).

### 2. Install

**Option A: build the exe** (needs Python 3.10+)

```
build.bat
```

Creates `.venv`, installs the dependencies and builds `dist\TidalDiscordRPC.exe`. Close the running app first, otherwise Windows blocks overwriting the exe.

**Option B: run from source**

```
pip install -r requirements.txt
run.bat
```

### 3. Configure

Start the app once, then in the tray menu choose **Open config**, set your Application ID and restart the app. Alternatively, edit `%APPDATA%\TidalDiscordRPC\config.json` directly.

```json
{
  "discord_client_id": "YOUR_APPLICATION_ID",
  "fallback_image": "tidal",
  "countries": ["de", "us"],
  "match_threshold": 0.85,
  "poll_seconds": 2
}
```

| Key | Description |
|---|---|
| `discord_client_id` | Application ID from the Developer Portal |
| `fallback_image` | Name of the art asset used when no cover matches |
| `countries` | iTunes stores, tried in this order (e.g. `["us", "de"]`) |
| `match_threshold` | Minimum album similarity, `0..1` |
| `poll_seconds` | Media session polling interval |

## Tray menu

| Item | What it does |
|---|---|
| Track / artist lines | Current song (play/pause symbol) |
| Presence enabled | Turn the Discord presence on or off |
| Start with Windows | Adds or removes a `HKCU\...\Run` registry entry (no admin rights needed) |
| Open config | Opens `config.json` (restart the app to apply changes) |
| Open debug log | Opens `debug.log` |
| Quit | Exits the app |

If you move the exe, toggle *Start with Windows* off and on again to update the stored path.

## Files and locations

| Path | Purpose |
|---|---|
| `%APPDATA%\TidalDiscordRPC\config.json` | Settings |
| `%APPDATA%\TidalDiscordRPC\covers.json` | Cover cache |
| `%APPDATA%\TidalDiscordRPC\debug.log` | Log (rotates at 512 KB, keeps `debug.old.log`) |

## Troubleshooting

Open the tray menu -> **Open debug log**. It lists the media sessions Windows sees, each track, every cover lookup with candidate scores, and Discord connection errors.

| Problem | Likely cause |
|---|---|
| Nothing in Discord | Wrong or missing Application ID, Discord not running (the browser version does not work), activity status disabled, or Discord set to *Invisible* |
| Wrong or missing cover | Check the lookup lines in the log; try lowering `match_threshold` or changing `countries` |
| Tray shows "Nothing playing" | TIDAL desktop app is not running or Windows does not see its media session |
| Build fails with "Access denied" | The app is still running, quit it first |
| exe icon looks old | Windows icon cache, rename the exe once or restart Explorer |

## Limitations

- Windows 10/11 only (uses the Windows media session API)
- Works with the TIDAL desktop app (a session whose ID contains "tidal")
- iTunes allows roughly 20 requests per minute, so the app throttles itself; the first track of an unknown artist can take a few seconds to get its cover

## Project structure

```
tidal_discord_rpc.py   main app
requirements.txt       dependencies
build.bat              builds dist\TidalDiscordRPC.exe
run.bat                starts the app from source without a console
icon.ico / icon.png    app icon
```

## Disclaimer

Not affiliated with or endorsed by TIDAL, Discord or Apple. Cover art is fetched from the public iTunes Search API and belongs to its respective owners.

## License

[MIT](LICENSE)