#!/usr/bin/env python3
"""
Download a very large file (100GB+) and store it as parts on a GitHub Release
of this repo.

Design goals
  * Disk usage is bounded: at most --max-on-disk parts exist on disk at any
    moment, so total file size does not matter (default ~4 x 1GB).
  * Resumable: re-running with the same URL continues where it stopped
    (already uploaded parts are detected and skipped). The workflow can
    chain itself when the 6h job limit is near.
  * Gentle on GitHub: one release per URL, uploads are serialized by default,
    pauses between uploads, honors Retry-After / rate-limit headers.
  * Integrity: sha256 per part (stored in the asset label), manifest.json,
    sha256sums.txt, direct_links.txt and join instructions in the release.

Usage:
  python3 split_upload.py <url> [chunk_mb] [options]

Env: GITHUB_TOKEN, GITHUB_REPOSITORY (set automatically in GitHub Actions)

Notes:
  * Direct links with Range support -> resumable, parallel downloads.
  * Servers without Range, and .m3u8 (via ffmpeg) -> single stream, NOT
    resumable (must finish inside one run).
"""
import argparse
import hashlib
import math
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlparse

import requests

MB = 1024 * 1024
API = "https://api.github.com"
MAX_PART_MB = 1900      # GitHub limit is 2 GiB per asset
MAX_ASSETS = 900        # GitHub limit is 1000 assets per release
TEMP_DIR = "upload_parts"

T0 = time.time()
_log_lock = threading.Lock()

A = None
REPO = ""
DEADLINE = 0.0
REL = {}
src = None
gh_s = None
dl_sem = None
up_sem = None
state = {"est": 0.0, "done": 0}


def log(msg):
    with _log_lock:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def set_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def time_left():
    return DEADLINE - time.time()


# --------------------------------------------------------------- GitHub API
def rate_wait(r):
    """Seconds to wait if GitHub says we are rate limited, else 0."""
    if r.status_code not in (403, 429):
        return 0
    ra = r.headers.get("Retry-After")
    if ra and ra.isdigit():
        return min(int(ra) + 1, 900)
    if r.headers.get("x-ratelimit-remaining") == "0":
        reset = int(r.headers.get("x-ratelimit-reset", "0") or 0)
        return min(max(reset - int(time.time()), 1) + 1, 900)
    low = r.text.lower()
    if "rate limit" in low or "abuse" in low:
        return 60
    return 0


def gh(method, path, **kw):
    url = path if path.startswith("http") else API + path
    delay = 3
    for attempt in range(1, A.retries + 1):
        try:
            r = gh_s.request(method, url, timeout=60, **kw)
        except requests.RequestException as e:
            log(f"GitHub API error ({e}); retry {attempt}")
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        wait = rate_wait(r)
        if wait:
            log(f"GitHub asked us to slow down - waiting {wait}s")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        return r
    raise RuntimeError(f"GitHub API {method} {url} kept failing")


def ensure_release(name):
    r = gh("GET", f"/repos/{REPO}/releases/tags/{A.tag}")
    if r.status_code == 404:
        r = gh("POST", f"/repos/{REPO}/releases", json={
            "tag_name": A.tag,
            "name": f"{name} (split)",
            "body": "Upload in progress...",
            "prerelease": True,
        })
    r.raise_for_status()
    d = r.json()
    REL.update(id=d["id"], upload_url=d["upload_url"].split("{")[0], html_url=d["html_url"])
    log(f"Release: {REL['html_url']}")


def list_assets():
    out, page = [], 1
    while True:
        r = gh("GET", f"/repos/{REPO}/releases/{REL['id']}/assets",
               params={"per_page": 100, "page": page})
        r.raise_for_status()
        batch = r.json()
        out += batch
        if len(batch) < 100:
            return out
        page += 1


def upload(path, name, label=""):
    """Upload one file as a release asset. Returns download URL or None."""
    size = os.path.getsize(path)
    delay = 5
    for attempt in range(1, A.retries + 1):
        stale = next((a for a in list_assets() if a["name"] == name), None)
        if stale:
            # a previous attempt may have actually succeeded
            if (attempt > 1 and label and stale["state"] == "uploaded"
                    and stale["size"] == size and stale.get("label") == label):
                return stale["browser_download_url"]
            gh("DELETE", f"/repos/{REPO}/releases/assets/{stale['id']}")
        params = {"name": name}
        if label:
            params["label"] = label
        t = time.time()
        try:
            with open(path, "rb") as f:
                r = gh_s.post(REL["upload_url"], params=params,
                              headers={"Content-Type": "application/octet-stream"},
                              data=f, timeout=(20, 1800))
        except requests.RequestException as e:
            log(f"upload of {name} failed ({e}); attempt {attempt}")
            time.sleep(delay)
            delay = min(delay * 2, 120)
            continue
        if r.status_code in (200, 201):
            log(f"uploaded {name} ({size / MB:.0f} MB in {time.time() - t:.0f}s)")
            return r.json()["browser_download_url"]
        wait = rate_wait(r)
        log(f"upload of {name} failed: HTTP {r.status_code} {r.text[:200]}")
        time.sleep(wait or delay)
        delay = min(delay * 2, 120)
    return None


# ------------------------------------------------------------------ source
def sanitize(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file.bin"
    return name[:150]


def guess_name(url, headers):
    cd = headers.get("Content-Disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    if m:
        return sanitize(unquote(m.group(1)))
    return sanitize(os.path.basename(unquote(urlparse(url).path)))


def probe(url):
    """Returns (total_size or None, supports_ranges, headers)."""
    r = src.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=(20, 30))
    try:
        if r.status_code == 206:
            m = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
            return (int(m.group(1)) if m else None), bool(m), r.headers
        r.raise_for_status()
        cl = r.headers.get("Content-Length")
        return (int(cl) if cl else None), False, r.headers
    finally:
        r.close()


def download_range(url, start, end, path):
    """Download bytes [start, end] into path, resuming on errors. Returns sha256."""
    expected = end - start + 1
    h = hashlib.sha256()
    got = 0
    delay = 3
    if os.path.exists(path):
        os.remove(path)
    for attempt in range(1, A.retries + 1):
        try:
            with src.get(url, headers={"Range": f"bytes={start + got}-{end}"},
                         stream=True, timeout=(20, 60)) as r:
                if r.status_code != 206:
                    raise RuntimeError(f"HTTP {r.status_code} for a range request")
                with open(path, "ab") as f:
                    for chunk in r.iter_content(8 * MB):
                        if chunk:
                            f.write(chunk)
                            h.update(chunk)
                            got += len(chunk)
            if got == expected:
                return h.hexdigest()
            raise RuntimeError(f"short read {got}/{expected}")
        except (requests.RequestException, RuntimeError) as e:
            log(f"download error ({e}); attempt {attempt}, have {got / MB:.0f} MB")
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError("download kept failing")


def source_chunks(url, is_m3u8):
    if is_m3u8:
        proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", url,
             "-c", "copy", "-f", "mpegts", "pipe:1"], stdout=subprocess.PIPE)
        try:
            while True:
                b = proc.stdout.read(8 * MB)
                if not b:
                    break
                yield b
            proc.stdout.close()
            if proc.wait() != 0:
                raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")
        except BaseException:
            proc.kill()
            raise
    else:
        with src.get(url, stream=True, timeout=(20, 60)) as r:
            r.raise_for_status()
            yield from r.iter_content(8 * MB)


# ------------------------------------------------------------- range mode
def process_part(p):
    if time_left() < state["est"] * 1.3:
        return "deferred"
    path = os.path.join(TEMP_DIR, p["name"])
    t = time.time()
    try:
        with dl_sem:
            sha = download_range(A.url, p["start"], p["end"], path)
        with up_sem:
            link = upload(path, p["name"], f"sha256:{sha}")
            time.sleep(A.pause)
    except Exception as e:  # noqa: BLE001
        log(f"part {p['idx']} failed: {e}")
        return "failed"
    finally:
        if os.path.exists(path):
            os.remove(path)
    if not link:
        return "failed"
    state["est"] = max(state["est"], time.time() - t)
    state["done"] += 1
    log(f"part {p['idx']} done ({state['done']} this run)")
    return "done"


def run_ranges(name, total, chunk):
    n = math.ceil(total / chunk)
    if n > MAX_ASSETS:
        chunk = math.ceil(math.ceil(total / MAX_ASSETS) / MB) * MB
        if chunk > MAX_PART_MB * MB:
            raise SystemExit("File too large for a single release (over ~1.7 TB).")
        n = math.ceil(total / chunk)
        log(f"Part size raised to {chunk // MB} MB to stay under the release asset limit")
    parts = []
    for i in range(n):
        s, e = i * chunk, min((i + 1) * chunk, total) - 1
        parts.append(dict(idx=i + 1, start=s, end=e, size=e - s + 1,
                          name=f"{name}.part{i + 1:04d}"))
    existing = {a["name"]: a for a in list_assets()}
    pending = [p for p in parts if not (
        p["name"] in existing and existing[p["name"]]["state"] == "uploaded"
        and existing[p["name"]]["size"] == p["size"])]
    log(f"{n} parts of up to {chunk // MB} MB: "
        f"{n - len(pending)} already uploaded, {len(pending)} to do")

    with ThreadPoolExecutor(A.max_on_disk) as pool:
        statuses = list(pool.map(process_part, pending))
    failed, done = statuses.count("failed"), statuses.count("done")

    names = [p["name"] for p in parts]
    now = {a["name"]: a for a in list_assets()}
    if all(nm in now and now[nm]["state"] == "uploaded" for nm in names):
        finalize(name, names)
        return "complete"
    if failed and not done:
        return "failed"
    return "continue"


# ------------------------------------------------------------ stream mode
def run_stream(name, chunk, is_m3u8):
    slots = threading.BoundedSemaphore(A.max_on_disk)
    pool = ThreadPoolExecutor(A.upload_workers)
    futs = []

    def finish(i, path, sha):
        try:
            link = upload(path, f"{name}.part{i:04d}", f"sha256:{sha}")
            time.sleep(A.pause)
            return link is not None
        except Exception as e:  # noqa: BLE001
            log(f"part {i} failed: {e}")
            return False
        finally:
            if os.path.exists(path):
                os.remove(path)
            slots.release()

    idx, cur, f, h, path = 0, 0, None, None, None
    aborted = False
    try:
        for chunk_bytes in source_chunks(A.url, is_m3u8):
            if time_left() <= 0:
                log("Time limit reached; stream mode cannot resume.")
                aborted = True
                break
            mv = memoryview(chunk_bytes)
            while len(mv):
                if f is None:
                    slots.acquire()          # back-pressure: stops reading the source
                    idx += 1
                    if idx > MAX_ASSETS:
                        raise RuntimeError("Too many parts - use a bigger chunk size")
                    path = os.path.join(TEMP_DIR, f"{name}.part{idx:04d}")
                    f, h, cur = open(path, "wb"), hashlib.sha256(), 0
                piece = mv[:chunk - cur]
                f.write(piece)
                h.update(piece)
                cur += len(piece)
                mv = mv[len(piece):]
                if cur >= chunk:
                    f.close()
                    futs.append(pool.submit(finish, idx, path, h.hexdigest()))
                    f = None
        if f is not None:
            f.close()
            futs.append(pool.submit(finish, idx, path, h.hexdigest()))
    except Exception as e:  # noqa: BLE001
        log(f"stream error: {e}")
        aborted = True
        if f is not None and not f.closed:
            f.close()
    ok = all(x.result() for x in futs)
    pool.shutdown()
    if aborted or not ok or idx == 0:
        return "failed"
    finalize(name, [f"{name}.part{i:04d}" for i in range(1, idx + 1)])
    return "complete"


# --------------------------------------------------------------- finalize
def finalize(name, part_names):
    assets = {a["name"]: a for a in list_assets()}
    rows = []
    for pn in part_names:
        a = assets[pn]
        rows.append(dict(name=pn, size=a["size"],
                         sha256=a.get("label", "").replace("sha256:", ""),
                         url=a["browser_download_url"]))
    total = sum(r["size"] for r in rows)
    files = {
        "direct_links.txt": "\n".join(r["url"] for r in rows) + "\n",
        "sha256sums.txt": "".join(f"{r['sha256']}  {r['name']}\n" for r in rows if r["sha256"]),
        "manifest.json": __import__("json").dumps(
            dict(file_name=name, total_size=total, parts=rows), indent=2),
    }
    for fname, content in files.items():
        p = os.path.join(TEMP_DIR, fname)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
        upload(p, fname)
    with open("direct_links.txt", "w", encoding="utf-8") as fh:
        fh.write(files["direct_links.txt"])
    body = (
        f"`{name}` - {len(rows)} parts, {total / (1024 ** 3):.2f} GB\n\n"
        f"Join (Linux / macOS / WSL / Git Bash):\n```\ncat {name}.part* > {name}\n```\n"
        f"Verify the parts first (optional):\n```\nsha256sum -c sha256sums.txt\n```\n"
        f"All links: `direct_links.txt`, details: `manifest.json`."
    )
    gh("PATCH", f"/repos/{REPO}/releases/{REL['id']}", json={"body": body})
    log(f"All {len(rows)} parts ready: {REL['html_url']}")


# ------------------------------------------------------------------- main
def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url")
    p.add_argument("chunk_mb", nargs="?", type=int, default=1000)
    p.add_argument("--name", help="output file name (default: from URL)")
    p.add_argument("--tag", help="release tag (default: derived from URL, so reruns resume)")
    p.add_argument("--max-on-disk", type=int, default=4, help="max parts on disk at once")
    p.add_argument("--download-workers", type=int, default=3)
    p.add_argument("--upload-workers", type=int, default=1)
    p.add_argument("--time-limit-min", type=float, default=330)
    p.add_argument("--pause", type=float, default=3.0, help="seconds between uploads")
    p.add_argument("--retries", type=int, default=5)
    p.add_argument("--user-agent", default="file-splitter/2.0")
    return p.parse_args()


def main():
    global A, REPO, DEADLINE, src, gh_s, dl_sem, up_sem
    A = parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    REPO = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not REPO:
        sys.exit("GITHUB_TOKEN and GITHUB_REPOSITORY are required.")
    DEADLINE = T0 + A.time_limit_min * 60
    A.max_on_disk = max(A.max_on_disk, 1)
    chunk = min(max(A.chunk_mb, 1), MAX_PART_MB) * MB
    if not A.tag:
        A.tag = "split-" + hashlib.sha1(A.url.encode()).hexdigest()[:10]

    src = requests.Session()
    src.headers.update({"User-Agent": A.user_agent, "Accept-Encoding": "identity"})
    gh_s = requests.Session()
    gh_s.headers.update({"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"})
    dl_sem = threading.Semaphore(A.download_workers)
    up_sem = threading.Semaphore(A.upload_workers)
    os.makedirs(TEMP_DIR, exist_ok=True)

    is_m3u8 = ".m3u8" in urlparse(A.url).path.lower()
    total, ranges, headers = None, False, {}
    if not is_m3u8:
        total, ranges, headers = probe(A.url)
    if A.name:
        name = sanitize(A.name)
    elif is_m3u8:
        name = sanitize(os.path.splitext(os.path.basename(urlparse(A.url).path))[0]) + ".ts"
    else:
        name = guess_name(A.url, headers)

    log(f"File: {name} | size: "
        f"{f'{total / (1024 ** 3):.2f} GB' if total else 'unknown'} | "
        f"mode: {'ranged (resumable)' if ranges and total else 'single stream (not resumable)'}")
    ensure_release(name)

    if ranges and total:
        status = run_ranges(name, total, chunk)
    else:
        status = run_stream(name, chunk, is_m3u8)

    set_output("status", status)
    set_output("release_url", REL.get("html_url", ""))
    log(f"Status: {status}")
    if status == "continue":
        log("Not finished yet - run again (the workflow does this automatically).")
    sys.exit(1 if status == "failed" else 0)


if __name__ == "__main__":
    main()
