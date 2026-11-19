"""we_source.py — Wallpaper Engine (Steam Workshop app 431960) video wallpapers.

Catalog
    The public Workshop browse page filtered to `requiredtags[]=Video` plus a
    rating tag (`Everyone` by default; selectable Questionable/Mature/All)
    embeds the complete item list for the page as JSON in
    <script type="application/json" id="valve-ssr-data">. No API key and no
    login are needed to read it. 30 items per page, deduped by us. Each rating
    slice has its own cache file (we_catalog*.json).

Thumbnails
    Each item carries a public preview_url on Steam's image CDN; we downscale
    with `?imw=640&impolicy=Letterbox` and cache on disk.

Downloads
    Valve only serves Workshop files to a Steam account that owns Wallpaper
    Engine, so there is no anonymous URL. Two supported paths:

      1. Local Workshop folder — set WE_WORKSHOP_DIR to an existing
         `.../steamapps/workshop/content/431960` folder (or subscribe in the
         Steam client and let it download), or
      2. SteamCMD — `brew install steamcmd`, log in once with
             steamcmd +login <account> +quit
         (Steam Guard asks once, the session is cached). Downloads then run
         with `+login <account> +workshop_download_item 431960 <id>`; the
         account name is auto-detected from Steam's `loginusers.vdf` (override
         with WE_STEAM_ACCOUNT). Note: omitting `+login` makes steamcmd run
         anonymous and fail with "Not logged on".

    Downloaded videos are copied into we_downloads/<title>.<ext>.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
APPID = "431960"
BROWSE_URL = ("https://steamcommunity.com/workshop/browse/"
              "?appid=431960&browsefilter=trend"
              "&requiredtags%5B%5D=Video{extra}&p={page}")
RATINGS = ("Everyone", "Questionable", "Mature")
CACHE = os.path.join(HERE, "we_catalog.json")
THUMB_DIR = os.path.join(HERE, "we_thumbs")
DL_DIR = os.path.join(HERE, "we_downloads")
CACHE_TTL = 6 * 3600
PAGE_SIZE = 30
MAX_PAGES = 1000  # Steam clamps every Workshop browse query to 1,000 pages
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
RES_RE = re.compile(r"(\d+)\s*[x\u00d7]\s*(\d+)")
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv")
_STEAMCMD_LOCK = threading.Lock()   # steamcmd cannot run two instances at once


def configure_data_dir(path):
    """Point caches/downloads at a user data directory (used by the package)."""
    global THUMB_DIR, CACHE, DL_DIR
    THUMB_DIR = os.path.join(path, "we_thumbs")
    CACHE = os.path.join(path, "we_catalog.json")
    DL_DIR = os.path.join(path, "we_downloads")
    os.makedirs(THUMB_DIR, exist_ok=True)


# ------------------------------------------------------------------ fetch ---

def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _walk_items(obj, out, depth=0):
    """Find every workshop item dict inside the (double-encoded) SSR JSON."""
    if depth > 12:
        return
    if isinstance(obj, dict):
        if "publishedfileid" in obj and "title" in obj:
            out.append(obj)
            return
        for value in obj.values():
            _walk_items(value, out, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            _walk_items(value, out, depth + 1)
    elif isinstance(obj, str) and obj.strip()[:1] in "{[":
        try:
            _walk_items(json.loads(obj), out, depth + 1)
        except ValueError:
            pass


def _page_items(page, rating="Everyone"):
    extra = ""
    if rating in RATINGS:
        extra = "&requiredtags%5B%5D=" + rating
    url = BROWSE_URL.format(extra=extra, page=page)
    html = _get(url).decode("utf-8", "replace")
    m = re.search(r'<script type="application/json" id="valve-ssr-data"[^>]*>'
                  r"(.*?)</script>", html, re.S)
    if not m:
        raise RuntimeError("workshop page layout changed (valve-ssr-data missing)")
    items = []
    _walk_items(json.loads(m.group(1)), items)
    return items


def _norm(item):
    tags = [t.get("tag", "") for t in item.get("tags", [])]
    res = next((t for t in tags if RES_RE.search(t)), "?")
    return {
        "id": item["publishedfileid"],
        "title": item.get("title") or item["publishedfileid"],
        "mb": round(int(item.get("file_size") or 0) / 1e6, 1),
        "bytes": int(item.get("file_size") or 0),
        "subs": int(item.get("subscriptions") or 0),
        "res": res,
        "preview": item.get("preview_url") or "",
        "tags": tags,
        "created": int(item.get("time_created") or 0),
    }


def _cache_path(rating):
    base_dir = os.path.dirname(CACHE)
    if rating in RATINGS:
        return CACHE if rating == "Everyone" else os.path.join(
            base_dir, "we_catalog_%s.json" % rating.lower())
    return os.path.join(base_dir, "we_catalog_all.json")


def _load_cache(rating="Everyone"):
    try:
        with open(_cache_path(rating)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _save_cache(data, rating="Everyone"):
    try:
        with open(_cache_path(rating), "w") as fh:
            json.dump(data, fh)
    except OSError as exc:
        print("we_source: cache write failed: %s" % exc, file=sys.stderr)


def known_items():
    """Merge every cached rating slice so thumbs/downloads resolve by id."""
    import glob
    items = {}
    for path in glob.glob(os.path.join(HERE, "we_catalog*.json")):
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        for rec in data.get("items", []):
            items.setdefault(rec["id"], rec)
    return items


def _collect(start_page, pages, rating="Everyone"):
    """Fetch a page range; returns (normalized items, pages actually fetched)."""
    page_numbers = [p for p in range(start_page, start_page + pages)
                    if p <= MAX_PAGES]
    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_page_items, p, rating): p for p in page_numbers}
        for future in futures:
            page = futures[future]
            try:
                results[page] = future.result()
            except Exception as exc:
                print("we_source: page %d failed: %s" % (page, exc),
                      file=sys.stderr)

    seen, items = set(), []
    for page in sorted(results):
        for item in results[page]:
            if "Video" not in [t.get("tag") for t in item.get("tags", [])]:
                continue
            rec = _norm(item)
            if rec["id"] in seen:
                continue
            seen.add(rec["id"])
            items.append(rec)
    items.sort(key=lambda r: -r["subs"])
    return items, len(results)


def catalog(pages=8, refresh=False, rating="Everyone"):
    """First `pages` of the browse query for `rating`, cached per rating."""
    data = _load_cache(rating)
    if data and not refresh and time.time() - data.get("fetched_at", 0) < CACHE_TTL:
        return data.get("items", [])

    items, fetched = _collect(1, pages, rating)
    _save_cache({"fetched_at": time.time(), "pages": fetched, "items": items},
                rating)
    return items


def fetch_more(pages=8, rating="Everyone"):
    """Fetch the next `pages` after the cache; only new items are returned.

    Returns (added_items, total_items, exhausted, reset). `reset` is True when
    the cache was stale and the client should replace its list, not append.
    """
    data = _load_cache(rating)
    if not data or time.time() - data.get("fetched_at", 0) >= CACHE_TTL:
        items = catalog(pages=pages, refresh=True, rating=rating)
        return items, len(items), False, True

    start = int(data.get("pages") or 0) + 1
    if start > MAX_PAGES:
        return [], len(data.get("items", [])), True, False

    new_items, fetched = _collect(start, pages, rating)
    seen = {r["id"] for r in data.get("items", [])}
    added = [r for r in new_items if r["id"] not in seen]

    data["items"] = data.get("items", []) + added
    data["pages"] = start - 1 + fetched
    data["fetched_at"] = time.time()
    _save_cache(data, rating)

    exhausted = fetched == 0 or (start - 1 + fetched) >= MAX_PAGES
    return added, len(data["items"]), exhausted, False


# ------------------------------------------------------------- thumbnails ---

def thumbnail(item_id, preview_url, max_width=640):
    """Returns (path, created)."""
    os.makedirs(THUMB_DIR, exist_ok=True)
    for ext in ("jpg", "gif", "png", "webp"):
        path = os.path.join(THUMB_DIR, "%s.%s" % (item_id, ext))
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path, False

    if not preview_url:
        return None, False
    url = preview_url + ("&" if "?" in preview_url else "?")
    url += "imw=%d&impolicy=Letterbox" % max_width

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        ctype = resp.headers.get("Content-Type", "image/jpeg").lower()
        data = resp.read()
    ext = ("gif" if "gif" in ctype else
           "png" if "png" in ctype else
           "webp" if "webp" in ctype else "jpg")
    path = os.path.join(THUMB_DIR, "%s.%s" % (item_id, ext))
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return path, True


def thumb_type(path):
    ext = os.path.splitext(path)[1].lower()
    return {"gif": "image/gif", "png": "image/png",
            "webp": "image/webp"}.get(ext.lstrip("."), "image/jpeg")


# ---------------------------------------------------------------- download --

def _workshop_roots():
    roots = []
    env = os.environ.get("WE_WORKSHOP_DIR")
    if env:
        roots.append(env)
    if sys.platform == "darwin":
        roots.append(os.path.expanduser(
            "~/Library/Application Support/Steam/steamapps/workshop/"
            "content/431960"))
    elif os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES(X86)"),
                     os.environ.get("PROGRAMFILES")):
            if base:
                roots.append(os.path.join(base, "Steam", "steamapps",
                                          "workshop", "content", "431960"))
        roots.append(os.path.expanduser(
            "~\\Steam\\steamapps\\workshop\\content\\431960"))
    else:
        for base in (os.path.expanduser("~/.steam/steam"),
                     os.path.expanduser("~/.local/share/Steam")):
            roots.append(os.path.join(base, "steamapps", "workshop",
                                      "content", "431960"))
    return [r for r in roots if r]


def _default_content_root():
    """Steam's default Workshop content folder for the current OS."""
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Steam/steamapps/workshop/"
            "content/431960")
    if os.name == "nt":
        base = (os.environ.get("PROGRAMFILES(X86)")
                or os.environ.get("PROGRAMFILES")
                or r"C:\Program Files (x86)")
        return os.path.join(base, "Steam", "steamapps", "workshop",
                            "content", "431960")
    return os.path.expanduser(
        "~/.steam/steam/steamapps/workshop/content/431960")


def _video_in(folder):
    if not folder or not os.path.isdir(folder):
        return None
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith(VIDEO_EXTS):
            path = os.path.join(folder, name)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return path
    return None


def local_video(item_id):
    for root in _workshop_roots():
        path = _video_in(os.path.join(root, item_id))
        if path:
            return path
    return None


def _safe_name(title):
    s = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]", " ", str(title))
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s or "wallpaper"


def _unique_path(directory, base, ext):
    path = os.path.join(directory, "%s%s" % (base, ext))
    n = 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(directory, "%s (%d)%s" % (base, n, ext))
    return path


def _tail(text, lines=3):
    return " | ".join((text or "").strip().splitlines()[-lines:]) or "(no output)"


def _steam_account():
    """Steam account name for steamcmd login: env override, else the last
    account cached by the Steam client / steamcmd on this machine."""
    env = os.environ.get("WE_STEAM_ACCOUNT")
    if env:
        return env
    candidates = []
    if sys.platform == "darwin":
        candidates.append(os.path.expanduser(
            "~/Library/Application Support/Steam/config/loginusers.vdf"))
    elif os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES(X86)"),
                     os.environ.get("PROGRAMFILES")):
            if base:
                candidates.append(os.path.join(base, "Steam", "config",
                                               "loginusers.vdf"))
    else:
        candidates += [
            os.path.expanduser("~/.steam/steam/config/loginusers.vdf"),
            os.path.expanduser("~/.local/share/Steam/config/loginusers.vdf"),
        ]
    for path in candidates:
        try:
            with open(path) as fh:
                text = fh.read()
        except OSError:
            continue
        m = re.search(r'"AccountName"\s*"([^"]+)"', text)
        if m:
            return m.group(1)
    return None


def _folder_bytes(folder):
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def download(item_id, title, on_progress=None, expected_bytes=0, dest_dir=None):
    """Copy/download one workshop item; returns (path, via, already_present).

    on_progress(done, total, phase) is called during steamcmd downloads
    (done = percent, total = 100) and local copies (bytes). dest_dir overrides
    the default we_downloads/ folder.
    """
    via = "local"
    src = local_video(item_id)
    if not src:
        steamcmd = shutil.which("steamcmd")
        if not steamcmd:
            raise RuntimeError(
                "steamcmd not installed and no local Workshop copy found. "
                "Either `brew install steamcmd` and log in once "
                "(`steamcmd +login <account> +quit`), or set WE_WORKSHOP_DIR "
                "to your steamapps/workshop/content/431960 folder.")
        account = _steam_account()
        if account:
            cmd = [steamcmd, "+login", account,
                   "+workshop_download_item", APPID, item_id, "+quit"]
        else:
            cmd = [steamcmd, "+workshop_download_item", APPID, item_id, "+quit"]

        deadline = time.time() + 1800
        with _STEAMCMD_LOCK:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            chunks = []

            def _reader():
                while True:
                    piece = proc.stdout.read(512)
                    if not piece:
                        break
                    chunks.append(piece)

            threading.Thread(target=_reader, daemon=True).start()

            content = os.path.join(_default_content_root(), item_id)
            workshop_root = os.path.dirname(os.path.dirname(
                _default_content_root()))
            staging = os.path.join(workshop_root, "downloads")
            baseline = _folder_bytes(staging)
            last_pct = -1
            while proc.poll() is None:
                if expected_bytes and on_progress:
                    staged = max(0, _folder_bytes(staging) - baseline)
                    got = staged + _folder_bytes(content)
                    pct = min(99, int(got * 100 / expected_bytes))
                    if pct > 0 and pct != last_pct:
                        last_pct = pct
                        on_progress(pct, 100, "downloading")
                if time.time() > deadline:
                    proc.kill()
                    break
                time.sleep(0.6)
            proc.wait()
        out = "".join(chunks)
        if "Not logged on" in out:
            raise RuntimeError(
                "steamcmd is not logged in. Run once in Terminal: "
                "steamcmd +login <your-account> +quit  "
                "(the account must own Wallpaper Engine; Steam Guard asks once)")
        m = re.search(r'Downloaded item\s+%s\s+to\s+"([^"]+)"'
                      % re.escape(item_id), out)
        if m:
            src = _video_in(m.group(1)) or _video_in(os.path.dirname(m.group(1)))
        if not src:
            src = local_video(item_id)
        if not src:
            raise RuntimeError("steamcmd download failed: " + _tail(out))
        via = "steamcmd"

    os.makedirs(dest_dir or DL_DIR, exist_ok=True)
    dest = dest_dir or DL_DIR
    ext = os.path.splitext(src)[1].lower() or ".mp4"
    base = _safe_name(title)
    first = os.path.join(dest, base + ext)
    if os.path.exists(first):
        return first, via, True
    target = _unique_path(dest, base, ext)
    tmp = target + ".part"
    total = os.path.getsize(src)
    done = 0
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        while True:
            chunk = fin.read(1 << 20)
            if not chunk:
                break
            fout.write(chunk)
            done += len(chunk)
            if on_progress:
                on_progress(done, total, "copying")
    try:
        shutil.copystat(src, tmp)
    except OSError:
        pass
    os.replace(tmp, target)
    return target, via, False
