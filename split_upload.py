#!/usr/bin/env python3
"""
Download a large file (or HLS/M3U8 stream), split it into parts, and upload
each part as an asset on a GitHub Release in this same repo.

Supports:
  - Direct file URLs (HTTP/HTTPS) with optional parallel range downloads
  - HLS / M3U8 playlists (master or media), including AES-128 encrypted streams

Usage:
    python3 split_upload.py <url> [chunk_size_mb] [upload_workers] [download_connections]
                            [--referer <url>] [--header "Key: Value"] [--cookies "k=v; k2=v2"]
                            [--user-agent "MyAgent/1.0"]

Required environment variables:
    GITHUB_TOKEN        - a token with contents:write on the target repo
    GITHUB_REPOSITORY   - "owner/repo" (GitHub Actions sets this automatically)

Examples:
    # Regular file
    python3 split_upload.py "https://example.com/file.zip" 100 4 8

    # M3U8 with referer (required by most streaming sites)
    python3 split_upload.py "https://cdn.example.com/playlist.m3u8" 100 4 16 \
        --referer "https://example.com/watch/123" \
        --header "Origin: https://example.com"

    # M3U8 with cookies
    python3 split_upload.py "https://cdn.example.com/playlist.m3u8" 100 4 16 \
        --cookies "session=abc123; token=xyz"
"""
import sys
import os
import re
import time
import shutil
import threading
import subprocess
import traceback
import requests
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── Config ───────────────────────────────────────────────────────────────────

TEMP_DIR   = "upload_parts"
LINKS_FILE = "direct_links.txt"

GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_API        = "https://api.github.com"

# ─── HTTP sessions (configured in main after CLI parsing) ────────────────────

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
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})

# ─── Shared state ─────────────────────────────────────────────────────────────

_upload_lock         = threading.Lock()
_progress_lock       = threading.Lock()
_total_downloaded    = [0]
_last_progress_print = [0]
PROGRESS_EVERY_BYTES = 10 * 1024 * 1024


# ══════════════════════════════════════════════════════════════════════════════
# CLI parsing
# ══════════════════════════════════════════════════════════════════════════════

def parse_args(argv):
    """
    Returns a namespace-like dict from argv.

    Positional (all optional except url):
        url  chunk_size_mb  upload_workers  download_workers

    Named flags (anywhere after the url):
        --referer <url>
        --header "Name: Value"   (repeatable)
        --cookies "k=v; k2=v2"
        --user-agent "string"
    """
    args = {
        "url": None,
        "chunk_size_mb": 100,
        "upload_workers": 2,
        "download_workers": 8,
        "referer": None,
        "extra_headers": {},
        "cookies": None,
        "user_agent": None,
    }

    positional = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--referer" and i + 1 < len(argv):
            args["referer"] = argv[i + 1]; i += 2
        elif a == "--header" and i + 1 < len(argv):
            raw = argv[i + 1]
            if ":" in raw:
                k, v = raw.split(":", 1)
                args["extra_headers"][k.strip()] = v.strip()
            i += 2
        elif a == "--cookies" and i + 1 < len(argv):
            args["cookies"] = argv[i + 1]; i += 2
        elif a == "--user-agent" and i + 1 < len(argv):
            args["user_agent"] = argv[i + 1]; i += 2
        elif a.startswith("--"):
            print(f"[warn] Unknown flag: {a}", flush=True); i += 1
        else:
            positional.append(a); i += 1

    if len(positional) > 0: args["url"]              = positional[0]
    if len(positional) > 1: args["chunk_size_mb"]    = int(positional[1])
    if len(positional) > 2: args["upload_workers"]   = int(positional[2])
    if len(positional) > 3: args["download_workers"] = int(positional[3])

    return args


def configure_session(args):
    """Apply CLI-supplied headers / cookies / user-agent to the download session."""
    if args["user_agent"]:
        session.headers["User-Agent"] = args["user_agent"]
    if args["referer"]:
        session.headers["Referer"] = args["referer"]
        # Many CDNs also want Origin
        parsed = urlparse(args["referer"])
        if "Origin" not in args["extra_headers"]:
            session.headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    for k, v in args["extra_headers"].items():
        session.headers[k] = v
    if args["cookies"]:
        for pair in args["cookies"].split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                session.cookies.set(k.strip(), v.strip())

    # Log effective headers (mask cookie values for safety)
    if args["referer"] or args["extra_headers"] or args["cookies"]:
        print("[session] Custom headers applied:", flush=True)
        for k, v in session.headers.items():
            if k.lower() not in ("authorization",):
                print(f"  {k}: {v}", flush=True)
        if session.cookies:
            print(f"  Cookies: {'; '.join(f'{k}=***' for k in session.cookies.keys())}", flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# Progress
# ══════════════════════════════════════════════════════════════════════════════

def report_download_progress(nbytes):
    with _progress_lock:
        _total_downloaded[0] += nbytes
        if _total_downloaded[0] - _last_progress_print[0] >= PROGRESS_EVERY_BYTES:
            print(
                f"[download] {_total_downloaded[0] / (1024*1024):.1f} MB downloaded so far...",
                flush=True,
            )
            _last_progress_print[0] = _total_downloaded[0]


# ══════════════════════════════════════════════════════════════════════════════
# GitHub Release helpers
# ══════════════════════════════════════════════════════════════════════════════

_release = {}

def ensure_release():
    if _release:
        return _release

    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        raise RuntimeError(
            "GITHUB_TOKEN and GITHUB_REPOSITORY environment variables are required."
        )

    tag     = f"split-upload-{int(time.time())}"
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
    _release["upload_url"] = data["upload_url"].split("{")[0]
    _release["html_url"]   = data["html_url"]
    print(f"[release] Created release {tag}: {_release['html_url']}", flush=True)
    return _release


def upload_asset_bytes(filepath, asset_name):
    release = ensure_release()
    start   = time.time()
    with _upload_lock:
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


def upload_github_release_asset(filepath, part_num):
    return upload_asset_bytes(filepath, os.path.basename(filepath))


def upload_links_file(path, max_retries=4):
    print(f"[upload] Uploading {os.path.basename(path)} to GitHub Release...", flush=True)
    backoff    = 5.0
    asset_name = os.path.basename(path)
    for attempt in range(1, max_retries + 1):
        try:
            ok, result = upload_asset_bytes(path, asset_name)
            if ok:
                print(f"[done] {asset_name}: {result}", flush=True)
                return result
            print(f"[warn] Attempt {attempt} for {asset_name} failed: {result}", flush=True)
            if "422" in result or "already_exists" in result.lower():
                asset_name = f"direct_links_{attempt}.txt"
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


# ══════════════════════════════════════════════════════════════════════════════
# Direct-file download helpers
# ══════════════════════════════════════════════════════════════════════════════

def check_range_support(url):
    try:
        r    = session.head(url, timeout=(10, 20), allow_redirects=True)
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
                report_download_progress(len(chunk))
    print(f"[download] Part {part_num} complete ({(end-start+1)/(1024*1024):.1f} MB)", flush=True)
    return part_num, dest_path


def run_parallel_download(url, total_size, chunk_size, download_workers, upload_workers):
    num_parts = (total_size + chunk_size - 1) // chunk_size
    print(
        f"[download] Parallel ranged downloads - {num_parts} part(s), "
        f"{download_workers} connections",
        flush=True,
    )
    dl_ex = ThreadPoolExecutor(max_workers=download_workers)
    up_ex = ThreadPoolExecutor(max_workers=upload_workers)

    dl_futs = []
    for p in range(1, num_parts + 1):
        start = (p - 1) * chunk_size
        end   = min(start + chunk_size - 1, total_size - 1)
        dest  = os.path.join(TEMP_DIR, f"part_{p:02d}.zip")
        dl_futs.append(dl_ex.submit(download_range, url, start, end, dest, p))

    up_futs = []
    for fut in as_completed(dl_futs):
        pnum, dest = fut.result()
        up_futs.append(up_ex.submit(upload_worker, pnum, dest))

    dl_ex.shutdown(wait=True)
    results = []
    for fut in as_completed(up_futs):
        res = fut.result()
        if res and res[1]: results.append(res)
        else: print("[error] One of the background uploads failed!", flush=True)
    up_ex.shutdown(wait=True)
    return results


def run_sequential_download(url, chunk_size, upload_workers):
    print("[download] Single-stream download", flush=True)
    up_ex  = ThreadPoolExecutor(max_workers=upload_workers)
    up_futs = []
    part_num = 1
    cur_size = 0

    with session.get(url, stream=True, timeout=(15, 60)) as r:
        r.raise_for_status()
        part_path = os.path.join(TEMP_DIR, f"part_{part_num:02d}.zip")
        part_file = open(part_path, "wb")
        try:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if not chunk: continue
                part_file.write(chunk)
                cur_size += len(chunk)
                report_download_progress(len(chunk))
                if cur_size >= chunk_size:
                    part_file.close()
                    print(f"[download] Part {part_num} complete ({cur_size/(1024*1024):.1f} MB), queuing upload...", flush=True)
                    up_futs.append(up_ex.submit(upload_worker, part_num, part_path))
                    part_num += 1; cur_size = 0
                    part_path = os.path.join(TEMP_DIR, f"part_{part_num:02d}.zip")
                    part_file = open(part_path, "wb")
            part_file.close()
            if cur_size > 0:
                print(f"[download] Final part {part_num} ({cur_size/(1024*1024):.1f} MB), queuing upload...", flush=True)
                up_futs.append(up_ex.submit(upload_worker, part_num, part_path))
            elif os.path.exists(part_path):
                os.remove(part_path)
        finally:
            if not part_file.closed: part_file.close()

    results = []
    for fut in as_completed(up_futs):
        res = fut.result()
        if res and res[1]: results.append(res)
        else: print("[error] One of the background uploads failed!", flush=True)
    up_ex.shutdown(wait=True)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# M3U8 / HLS support
# ══════════════════════════════════════════════════════════════════════════════

def is_m3u8(url: str) -> bool:
    """Detect HLS playlist by extension first (no network needed), then Content-Type."""
    path = urlparse(url).path.lower()
    if path.endswith(".m3u8") or path.endswith(".m3u"):
        return True
    # Only probe content-type when extension is ambiguous
    try:
        r  = session.head(url, timeout=(10, 15), allow_redirects=True)
        ct = r.headers.get("Content-Type", "").lower()
        return "mpegurl" in ct or "x-m3u" in ct
    except Exception:
        return False


def _fetch_playlist(url: str):
    """Fetch playlist URL. Returns (text, effective_url)."""
    r = session.get(url, timeout=(20, 45))
    if r.status_code != 200:
        raise RuntimeError(
            f"Playlist fetch failed: HTTP {r.status_code}\n"
            f"URL: {url}\n"
            f"Response headers: {dict(r.headers)}\n"
            f"Body preview: {r.text[:500]}"
        )
    return r.text, r.url


def _best_variant(content: str, base_url: str) -> str:
    """Select the highest-bandwidth variant from a master playlist."""
    best_bw  = -1
    best_url = None
    lines    = content.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        m  = re.search(r"BANDWIDTH=(\d+)", line)
        bw = int(m.group(1)) if m else 0
        for j in range(i + 1, len(lines)):
            candidate = lines[j].strip()
            if candidate and not candidate.startswith("#"):
                if bw > best_bw:
                    best_bw  = bw
                    best_url = urljoin(base_url, candidate)
                break
    if best_url is None:
        raise RuntimeError("No variant stream found in master playlist")
    print(f"[m3u8] Selected variant BANDWIDTH={best_bw}: {best_url}", flush=True)
    return best_url


def _parse_key(line: str, base_url: str):
    method_m = re.search(r'METHOD=([^,\s]+)', line)
    method   = method_m.group(1) if method_m else "NONE"
    if method == "NONE":
        return None
    uri_m = re.search(r'URI="([^"]+)"', line)
    iv_m  = re.search(r'IV=0x([0-9a-fA-F]+)', line)
    uri   = urljoin(base_url, uri_m.group(1)) if uri_m else None
    iv    = bytes.fromhex(iv_m.group(1).zfill(32)) if iv_m else None
    return {"method": method, "uri": uri, "iv": iv}


def resolve_m3u8(url: str):
    """
    Parse M3U8 (master or media) and return ordered list of segment dicts:
        {"url": str, "key": dict|None, "seq": int}
    """
    content, final_url = _fetch_playlist(url)
    print(f"[m3u8] Playlist fetched from: {final_url}", flush=True)

    # Detect & handle master playlist
    if "#EXT-X-STREAM-INF" in content:
        print("[m3u8] Master playlist → selecting best quality...", flush=True)
        variant_url        = _best_variant(content, final_url)
        content, final_url = _fetch_playlist(variant_url)

    # Parse media playlist
    segments    = []
    current_key = None
    seq         = 0
    m           = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", content)
    if m:
        seq = int(m.group(1))

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-KEY"):
            current_key = _parse_key(line, final_url)
        elif not line.startswith("#"):
            seg_url = urljoin(final_url, line)
            segments.append({"url": seg_url, "key": current_key, "seq": seq})
            seq += 1

    if not segments:
        # Show first 30 lines of the playlist for debugging
        preview = "\n".join(content.splitlines()[:30])
        raise RuntimeError(
            f"No segments found in playlist.\n"
            f"Effective URL: {final_url}\n"
            f"Playlist preview:\n{preview}"
        )

    has_enc = any(s["key"] for s in segments)
    print(
        f"[m3u8] {len(segments)} segment(s)"
        + (" — AES-128 encrypted" if has_enc else ""),
        flush=True,
    )
    return segments


# ── AES-128 decryption ────────────────────────────────────────────────────────

_key_cache = {}   # uri → raw key bytes  (avoid re-downloading the same key)
_key_lock  = threading.Lock()

def _fetch_key(uri: str) -> bytes:
    with _key_lock:
        if uri in _key_cache:
            return _key_cache[uri]
    r = session.get(uri, timeout=(10, 20))
    if r.status_code != 200:
        raise RuntimeError(f"AES key fetch failed: HTTP {r.status_code}  URI={uri}")
    key = r.content
    with _key_lock:
        _key_cache[uri] = key
    return key

def _decrypt_segment(data: bytes, key_info: dict, seg_seq: int) -> bytes:
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise RuntimeError(
            "pycryptodome is required for AES-128 encrypted streams.\n"
            "Install it with:  pip install pycryptodome"
        )
    key    = _fetch_key(key_info["uri"])
    iv     = key_info["iv"] or seg_seq.to_bytes(16, "big")
    cipher = AES.new(key, AES.MODE_CBC, iv)
    dec    = cipher.decrypt(data)
    pad    = dec[-1]
    if 1 <= pad <= 16:
        dec = dec[:-pad]
    return dec


# ── Segment download with retry ───────────────────────────────────────────────

def _download_segment(seg: dict, dest_path: str, idx: int, total: int,
                      max_retries: int = 5) -> tuple:
    """
    Download one HLS segment with automatic retry on failure.
    Decrypts in-memory if AES-128 encrypted.
    Returns (idx, dest_path).
    """
    backoff = 3.0
    last_err = None

    for attempt in range(1, max_retries + 1):
        try:
            with session.get(seg["url"], stream=True, timeout=(20, 90)) as r:
                if r.status_code not in (200, 206):
                    raise RuntimeError(
                        f"Segment {idx+1}/{total} HTTP {r.status_code}  URL={seg['url']}"
                    )
                raw = b"".join(
                    chunk for chunk in r.iter_content(chunk_size=1024 * 1024) if chunk
                )
            report_download_progress(len(raw))

            if seg["key"]:
                raw = _decrypt_segment(raw, seg["key"], seg["seq"])

            with open(dest_path, "wb") as f:
                f.write(raw)

            return idx, dest_path

        except Exception as e:
            last_err = e
            if attempt < max_retries:
                print(
                    f"[warn] Segment {idx+1}/{total} attempt {attempt} failed: {e} "
                    f"— retrying in {backoff:.0f}s",
                    flush=True,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    raise RuntimeError(
        f"Segment {idx+1}/{total} failed after {max_retries} attempts: {last_err}\n"
        f"URL: {seg['url']}"
    )


# ── ffmpeg helper ─────────────────────────────────────────────────────────────

def _has_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _mux_with_ffmpeg(concat_list: str, output_path: str) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", concat_list, "-c", "copy", output_path],
        capture_output=True, timeout=600,
    )
    if result.returncode != 0:
        print(
            f"[m3u8] ffmpeg error: {result.stderr[-400:].decode(errors='replace')}",
            flush=True,
        )
    return result.returncode == 0


# ── Main M3U8 pipeline ────────────────────────────────────────────────────────

def run_m3u8_download(url: str, chunk_size: int, upload_workers: int,
                      download_workers: int):
    segments  = resolve_m3u8(url)
    total_seg = len(segments)
    seg_dir   = os.path.join(TEMP_DIR, "segments")
    os.makedirs(seg_dir, exist_ok=True)

    use_ffmpeg = _has_ffmpeg()
    ext        = ".mp4" if use_ffmpeg else ".ts"
    print(
        f"[m3u8] ffmpeg {'found → parts will be MP4' if use_ffmpeg else 'not found → raw .ts'}",
        flush=True,
    )

    # ── Phase 1: parallel segment download ───────────────────────────────────
    print(
        f"[m3u8] Downloading {total_seg} segments with {download_workers} workers...",
        flush=True,
    )
    seg_paths = [None] * total_seg
    failed    = []

    with ThreadPoolExecutor(max_workers=download_workers) as ex:
        futures = {
            ex.submit(
                _download_segment,
                seg,
                os.path.join(seg_dir, f"seg_{i:06d}.ts"),
                i,
                total_seg,
            ): i
            for i, seg in enumerate(segments)
        }
        done = 0
        for fut in as_completed(futures):
            try:
                idx, path   = fut.result()
                seg_paths[idx] = path
            except Exception as e:
                print(f"[error] {e}", flush=True)
                failed.append(e)
            done += 1
            step = max(1, total_seg // 20)
            if done % step == 0 or done == total_seg:
                print(f"[m3u8] {done}/{total_seg} segments downloaded", flush=True)

    if failed:
        raise RuntimeError(
            f"{len(failed)} segment(s) could not be downloaded. "
            "Check headers/cookies with --referer / --header / --cookies."
        )

    # ── Phase 2: assemble parts → upload ─────────────────────────────────────
    up_ex   = ThreadPoolExecutor(max_workers=upload_workers)
    up_futs = []

    part_num     = 1
    cur_size     = 0
    part_path    = os.path.join(TEMP_DIR, f"part_{part_num:03d}{ext}")
    buf          = []   # segment paths for the current part

    def _flush(pnum, pbuf, ppath, psize):
        if not pbuf:
            return
        if use_ffmpeg:
            concat_f = os.path.join(seg_dir, f"concat_{pnum}.txt")
            with open(concat_f, "w") as cf:
                for sp in pbuf:
                    cf.write(f"file '{os.path.abspath(sp)}'\n")
            ok = _mux_with_ffmpeg(concat_f, ppath)
            if not ok:
                ppath = ppath.replace(".mp4", ".ts")
                with open(ppath, "wb") as out:
                    for sp in pbuf:
                        with open(sp, "rb") as sf:
                            shutil.copyfileobj(sf, out)
        else:
            with open(ppath, "wb") as out:
                for sp in pbuf:
                    with open(sp, "rb") as sf:
                        shutil.copyfileobj(sf, out)

        print(
            f"[m3u8] Part {pnum} ready ({psize/(1024*1024):.1f} MB), queuing upload...",
            flush=True,
        )
        up_futs.append(up_ex.submit(upload_worker, pnum, ppath))
        for sp in pbuf:
            try: os.remove(sp)
            except OSError: pass

    for sp in seg_paths:
        seg_size = os.path.getsize(sp)
        if cur_size > 0 and cur_size + seg_size > chunk_size:
            _flush(part_num, buf, part_path, cur_size)
            part_num += 1
            cur_size  = 0
            part_path = os.path.join(TEMP_DIR, f"part_{part_num:03d}{ext}")
            buf       = []
        buf.append(sp)
        cur_size += seg_size

    _flush(part_num, buf, part_path, cur_size)

    results = []
    for fut in as_completed(up_futs):
        res = fut.result()
        if res and res[1]: results.append(res)
        else: print("[error] A background upload failed!", flush=True)
    up_ex.shutdown(wait=True)

    shutil.rmtree(seg_dir, ignore_errors=True)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    args = parse_args(sys.argv)
    if not args["url"]:
        print("Error: No URL provided."); sys.exit(1)

    configure_session(args)

    url              = args["url"]
    chunk_size       = args["chunk_size_mb"] * 1024 * 1024
    upload_workers   = args["upload_workers"]
    download_workers = args["download_workers"]

    os.makedirs(TEMP_DIR, exist_ok=True)

    print(
        f"Starting — chunk {args['chunk_size_mb']} MB — "
        f"upload workers: {upload_workers} — download workers: {download_workers}",
        flush=True,
    )

    try:
        if is_m3u8(url):
            print(f"[m3u8] HLS/M3U8 detected: {url}", flush=True)
            results = run_m3u8_download(url, chunk_size, upload_workers, download_workers)
        else:
            print("Checking source server capabilities...", flush=True)
            supports_ranges, total_size = check_range_support(url)
            if total_size:
                print(f"Source size: {total_size/(1024*1024):.1f} MB", flush=True)
            if supports_ranges and total_size:
                results = run_parallel_download(
                    url, total_size, chunk_size, download_workers, upload_workers
                )
            else:
                results = run_sequential_download(url, chunk_size, upload_workers)

    except Exception as e:
        print(f"\n[error] ── Fatal error ──────────────────────", flush=True)
        print(f"{e}", flush=True)
        print("\nFull traceback:", flush=True)
        traceback.print_exc()
        print("\nTips for M3U8 streams:", flush=True)
        print("  • Add --referer \"https://the-website.com/page-with-video\"", flush=True)
        print("  • Add --header \"Origin: https://the-website.com\"", flush=True)
        print("  • Add --cookies \"session=xxx; token=yyy\"", flush=True)
        print("  • For AES-128:  pip install pycryptodome", flush=True)
        sys.exit(1)

    results.sort(key=lambda x: x[0])

    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        for _, link in results:
            f.write(f"{link}\n")

    links_url = upload_links_file(LINKS_FILE)
    if links_url:
        print(f"[done] {LINKS_FILE}: {links_url}", flush=True)
    else:
        print(f"[error] Failed to upload {LINKS_FILE} (parts still uploaded).", flush=True)

    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

    print(f"[finished] All parts uploaded. Links saved to {LINKS_FILE}.", flush=True)


if __name__ == "__main__":
    main()
