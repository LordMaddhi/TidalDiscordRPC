"""TidalDiscordRPC - lightweight Discord Rich Presence for TIDAL (Windows).

Reads the currently playing track from the Windows media session (GSMTC),
looks up matching cover art via the iTunes Search API and shows it in Discord.
"""
import asyncio
import ctypes
import json
import os
import re
import sys
import threading
import time
import traceback
import unicodedata
import urllib.parse
import urllib.request
import winreg
from difflib import SequenceMatcher
from pathlib import Path

import pystray
from PIL import Image, ImageDraw
from pypresence import ActivityType, Presence
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlayStatus,
)

APP_NAME = "TidalDiscordRPC"
PLACEHOLDER_ID = "1561271278466244688"

DATA_DIR = Path(os.environ.get("APPDATA", ".")) / APP_NAME
CACHE_FILE = DATA_DIR / "covers.json"
CONFIG_FILE = DATA_DIR / "config.json"
LOG_FILE = DATA_DIR / "debug.log"
LOG_MAX_BYTES = 512 * 1024  # rotate the log at 512 KB

DEFAULT_CONFIG = {
    "discord_client_id": PLACEHOLDER_ID,  # Discord Developer Portal -> Application ID
    "fallback_image": "tidal",            # Name of an uploaded Rich Presence art asset
    "countries": ["de", "us"],            # iTunes stores, tried in this order
    "match_threshold": 0.85,              # Minimum album similarity (0..1)
    "poll_seconds": 2,                    # Media session polling interval
}


CONFIG_ERROR = ""  # set if config.json could not be parsed (logged at startup)


def load_config() -> dict:
    """Load config.json from %APPDATA%; create it with defaults on first run."""
    global CONFIG_ERROR
    cfg = dict(DEFAULT_CONFIG)
    try:
        # utf-8-sig also accepts files saved "with BOM" by some editors
        cfg.update(json.loads(CONFIG_FILE.read_text("utf-8-sig")))
    except FileNotFoundError:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2), "utf-8")
        except OSError:
            pass
    except Exception as e:
        CONFIG_ERROR = repr(e)  # broken file: fall back to defaults
    return cfg


CFG = load_config()
_c = CFG.get("countries") or ["de", "us"]
COUNTRIES = [_c] if isinstance(_c, str) else [str(x).lower() for x in _c]


# ---------------------------------------------------------------- Autostart
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _autostart_cmd() -> str:
    """Command line written to the registry Run key."""
    if getattr(sys, "frozen", False):  # PyInstaller exe
        return f'"{sys.executable}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")  # no console window
    return f'"{pyw}" "{Path(__file__).resolve()}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _autostart_cmd())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


# ------------------------------------------------------------ Cover lookup
# Strips bracketed suffixes and trailing "- Single", "- Remastered ...", etc.
_NOISE = re.compile(r"[\(\[][^)\]]*[\)\]]|\s-\s(single|ep|remaster\w*|deluxe.*)$", re.I)


def norm(s: str) -> str:
    """Lowercase, strip accents, brackets and special characters."""
    s = _NOISE.sub(" ", s or "")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def artist_ok(wanted: str, found: str) -> bool:
    """Artist must match (exact or contained), otherwise the result is rejected."""
    w, f = norm(wanted), norm(found)
    if not w or not f:
        return False
    if w in f or f in w:
        return True
    # TIDAL often delivers "A, B" or "A & B": also check the first artist only
    first = norm(re.split(r",|&| feat\.? | x ", wanted, flags=re.I)[0])
    return bool(first) and (first in f or f in first)


def cache_key(artist: str, album: str, title: str) -> str:
    return f"{norm(artist)}|{norm(album) or norm(title)}"


class CoverResolver:
    def __init__(self):
        self.cache: dict = {}  # key -> url (misses only live in memory)
        self.last_request = 0.0
        self.failed = False    # True if a network request failed during resolve()
        self.artist_ids: dict = {}     # (country, artist) -> [(id, name)], memory only
        self.artist_albums: dict = {}  # (country, id) -> album list, memory only
        try:
            loaded = json.loads(CACHE_FILE.read_text("utf-8"))
            self.cache = {k: v for k, v in loaded.items() if v}  # drop old misses
        except Exception:
            pass

    def _save(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            hits = {k: v for k, v in self.cache.items() if v}
            CACHE_FILE.write_text(json.dumps(hits), "utf-8")
        except OSError:
            pass

    def _get(self, endpoint: str, params: dict, label: str) -> list:
        """One iTunes API request (rate limited). Returns [] and sets self.failed on errors."""
        wait = 3.0 - (time.time() - self.last_request)  # iTunes ~20 req/min
        if wait > 0:
            time.sleep(wait)
        self.last_request = time.time()
        country = params.get("country")
        try:
            url = f"https://itunes.apple.com/{endpoint}?{urllib.parse.urlencode(params)}"
            with urllib.request.urlopen(url, timeout=8) as r:
                results = json.load(r).get("results", [])
            log(f"iTunes {endpoint} [{country}] {label}: {len(results)} results")
            return results
        except Exception as e:
            log(f"iTunes {endpoint} failed [{country}] {label}: {e!r}")
            self.failed = True  # so a miss caused by an error is not cached
            return []

    def _search_safe(self, term: str, entity: str, attribute=None, limit: int = 50,
                     country: str = "de") -> list:
        params = {"term": term, "entity": entity, "limit": limit, "country": country}
        if attribute:
            params["attribute"] = attribute  # e.g. albumTerm: match album names only
        return self._get("search", params, f"({entity}) {term!r}")

    def _artist_albums(self, artist_id, country: str) -> list:
        """All albums of one artist (cached in memory per artist)."""
        key = (country, artist_id)
        if key not in self.artist_albums:
            res = self._get("lookup", {"id": artist_id, "entity": "album",
                                       "limit": 200, "country": country},
                            f"albums of artist {artist_id}")
            if not res and self.failed:
                return []  # do not cache failures
            self.artist_albums[key] = [r for r in res if r.get("wrapperType") == "collection"]
        return self.artist_albums[key]

    @staticmethod
    def _hires(url: str) -> str:
        return re.sub(r"/\d+x\d+bb", "/600x600bb", url)

    def _by_artist(self, artist, album, country, threshold):
        """Find the artist ID first, then match the album among that artist's albums."""
        a_norm = norm(album)
        if not a_norm:
            return None
        # TIDAL often delivers "A, B" or "A & B": use the first artist
        primary = re.split(r",|&| feat\.? | x ", artist, flags=re.I)[0].strip()
        p_norm = norm(primary)

        ck = (country, p_norm)
        if ck not in self.artist_ids:
            found = []
            for r in self._search_safe(primary, "musicArtist", None, 10, country):
                if r.get("artistId") and sim(p_norm, norm(r.get("artistName", ""))) >= 0.9:
                    found.append((r["artistId"], r.get("artistName", "")))
            if found or not self.failed:
                self.artist_ids[ck] = found[:3]
            else:
                return None
        best, best_score = None, 0.0
        for artist_id, artist_name in self.artist_ids.get(ck, []):
            log(f"  artist candidate: {artist_name!r} id={artist_id}")
            for r in self._artist_albums(artist_id, country):
                found_album = r.get("collectionName", "")
                score = sim(a_norm, norm(found_album))
                if found_album.lower() == album.lower():
                    score += 0.05  # bonus for an exact name match
                if score >= 0.6:
                    log(f"  artist album: {found_album!r} score={score:.2f}")
                if score > best_score:
                    best, best_score = r, score
            if best and best_score >= threshold:
                break
        if best and best_score >= threshold:
            log(f"  -> artist match accepted: {best.get('collectionName')!r} (score={best_score:.2f})")
            return best.get("artworkUrl100")
        log(f"  -> no artist-album match above threshold (best={best_score:.2f})")
        return None

    def _by_album(self, artist, album, country, threshold):
        """Album search: returns a cover URL or None."""
        a_norm = norm(album)
        if not a_norm:
            return None
        best, best_score = None, 0.0
        # Query 1 matches album names only (the artist name would push the right
        # album out of the top results); query 2 is a combined search.
        for term, attr in ((album, "albumTerm"), (f"{artist} {album}", None)):
            for r in self._search_safe(term, "album", attr, 50, country):
                found_artist = r.get("artistName", "")
                found_album = r.get("collectionName", "")
                if not artist_ok(artist, found_artist):
                    continue
                score = sim(a_norm, norm(found_album))
                if found_album.lower() == album.lower():
                    score += 0.05  # bonus for an exact name match
                if score >= 0.6:
                    log(f"  album candidate: {found_artist!r} - {found_album!r} score={score:.2f}")
                if score > best_score:
                    best, best_score = r, score
            if best and best_score >= threshold:
                break  # good match found, skip the next query
        if best and best_score >= threshold:
            log(f"  -> album match accepted: {best.get('collectionName')!r} (score={best_score:.2f})")
            return best.get("artworkUrl100")
        log(f"  -> no album match above threshold (best={best_score:.2f})")
        return None

    def _by_song(self, artist, album, title, country, threshold):
        """Song search: verify title/artist and the album name."""
        a_norm, t_norm = norm(album), norm(title)
        for r in self._search_safe(f"{artist} {t_norm}", "song", None, 25, country):
            found_artist = r.get("artistName", "")
            found_title = r.get("trackName", "")
            found_album = r.get("collectionName", "")
            if not artist_ok(artist, found_artist):
                continue
            t_score = sim(t_norm, norm(found_title))
            a_score = sim(a_norm, norm(found_album)) if a_norm else 1.0
            if t_score >= 0.6:
                log(f"  song candidate: {found_artist!r} - {found_title!r} "
                    f"[{found_album!r}] title={t_score:.2f} album={a_score:.2f}")
            if t_score >= threshold and a_score >= threshold:
                log("  -> song match accepted")
                return r.get("artworkUrl100")
        return None

    def resolve(self, artist: str, album: str, title: str):
        """Return a cover URL, or None if no confident match was found."""
        key = cache_key(artist, album, title)
        if key in self.cache:
            log(f"Cover cache hit [{key}] -> {self.cache[key]}")
            return self.cache[key]

        self.failed = False
        threshold = float(CFG["match_threshold"])
        log(f"Cover lookup: artist={artist!r} album={album!r} title={title!r} "
            f"(stores={COUNTRIES}, threshold={threshold})")

        # Try each store in order: album search first, then song search
        url = None
        for country in COUNTRIES:
            log(f"Store [{country}]")
            url = (self._by_artist(artist, album, country, threshold)
                   or self._by_album(artist, album, country, threshold)
                   or self._by_song(artist, album, title, country, threshold))
            if url:
                break

        url = self._hires(url) if url else None
        log(f"Cover result: {url}")
        if url or not self.failed:  # do not remember misses caused by errors
            self.cache[key] = url
            if url:
                self._save()
        return url


# ------------------------------------------------------------------- Logic
def log(msg: str) -> None:
    """Append a line to %APPDATA%\\TidalDiscordRPC\\debug.log (the exe has no console)."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            # keep exactly one old log (debug.old.log), overwritten on each rotation
            os.replace(LOG_FILE, LOG_FILE.with_name("debug.old.log"))
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


class State:
    enabled = True
    stop = False
    line1 = "Nothing playing"  # tray menu: title
    line2 = ""                # tray menu: artist - album


state = State()
resolver = CoverResolver()


def pad(s: str) -> str:
    """Discord requires 2..128 characters for details/state."""
    s = (s or "").strip() or "Unknown"
    return (s + "  ")[:128] if len(s) < 2 else s[:128]


async def worker():
    rpc = None
    last_key = None
    last_position = None
    last_position_mono = None
    poll = float(CFG["poll_seconds"])
    # compare against the literal text, so editing PLACEHOLDER_ID cannot break this
    configured = str(CFG["discord_client_id"]).strip() not in ("", "YOUR_APPLICATION_ID")
    mgr = await SessionManager.request_async()
    last_ids = None
    warned = False
    log("Worker started, session manager acquired")
    log(f"Config file: {CONFIG_FILE} (exists: {CONFIG_FILE.exists()}, "
        f"client id set: {configured}, value: {CFG['discord_client_id']!r})")
    if CONFIG_ERROR:
        log(f"Config could not be read, using defaults: {CONFIG_ERROR}")

    async def clear():
        nonlocal last_key
        if rpc and last_key is not None:
            try:
                # pypresence has its own event loop: never call it inside ours
                await asyncio.to_thread(rpc.clear)
            except Exception:
                pass
        last_key = None

    while not state.stop:
        await asyncio.sleep(poll)
        sessions = list(mgr.get_sessions())
        ids = [s.source_app_user_model_id or "" for s in sessions]
        if ids != last_ids:  # log whenever the set of media sessions changes
            log(f"Media sessions: {ids}")
            last_ids = ids
        session = next((s for s in sessions
                        if "tidal" in (s.source_app_user_model_id or "").lower()), None)
        if session is None:
            state.line1, state.line2 = "Nothing playing", ""
            await clear()
            continue

        props = await session.try_get_media_properties_async()
        playing = session.get_playback_info().playback_status == PlayStatus.PLAYING

        # Update the tray menu info (also while paused)
        if props.title:
            icon = "\u25b6" if playing else "\u23f8"  # play / pause symbol
            state.line1 = f"{icon} {props.title}"
            state.line2 = (f"{props.artist} \u2013 {props.album_title}"
                           if props.album_title else props.artist)
        else:
            state.line1, state.line2 = "Nothing playing", ""

        if not state.enabled or not playing or not props.title:
            await clear()
            continue

        tl = session.get_timeline_properties()
        pos = tl.position.total_seconds()
        dur = (tl.end_time - tl.start_time).total_seconds()

        key = (props.artist, props.title, props.album_title)
        now_mono = time.monotonic()

        track_changed = key != last_key
        seeked = False

        if (
                not track_changed
                and last_position is not None
                and last_position_mono is not None
        ):
            expected_pos = last_position + (now_mono - last_position_mono)

            seeked = abs(pos - expected_pos) > 4

        last_position = pos
        last_position_mono = now_mono

        if not track_changed and not seeked:
            continue

        start = time.time() - pos

        if key != last_key:
            log(f"Track: artist={props.artist!r} title={props.title!r} "
                f"album={props.album_title!r} duration={dur:.0f}s")

        if rpc is None:
            if not configured:
                # no client ID set yet: tell the user instead of failing silently
                state.line2 = "Set discord_client_id in config.json"
                if not warned:
                    log("No discord_client_id configured: open config.json and set it")
                    warned = True
                continue
            try:
                rpc = Presence(CFG["discord_client_id"])
                await asyncio.to_thread(rpc.connect)
                log("Connected to Discord")
            except Exception as e:
                log(f"Discord connect failed: {e!r}")
                rpc = None  # Discord is not running; retry on next tick
                continue

        if key != last_key:
            cover = await asyncio.to_thread(
                resolver.resolve, props.artist, props.album_title, props.title)
        else:
            cover = resolver.cache.get(
                cache_key(props.artist, props.album_title, props.title))

        try:
            await asyncio.to_thread(
                rpc.update,
                activity_type=ActivityType.LISTENING,
                details=pad(props.title),
                state=pad(props.artist),
                large_image=cover or CFG["fallback_image"],
                large_text=pad(props.album_title or props.title),
                start=int(start),
                end=int(start + dur) if dur > 0 else None,
            )
            if key != last_key:
                log(f"Presence updated, large_image={cover or CFG['fallback_image']}")
            last_key = key
        except Exception:
            log("Presence update failed:\n" + traceback.format_exc())
            rpc = None  # connection lost; reconnect on next tick


def run_worker():
    # Restart the worker on errors instead of dying silently
    while not state.stop:
        try:
            asyncio.run(worker())
        except Exception:
            log(traceback.format_exc())
            state.line1, state.line2 = "Error - see debug.log", ""
            time.sleep(5)


# -------------------------------------------------------------------- Tray
def make_icon() -> Image.Image:
    """Draw the tray icon in code, so no image file is needed."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), 12, fill=(15, 15, 15, 255))
    for cx, cy in ((32, 20), (20, 32), (44, 32), (32, 44)):
        d.polygon([(cx, cy - 8), (cx + 8, cy), (cx, cy + 8), (cx - 8, cy)],
                  fill=(255, 255, 255, 255))
    return img


def short(s: str, n: int = 55) -> str:
    """Keep tray menu entries at a readable length."""
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def main():
    # Single instance via named mutex (ERROR_ALREADY_EXISTS = 183)
    ctypes.windll.kernel32.CreateMutexW(None, False, f"Global\\{APP_NAME}")
    if ctypes.windll.kernel32.GetLastError() == 183:
        return

    threading.Thread(target=run_worker, daemon=True).start()

    def toggle_enabled(icon, item):
        state.enabled = not state.enabled

    def toggle_autostart(icon, item):
        set_autostart(not autostart_enabled())

    def open_config(icon, item):
        load_config()  # make sure the file exists
        os.startfile(CONFIG_FILE)

    def open_log(icon, item):
        os.startfile(LOG_FILE if LOG_FILE.exists() else DATA_DIR)

    def quit_app(icon, item):
        state.stop = True
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: short(state.line1), None, enabled=False),
        pystray.MenuItem(lambda item: short(state.line2), None, enabled=False,
                         visible=lambda item: bool(state.line2)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Presence enabled", toggle_enabled,
                         checked=lambda i: state.enabled),
        pystray.MenuItem("Start with Windows", toggle_autostart,
                         checked=lambda i: autostart_enabled()),
        pystray.MenuItem("Open config (restart to apply)", open_config),
        pystray.MenuItem("Open debug log", open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )
    pystray.Icon(APP_NAME, make_icon(), APP_NAME, menu).run()


if __name__ == "__main__":
    main()