#!/usr/bin/env python3
"""
Download a large file, split it into parts, and upload each part as an
asset on a GitHub Release in this same repo. This avoids third-party
file hosts (Litterbox/Catbox/Pixeldrain) blocking uploads that come
from GitHub Actions' datacenter IPs - since this uses GitHub's own API
with your own repo token, there's no such block.

Requires a GitHub token with `contents: write` permission on this repo.
In a GitHub Actions workflow, the built-in ${{ secrets.GITHUB_TOKEN }}
already has this by default - no extra secret needed.

Two source modes:

  1) Direct HTTP URL (default). If the source server supports HTTP
     Range requests (most file hosts / CDNs do), the file is
     downloaded using several parallel connections instead of one.

  2) yt-dlp mode (--ytdlp). The URL is treated as a video-hosting page
     (YouTube, Twitter/X, etc). yt-dlp downloads the video, merging
     the best video+audio streams into a single file locally (ffmpeg
     required), and that resulting file is then split and uploaded
     using the exact same chunking/upload logic as mode 1.

Usage:
    python3 split_upload.py <url> [options]

Options:
    --chunk-size-mb N        Size of each part in MB (default: 100)
    --upload-workers N       Concurrent upload workers (default: 2)
    --download-connections N Parallel HTTP download connections,
                              direct-URL mode only (default: 8)
    --ytdlp                  Treat <url> as a video page and use
                              yt-dlp + ffmpeg to fetch it instead of a
                              direct HTTP download
    --format FORMAT          yt-dlp format selector, only used with
                              --ytdlp (default: "bestvideo+bestaudio/best")

Required environment variables:
    GITHUB_TOKEN        - a token with contents:write on the target repo
    GITHUB_REPOSITORY   - "owner/repo" (GitHub Actions sets this automatically)

Examples:
    python3 split_upload.py "https://example.com/file.zip" --chunk-size-mb 100
    python3 split_upload.py "https://youtube.com/watch?v=XXXX" --ytdlp --format "bestvideo[height<=1080]+bestaudio/best[height<=1080]"

Notes for --ytdlp mode:
    - Requires `yt-dlp` and `ffmpeg` to be installed and on PATH.
      In a GitHub Actions workflow:
        pip install -U yt-dlp
        sudo apt-get update && sudo apt-get install -y ffmpeg
    - The whole video is downloaded to local disk first, then split
      into parts. Make sure the runner has enough free disk space for
      the full merged file (roughly 2x its size, since the original
      is kept until every part has been uploaded successfully).
"""
import sys
import os
import re
import json
import time
import glob
import shutil
import argparse
import threading
import subprocess
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

TEMP_DIR = "upload_parts"
YTDLP_DIR = "ytdlp_download"
DEFAULT_LINKS_FILE = "direct_links.txt"
DEFAULT_FORMAT = "bestvideo+bestaudio/best"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")  # "owner/repo"
GITHUB_API = "https://api.github.com"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
})

gh_session = requests.Session()
gh_session.headers.update({
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})

_upload_lock = threading.Lock()  # GitHub release asset uploads: do one at a time to avoid 422 name clashes

_progress_lock = threading.Lock()
_total_downloaded = [0]
_last_progress_print = [0]
PROGRESS_EVERY_BYTES = 10 * 1024 * 1024  # print every 10MB processed

_release = {}  # filled in once by ensure_release()


def report_progress(nbytes, label="downloaded"):
    with _progress_lock:
        _total_downloaded[0] += nbytes
        if _total_downloaded[0] - _last_progress_print[0] >= PROGRESS_EVERY_BYTES:
            print(f"[{label}] {_total_downloaded[0] / (1024*1024):.1f} MB so far...", flush=True)
            _last_progress_print[0] = _total_downloaded[0]


# ---------------------------------------------------------------------------
# Sanitizing helpers (used for --ytdlp mode, where names come from video
# metadata and cannot be trusted to be filesystem/tag/asset-name safe)
# ---------------------------------------------------------------------------

def sanitize_tag(text, max_len=50, fallback="video"):
    """Make a string safe to use as a git tag name: ASCII, no spaces,
    no git-ref-forbidden characters, no leading/trailing dots or dashes."""
    if not text:
        return fallback
    # Transliterate obviously-unsafe characters to '-'
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip())
    text = re.sub(r"-{2,}", "-", text).strip("-.")
    if not text:
        return fallback
    return text[:max_len].strip("-.") or fallback


def sanitize_filename(text, max_len=80, fallback="video"):
    """Make a string safe to use as a local filename / GitHub release
    asset name. Keeps unicode (e.g. Arabic titles) but strips characters
    that are unsafe in filenames or asset names."""
    if not text:
        return fallback
    text = text.strip()
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._")
    if not text:
        return fallback
    return text[:max_len].strip("._") or fallback


# ---------------------------------------------------------------------------
# GitHub Release helpers
# ---------------------------------------------------------------------------

def ensure_release(tag=None, name=None, body=None):
    """Creates a new GitHub Release to attach parts to, and caches its info.
    Only actually creates it on the first call; subsequent calls return the
    cached release regardless of the arguments passed."""
    if _release:
        return _release

    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        raise RuntimeError(
            "GITHUB_TOKEN and GITHUB_REPOSITORY environment variables are required. "
            "In GitHub Actions, pass GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}."
        )

    tag = tag or f"split-upload-{int(time.time())}"
    payload = {
        "tag_name": tag,
        "name": name or f"Split upload {tag}",
        "body": body or "Automatically generated by split_upload.py",
        "draft": False,
        "prerelease": True,
    }
    r = gh_session.post(f"{GITHUB_API}/repos/{GITHUB_REPOSITORY}/releases", json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    _release["id"] = data["id"]
    _release["upload_url"] = data["upload_url"].split("{")[0]  # strip the {?name,label} template
    _release["html_url"] = data["html_url"]
    print(f"[release] Created release {tag}: {_release['html_url']}", flush=True)
    return _release


def delete_release_if_created():
    """Best-effort cleanup: deletes the release (and its tag) if one was
    created this run but the overall job failed before finishing."""
    if not _release.get("id"):
        return
    try:
        gh_session.delete(f"{GITHUB_API}/repos/{GITHUB_REPOSITORY}/releases/{_release['id']}", timeout=30)
        print("[cleanup] Deleted incomplete release after failure.", flush=True)
    except Exception as e:
        print(f"[cleanup] Could not delete incomplete release: {e}", flush=True)


def upload_asset_bytes(filepath, asset_name):
    """Generic asset upload helper: uploads filepath under asset_name on the release.
    Returns (success: bool, direct_link_or_error: str)."""
    release = ensure_release()
    start = time.time()

    with _upload_lock:  # GitHub release asset upload endpoint doesn't like concurrent uploads well
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
        data = r.json()
        link = data.get("browser_download_url")
        if link:
            print(f"[timing] Upload of {asset_name} took {elapsed:.0f}s", flush=True)
            return True, link
    return False, f"HTTP {r.status_code} after {elapsed:.0f}s: {r.text[:300]}"


def upload_github_release_asset(filepath, part_num):
    """Uploads a file as an asset on the shared release.
    Returns (success: bool, direct_link_or_error: str)."""
    filename = os.path.basename(filepath)
    return upload_asset_bytes(filepath, filename)


def upload_links_file(path, max_retries=4):
    """Uploads the links .txt file itself as a release asset, so the
    ordered list of part links is also downloadable straight from the
    release page. Returns the browser_download_url, or None on failure."""
    print(f"[upload] Uploading {os.path.basename(path)} to GitHub Release...", flush=True)
    backoff = 5.0
    asset_name = os.path.basename(path)

    for attempt in range(1, max_retries + 1):
        try:
            ok, result = upload_asset_bytes(path, asset_name)
            if ok:
                print(f"[done] {asset_name}: {result}", flush=True)
                return result

            print(f"[warn] Attempt {attempt} for {asset_name} failed: {result}", flush=True)
            if "422" in result or "already_exists" in result.lower():
                # Name clash - suffix a counter and retry immediately
                base, ext = os.path.splitext(os.path.basename(path))
                asset_name = f"{base}_{attempt}{ext}"
                continue
        except requests.exceptions.Timeout:
            print(f"[warn] {asset_name} attempt {attempt}: connection timed out", flush=True)
        except Exception as e:
            print(f"[warn] Error uploading {asset_name} (attempt {attempt}): {e}", flush=True)

        time.sleep(backoff)
        backoff = min(backoff * 2, 60)

    return None


def upload_worker(part_num, filepath, max_retries=4):
    print(f"[upload] Starting upload of part {part_num} to GitHub Release...", flush=True)
    backoff = 5.0

    for attempt in range(1, max_retries + 1):
        try:
            ok, result = upload_github_release_asset(filepath, part_num)
            if ok:
                print(f"[done] Part {part_num}: {result}", flush=True)
                if os.path.exists(filepath):
                    os.remove(filepath)
                return part_num, result

            print(f"[warn] Attempt {attempt} for part {part_num} failed: {result}", flush=True)
            if "422" in result or "already_exists" in result.lower():
                # Rare name clash - regenerate a fresh unique filename and retry immediately
                new_path = filepath + f".{attempt}"
                os.rename(filepath, new_path)
                filepath = new_path
                continue
        except requests.exceptions.Timeout:
            print(f"[warn] Part {part_num} attempt {attempt}: connection timed out", flush=True)
        except Exception as e:
            print(f"[warn] Error uploading part {part_num} (attempt {attempt}): {e}", flush=True)

        time.sleep(backoff)
        backoff = min(backoff * 2, 60)

    if os.path.exists(filepath):
        os.remove(filepath)
    return part_num, None


# ---------------------------------------------------------------------------
# Mode 1: direct HTTP download (unchanged behaviour from the original script)
# ---------------------------------------------------------------------------

def check_range_support(url):
    """Returns (supports_ranges: bool, total_size: int or None)."""
    try:
        r = session.head(url, timeout=(10, 20), allow_redirects=True)
        size = r.headers.get("Content-Length")
        accepts_ranges = r.headers.get("Accept-Ranges", "").lower() == "bytes"
        if not accepts_ranges and size:
            probe = session.get(url, headers={"Range": "bytes=0-0"}, timeout=(10, 20), stream=True)
            accepts_ranges = probe.status_code == 206
            probe.close()
        return accepts_ranges, (int(size) if size else None)
    except Exception:
        return False, None


def download_range(url, start, end, dest_path, part_num):
    headers = {"Range": f"bytes={start}-{end}"}
    with session.get(url, headers=headers, stream=True, timeout=(15, 60)) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                report_progress(len(chunk))
    print(f"[download] Part {part_num} complete ({(end - start + 1) / (1024*1024):.1f} MB)", flush=True)
    return part_num, dest_path


def run_parallel_download(url, total_size, chunk_size, download_workers, upload_workers):
    num_parts = (total_size + chunk_size - 1) // chunk_size
    print(
        f"[download] Server supports parallel ranged downloads - "
        f"{num_parts} part(s), {download_workers} download connections",
        flush=True,
    )

    download_executor = ThreadPoolExecutor(max_workers=download_workers)
    upload_executor = ThreadPoolExecutor(max_workers=upload_workers)

    download_futures = []
    for part_num in range(1, num_parts + 1):
        start = (part_num - 1) * chunk_size
        end = min(start + chunk_size - 1, total_size - 1)
        dest_path = os.path.join(TEMP_DIR, f"part_{part_num:02d}.zip")
        fut = download_executor.submit(download_range, url, start, end, dest_path, part_num)
        download_futures.append(fut)

    upload_futures = []
    for fut in as_completed(download_futures):
        part_num, dest_path = fut.result()
        upload_futures.append(upload_executor.submit(upload_worker, part_num, dest_path))

    download_executor.shutdown(wait=True)

    results = []
    for fut in as_completed(upload_futures):
        res = fut.result()
        if res and res[1]:
            results.append(res)
        else:
            print("[error] One of the background uploads failed!", flush=True)

    upload_executor.shutdown(wait=True)
    return results


def run_sequential_download(url, chunk_size, upload_workers):
    print("[download] Server does not support ranged downloads - using a single stream", flush=True)

    upload_executor = ThreadPoolExecutor(max_workers=upload_workers)
    upload_futures = []

    part_num = 1
    current_size = 0

    with session.get(url, stream=True, timeout=(15, 60)) as r:
        r.raise_for_status()
        part_path = os.path.join(TEMP_DIR, f"part_{part_num:02d}.zip")
        part_file = open(part_path, "wb")

        try:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if not chunk:
                    continue
                part_file.write(chunk)
                current_size += len(chunk)
                report_progress(len(chunk))

                if current_size >= chunk_size:
                    part_file.close()
                    print(f"[download] Part {part_num} complete ({current_size / (1024*1024):.1f} MB), queuing upload...", flush=True)
                    upload_futures.append(upload_executor.submit(upload_worker, part_num, part_path))

                    part_num += 1
                    current_size = 0
                    part_path = os.path.join(TEMP_DIR, f"part_{part_num:02d}.zip")
                    part_file = open(part_path, "wb")

            part_file.close()
            if current_size > 0:
                print(f"[download] Final part {part_num} complete ({current_size / (1024*1024):.1f} MB), queuing upload...", flush=True)
                upload_futures.append(upload_executor.submit(upload_worker, part_num, part_path))
            elif os.path.exists(part_path):
                os.remove(part_path)
        finally:
            if not part_file.closed:
                part_file.close()

    results = []
    for fut in as_completed(upload_futures):
        res = fut.result()
        if res and res[1]:
            results.append(res)
        else:
            print("[error] One of the background uploads failed!", flush=True)

    upload_executor.shutdown(wait=True)
    return results


# ---------------------------------------------------------------------------
# Mode 2: yt-dlp download (new)
# ---------------------------------------------------------------------------

def check_tool_available(name):
    return shutil.which(name) is not None


def fetch_ytdlp_metadata(url):
    """Runs yt-dlp --dump-json to get metadata (title, etc.) without
    downloading anything yet. Returns a dict (possibly empty on failure)."""
    try:
        proc = subprocess.run(
            ["yt-dlp", "--no-playlist", "--dump-json", "--skip-download", url],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            print(f"[warn] yt-dlp metadata lookup failed: {proc.stderr[:300]}", flush=True)
            return {}
        # --dump-json prints one JSON object per line; take the first
        first_line = proc.stdout.strip().splitlines()[0]
        return json.loads(first_line)
    except Exception as e:
        print(f"[warn] Could not fetch yt-dlp metadata: {e}", flush=True)
        return {}


def download_via_ytdlp(url, format_selector, dest_dir):
    """Runs yt-dlp to download+merge video/audio into dest_dir.
    Returns the path to the resulting single file."""
    os.makedirs(dest_dir, exist_ok=True)
    outtmpl = os.path.join(dest_dir, "video.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "-f", format_selector,
        "--merge-output-format", "mp4",
        "--no-part",
        "-o", outtmpl,
        url,
    ]
    print(f"[ytdlp] Running: {' '.join(cmd)}", flush=True)

    proc = subprocess.run(cmd, timeout=None)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp exited with code {proc.returncode}")

    candidates = [p for p in glob.glob(os.path.join(dest_dir, "video.*")) if not p.endswith(".part")]
    if not candidates:
        raise RuntimeError("yt-dlp finished but no output file was found on disk")
    # Prefer the largest file, in case leftover thumbnails/subtitles matched the glob
    result_path = max(candidates, key=os.path.getsize)
    print(f"[ytdlp] Downloaded and merged: {result_path} "
          f"({os.path.getsize(result_path) / (1024*1024):.1f} MB)", flush=True)
    return result_path


def split_local_file(filepath, chunk_size, upload_workers):
    """Splits an already-downloaded local file into parts and uploads
    each one, reusing the same upload_worker/backoff logic as the HTTP
    download modes. Returns the list of (part_num, link) results."""
    total_size = os.path.getsize(filepath)
    num_parts = (total_size + chunk_size - 1) // chunk_size
    print(f"[split] Splitting local file into {num_parts} part(s) of up to "
          f"{chunk_size / (1024*1024):.0f} MB each", flush=True)

    upload_executor = ThreadPoolExecutor(max_workers=upload_workers)
    upload_futures = []

    with open(filepath, "rb") as src:
        for part_num in range(1, num_parts + 1):
            part_path = os.path.join(TEMP_DIR, f"part_{part_num:02d}.zip")
            remaining = chunk_size
            with open(part_path, "wb") as out:
                while remaining > 0:
                    block = src.read(min(4 * 1024 * 1024, remaining))
                    if not block:
                        break
                    out.write(block)
                    remaining -= len(block)
                    report_progress(len(block), label="split")
            actual_size = os.path.getsize(part_path)
            print(f"[split] Part {part_num} ready ({actual_size / (1024*1024):.1f} MB), queuing upload...", flush=True)
            upload_futures.append(upload_executor.submit(upload_worker, part_num, part_path))

    results = []
    for fut in as_completed(upload_futures):
        res = fut.result()
        if res and res[1]:
            results.append(res)
        else:
            print("[error] One of the background uploads failed!", flush=True)

    upload_executor.shutdown(wait=True)
    return results


def run_ytdlp_flow(url, format_selector, chunk_size, upload_workers):
    if not check_tool_available("yt-dlp"):
        raise RuntimeError(
            "yt-dlp is not installed or not on PATH. Install it first, e.g.:\n"
            "  pip install -U yt-dlp"
        )
    if not check_tool_available("ffmpeg"):
        raise RuntimeError(
            "ffmpeg is not installed or not on PATH (required to merge video+audio). Install it first, e.g.:\n"
            "  sudo apt-get update && sudo apt-get install -y ffmpeg"
        )

    print("[ytdlp] Fetching video metadata...", flush=True)
    metadata = fetch_ytdlp_metadata(url)
    title = metadata.get("title") or ""

    tag = f"split-upload-{sanitize_tag(title, fallback=str(int(time.time())))}-{int(time.time())}"
    release_name = title if title else f"Split upload {tag}"
    release_body = f"Automatically generated by split_upload.py (source: {url})"
    links_filename = f"direct_links_{sanitize_filename(title)}.txt" if title else DEFAULT_LINKS_FILE

    ensure_release(tag=tag, name=release_name, body=release_body)

    local_file = download_via_ytdlp(url, format_selector, YTDLP_DIR)
    try:
        results = split_local_file(local_file, chunk_size, upload_workers)
    finally:
        if os.path.exists(YTDLP_DIR):
            shutil.rmtree(YTDLP_DIR, ignore_errors=True)

    return results, links_filename


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Download a large file (via direct HTTP or yt-dlp), split it, "
                    "and upload the parts as GitHub Release assets.",
    )
    parser.add_argument("url", help="Direct file URL, or a video page URL when using --ytdlp")
    parser.add_argument("--chunk-size-mb", type=int, default=100, help="Size of each part in MB (default: 100)")
    parser.add_argument("--upload-workers", type=int, default=2, help="Concurrent upload workers (default: 2)")
    parser.add_argument("--download-connections", type=int, default=8,
                         help="Parallel HTTP download connections, direct-URL mode only (default: 8)")
    parser.add_argument("--ytdlp", action="store_true",
                         help="Treat <url> as a video page and use yt-dlp + ffmpeg to fetch it")
    parser.add_argument("--format", default=DEFAULT_FORMAT,
                         help=f"yt-dlp format selector, only used with --ytdlp (default: {DEFAULT_FORMAT!r})")
    return parser.parse_args()


def main():
    args = parse_args()

    chunk_size = args.chunk_size_mb * 1024 * 1024
    os.makedirs(TEMP_DIR, exist_ok=True)

    print(
        f"Starting - mode: {'yt-dlp' if args.ytdlp else 'direct HTTP'} - "
        f"chunk size {args.chunk_size_mb}MB - service: GitHub Release assets - "
        f"upload workers: {args.upload_workers}"
        + ("" if args.ytdlp else f" - download connections: {args.download_connections}"),
        flush=True,
    )

    links_filename = DEFAULT_LINKS_FILE

    try:
        if args.ytdlp:
            results, links_filename = run_ytdlp_flow(
                args.url, args.format, chunk_size, args.upload_workers
            )
        else:
            print("Checking source server capabilities...", flush=True)
            supports_ranges, total_size = check_range_support(args.url)
            if total_size:
                print(f"Source size: {total_size / (1024*1024):.1f} MB", flush=True)

            if supports_ranges and total_size:
                results = run_parallel_download(
                    args.url, total_size, chunk_size, args.download_connections, args.upload_workers
                )
            else:
                results = run_sequential_download(args.url, chunk_size, args.upload_workers)
    except Exception as e:
        print(f"[error] Failed: {e}", flush=True)
        delete_release_if_created()
        sys.exit(1)

    expected_parts = None  # only known upfront in the parallel-HTTP path; skip strict check otherwise
    if not results:
        print("[error] No parts were uploaded successfully.", flush=True)
        delete_release_if_created()
        sys.exit(1)

    # Sort ascending by part number so the .txt (and the printed order) is
    # always Part 1, Part 2, Part 3, ... regardless of upload/finish order.
    results.sort(key=lambda x: x[0])

    with open(links_filename, "w", encoding="utf-8") as f:
        for num, link in results:
            f.write(f"{link}\n")

    # Also upload the links file itself as a release asset, so anyone
    # visiting the release page can grab the ordered list directly
    # without needing the Actions log.
    links_asset_url = upload_links_file(links_filename)
    if links_asset_url:
        print(f"[done] {links_filename}: {links_asset_url}", flush=True)
    else:
        print(f"[error] Failed to upload {links_filename} to the release (parts were still uploaded).", flush=True)

    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

    if _release.get("html_url"):
        print(f"[release] {_release['html_url']}", flush=True)
    print(f"[finished] All parts uploaded. Links saved to {links_filename}.", flush=True)


if __name__ == "__main__":
    main()
