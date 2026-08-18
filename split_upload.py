#!/usr/bin/env python3
"""
Download a large file (or video/m3u8 stream), split it into parts, and upload
each part as an asset on a GitHub Release in this same repo.

Supports two download modes:
  1. Direct HTTP download  – for plain file URLs (with optional parallel ranges)
  2. yt-dlp download        – automatically used for m3u8 streams and video
                              platform URLs (YouTube, Vimeo, Twitch, Twitter/X,
                              TikTok, Facebook, Instagram, Dailymotion, …)
                              yt-dlp merges all HLS/DASH segments into a single
                              mp4 before splitting begins.

Requires a GitHub token with `contents: write` permission on this repo.
In a GitHub Actions workflow, the built-in ${{ secrets.GITHUB_TOKEN }}
already has this by default – no extra secret needed.

Usage:
    python3 split_upload.py <file_url> [chunk_size_mb] [upload_workers] [download_connections]

Required environment variables:
    GITHUB_TOKEN         – a token with contents:write on the target repo
    GITHUB_REPOSITORY   – "owner/repo" (GitHub Actions sets this automatically)

Optional environment variables:
    YTDLP_FORMAT        – yt-dlp -f format string (default: "bestvideo+bestaudio/best")
    YTDLP_OUTPUT_EXT    – merge output extension (default: "mp4")
"""

import sys
import os
import time
import glob
import shutil
import threading
import subprocess
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs

# ── constants ─────────────────────────────────────────────────────────────────

TEMP_DIR   = "upload_parts"
YTDLP_DIR  = "ytdlp_download"
LINKS_FILE = "direct_links.txt"

GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")   # "owner/repo"
GITHUB_API        = "https://api.github.com"

YTDLP_FORMAT       = os.environ.get("YTDLP_FORMAT",       "bestvideo+bestaudio/best")
YTDLP_OUTPUT_EXT   = os.environ.get("YTDLP_OUTPUT_EXT",   "mp4")
YTDLP_COOKIES_B64  = os.environ.get("YTDLP_COOKIES_B64",  "")
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "")
YTDLP_EXTRA_ARGS   = os.environ.get("YTDLP_EXTRA_ARGS",   "")
YTDLP_COOKIES_TMP  = "ytdlp_cookies.txt"

YTDLP_DOMAINS = {
    "youtube.com", "youtu.be",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv",
    "twitter.com", "x.com",
    "facebook.com", "fb.watch",
    "instagram.com",
    "tiktok.com",
    "streamtape.com",
    "doodstream.com", "dood.watch",
    "vidlox.me",
    "ok.ru",
    "rutube.ru",
}

# ── HTTP sessions ──────────────────────────────────────────────────────────────

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    )
})

gh_session = requests.Session()
gh_session.headers.update({
    "Authorization":        f"Bearer {GITHUB_TOKEN}",
    "Accept":               "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})

# ── shared state ──────────────────────────────────────────────────────────────

_upload_lock   = threading.Lock()
_progress_lock = threading.Lock()
_total_downloaded   = [0]
_last_progress_print = [0]
PROGRESS_EVERY_BYTES = 10 * 1024 * 1024   # print every 10 MB
_release = {}                               # filled once by ensure_release()


# ── helpers ───────────────────────────────────────────────────────────────────

def report_download_progress(nbytes: int) -> None:
    with _progress_lock:
        _total_downloaded[0] += nbytes
        if _total_downloaded[0] - _last_progress_print[0] >= PROGRESS_EVERY_BYTES:
            print(
                f"[download] {_total_downloaded[0] / (1024*1024):.1f} MB downloaded so far…",
                flush=True,
            )
            _last_progress_print[0] = _total_downloaded[0]


def is_ytdlp_url(url: str) -> bool:
    """Return True if this URL should be handled by yt-dlp."""
    url_lower = url.lower()
    if ".m3u8" in url_lower or "m3u8" in url_lower:
        return True
    try:
        host = urlparse(url).hostname or ""
        host = host.removeprefix("www.")
        for domain in YTDLP_DOMAINS:
            if host == domain or host.endswith("." + domain):
                return True
    except Exception:
        pass
    return False


def check_ytdlp() -> None:
    """Install yt-dlp automatically if it is not already on PATH."""
    if shutil.which("yt-dlp") is None:
        print("[yt-dlp] Not found on PATH – installing via pip…", flush=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "yt-dlp"],
            check=True,
        )
        print("[yt-dlp] Installation complete.", flush=True)


# ── GitHub Release helpers ────────────────────────────────────────────────────

def ensure_release() -> dict:
    """Creates a new GitHub Release once and caches its metadata."""
    if _release:
        return _release

    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        raise RuntimeError(
            "GITHUB_TOKEN and GITHUB_REPOSITORY environment variables are required.\n"
            "In GitHub Actions, pass:  GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}"
        )

    tag = f"split-upload-{int(time.time())}"
    payload = {
        "tag_name":   tag,
        "name":       f"Split upload {tag}",
        "body":       "Automatically generated by split_upload.py",
        "draft":      False,
        "prerelease": True,
    }
    r = gh_session.post(
        f"{GITHUB_API}/repos/{GITHUB_REPOSITORY}/releases",
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    _release["id"]         = data["id"]
    _release["upload_url"] = data["upload_url"].split("{")[0]   # strip {?name,label}
    _release["html_url"]   = data["html_url"]
    print(f"[release] Created release {tag}: {_release['html_url']}", flush=True)
    return _release


def upload_asset_bytes(filepath: str, asset_name: str):
    """Upload *filepath* as *asset_name* on the shared release."""
    release = ensure_release()
    start   = time.time()

    with _upload_lock:   # one-at-a-time to avoid 422 name clashes
        with open(filepath, "rb") as f:
            r = gh_session.post(
                release["upload_url"],
                params={"name": asset_name},
                headers={"Content-Type": "application/octet-stream"},
                data=f,
                timeout=(15, 600),
            )
    elapsed = time.time() - start

    if r.status_code in (200, 201):
        link = r.json().get("browser_download_url")
        if link:
            print(f"[timing] Upload of {asset_name} took {elapsed:.0f}s", flush=True)
            return True, link

    return False, f"HTTP {r.status_code} after {elapsed:.0f}s: {r.text[:300]}"


def upload_worker(part_num: int, filepath: str, max_retries: int = 4):
    """Upload one part file; retry on transient failures."""
    print(f"[upload] Starting upload of part {part_num}…", flush=True)
    backoff  = 5.0
    filename = os.path.basename(filepath)

    for attempt in range(1, max_retries + 1):
        try:
            ok, result = upload_asset_bytes(filepath, filename)
            if ok:
                print(f"[done] Part {part_num}: {result}", flush=True)
                if os.path.exists(filepath):
                    os.remove(filepath)
                return part_num, result

            print(f"[warn] Attempt {attempt} for part {part_num} failed: {result}", flush=True)
            if "422" in result or "already_exists" in result.lower():
                new_path = filepath + f".retry{attempt}"
                os.rename(filepath, new_path)
                filepath = new_path
                filename = os.path.basename(filepath)
                continue
        except requests.exceptions.Timeout:
            print(f"[warn] Part {part_num} attempt {attempt}: timed out", flush=True)
        except Exception as exc:
            print(f"[warn] Part {part_num} attempt {attempt}: {exc}", flush=True)

        time.sleep(backoff)
        backoff = min(backoff * 2, 60)

    if os.path.exists(filepath):
        os.remove(filepath)
    return part_num, None


def upload_links_file(path: str, max_retries: int = 4):
    """Upload direct_links.txt as a release asset."""
    print(f"[upload] Uploading {os.path.basename(path)}…", flush=True)
    backoff     = 5.0
    asset_name  = os.path.basename(path)

    for attempt in range(1, max_retries + 1):
        try:
            ok, result = upload_asset_bytes(path, asset_name)
            if ok:
                print(f"[done] {asset_name}: {result}", flush=True)
                return result
            print(f"[warn] Attempt {attempt} for {asset_name}: {result}", flush=True)
            if "422" in result or "already_exists" in result.lower():
                asset_name = f"direct_links_{attempt}.txt"
                continue
        except requests.exceptions.Timeout:
            print(f"[warn] {asset_name} attempt {attempt}: timed out", flush=True)
        except Exception as exc:
            print(f"[warn] {asset_name} attempt {attempt}: {exc}", flush=True)

        time.sleep(backoff)
        backoff = min(backoff * 2, 60)

    return None


# ── yt-dlp download path ──────────────────────────────────────────────────────

def _resolve_cookies_file() -> str:
    """Return a path to a cookies file, or '' if none configured."""
    if YTDLP_COOKIES_B64:
        import base64
        raw = base64.b64decode(YTDLP_COOKIES_B64)
        with open(YTDLP_COOKIES_TMP, "wb") as f:
            f.write(raw)
        print(f"[yt-dlp] Decoded cookies → {YTDLP_COOKIES_TMP}", flush=True)
        return YTDLP_COOKIES_TMP
    if YTDLP_COOKIES_FILE and os.path.isfile(YTDLP_COOKIES_FILE):
        print(f"[yt-dlp] Using cookie file: {YTDLP_COOKIES_FILE}", flush=True)
        return YTDLP_COOKIES_FILE
    return ""


def _build_ytdlp_cmd(url: str, output_template: str, extractor_args: str = "") -> list:
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--format",               YTDLP_FORMAT,
        "--merge-output-format", YTDLP_OUTPUT_EXT,
        "--output",              output_template,
        "--no-warnings",
        "--progress",
        "--retries",              "10",
        "--fragment-retries",    "10",
    ]
    cookies_path = _resolve_cookies_file()
    if cookies_path:
        cmd += ["--cookies", cookies_path]
    if extractor_args:
        cmd += ["--extractor-args", extractor_args]
    if YTDLP_EXTRA_ARGS:
        cmd += YTDLP_EXTRA_ARGS.split()
    cmd.append(url)
    return cmd


def _ytdlp_cleanup_parts() -> None:
    for f in glob.glob(os.path.join(YTDLP_DIR, "*.part")):
        try:
            os.remove(f)
        except OSError:
            pass


def _ytdlp_find_output() -> str:
    """Return the newest non-.part file in YTDLP_DIR, or raise."""
    files = [
        f for f in glob.glob(os.path.join(YTDLP_DIR, "*"))
        if os.path.isfile(f) and not f.endswith(".part")
    ]
    if not files:
        raise RuntimeError(
            f"yt-dlp finished but no output file found in {YTDLP_DIR}."
        )
    return max(files, key=os.path.getmtime)


# ── YouTube URL helpers ───────────────────────────────────────────────────────

def _is_youtube_url(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host.removeprefix("www.") in ("youtube.com", "youtu.be")


def _extract_youtube_id(url: str) -> str:
    """Return the 11-char video ID or raise ValueError."""
    p = urlparse(url)
    host = (p.hostname or "").removeprefix("www.")
    if host == "youtu.be":
        return p.path.lstrip("/").split("?")[0]
    if host == "youtube.com":
        vid = parse_qs(p.query).get("v", [None])[0]
        if vid:
            return vid
    raise ValueError(f"Cannot extract video ID from: {url}")


def _stream_to_file(download_url: str, dest: str, label: str) -> str:
    """Download *download_url* streaming to *dest*. Returns *dest*."""
    with session.get(download_url, stream=True, timeout=(20, 600)) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
                    report_download_progress(len(chunk))
    size_mb = os.path.getsize(dest) / (1024 * 1024)
    print(f"[{label}] Download complete → {dest} ({size_mb:.1f} MB)", flush=True)
    return dest


# ── Invidious fallback ────────────────────────────────────────────────────────

INVIDIOUS_INSTANCES = [
    "https://inv.nadeko.net",
    "https://invidious.fdn.fr",
    "https://yt.artemislena.eu",
    "https://iv.ggtyler.dev",
    "https://invidious.privacyredirect.com",
    "https://invidious.nerdvpn.de",
]


def download_via_invidious(url: str) -> str:
    import re
    video_id = _extract_youtube_id(url)
    os.makedirs(YTDLP_DIR, exist_ok=True)

    for instance in INVIDIOUS_INSTANCES:
        api_url = f"{instance}/api/v1/videos/{video_id}?fields=title,formatStreams"
        print(f"[invidious] Trying {instance} …", flush=True)
        try:
            r = session.get(api_url, timeout=15)
            if r.status_code != 200:
                print(f"[invidious] {instance} → HTTP {r.status_code}, skipping", flush=True)
                continue
            data = r.json()
        except Exception as exc:
            print(f"[invidious] {instance} error: {exc}", flush=True)
            continue

        format_streams = data.get("formatStreams") or []
        streams = [s for s in format_streams if "mp4" in s.get("type", "")]
        if not streams:
            print(f"[invidious] {instance} returned no mp4 streams, skipping", flush=True)
            continue

        def _res(s):
            m = re.search(r"(\d+)p", s.get("resolution", "0p"))
            return int(m.group(1)) if m else 0

        best = max(streams, key=_res)
        res  = best.get("resolution", "?")
        dl   = best.get("url")
        if not dl:
            continue

        title    = re.sub(r'[^\w\s-]', '', data.get("title", video_id))[:80].strip()
        dest     = os.path.join(YTDLP_DIR, f"{title or video_id}.mp4")
        print(f"[invidious] Downloading {res} stream from {instance}…", flush=True)
        try:
            return _stream_to_file(dl, dest, "invidious")
        except Exception as exc:
            print(f"[invidious] Download from {instance} failed: {exc}", flush=True)
            if os.path.exists(dest):
                os.remove(dest)

    raise RuntimeError(
        "All Invidious instances failed. "
        "The video may be age-restricted, private, or unavailable in the instance's region."
    )


# ── cobalt.tools fallback ─────────────────────────────────────────────────────

COBALT_INSTANCES = [
    "https://api.cobalt.tools",
    "https://cobalt.nadeko.net",
]


def download_via_cobalt(url: str) -> str:
    """Download via a cobalt.tools API instance (supports YouTube and others)."""
    os.makedirs(YTDLP_DIR, exist_ok=True)

    for api_base in COBALT_INSTANCES:
        print(f"[cobalt] Trying {api_base} …", flush=True)
        headers = {
            "Accept":       "application/json",
            "Content-Type": "application/json",
            "User-Agent":   "Mozilla/5.0 (compatible; split_upload/1.0)",
        }
        payload = {"url": url, "downloadMode": "auto"}
        try:
            r = session.post(
                f"{api_base}/",
                json=payload,
                headers=headers,
                timeout=30,
            )
            if r.status_code == 400:
                payload = {"url": url, "vQuality": "1080", "isAudioMuted": False}
                r = session.post(f"{api_base}/api/json", json=payload, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            print(f"[cobalt] {api_base} request failed: {exc}", flush=True)
            continue

        status = data.get("status", "")
        if status == "error":
            print(f"[cobalt] {api_base} error: {data.get('text', data)}", flush=True)
            continue
        if status not in ("tunnel", "redirect", "stream", "success"):
            print(f"[cobalt] {api_base} unexpected status '{status}'", flush=True)
            continue

        dl       = data.get("url") or data.get("tunnel")
        filename = data.get("filename", f"cobalt_video.{YTDLP_OUTPUT_EXT}")
        if not dl:
            continue

        dest = os.path.join(YTDLP_DIR, filename)
        print(f"[cobalt] Downloading '{filename}' …", flush=True)
        try:
            return _stream_to_file(dl, dest, "cobalt")
        except Exception as exc:
            print(f"[cobalt] Download failed: {exc}", flush=True)
            if os.path.exists(dest):
                os.remove(dest)

    raise RuntimeError("All cobalt.tools instances failed.")


# ── main yt-dlp entry point ───────────────────────────────────────────────────

def download_with_ytdlp(url: str) -> str:
    check_ytdlp()
    os.makedirs(YTDLP_DIR, exist_ok=True)
    output_template = os.path.join(YTDLP_DIR, "%(title).100s.%(ext)s")

    yt_attempts = [
        ("tv_embedded", "youtube:player_client=tv_embedded"),
        ("ios",         "youtube:player_client=ios"),
        ("android",     "youtube:player_client=android"),
        ("mweb",        "youtube:player_client=mweb"),
        ("web",         "youtube:player_client=web"),
        ("default",     ""),
    ]

    ytdlp_ok = False
    for label, ext_args in yt_attempts:
        cmd = _build_ytdlp_cmd(url, output_template, ext_args)
        print(f"[yt-dlp] Trying client={label}…", flush=True)
        try:
            subprocess.run(cmd, check=True)
            ytdlp_ok = True
            break
        except subprocess.CalledProcessError as exc:
            print(f"[yt-dlp] client={label} failed (exit {exc.returncode})", flush=True)
            _ytdlp_cleanup_parts()

    if ytdlp_ok:
        output_file = _ytdlp_find_output()
    elif _is_youtube_url(url):
        print("[fallback] All yt-dlp clients failed. Trying Invidious…", flush=True)
        try:
            output_file = download_via_invidious(url)
        except Exception as exc:
            print(f"[fallback] Invidious failed: {exc}\nTrying cobalt.tools…", flush=True)
            output_file = download_via_cobalt(url)
    else:
        print("[fallback] All yt-dlp clients failed. Trying cobalt.tools…", flush=True)
        output_file = download_via_cobalt(url)

    size_mb = os.path.getsize(output_file) / (1024 * 1024)
    print(f"[done] Output file: {output_file} ({size_mb:.1f} MB)", flush=True)

    if os.path.exists(YTDLP_COOKIES_TMP):
        os.remove(YTDLP_COOKIES_TMP)

    return output_file


def split_and_upload_local_file(filepath: str, chunk_size: int, upload_workers: int):
    file_size = os.path.getsize(filepath)
    num_parts = max(1, (file_size + chunk_size - 1) // chunk_size)
    ext       = os.path.splitext(filepath)[1]

    print(
        f"[split] {os.path.basename(filepath)} is {file_size / (1024*1024):.1f} MB → "
        f"{num_parts} part(s) of up to {chunk_size // (1024*1024)} MB each",
        flush=True,
    )

    os.makedirs(TEMP_DIR, exist_ok=True)
    upload_executor = ThreadPoolExecutor(max_workers=upload_workers)
    upload_futures  = []

    with open(filepath, "rb") as src:
        for part_num in range(1, num_parts + 1):
            data      = src.read(chunk_size)
            part_path = os.path.join(TEMP_DIR, f"part_{part_num:02d}{ext}")
            with open(part_path, "wb") as pf:
                pf.write(data)
            print(
                f"[split] Part {part_num} written ({len(data) / (1024*1024):.1f} MB), queuing upload…",
                flush=True,
            )
            upload_futures.append(
                upload_executor.submit(upload_worker, part_num, part_path)
            )

    results = []
    for fut in as_completed(upload_futures):
        res = fut.result()
        if res and res[1]:
            results.append(res)
        else:
            print("[error] One background upload failed!", flush=True)

    upload_executor.shutdown(wait=True)
    return results


# ── direct HTTP download paths ────────────────────────────────────────────────

def check_range_support(url: str):
    try:
        r    = session.head(url, timeout=(10, 20), allow_redirects=True)
        size = r.headers.get("Content-Length")
        accepts = r.headers.get("Accept-Ranges", "").lower() == "bytes"
        if not accepts and size:
            probe   = session.get(url, headers={"Range": "bytes=0-0"}, timeout=(10, 20), stream=True)
            accepts = probe.status_code == 206
            probe.close()
        return accepts, (int(size) if size else None)
    except Exception:
        return False, None


def download_range(url: str, start: int, end: int, dest_path: str, part_num: int):
    headers = {"Range": f"bytes={start}-{end}"}
    with session.get(url, headers=headers, stream=True, timeout=(15, 60)) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                report_download_progress(len(chunk))
    print(
        f"[download] Part {part_num} complete ({(end - start + 1) / (1024*1024):.1f} MB)",
        flush=True,
    )
    return part_num, dest_path


def run_parallel_download(url: str, total_size: int, chunk_size: int,
                          download_workers: int, upload_workers: int):
    num_parts = (total_size + chunk_size - 1) // chunk_size
    print(
        f"[download] Parallel ranged download – {num_parts} part(s), "
        f"{download_workers} connections",
        flush=True,
    )

    dl_exec  = ThreadPoolExecutor(max_workers=download_workers)
    up_exec  = ThreadPoolExecutor(max_workers=upload_workers)
    dl_futs  = []

    for part_num in range(1, num_parts + 1):
        start     = (part_num - 1) * chunk_size
        end       = min(start + chunk_size - 1, total_size - 1)
        dest_path = os.path.join(TEMP_DIR, f"part_{part_num:02d}.bin")
        dl_futs.append(dl_exec.submit(download_range, url, start, end, dest_path, part_num))

    up_futs = []
    for fut in as_completed(dl_futs):
        part_num, dest_path = fut.result()
        up_futs.append(up_exec.submit(upload_worker, part_num, dest_path))

    dl_exec.shutdown(wait=True)

    results = []
    for fut in as_completed(up_futs):
        res = fut.result()
        if res and res[1]:
            results.append(res)
        else:
            print("[error] One background upload failed!", flush=True)

    up_exec.shutdown(wait=True)
    return results


def run_sequential_download(url: str, chunk_size: int, upload_workers: int):
    print("[download] Single-stream download (server does not support ranges)", flush=True)

    up_exec   = ThreadPoolExecutor(max_workers=upload_workers)
    up_futs   = []
    part_num  = 1
    cur_size  = 0

    with session.get(url, stream=True, timeout=(15, 60)) as r:
        r.raise_for_status()
        part_path = os.path.join(TEMP_DIR, f"part_{part_num:02d}.bin")
        part_file = open(part_path, "wb")

        try:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if not chunk:
                    continue
                part_file.write(chunk)
                cur_size += len(chunk)
                report_download_progress(len(chunk))

                if cur_size >= chunk_size:
                    part_file.close()
                    print(
                        f"[download] Part {part_num} complete ({cur_size / (1024*1024):.1f} MB), "
                        f"queuing upload…",
                        flush=True,
                    )
                    up_futs.append(up_exec.submit(upload_worker, part_num, part_path))
                    part_num  += 1
                    cur_size   = 0
                    part_path  = os.path.join(TEMP_DIR, f"part_{part_num:02d}.bin")
                    part_file  = open(part_path, "wb")

            part_file.close()
            if cur_size > 0:
                print(
                    f"[download] Final part {part_num} ({cur_size / (1024*1024):.1f} MB), "
                    f"queuing upload…",
                    flush=True,
                )
                up_futs.append(up_exec.submit(upload_worker, part_num, part_path))
            elif os.path.exists(part_path):
                os.remove(part_path)
        finally:
            if not part_file.closed:
                part_file.close()

    results = []
    for fut in as_completed(up_futs):
        res = fut.result()
        if res and res[1]:
            results.append(res)
        else:
            print("[error] One background upload failed!", flush=True)

    up_exec.shutdown(wait=True)
    return results


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    url              = sys.argv[1]
    chunk_size_mb    = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    upload_workers   = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    download_workers = int(sys.argv[4]) if len(sys.argv) > 4 else 8

    chunk_size = chunk_size_mb * 1024 * 1024
    os.makedirs(TEMP_DIR, exist_ok=True)

    print(
        f"[start] chunk={chunk_size_mb}MB  upload_workers={upload_workers}  "
        f"download_connections={download_workers}",
        flush=True,
    )

    if is_ytdlp_url(url):
        print(f"[mode] yt-dlp detected for URL: {url}", flush=True)
        try:
            local_file = download_with_ytdlp(url)
        except subprocess.CalledProcessError as exc:
            print(f"[error] yt-dlp exited with code {exc.returncode}", flush=True)
            sys.exit(1)
        except Exception as exc:
            print(f"[error] yt-dlp download failed: {exc}", flush=True)
            sys.exit(1)

        results = split_and_upload_local_file(local_file, chunk_size, upload_workers)

        if os.path.exists(YTDLP_DIR):
            shutil.rmtree(YTDLP_DIR, ignore_errors=True)

    else:
        print(f"[mode] direct HTTP download for URL: {url}", flush=True)
        print("Checking source server capabilities…", flush=True)
        supports_ranges, total_size = check_range_support(url)
        if total_size:
            print(f"Source size: {total_size / (1024*1024):.1f} MB", flush=True)

        try:
            if supports_ranges and total_size:
                results = run_parallel_download(
                    url, total_size, chunk_size, download_workers, upload_workers
                )
            else:
                results = run_sequential_download(url, chunk_size, upload_workers)
        except Exception as exc:
            print(f"[error] Download failed: {exc}", flush=True)
            sys.exit(1)

    results.sort(key=lambda x: x[0])

    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        for num, link in results:
            f.write(f"{link}\n")

    links_url = upload_links_file(LINKS_FILE)
    if links_url:
        print(f"[done] {LINKS_FILE}: {links_url}", flush=True)
    else:
        print(f"[error] Could not upload {LINKS_FILE} (parts were still uploaded).", flush=True)

    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

    print(f"[finished] All parts uploaded. Links saved to {LINKS_FILE}.", flush=True)


if __name__ == "__main__":
    main()
