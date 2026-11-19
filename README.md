# GetWallapper

A local web gallery for live wallpapers from two sources:

- **iWallpaper** (macOS app, Apple CloudKit public catalogue) — browse the full
  census, download the encrypted video assets and decrypt them locally.
- **Wallpaper Engine** (Steam Workshop, app `431960`) — browse trending *video*
  wallpapers, filter by content rating, and download them through SteamCMD
  (requires owning Wallpaper Engine on Steam).

Everything runs on your Mac. No account, API key, or cloud service is needed for
browsing; the server binds to `127.0.0.1` only.

## Features

- 4-column grid of poster thumbnails (1 GiB on-disk cache per source, oldest
  evicted first)
- Smart search: comma-separated terms, **all must match** (AND), each matched
  against titles and Wallpaper Engine tags — `anime, girls, raiden`
- Infinite scroll (Wallpaper Engine pages through Steam Workshop; iWallpaper
  loads its full census at once)
- Content-rating selector for Wallpaper Engine: Everyone (default),
  Questionable, Mature, All
- Hover a tile → yellow download tile; click → spinner with live progress
  (`42%`, `Decrypting…`, `Copying…`); downloaded tiles are green with a ✓
- Hover a downloaded tile → blue folder tile; click reveals the file in Finder;
  if the file was deleted outside the app, the tile resets for re-download
- Configurable download folder via the ⚙ icon (default
  `~/Downloads/GetWallpaperGallery/`)
- Files are named after the wallpaper title

## Requirements

- macOS (uses `open -R` for Finder reveal and `openssl` for decryption)
- Python 3.10+ (standard library only — no pip installs)
- For Wallpaper Engine downloads: [SteamCMD](https://developer.valvesoftware.com/wiki/SteamCMD)
  and a Steam account that owns Wallpaper Engine.

```bash
brew install steamcmd
steamcmd +login <your-account> +quit   # once; Steam Guard asks for a code
```

The app auto-detects the cached account (`loginusers.vdf`) and passes
`+login` to SteamCMD, so subsequent downloads need no interaction. Override
with `WE_STEAM_ACCOUNT=<account>`.

Browsing Wallpaper Engine metadata and thumbnails requires no Steam login.

## Run

```bash
python3 webui.py            # opens http://127.0.0.1:8000
python3 webui.py 8123       # custom port
```

## Download folder

Default: `~/Downloads/GetWallpaperGallery/`. Click the ⚙ icon in the top-right
corner to change it; the path is validated (created if missing, write-tested)
and saved to `settings.json` next to the app.

Downloaded-file tracking is stored in `indexes/` as absolute paths, so changing
the folder does not forget files downloaded earlier.

## How it works

| Source | Catalogue | Thumbnails | Video |
|---|---|---|---|
| iWallpaper | `wallpaper_census.json` (bundled metadata snapshot) | Apple CloudKit asset URLs | AES-128-ECB asset, decrypted with `decrypt.sh` |
| Wallpaper Engine | Steam Workshop browse page (embedded JSON, `requiredtags[]=Video` + rating) | Steam image CDN (`preview_url`, downscaled) | `steamcmd workshop_download_item 431960 <id>` |

Wallpaper Engine pages are fetched eight at a time (240 items) in parallel and
cached for six hours in `we_catalog*.json`.

### Environment variables

| Variable | Purpose |
|---|---|
| `WE_STEAM_ACCOUNT` | Steam account name for SteamCMD (default: auto-detected) |
| `WE_WORKSHOP_DIR` | Existing `steamapps/workshop/content/431960` folder to copy from instead of downloading |
| `THUMB_CAP_BYTES` | Thumbnail cache cap per source (default `1073741824` = 1 GiB) |

### Endpoints

- `GET /` — UI
- `GET /api/records`, `/thumb/<uuid>`, `/download/<uuid>`, `/reveal/<uuid>`, `/progress/iw/<uuid>`
- `GET /api/we/records?rating=…`, `/we/thumb/<id>`, `/we/download/<id>`, `/we/reveal/<id>`, `/progress/we/<id>`
- `GET|POST /api/settings` — read/change `download_root`
- `GET /health`

## Notes

- Downloads are for personal use. Wallpaper Engine Workshop items belong to
  their authors; do not redistribute them. iWallpaper assets are likewise
  third-party content.
- The Wallpaper Engine source is unofficial and relies on Steam's public
  Workshop pages; it may need updating if Steam changes their markup.
- Scene-type Wallpaper Engine wallpapers are intentionally excluded — they are
  packed `.pkg` projects, not plain video files.
