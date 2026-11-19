#!/usr/bin/env python3
"""Local web gallery for iWallpaper and Wallpaper Engine video wallpapers.

Run standalone with `python3 webui.py [port]`, or via the `getw` CLI
(`getw start`).

Sources:
    iWallpaper       (Apple CloudKit public database)
    Wallpaper Engine (Steam Workshop app 431960, video items only)

Endpoints:
    GET /                    grid page (source switch, search, hover-download)
    GET /api/records         iWallpaper census metadata (JSON)
    GET /thumb/<uuid>        iWallpaper thumbnail (fetched on demand, cached)
    GET /download/<uuid>     lookup -> download -> decrypt -> downloads/<title>.<ext>
    GET /api/we/records      Wallpaper Engine video items (JSON)
    GET /we/thumb/<id>       Workshop preview (cached in we_thumbs/)
    GET /we/download/<id>    copy from local Workshop or steamcmd -> we_downloads/
    GET /we/reveal/<id>      reveal a downloaded Workshop item in the file manager
    GET /reveal/<uuid>       reveal a downloaded iWallpaper file in the file manager
    GET|POST /api/settings   read/change the download folder
    GET /progress/<kind>/<id> download progress for a tile

The server binds to 127.0.0.1 only.
"""

import hashlib
import json
import os
import pkgutil
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:                      # installed package
    from . import we_source
except ImportError:       # standalone checkout
    import we_source

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
IS_PACKAGE = os.path.basename(PKG_DIR) == "getw"


def default_data_dir():
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "GetWallpaper")


STATE_DIR = os.environ.get("GETW_DATA_DIR") or (
    default_data_dir() if IS_PACKAGE else PKG_DIR)
if IS_PACKAGE:
    _legacy = STATE_DIR.replace("GetWallpaper", "GetWallapper")
    if not os.path.exists(STATE_DIR) and os.path.exists(_legacy):
        try:
            os.rename(_legacy, STATE_DIR)
        except OSError:
            pass
os.makedirs(STATE_DIR, exist_ok=True)
we_source.configure_data_dir(STATE_DIR)

CENSUS = os.path.join(PKG_DIR, "wallpaper_census.json")
THUMB_DIR = os.path.join(STATE_DIR, "thumbs")
AES_KEY_HEX = "4e534461746100000000000000000000"   # ASCII "NSData" zero-padded
LOOKUP_URL = ("https://api.apple-cloudkit.com/database/1/"
              "iCloud.com.macosgame.iwallpaper/production/public/records/lookup")
UUID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                     r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\Z")

SETTINGS_PATH = os.path.join(STATE_DIR, "settings.json")
INDEX_DIR = os.path.join(STATE_DIR, "indexes")
DEFAULT_DOWNLOAD_ROOT = os.path.expanduser("~/Downloads/GetWallpaperGallery")


def _load_settings():
    try:
        with open(SETTINGS_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


SETTINGS = _load_settings()
DOWNLOAD_ROOT = os.path.abspath(os.path.expanduser(
    SETTINGS.get("download_root") or DEFAULT_DOWNLOAD_ROOT))
THUMB_CAP_MB = max(64, int(SETTINGS.get("thumb_cap_mb") or 1024))
THUMB_CAP = THUMB_CAP_MB * 1024 * 1024
PREFILL_ENABLED = bool(SETTINGS.get("prefill_thumbs"))


def ensure_download_root():
    os.makedirs(DOWNLOAD_ROOT, exist_ok=True)
    return DOWNLOAD_ROOT


def set_download_root(path):
    """Validate, create and persist a new download folder."""
    global DOWNLOAD_ROOT
    path = os.path.abspath(os.path.expanduser(str(path).strip()))
    os.makedirs(path, exist_ok=True)
    probe = os.path.join(path, ".getwallpaper-write-test")
    with open(probe, "w") as fh:
        fh.write("ok")
    os.remove(probe)
    DOWNLOAD_ROOT = path
    SETTINGS["download_root"] = path
    _save_settings()
    return path


def _save_settings():
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(SETTINGS, fh, indent=1)
    os.replace(tmp, SETTINGS_PATH)


def _dir_stats(path):
    total = count = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_file():
                        count += 1
                        total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return {"mb": round(total / 1e6, 1), "count": count}


_INTERACTIVE_LOCK = threading.Lock()
_INTERACTIVE = {"count": 0}


def _enter_interactive():
    with _INTERACTIVE_LOCK:
        _INTERACTIVE["count"] += 1


def _leave_interactive():
    with _INTERACTIVE_LOCK:
        _INTERACTIVE["count"] = max(0, _INTERACTIVE["count"] - 1)


def _interactive_active():
    with _INTERACTIVE_LOCK:
        return _INTERACTIVE["count"] > 0


def _prefill_gate():
    while _interactive_active() and PF.get("running"):
        time.sleep(0.15)


PF = {"running": False, "done": 0, "total": 0, "source": "", "error": None}
_PF_LOCK = threading.Lock()


def _pf_set(**kw):
    with _PF_LOCK:
        PF.update(kw)


def _pf_active():
    return PF.get("running") and SETTINGS.get("prefill_thumbs")


def start_prefill():
    with _PF_LOCK:
        if PF.get("running"):
            return dict(PF)
        PF.update(running=True, done=0, total=0, source="iWallpaper",
                  error=None)
    threading.Thread(target=_prefill_worker, daemon=True).start()
    return dict(PF)


def stop_prefill():
    _pf_set(running=False)


def _prefill_worker():
    from concurrent.futures import ThreadPoolExecutor

    def fetch_iw(rec):
        try:
            fetch_thumbnail(rec["id"])
        except Exception:
            pass

    def fetch_we(rec):
        try:
            we_source.thumbnail(rec["id"], rec["preview"])
        except Exception:
            pass

    try:
        _pf_set(source="iWallpaper", total=len(RECORDS), done=0)
        for i in range(0, len(RECORDS), 16):
            if not _pf_active():
                return
            if _dir_stats(THUMB_DIR)["mb"] * 1e6 >= THUMB_CAP:
                break
            _prefill_gate()
            batch = RECORDS[i:i + 16]
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(fetch_iw, batch))
            _pf_set(done=min(len(RECORDS), PF.get("done", 0) + len(batch)))

        items = we_source.catalog(rating="Everyone")
        _pf_set(source="Wallpaper Engine", total=len(items), done=0)
        for i in range(0, len(items), 16):
            if not _pf_active():
                return
            if _dir_stats(we_source.THUMB_DIR)["mb"] * 1e6 >= THUMB_CAP:
                break
            _prefill_gate()
            batch = items[i:i + 16]
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(fetch_we, batch))
            _pf_set(done=min(len(items), PF.get("done", 0) + len(batch)))
    except Exception as exc:
        _pf_set(error=str(exc))
    finally:
        _pf_set(running=False)


def settings_payload():
    return {
        "download_root": DOWNLOAD_ROOT,
        "default_root": DEFAULT_DOWNLOAD_ROOT,
        "thumb_cap_mb": THUMB_CAP_MB,
        "prefill_thumbs": bool(SETTINGS.get("prefill_thumbs")),
        "thumb_cache": {
            "iw": _dir_stats(THUMB_DIR),
            "we": _dir_stats(we_source.THUMB_DIR),
        },
        "prefill": dict(PF),
    }


def update_settings(payload):
    global THUMB_CAP, THUMB_CAP_MB
    if payload.get("download_root"):
        set_download_root(payload["download_root"])
    if payload.get("thumb_cap_mb") is not None:
        mb = int(payload["thumb_cap_mb"])
        if mb < 64:
            raise ValueError("cache size must be at least 64 MB")
        THUMB_CAP_MB = mb
        THUMB_CAP = mb * 1024 * 1024
        SETTINGS["thumb_cap_mb"] = mb
        enforce_cache_cap(THUMB_DIR, THUMB_CAP)
        enforce_cache_cap(we_source.THUMB_DIR, THUMB_CAP)
    if payload.get("prefill_thumbs") is not None:
        enabled = bool(payload["prefill_thumbs"])
        SETTINGS["prefill_thumbs"] = enabled
        if enabled:
            start_prefill()
        else:
            stop_prefill()
    _save_settings()
    return settings_payload()

os.makedirs(THUMB_DIR, exist_ok=True)
os.makedirs(INDEX_DIR, exist_ok=True)
ensure_download_root()

_CACHE_LOCK = threading.Lock()


def enforce_cache_cap(directory, cap=None):
    """Delete the oldest files in `directory` until total size <= cap."""
    cap = THUMB_CAP if cap is None else cap
    with _CACHE_LOCK:
        try:
            entries, total = [], 0
            with os.scandir(directory) as scan:
                for entry in scan:
                    try:
                        if entry.is_file():
                            st = entry.stat()
                            entries.append((st.st_mtime, st.st_size, entry.path))
                            total += st.st_size
                    except OSError:
                        pass
            if total <= cap:
                return
            entries.sort(key=lambda e: e[0])
            removed = freed = 0
            for _mtime, size, path in entries:
                if total <= cap:
                    break
                try:
                    os.remove(path)
                    total -= size
                    removed += 1
                    freed += size
                except OSError:
                    pass
            if removed:
                print("thumb cache pruned: %d files, %.1f MB freed (%s)"
                      % (removed, freed / 1e6, directory), file=sys.stderr)
        except OSError as exc:
            print("thumb cache prune failed: %s" % exc, file=sys.stderr)

# --- download progress -------------------------------------------------------

_PROGRESS_LOCK = threading.Lock()
_PROGRESS = {}


def set_progress(kind, item_id, done=None, total=None, phase=None):
    with _PROGRESS_LOCK:
        now = time.time()
        key = (kind, item_id)
        cur = _PROGRESS.get(key, {})
        if done is not None:
            cur["done"] = done
        if total is not None:
            cur["total"] = total
        if phase is not None:
            cur["phase"] = phase
        cur["ts"] = now
        _PROGRESS[key] = cur
        for k in [k for k, v in _PROGRESS.items()
                  if now - v.get("ts", 0) > 3600]:
            _PROGRESS.pop(k, None)


def get_progress(kind, item_id):
    with _PROGRESS_LOCK:
        cur = _PROGRESS.get((kind, item_id))
        if not cur:
            return {}
        cur = dict(cur)
    done, total = cur.get("done") or 0, cur.get("total") or 0
    cur["pct"] = round(done * 100 / total) if total else None
    return cur


def is_downloading(kind, item_id):
    with _PROGRESS_LOCK:
        cur = _PROGRESS.get((kind, item_id))
        if not cur:
            return False
        return (cur.get("phase") in ("downloading", "copying", "decrypting")
                and time.time() - cur.get("ts", 0) < 3600)

DL_INDEX = os.path.join(INDEX_DIR, "iw.json")
_INDEX_LOCK = threading.Lock()


def _migrate_index(new_path, old_path, old_dir):
    """Convert a legacy filename-based index into absolute paths."""
    if os.path.exists(new_path) or not os.path.exists(old_path):
        return
    try:
        with open(old_path) as fh:
            data = json.load(fh)
        converted = {k: os.path.join(old_dir, os.path.basename(v))
                     for k, v in data.items()}
        with open(new_path, "w") as fh:
            json.dump(converted, fh, indent=0)
    except (OSError, ValueError):
        pass


_migrate_index(DL_INDEX, os.path.join(STATE_DIR, "downloads", "index.json"),
               os.path.join(STATE_DIR, "downloads"))


def _load_index():
    try:
        with open(DL_INDEX) as fh:
            data = json.load(fh)
        return {k: v for k, v in data.items()
                if os.path.isabs(v) and os.path.exists(v)}
    except (OSError, ValueError):
        return {}


INDEX = _load_index()


def mark_downloaded(uuid, path):
    with _INDEX_LOCK:
        INDEX[uuid] = path
        tmp = DL_INDEX + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(INDEX, fh, indent=0)
        os.replace(tmp, DL_INDEX)

def _load_census():
    data = None
    if __package__:
        try:
            data = pkgutil.get_data(__package__, "wallpaper_census.json")
        except Exception:
            data = None
    if data is None:
        with open(CENSUS, "rb") as fh:
            data = fh.read()
    return json.loads(data)


_census = _load_census()
RECORDS = sorted(_census, key=lambda r: -(r.get("hotCount") or 0))
RECORDS = [{"id": r["recordName"],
            "title": r.get("tagsStr") or r["recordName"],
            "res": r.get("resolution") or "?",
            "mb": round((r.get("videoBytes") or 0) / 1e6, 1),
            "dur": r.get("durationSec") or 0,
            "hot": r.get("hotCount") or 0} for r in RECORDS]

# --- Wallpaper Engine (Steam Workshop) source --------------------------------

WE_INDEX_PATH = os.path.join(INDEX_DIR, "we.json")
_WE_INDEX_LOCK = threading.Lock()

_migrate_index(WE_INDEX_PATH, os.path.join(STATE_DIR, "we_downloads", "index.json"),
               os.path.join(STATE_DIR, "we_downloads"))


def _load_we_index():
    try:
        with open(WE_INDEX_PATH) as fh:
            data = json.load(fh)
        return {k: v for k, v in data.items()
                if os.path.isabs(v) and os.path.exists(v)}
    except (OSError, ValueError):
        return {}


WE_INDEX = _load_we_index()

# prune thumbnail caches at startup (oldest-first, 1 GiB cap each)
os.makedirs(we_source.THUMB_DIR, exist_ok=True)
enforce_cache_cap(THUMB_DIR)
enforce_cache_cap(we_source.THUMB_DIR)


def we_mark_downloaded(item_id, filename):
    with _WE_INDEX_LOCK:
        WE_INDEX[item_id] = filename
        tmp = WE_INDEX_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(WE_INDEX, fh, indent=0)
        os.replace(tmp, WE_INDEX_PATH)


_WE_LOCK = threading.Lock()
WE_ITEMS = we_source.known_items()
WE_RATINGS = ("Everyone", "Questionable", "Mature", "All")


def _we_rows(items):
    with _WE_INDEX_LOCK:
        done = {i for i, f in WE_INDEX.items()
                if os.path.exists(f)}
    return [{"id": r["id"],
             "title": r["title"],
             "meta": "%s · %.1f MB · %s subs" % (r["res"], r["mb"], r["subs"]),
             "tags": " ".join(r.get("tags", [])),
             "api": "/we",
             "downloaded": r["id"] in done,
             "downloading": is_downloading("we", r["id"])} for r in items]


def we_records(refresh=False, rating="Everyone"):
    """Normalized Workshop video items + downloaded flags for the grid."""
    rating = rating if rating in WE_RATINGS else "Everyone"
    with _WE_LOCK:
        items = we_source.catalog(pages=8, refresh=refresh, rating=rating)
        WE_ITEMS.update({r["id"]: r for r in items})
    return _we_rows(items)


def we_more(pages=8, rating="Everyone"):
    """Next batch: only items not already in the cache, plus new total."""
    rating = rating if rating in WE_RATINGS else "Everyone"
    with _WE_LOCK:
        added, total, exhausted, reset = we_source.fetch_more(pages=pages,
                                                              rating=rating)
        WE_ITEMS.update({r["id"]: r for r in added})
    return {"items": _we_rows(added), "total": total,
            "exhausted": exhausted, "reset": reset}


# background catalogue fill: keep paging until Steam's 1,000-page cap
_FILL = {}
_FILL_LOCK = threading.Lock()


def we_records_slice(offset, rating="Everyone"):
    """Rows from `offset` onward without building rows for the whole catalogue."""
    rating = rating if rating in WE_RATINGS else "Everyone"
    with _WE_LOCK:
        items = we_source.catalog(rating=rating)
        WE_ITEMS.update({r["id"]: r for r in items})
    total = len(items)
    off = max(0, min(int(offset), total))
    return {"items": _we_rows(items[off:]), "total": total}


def fill_status(rating):
    with _FILL_LOCK:
        st = _FILL.get(rating)
        if not st:
            return {"running": False, "total": 0, "exhausted": False,
                    "error": None}
        return dict(st)


def start_fill(rating):
    rating = rating if rating in WE_RATINGS else "Everyone"
    with _FILL_LOCK:
        st = _FILL.get(rating)
        if st and st.get("running"):
            return dict(st)
        _FILL[rating] = {"running": True, "total": 0, "exhausted": False,
                         "error": None}
    threading.Thread(target=_fill_worker, args=(rating,), daemon=True).start()
    return fill_status(rating)


def _fill_worker(rating):
    zero = 0
    try:
        for _ in range(200):
            with _WE_LOCK:
                added, total, exhausted, reset = we_source.fetch_more(
                    pages=24, rating=rating, persist=False)
                WE_ITEMS.update({r["id"]: r for r in added})
            with _FILL_LOCK:
                _FILL.setdefault(rating, {}).update(
                    total=total, exhausted=exhausted, error=None)
            if exhausted:
                break
            zero = zero + 1 if not added else 0
            _n = globals().get("_FILL_N", 0) + 1
            globals()["_FILL_N"] = _n
            if _n % 10 == 0:
                we_source.flush_cache(rating)
            time.sleep(5 if zero >= 3 else 1.0)
    except Exception as exc:
        with _FILL_LOCK:
            _FILL.setdefault(rating, {}).update(error=str(exc))
    finally:
        we_source.flush_cache(rating)
        with _FILL_LOCK:
            _FILL.setdefault(rating, {})["running"] = False


def we_item(item_id):
    rec = WE_ITEMS.get(item_id)
    if rec is None:
        we_records()
        rec = WE_ITEMS.get(item_id)
    return rec


# ---------------------------------------------------------------- helpers ---

def cloudkit_lookup(uuid):
    body = json.dumps({"records": [{"recordName": uuid}]}).encode()
    req = urllib.request.Request(LOOKUP_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    records = payload.get("records") or []
    if not records or "fields" not in records[0]:
        raise RuntimeError("record not found (server said: %s)"
                           % json.dumps(payload)[:200])
    return records[0]["fields"]


def field(fields, name, default=""):
    v = fields.get(name)
    if isinstance(v, dict):
        v = v.get("value")
    return v if v is not None else default


def asset_url(fields, name):
    v = field(fields, name, None)
    if isinstance(v, dict) and v.get("downloadURL"):
        return v["downloadURL"].replace("${f}", "asset.bin")
    return None


def download_to(url, path, timeout=300, on_progress=None):
    with urllib.request.urlopen(url, timeout=timeout) as src, open(path, "wb") as out:
        total = int(src.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if on_progress:
                on_progress(done, total, "downloading")


def safe_name(title):
    s = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]", " ", str(title))
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s or "wallpaper"


def unique_path(directory, base, ext):
    path = os.path.join(directory, "%s.%s" % (base, ext))
    n = 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(directory, "%s (%d).%s" % (base, n, ext))
    return path


def fetch_thumbnail(uuid):
    """Returns (path, created)."""
    path = os.path.join(THUMB_DIR, uuid + ".jpg")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path, False
    url = asset_url(cloudkit_lookup(uuid), "videoScreenshotImage")
    if not url:
        return None, False
    tmp = path + ".part"
    download_to(url, tmp, timeout=60)
    os.replace(tmp, path)
    return path, True


_DL_LOCKS = {}
_DL_LOCKS_GUARD = threading.Lock()


def decrypt_asset(src, dst):
    """AES-128-ECB decrypt an iWallpaper asset (key "NSData" + zero pad)."""
    ossl = shutil.which("openssl")
    if ossl:
        proc = subprocess.run(
            [ossl, "enc", "-aes-128-ecb", "-d", "-K", AES_KEY_HEX,
             "-in", src, "-out", dst],
            capture_output=True)
        return proc.returncode == 0
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise RuntimeError(
            "openssl not found and the `cryptography` package is not installed. "
            "Install OpenSSL (Windows: https://slproweb.com/products/Win32OpenSSL.html) "
            "or run: pip install cryptography")
    key = bytes.fromhex(AES_KEY_HEX)
    with open(src, "rb") as fh:
        data = fh.read()
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    out = dec.update(data) + dec.finalize()
    pad = out[-1]
    if 0 < pad <= 16:
        out = out[:-pad]
    with open(dst, "wb") as fh:
        fh.write(out)
    return True


def reveal_in_file_manager(path):
    if sys.platform == "darwin":
        reveal_in_file_manager(path)
    elif os.name == "nt":
        subprocess.run(["explorer", "/select,", os.path.normpath(path)],
                       check=False)
    else:
        subprocess.run(["xdg-open", os.path.dirname(path)], check=False)


def fetch_video(uuid):
    """Fetch + decrypt a wallpaper; returns (path, title, cached)."""
    with _DL_LOCKS_GUARD:
        lock = _DL_LOCKS.setdefault(uuid, threading.Lock())
    with lock:
        fields = cloudkit_lookup(uuid)
        title = str(field(fields, "tagsStr") or uuid).strip()
        base = safe_name(title)
        ext = str(field(fields, "videoExtension") or "mp4").lower()
        aes = field(fields, "correctAES", 0)
        aes = int(aes) == 1 if isinstance(aes, (int, float)) else False
        want_md5 = str(field(fields, "videoMd5") or "")
        url = asset_url(fields, "videoData")
        if not url:
            raise RuntimeError("record has no videoData URL")

        root = ensure_download_root()
        first = os.path.join(root, "%s.%s" % (base, ext))
        if os.path.exists(first):
            return first, title, True

        target = unique_path(root, base, ext)
        tmp_asset = target + ".part"
        set_progress("iw", uuid, 0, None, "downloading")
        try:
            download_to(url, tmp_asset,
                        on_progress=lambda d, t, ph:
                            set_progress("iw", uuid, d, t, ph))
            if aes:
                set_progress("iw", uuid, None, None, "decrypting")
                ok = decrypt_asset(tmp_asset, target)
                os.remove(tmp_asset)
                if not ok:
                    raise RuntimeError("decrypt failed")
            else:
                os.replace(tmp_asset, target)
        except Exception:
            if os.path.exists(tmp_asset):
                os.remove(tmp_asset)
            raise

        if want_md5:
            with open(target, "rb") as fh:
                got = hashlib.md5(fh.read()).hexdigest()
            if got != want_md5:
                print("warning: md5 mismatch for %s (got %s, want %s)"
                      % (uuid, got, want_md5), file=sys.stderr)
        set_progress("iw", uuid, None, None, "done")
        return target, title, False


# ------------------------------------------------------------------ page ----

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GetWallpaper</title>
<style>
  :root { --bg:#0f0f12; --card:#1a1a1f; --line:#2a2a31; --txt:#e8e8ea; --dim:#9a9aa2; --yellow:#ffd400; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font:14px/1.4 -apple-system, "SF Pro Text", system-ui, sans-serif; }
  header { position:sticky; top:0; z-index:10; display:flex; gap:12px; align-items:center;
           padding:12px 16px; background:rgba(15,15,18,.92); backdrop-filter:blur(8px);
           border-bottom:1px solid var(--line); }
  header h1 { font-size:16px; margin:0; font-weight:600; white-space:nowrap; }
  #q { flex:1; max-width:520px; padding:8px 12px; border-radius:8px; border:1px solid var(--line);
       background:#000; color:var(--txt); outline:none; }
  #q:focus { border-color:var(--yellow); }
  .hidden { display:none; }
  #morebar { display:flex; justify-content:center; padding:18px 0 40px;
             color:var(--dim); font-size:12px; min-height:20px; }
  #count { color:var(--dim); font-size:12px; white-space:nowrap; }
  #src { display:flex; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
  #src button { border:0; background:transparent; color:var(--dim); padding:7px 12px;
                font:600 12px/1 -apple-system, "SF Pro Text", system-ui, sans-serif;
                cursor:pointer; white-space:nowrap; }
  #src button.on { background:var(--yellow); color:#111; }
  #src button:not(.on):hover { color:var(--txt); }
  #settings { margin-left:auto; width:34px; height:34px; border-radius:50%;
              border:1px solid var(--line); background:#000; color:var(--dim);
              display:flex; align-items:center; justify-content:center;
              cursor:pointer; flex:none; }
  #settings:hover { color:var(--txt); border-color:var(--yellow); }
  #settings svg { width:17px; height:17px; }
  #rating { background:#000; color:var(--txt); border:1px solid var(--line);
            border-radius:8px; padding:7px 8px; outline:none;
            font:600 12px/1 -apple-system, "SF Pro Text", system-ui, sans-serif; }
  #rating:focus { border-color:var(--yellow); }
  main { padding:16px; }
  #grid { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:0;
          border-top:1px solid var(--line); border-left:1px solid var(--line); }
  @media (max-width: 760px) { #grid { grid-template-columns:repeat(2, minmax(0,1fr)); } }
  .card { position:relative; overflow:hidden; background:var(--card);
          cursor:zoom-in; aspect-ratio:16/9;
          border-right:1px solid var(--line); border-bottom:1px solid var(--line);
          content-visibility:auto; contain-intrinsic-size:auto 220px; }
  .card img { width:100%; height:100%; object-fit:cover; display:block; background:#000; }
  .card .meta { position:absolute; left:0; right:0; bottom:0; padding:22px 10px 8px; z-index:2;
                background:linear-gradient(transparent, rgba(0,0,0,.85));
                display:flex; flex-direction:column; gap:1px; pointer-events:none; }
  .card .t { font-size:12.5px; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .card .s { font-size:11px; color:var(--dim); }
  .cover { position:absolute; inset:0; z-index:4; display:flex; align-items:center;
           justify-content:center; flex-direction:column; gap:6px;
           opacity:0; pointer-events:none;
           transition:opacity .12s, background .12s; }
  .cover span { display:none; }
  .cover span.pct { display:block;
                    font:600 12px/1.2 -apple-system, "SF Pro Text", system-ui, sans-serif; }
  .cover span.pct:empty { display:none; }
  .cover svg { width:42px; height:42px; }
  .card:not(.downloaded):not(.busy):not(.err):hover .cover,
  .card.busy .cover,
  .card.downloaded .cover,
  .card.err .cover { opacity:1; pointer-events:auto; }
  .card:not(.downloaded):not(.busy):not(.err):hover .cover,
  .card.busy .cover { background:var(--yellow); color:#111; }
  .card:not(.downloaded):not(.busy):not(.err):hover .cover span.dl { display:block; }
  .card.busy .cover span.spin { display:block; }
  .card.downloaded .cover { background:#2ecc71; color:#06270f; cursor:pointer; }
  .card.downloaded .cover span.ok { display:block; }
  .card.downloaded:hover .cover { background:#3b82f6; color:#fff; }
  .card.downloaded:hover .cover span.ok { display:none; }
  .card.downloaded:hover .cover span.folder { display:block; }
  .card.err .cover { background:#e5484d; color:#fff; }
  .card.err .cover span.err { display:block; }
  .cover .ring { width:36px; height:36px; border-radius:50%;
                 border:4px solid rgba(0,0,0,.18); border-top-color:#111;
                 animation:spin .7s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .card.broken img { visibility:hidden; }
  .card.broken::after { content:"no thumbnail"; position:absolute; inset:0; display:flex;
                        align-items:center; justify-content:center; color:var(--dim); font-size:12px; }
  #lb { position:fixed; inset:0; z-index:50; background:rgba(0,0,0,.88);
        display:flex; align-items:center; justify-content:center; }
  #lb.hidden { display:none; }
  #lb img { max-width:92vw; max-height:88vh; border-radius:8px; box-shadow:0 10px 60px rgba(0,0,0,.7); }
  #lbclose { position:absolute; top:18px; right:22px; width:46px; height:46px; border-radius:50%;
             border:0; background:rgba(255,255,255,.12); color:#fff; font-size:26px; cursor:pointer; }
  #lbclose:hover { background:rgba(255,255,255,.25); }
  #cfg { position:fixed; inset:0; z-index:60; background:rgba(0,0,0,.75);
         display:flex; align-items:center; justify-content:center; }
  #cfg.hidden { display:none; }
  #cfg .sheet { width:min(560px, 92vw); background:var(--card);
                border:1px solid var(--line); border-radius:12px;
                padding:18px 20px; display:flex; flex-direction:column; gap:10px; }
  #cfg h2 { margin:0; font-size:15px; }
  #cfg input[type=text], #cfg input[type=number] { width:100%; padding:10px 12px;
               border-radius:8px; border:1px solid var(--line); background:#000;
               color:var(--txt); outline:none; font-size:13px; }
  #cfg input:focus { border-color:var(--yellow); }
  #cfg .field { display:flex; flex-direction:column; gap:5px;
                color:var(--dim); font-size:11.5px; }
  #cfg .check { display:flex; align-items:center; gap:8px; color:var(--txt);
                font-size:12.5px; }
  #cfg .check input { accent-color: var(--yellow); }
  #cfg .hint { margin:0; color:var(--dim); font-size:11.5px; word-break:break-all; }
  #cfg .row { display:flex; justify-content:flex-end; gap:8px; margin-top:4px; }
  #cfg button { border:1px solid var(--line); background:transparent; color:var(--txt);
                padding:8px 14px; border-radius:8px; font-size:12px; font-weight:600;
                cursor:pointer; }
  #cfg button.primary { background:var(--yellow); color:#111; border-color:var(--yellow); }
</style>
</head>
<body>
<header>
  <h1>GetWallpaper</h1>
  <div id="src">
    <button data-src="iw" class="on">iWallpaper</button>
    <button data-src="we">Wallpaper Engine</button>
  </div>
  <select id="rating" class="hidden" title="Workshop content rating">
    <option>Everyone</option>
    <option>Questionable</option>
    <option>Mature</option>
    <option>All</option>
  </select>
  <input id="q" type="search" placeholder='Search — commas mean AND: "anime, girls, raiden"' autocomplete="off">
  <span id="count"></span>
  <button id="settings" title="Download folder">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09A1.7 1.7 0 0 0 9 19.4a1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09A1.7 1.7 0 0 0 15 4.6a1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 19.4 9a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.51 1z"/></svg>
  </button>
</header>
<main><div id="grid"></div></main>
<footer id="morebar" class="hidden"><span id="morelabel"></span></footer>
<div id="lb" class="hidden">
  <button id="lbclose" title="Close">&times;</button>
  <img id="lbimg" alt="">
</div>
<div id="cfg" class="hidden">
  <div class="sheet">
    <h2>Settings</h2>
    <label class="field"><span>Download folder</span>
      <input id="dlroot" type="text" spellcheck="false" autocomplete="off"></label>
    <p class="hint" id="dlhint"></p>
    <label class="field"><span>Thumbnail cache limit (MB)</span>
      <input id="dlcap" type="number" min="64" step="64" value="1024"></label>
    <label class="check"><input id="dlprefill" type="checkbox">
      Prefill thumbnails (fills the cache automatically)</label>
    <p class="hint" id="dlcache"></p>
    <p class="hint" id="dlpf"></p>
    <div class="row">
      <button id="dlcancel">Cancel</button>
      <button id="dlsave" class="primary">Save</button>
    </div>
  </div>
</div>
<script>
const grid = document.getElementById('grid');
const countEl = document.getElementById('count');
const q = document.getElementById('q');
const lb = document.getElementById('lb');
const lbimg = document.getElementById('lbimg');
const cfg = document.getElementById('cfg');
const dlroot = document.getElementById('dlroot');
const dlcap = document.getElementById('dlcap');
const dlprefill = document.getElementById('dlprefill');
const dlhint = document.getElementById('dlhint');
const dlcache = document.getElementById('dlcache');
const dlpf = document.getElementById('dlpf');
let cfgTimer = null;
const settingsBtn = document.getElementById('settings');
const moreBar = document.getElementById('morebar');
const moreLabel = document.getElementById('morelabel');
const ratingEl = document.getElementById('rating');
let records = [];
let source = 'iw';
let weExhausted = false;
let weLoading = false;
let weInitialLoading = false;
let fillTimer = null;
let fillStatus = { running: false, total: 0, exhausted: false };
let filterBurst = 0;            // consecutive auto-batches while searching
const FILTER_BURST_MAX = 5;     // then wait for another scroll/wheel gesture
let searchTimer = null;
const CHUNK = 120;              // cards rendered per batch
let filtered = [];
let visibleCount = 0;
const srcBtns = document.querySelectorAll('#src button');

const ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 21h16"/></svg>';
const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg>';
const CROSS = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M5 5l14 14M19 5L5 19"/></svg>';
const FOLDER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const SPIN = '<span class="spin"></span>';

// Load thumbnails visible first: a tight band assigns src (high priority),
// while a wider band keeps already-loaded images around before dropping them.
const pendingLoads = new Set();
let settleTimer = null;
let lastScrollTs = 0;

window.addEventListener('scroll', () => { lastScrollTs = Date.now(); }, { passive: true });

function assignSrc(img) {
  if (img.getAttribute('src')) return;
  img.fetchPriority = 'high';
  img.src = img.dataset.src;
}

function scheduleSettle() {
  if (settleTimer) clearTimeout(settleTimer);
  settleTimer = setTimeout(() => {
    settleTimer = null;
    for (const img of [...pendingLoads]) {
      pendingLoads.delete(img);
      if (!img.isConnected) continue;
      const r = img.getBoundingClientRect();
      if (r.bottom > -600 && r.top < window.innerHeight + 600) assignSrc(img);
    }
  }, 250);
}

const loadIO = new IntersectionObserver(entries => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    const img = e.target;
    if (Date.now() - lastScrollTs < 200) {
      // scrolling: defer so stale tiles don't queue ahead of what you stop on
      pendingLoads.add(img);
      scheduleSettle();
    } else {
      assignSrc(img);
    }
  }
}, { rootMargin: '600px 0px' });

const dropIO = new IntersectionObserver(entries => {
  for (const e of entries) {
    const img = e.target;
    if (!e.isIntersecting && img.getAttribute('src')) {
      img.removeAttribute('src');
      img.fetchPriority = 'low';
    }
  }
}, { rootMargin: '2500px 0px' });

// Infinite scroll for the Wallpaper Engine source: when the sentinel at the
// bottom of the grid approaches the viewport, fetch the next batch of pages.
const moreIO = new IntersectionObserver(
  entries => { if (entries.some(e => e.isIntersecting)) maybeLoadMore(); },
  { rootMargin: '900px 0px' });
moreIO.observe(moreBar);

srcBtns.forEach(b => b.addEventListener('click', () => setSource(b.dataset.src)));
ratingEl.addEventListener('change', () => {
  if (source !== 'we') return;
  weExhausted = false;
  weLoading = false;
  filterBurst = 0;
  load('we');
});
q.addEventListener('input', () => {
  filterBurst = 0;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(rebuildGrid, 150);
});

let growScheduled = false;
function onScroll() {
  if (growScheduled) return;
  growScheduled = true;
  requestAnimationFrame(() => {
    growScheduled = false;
    if (!nearPageBottom()) return;
    filterBurst = 0;
    if (!growGrid()) maybeLoadMore();
  });
}
window.addEventListener('scroll', onScroll, { passive: true });
window.addEventListener('wheel', () => {
  if (source === 'we') filterBurst = 0;
}, { passive: true });

async function load(src) {
  weInitialLoading = true;
  countEl.textContent = 'loading…';
  grid.textContent = '';
  try {
    const url = src === 'we'
      ? '/api/we/records?rating=' + encodeURIComponent(ratingEl.value)
      : '/api/records';
    const res = await fetch(url);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    records = data;
    render();
  } catch (err) {
    records = [];
    countEl.textContent = 'failed: ' + err.message;
  }
  weInitialLoading = false;
  updateMore();
  if (src === 'we') startFill();
  else stopFillPoll();
}

function setSource(src) {
  source = src;
  weExhausted = false;
  weLoading = false;
  filterBurst = 0;
  stopFillPoll();
  fillStatus = { running: false, total: 0, exhausted: false };
  ratingEl.classList.toggle('hidden', src !== 'we');
  srcBtns.forEach(b => b.classList.toggle('on', b.dataset.src === src));
  q.value = '';
  load(src);
}

function updateMore() {
  const show = source === 'we' && records.length > 0 &&
               (!weExhausted || fillStatus.running);
  moreBar.classList.toggle('hidden', !show);
  if (weLoading) moreLabel.textContent = 'Loading more…';
  else if (fillStatus.running)
    moreLabel.textContent = 'Indexing catalog… ' +
      records.length.toLocaleString() + ' items';
  else moreLabel.textContent = '';
}

function stopFillPoll() {
  if (fillTimer) { clearInterval(fillTimer); fillTimer = null; }
}

async function startFill() {
  stopFillPoll();
  fillStatus = { running: true, total: records.length, exhausted: false };
  try {
    await fetch('/api/we/fill', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rating: ratingEl.value })
    });
  } catch (e) {
    fillStatus.running = false;
  }
  updateMore();
  fillTimer = setInterval(pollFill, 1500);
  pollFill();
}

async function pollFill() {
  if (source !== 'we') { stopFillPoll(); return; }
  try {
    const res = await fetch('/api/we/fill?rating=' +
      encodeURIComponent(ratingEl.value));
    if (!res.ok) return;
    fillStatus = await res.json();
    if (fillStatus.total > records.length) {
      const res2 = await fetch('/api/we/records?rating=' +
        encodeURIComponent(ratingEl.value) + '&offset=' + records.length);
      if (res2.ok) {
        const j = await res2.json();
        if (j.items && j.items.length) {
          records = records.concat(j.items);
          computeFiltered();
        }
      }
    }
    updateMore();
    if (!fillStatus.running) stopFillPoll();
  } catch (e) { /* next tick */ }
}

function nearBottom() {
  return moreBar.getBoundingClientRect().top < window.innerHeight + 900;
}

function maybeLoadMore() {
  if (source !== 'we' || weExhausted || weLoading || weInitialLoading) return;
  if (visibleCount < filtered.length) return;   // still have local cards to show
  if (q.value.trim() && filterBurst >= FILTER_BURST_MAX) return;
  loadMore();
}

async function loadMore() {
  if (weLoading || source !== 'we' || weExhausted) return;
  weLoading = true;
  updateMore();
  try {
    const res = await fetch('/api/we/more?rating=' + encodeURIComponent(ratingEl.value));
    const j = await res.json();
    if (!res.ok) throw new Error(j.error || ('HTTP ' + res.status));
    records = j.reset ? j.items : records.concat(j.items);
    weExhausted = !!j.exhausted;
    filterBurst = q.value.trim() ? filterBurst + 1 : 0;
    computeFiltered();
    if (nearPageBottom()) growGrid();
  } catch (err) {
    console.warn('auto load failed:', err);
    weLoading = false;
    moreLabel.textContent = 'Load failed — retrying…';
    setTimeout(maybeLoadMore, 5000);
    return;
  }
  weLoading = false;
  updateMore();
  if (!weExhausted && nearBottom()) setTimeout(maybeLoadMore, 250);
}

load('iw');

const norm = s => String(s).normalize('NFD')
  .replace(/[\u0300-\u036f]/g, '').toLowerCase();

function parseQuery() {
  return q.value.split(',')
    .map(s => norm(s.trim()).replace(/\s+/g, ' '))
    .filter(Boolean);
}

function render() {
  rebuildGrid();
}

function computeFiltered() {
  const terms = parseQuery();
  filtered = [];
  for (const r of records) {
    if (r._hay === undefined) r._hay = norm(r.title + ' ' + (r.tags || ''));
    if (!terms.length || terms.every(t => r._hay.includes(t))) filtered.push(r);
  }
  countEl.textContent = filtered.length + ' / ' + records.length;
}

function appendRange(from, to) {
  const frag = document.createDocumentFragment();
  for (let i = from; i < to; i++) frag.appendChild(card(filtered[i]));
  grid.appendChild(frag);
}

function rebuildGrid() {
  computeFiltered();
  visibleCount = Math.min(CHUNK, filtered.length);
  grid.textContent = '';
  appendRange(0, visibleCount);
  updateMore();
}

function growGrid() {
  if (visibleCount >= filtered.length) return false;
  const to = Math.min(visibleCount + CHUNK, filtered.length);
  appendRange(visibleCount, to);
  visibleCount = to;
  return true;
}

function nearPageBottom() {
  return window.innerHeight + window.scrollY >=
         document.documentElement.scrollHeight - 1400;
}

function card(r) {
  const el = document.createElement('div');
  el.className = 'card' + (r.downloaded ? ' downloaded' : '');
  el.dataset.id = r.id;
  if (activeDownloads.has((r.api || '') + '/' + r.id)) el.classList.add('busy');
  if (r.downloading && !r.downloaded) {
    el.classList.add('busy');
    startProgress(r.api || '', r.id);
  }
  el.innerHTML =
    '<img loading="lazy" alt="">' +
    '<div class="meta"><span class="t"></span><span class="s"></span></div>' +
    '<div class="cover">' +
      '<span class="dl">' + ICON + '</span>' +
      '<span class="spin"><div class="ring"></div></span>' +
      '<span class="pct"></span>' +
      '<span class="ok">' + CHECK + '</span>' +
      '<span class="folder">' + FOLDER + '</span>' +
      '<span class="err">' + CROSS + '</span>' +
    '</div>';
  const img = el.querySelector('img');
  img.dataset.src = (r.api || '') + '/thumb/' + r.id;
  img.alt = r.title;
  img.addEventListener('click', () => openLightbox(r));
  img.addEventListener('error', () => el.classList.add('broken'));
  loadIO.observe(img);
  dropIO.observe(img);
  el.querySelector('.t').textContent = r.title;
  el.querySelector('.s').textContent = r.meta;
  el.querySelector('.cover').addEventListener('click', e => {
    e.stopPropagation();
    if (el.classList.contains('busy')) return;
    if (el.classList.contains('downloaded')) { reveal(r.id, r.api || '', el); return; }
    download(r.id, el, r.api || '');
  });
  return el;
}

const activeDownloads = new Map();  // key -> {api, id, stopped}

function pctElFor(api, id) {
  return document.querySelector('.card[data-id="' + id + '"] .pct');
}

function cardElFor(api, id) {
  return document.querySelector('.card[data-id="' + id + '"]');
}

function startProgress(api, id) {
  const kind = api === '/we' ? 'we' : 'iw';
  const key = (api || '') + '/' + id;
  if (activeDownloads.has(key)) return () => {};
  const state = { api, id, stopped: false };
  activeDownloads.set(key, state);

  function finish(good) {
    state.stopped = true;
    activeDownloads.delete(key);
    const el = cardElFor(api, id);
    if (!el) return;
    const pct = el.querySelector('.pct');
    if (pct) pct.textContent = '';
    if (good === true) {
      el.classList.remove('busy', 'err');
      el.classList.add('downloaded');
    } else if (good === false) {
      el.classList.remove('busy');
      el.classList.add('err');
      setTimeout(() => el.classList.remove('err'), 2500);
    }
  }

  const tick = async () => {
    if (state.stopped) return;
    const pctEl = pctElFor(api, id);
    if (pctEl) {
      try {
        const r = await fetch('/progress/' + kind + '/' + id);
        if (r.ok) {
          const p = await r.json();
          if (p.phase === 'done') {
            const rec = records.find(x => x.id === id && (x.api || '') === api);
            if (rec) rec.downloaded = true;
            finish(true);
            return;
          }
          if (p.phase === 'error') { finish(false); return; }
          if (p.phase === 'decrypting') pctEl.textContent = 'Decrypting…';
          else if (p.phase === 'copying')
            pctEl.textContent = p.pct != null ? 'Copying ' + p.pct + '%' : 'Copying…';
          else if (p.phase === 'downloading')
            pctEl.textContent = p.pct != null ? p.pct + '%' : 'Downloading…';
        }
      } catch (e) { /* transient */ }
    }
    if (!state.stopped) setTimeout(tick, 700);
  };
  tick();
  return () => finish(undefined);
}

async function download(id, cardEl, api) {
  if (cardEl.classList.contains('busy') || cardEl.classList.contains('downloaded')) return;
  cardEl.classList.remove('err');
  cardEl.classList.add('busy');
  const stopPoll = startProgress(api, id);
  try {
    const res = await fetch(api + '/download/' + id);
    const j = await res.json();
    if (!res.ok || !j.ok) throw new Error(j.error || ('HTTP ' + res.status));
    const rec = records.find(x => x.id === id && (x.api || '') === api);
    if (rec) rec.downloaded = true;
    cardEl.classList.remove('busy');
    cardEl.classList.add('downloaded');
  } catch (err) {
    cardEl.classList.remove('busy');
    cardEl.classList.add('err');
    cardEl.title = 'Failed: ' + err;
    setTimeout(() => cardEl.classList.remove('err'), 2500);
  } finally {
    stopPoll();
  }
}

async function reveal(id, api, cardEl) {
  try {
    const res = await fetch(api + '/reveal/' + id);
    if (res.ok) return;
    if (res.status === 404) {
      // the file was deleted outside the app: make the tile downloadable again
      cardEl.classList.remove('downloaded');
      return;
    }
    console.warn('reveal failed:', await res.text());
  } catch (err) {
    console.warn('reveal failed:', err);
  }
}

function openLightbox(r) {
  lbimg.src = '/thumb/' + r.id;
  lb.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}
function closeLightbox() {
  lb.classList.add('hidden');
  lbimg.src = '';
  document.body.style.overflow = '';
}
document.getElementById('lbclose').addEventListener('click', closeLightbox);
lb.addEventListener('click', e => { if (e.target === lb) closeLightbox(); });
settingsBtn.addEventListener('click', openSettings);
document.getElementById('dlcancel').addEventListener('click', closeSettings);
document.getElementById('dlsave').addEventListener('click', saveSettings);
cfg.addEventListener('click', e => { if (e.target === cfg) closeSettings(); });
dlroot.addEventListener('keydown', e => { if (e.key === 'Enter') saveSettings(); });
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (!cfg.classList.contains('hidden')) { closeSettings(); return; }
  closeLightbox();
});

function closeSettings() {
  cfg.classList.add('hidden');
  if (cfgTimer) { clearInterval(cfgTimer); cfgTimer = null; }
}

function updateCacheLines(j) {
  const c = j.thumb_cache || { iw: { mb: 0, count: 0 }, we: { mb: 0, count: 0 } };
  dlcache.textContent = 'Cache used: iWallpaper ' + c.iw.mb + ' MB (' +
    c.iw.count + ' files) · Workshop ' + c.we.mb + ' MB (' + c.we.count +
    ' files) — limit ' + j.thumb_cap_mb + ' MB per source';
  const p = j.prefill || {};
  dlpf.textContent = p.running
    ? 'Prefilling ' + p.source + '… ' + (p.done || 0).toLocaleString() +
      ' / ' + (p.total || 0).toLocaleString()
    : (p.error ? 'Prefill error: ' + p.error : '');
}

async function openSettings() {
  try {
    const res = await fetch('/api/settings');
    const j = await res.json();
    dlroot.value = j.download_root || '';
    dlcap.value = j.thumb_cap_mb || 1024;
    dlprefill.checked = !!j.prefill_thumbs;
    dlhint.textContent = 'Default: ' + (j.default_root || '');
    updateCacheLines(j);
  } catch (err) {
    dlhint.textContent = 'Could not read settings: ' + err;
  }
  cfg.classList.remove('hidden');
  dlroot.focus();
  dlroot.select();
  if (cfgTimer) clearInterval(cfgTimer);
  cfgTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/settings');
      if (r.ok) updateCacheLines(await r.json());
    } catch (e) { /* ignore */ }
  }, 1200);
}

async function saveSettings() {
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        download_root: dlroot.value,
        thumb_cap_mb: parseInt(dlcap.value, 10) || 1024,
        prefill_thumbs: dlprefill.checked
      })
    });
    let j = {};
    try { j = await res.json(); } catch (e) { /* ignore */ }
    if (!res.ok || !j.ok) throw new Error(j.error || ('HTTP ' + res.status));
    closeSettings();
    load(source);
  } catch (err) {
    dlhint.textContent = 'Error: ' + err.message;
  }
}
</script>
</body>
</html>
"""


# --------------------------------------------------------------- server -----

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "iWallpaperUI/1.0"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), file=sys.stderr)

    def _send(self, code, body, ctype="text/plain", headers=()):
        if isinstance(body, str):
            body = body.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/api/records":
                with _INDEX_LOCK:
                    done = {u for u, f in INDEX.items()
                            if os.path.exists(f)}
                payload = json.dumps([
                    dict(r,
                         api="",
                         meta="%s · %s MB · %ss · hot %s"
                              % (r["res"], r["mb"], r["dur"], r["hot"]),
                         downloaded=r["id"] in done,
                         downloading=is_downloading("iw", r["id"]))
                    for r in RECORDS])
                self._send(200, payload, "application/json",
                           [("Cache-Control", "no-store")])
            elif path == "/api/we/records":
                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query)
                refresh = query.get("refresh", ["0"])[0] == "1"
                rating = query.get("rating", ["Everyone"])[0]
                offset = query.get("offset", [None])[0]
                try:
                    if offset is not None:
                        try:
                            off = int(offset)
                        except ValueError:
                            off = 0
                        payload = json.dumps(we_records_slice(off, rating))
                    else:
                        payload = json.dumps(we_records(refresh, rating))
                except Exception as exc:
                    self._send(500, json.dumps({"ok": False, "error": str(exc)}),
                               "application/json")
                    return
                self._send(200, payload, "application/json",
                           [("Cache-Control", "no-store")])
            elif path == "/api/we/more":
                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query)
                rating = query.get("rating", ["Everyone"])[0]
                try:
                    payload = json.dumps(we_more(rating=rating))
                except Exception as exc:
                    self._send(500, json.dumps({"ok": False, "error": str(exc)}),
                               "application/json")
                    return
                self._send(200, payload, "application/json",
                           [("Cache-Control", "no-store")])
            elif path == "/api/we/fill":
                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query)
                rating = query.get("rating", ["Everyone"])[0]
                self._send(200, json.dumps(fill_status(rating)),
                           "application/json", [("Cache-Control", "no-store")])
            elif path == "/api/settings":
                self._send(200, json.dumps(settings_payload()),
                           "application/json",
                           [("Cache-Control", "no-store")])
            elif path.startswith("/thumb/"):
                self._thumb(path[len("/thumb/"):])
            elif path.startswith("/download/"):
                self._download(path[len("/download/"):])
            elif path.startswith("/reveal/"):
                self._reveal(path[len("/reveal/"):])
            elif path.startswith("/progress/"):
                self._progress(path[len("/progress/"):])
            elif path.startswith("/we/thumb/"):
                self._we_thumb(path[len("/we/thumb/"):])
            elif path.startswith("/we/download/"):
                self._we_download(path[len("/we/download/"):])
            elif path.startswith("/we/reveal/"):
                self._we_reveal(path[len("/we/reveal/"):])
            elif path == "/favicon.ico":
                self._send(204, b"")
            elif path == "/health":
                self._send(200, '{"ok":true}', "application/json")
            else:
                self._send(404, "not found")
        except Exception as exc:
            self._send(500, json.dumps({"ok": False, "error": str(exc)}),
                       "application/json")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/we/fill":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                rating = payload.get("rating") or "Everyone"
            except ValueError:
                self._send(400, json.dumps({"ok": False}),
                           "application/json")
                return
            self._send(200, json.dumps(start_fill(rating)),
                       "application/json")
            return
        if path != "/api/settings":
            self._send(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            result = update_settings(payload)
        except ValueError as exc:
            self._send(400, json.dumps({"ok": False, "error": str(exc)}),
                       "application/json")
            return
        except OSError as exc:
            self._send(500, json.dumps({"ok": False, "error": str(exc)}),
                       "application/json")
            return
        self._send(200, json.dumps(dict(result, ok=True)),
                   "application/json")

    def _thumb(self, uuid):
        if not UUID_RE.fullmatch(uuid):
            self._send(404, "bad record id")
            return
        _enter_interactive()
        try:
            path, created = fetch_thumbnail(uuid)
        finally:
            _leave_interactive()
        if not path:
            self._send(404, "no thumbnail")
            return
        if created:
            enforce_cache_cap(THUMB_DIR)
        with open(path, "rb") as fh:
            self._send(200, fh.read(), "image/jpeg",
                       [("Cache-Control", "public, max-age=86400")])

    def _download(self, uuid):
        if not UUID_RE.fullmatch(uuid):
            self._send(404, "bad record id")
            return
        set_progress("iw", uuid, 0, None, "downloading")
        try:
            path, title, cached = fetch_video(uuid)
        except Exception as exc:
            set_progress("iw", uuid, None, None, "error")
            print("iWallpaper download failed %s: %s" % (uuid, exc),
                  file=sys.stderr)
            self._send(500, json.dumps({"ok": False, "error": str(exc)}),
                       "application/json")
            return
        set_progress("iw", uuid, None, None, "done")
        mark_downloaded(uuid, path)
        self._send(200, json.dumps({"ok": True, "file": os.path.basename(path),
                                    "title": title, "cached": cached}),
                   "application/json")

    def _reveal(self, uuid):
        if not UUID_RE.fullmatch(uuid):
            self._send(404, json.dumps({"ok": False, "error": "bad record id"}),
                       "application/json")
            return
        with _INDEX_LOCK:
            fname = INDEX.get(uuid)
        path = fname if fname and os.path.isabs(fname) else None
        if not path or not os.path.exists(path):
            self._send(404, json.dumps({"ok": False, "error": "file not found"}),
                       "application/json")
            return
        reveal_in_file_manager(path)
        self._send(200, json.dumps({"ok": True, "file": os.path.basename(path)}),
                   "application/json")

    # --- Wallpaper Engine ---------------------------------------------------

    def _we_thumb(self, item_id):
        if not re.fullmatch(r"\d{1,20}", item_id):
            self._send(404, "bad item id")
            return
        rec = we_item(item_id)
        _enter_interactive()
        try:
            path, created = we_source.thumbnail(item_id,
                                                rec["preview"] if rec else "")
        finally:
            _leave_interactive()
        if not path:
            self._send(404, "no thumbnail")
            return
        if created:
            enforce_cache_cap(we_source.THUMB_DIR)
        with open(path, "rb") as fh:
            self._send(200, fh.read(), we_source.thumb_type(path),
                       [("Cache-Control", "public, max-age=604800")])

    def _we_download(self, item_id):
        if not re.fullmatch(r"\d{1,20}", item_id):
            self._send(404, "bad item id")
            return
        rec = we_item(item_id)
        title = rec["title"] if rec else item_id
        expected = int(rec.get("bytes") or 0) if rec else 0
        if not expected and rec:
            expected = int((rec.get("mb") or 0) * 1e6)
        set_progress("we", item_id, 0, None, "downloading")
        try:
            path, via, cached = we_source.download(
                item_id, title, expected_bytes=expected,
                dest_dir=ensure_download_root(),
                on_progress=lambda d, t, ph:
                    set_progress("we", item_id, d, t, ph))
        except Exception as exc:
            set_progress("we", item_id, None, None, "error")
            print("WE download failed %s: %s" % (item_id, exc), file=sys.stderr)
            self._send(500, json.dumps({"ok": False, "error": str(exc)}),
                       "application/json")
            return
        set_progress("we", item_id, None, None, "done")
        we_mark_downloaded(item_id, path)
        self._send(200, json.dumps({"ok": True, "file": os.path.basename(path),
                                    "via": via, "cached": cached}),
                   "application/json")

    def _we_reveal(self, item_id):
        if not re.fullmatch(r"\d{1,20}", item_id):
            self._send(404, json.dumps({"ok": False, "error": "bad item id"}),
                       "application/json")
            return
        with _WE_INDEX_LOCK:
            fname = WE_INDEX.get(item_id)
        path = fname if fname and os.path.isabs(fname) else None
        if not path or not os.path.exists(path):
            self._send(404, json.dumps({"ok": False, "error": "file not found"}),
                       "application/json")
            return
        reveal_in_file_manager(path)
        self._send(200, json.dumps({"ok": True, "file": os.path.basename(path)}),
                   "application/json")

    def _progress(self, rest):
        parts = rest.split("/", 1)
        if len(parts) != 2:
            self._send(404, json.dumps({"ok": False}),
                       "application/json")
            return
        kind, item_id = parts
        if kind not in ("iw", "we") or not re.fullmatch(r"[0-9A-Za-z-]{1,64}",
                                                        item_id):
            self._send(404, json.dumps({"ok": False}),
                       "application/json")
            return
        self._send(200, json.dumps(get_progress(kind, item_id)),
                   "application/json", [("Cache-Control", "no-store")])


def start(port=8000, open_browser=True):
    enforce_cache_cap(THUMB_DIR, THUMB_CAP)
    enforce_cache_cap(we_source.THUMB_DIR, THUMB_CAP)
    if SETTINGS.get("prefill_thumbs"):
        start_prefill()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    print("GetWallpaper: %s" % url)
    print("state dir:    %s" % STATE_DIR)
    print("downloads:    %s" % DOWNLOAD_ROOT)
    print("Ctrl-C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    start(port)


if __name__ == "__main__":
    main()
