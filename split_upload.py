#!/usr/bin/env python3
"""
Download a large file, split it into parts, and upload each part in
parallel to Litterbox (litterbox.catbox.moe) - anonymous, no account
needed, returns a real direct download link (not a landing page).

Faster download: if the source server supports HTTP Range requests
(most file hosts / CDNs do), the file is downloaded using several
parallel connections instead of one - this is usually the biggest
speed win, since a single connection is often capped well below your
real bandwidth. Falls back to a normal single-stream download if the
server doesn't support ranges.

All uploaded parts expire after 1 hour (Litterbox's shortest retention
option), so make sure to download them before the link expires.

Usage:
    python3 multi_upload.py <file_url> [chunk_size_mb] [upload_workers] [seconds_between_requests] [download_connections]

Example (fast, default):
    python3 multi_upload.py "https://example.com/file.zip"

Example (more download connections, more upload workers):
    python3 multi_upload.py "https://example.com/file.zip" 200 4 1 8
"""
import sys
import os
import time
import shutil
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

TEMP_DIR = "upload_parts"
LINKS_FILE = "direct_links.txt"
LITTERBOX_URL = "https://litterbox.catbox.moe/resources/internals/api.php"
LITTERBOX_TIME = "1h"  # fixed: all links expire after 1 hour

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
})

# ---------------------------------------------------------------------------
# Request rate limiting for uploads - avoids tripping the service's abuse
# protection instead of trying to bypass it.
# ---------------------------------------------------------------------------
MIN_SECONDS_BETWEEN_REQUESTS = 1.0
_rate_lock = threading.Lock()
_last_request_time = [0.0]

_progress_lock = threading.Lock()
_total_downloaded = [0]
_last_progress_print = [0]
PROGRESS_EVERY_BYTES = 10 * 1024 * 1024  # print every 10MB downloaded


def throttle():
    """Waits as needed before sending a new request, so we don't exceed
    the service's request rate."""
    with _rate_lock:
        now = time.time()
        wait = MIN_SECONDS_BETWEEN_REQUESTS - (now - _last_request_time[0])
        if wait > 0:
            time.sleep(wait)
        _last_request_time[0] = time.time()


def report_download_progress(nbytes):
    with _progress_lock:
        _total_downloaded[0] += nbytes
        if _total_downloaded[0] - _last_progress_print[0] >= PROGRESS_EVERY_BYTES:
            print(f"[download] {_total_downloaded[0] / (1024*1024):.1f} MB downloaded so far...", flush=True)
            _last_progress_print[0] = _total_downloaded[0]


def upload_litterbox(filepath):
    """Uploads a file to Litterbox with a fixed 1-hour expiry.
    Returns (success: bool, direct_link_or_error: str)."""
    with open(filepath, "rb") as f:
        files = {"fileToUpload": f}
        data = {"reqtype": "fileupload", "time": LITTERBOX_TIME}
        r = session.post(LITTERBOX_URL, data=data, files=files, timeout=900)
    if r.status_code == 200 and r.text.strip().startswith("http"):
        return True, r.text.strip()
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


def is_rate_limited(error_text):
    text = str(error_text).lower()
    return "429" in text or "rate" in text or "too many" in text


def upload_worker(part_num, filepath, max_retries=5):
    print(f"[upload] Starting upload of part {part_num} to Litterbox ({LITTERBOX_TIME})...", flush=True)

    backoff = 5.0  # seconds - doubles on each rate-limit hit

    for attempt in range(1, max_retries + 1):
        throttle()
        try:
            ok, result = upload_litterbox(filepath)
            if ok:
                print(f"[done] Part {part_num}: {result}", flush=True)
                if os.path.exists(filepath):
                    os.remove(filepath)
                return part_num, result

            print(f"[warn] Attempt {attempt} for part {part_num} failed: {result}", flush=True)
            if is_rate_limited(result):
                print(f"[wait] Rate limited, waiting {backoff:.0f}s before retrying...", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
        except Exception as e:
            print(f"[warn] Error uploading part {part_num} (attempt {attempt}): {e}", flush=True)

        time.sleep(2)

    if os.path.exists(filepath):
        os.remove(filepath)
    return part_num, None


def check_range_support(url):
    """Returns (supports_ranges: bool, total_size: int or None)."""
    try:
        r = session.head(url, timeout=(10, 20), allow_redirects=True)
        size = r.headers.get("Content-Length")
        accepts_ranges = r.headers.get("Accept-Ranges", "").lower() == "bytes"
        if not accepts_ranges and size:
            # Some servers omit Accept-Ranges on HEAD but still honor Range.
            # Do a small probe GET with a Range header to confirm.
            probe = session.get(url, headers={"Range": "bytes=0-0"}, timeout=(10, 20), stream=True)
            accepts_ranges = probe.status_code == 206
            probe.close()
        return accepts_ranges, (int(size) if size else None)
    except Exception:
        return False, None


def download_range(url, start, end, dest_path, part_num):
    """Downloads bytes [start, end] (inclusive) into dest_path."""
    headers = {"Range": f"bytes={start}-{end}"}
    with session.get(url, headers=headers, stream=True, timeout=(15, 60)) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                report_download_progress(len(chunk))
    print(f"[download] Part {part_num} complete ({(end - start + 1) / (1024*1024):.1f} MB)", flush=True)
    return part_num, dest_path


def run_parallel_download(url, total_size, chunk_size, download_workers, upload_workers):
    """Downloads the file as parallel ranged parts and uploads each part as
    soon as it finishes downloading."""
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
        dest_path = os.path.join(TEMP_DIR, f"part_{part_num:03d}.bin")
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
    """Fallback: single-stream download, splitting into parts as it goes
    and uploading each part in the background as soon as it's ready."""
    print("[download] Server does not support ranged downloads - using a single stream", flush=True)

    upload_executor = ThreadPoolExecutor(max_workers=upload_workers)
    upload_futures = []

    part_num = 1
    current_size = 0

    with session.get(url, stream=True, timeout=(15, 60)) as r:
        r.raise_for_status()
        part_path = os.path.join(TEMP_DIR, f"part_{part_num:03d}.bin")
        part_file = open(part_path, "wb")

        try:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if not chunk:
                    continue
                part_file.write(chunk)
                current_size += len(chunk)
                report_download_progress(len(chunk))

                if current_size >= chunk_size:
                    part_file.close()
                    print(f"[download] Part {part_num} complete ({current_size / (1024*1024):.1f} MB), queuing upload...", flush=True)
                    upload_futures.append(upload_executor.submit(upload_worker, part_num, part_path))

                    part_num += 1
                    current_size = 0
                    part_path = os.path.join(TEMP_DIR, f"part_{part_num:03d}.bin")
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


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: python3 multi_upload.py <file_url> "
            "[chunk_size_mb] [upload_workers] [seconds_between_requests] [download_connections]"
        )
        print(f"All links expire after {LITTERBOX_TIME} (Litterbox).")
        print("Defaults: 200MB chunks, 4 upload workers, 1s min delay, 4 download connections.")
        sys.exit(1)

    url = sys.argv[1]
    chunk_size_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    upload_workers = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    download_workers = int(sys.argv[5]) if len(sys.argv) > 5 else 4
    global MIN_SECONDS_BETWEEN_REQUESTS
    MIN_SECONDS_BETWEEN_REQUESTS = float(sys.argv[4]) if len(sys.argv) > 4 else MIN_SECONDS_BETWEEN_REQUESTS

    chunk_size = chunk_size_mb * 1024 * 1024
    os.makedirs(TEMP_DIR, exist_ok=True)

    print(
        f"Starting - chunk size {chunk_size_mb}MB - service: litterbox (expires in {LITTERBOX_TIME}) - "
        f"upload workers: {upload_workers} - download connections: {download_workers} - "
        f"min delay between upload requests: {MIN_SECONDS_BETWEEN_REQUESTS}s",
        flush=True,
    )

    print("Checking source server capabilities...", flush=True)
    supports_ranges, total_size = check_range_support(url)
    if total_size:
        print(f"Source size: {total_size / (1024*1024):.1f} MB", flush=True)

    try:
        if supports_ranges and total_size:
            results = run_parallel_download(url, total_size, chunk_size, download_workers, upload_workers)
        else:
            results = run_sequential_download(url, chunk_size, upload_workers)
    except Exception as e:
        print(f"[error] Failed downloading the source file: {e}", flush=True)
        sys.exit(1)

    results.sort(key=lambda x: x[0])

    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        f.write(f"# Litterbox links - expire in {LITTERBOX_TIME}\n")
        for num, link in results:
            f.write(f"Part {num}: {link}\n")

    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

    print(f"[finished] All parts uploaded. Links saved to {LINKS_FILE} (expire in {LITTERBOX_TIME}).", flush=True)


if __name__ == "__main__":
    main()
