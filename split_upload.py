#!/usr/bin/env python3
"""
Download a large file, split it into parts, and upload each part in
parallel to Litterbox (litterbox.catbox.moe) - anonymous, no account
needed, returns a real direct download link (not a landing page).

All uploaded parts expire after 1 hour (Litterbox's shortest retention
option), so make sure to download them before the link expires.

Fast settings: full 200MB chunks (no reduction in uploaded part size),
4 parallel workers, and a shorter 1s delay between requests - so the
download+upload pipeline moves as fast as the service allows.

Usage:
    python3 multi_upload.py <file_url> [chunk_size_mb] [max_workers] [seconds_between_requests]

Example (fast, default):
    python3 multi_upload.py "https://example.com/file.zip"

Example (even more parallel workers):
    python3 multi_upload.py "https://example.com/file.zip" 200 6 0.5
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
# Request rate limiting - avoids tripping the service's abuse protection
# instead of trying to bypass it.
# ---------------------------------------------------------------------------
MIN_SECONDS_BETWEEN_REQUESTS = 1.0
_rate_lock = threading.Lock()
_last_request_time = [0.0]


def throttle():
    """Waits as needed before sending a new request, so we don't exceed
    the service's request rate."""
    with _rate_lock:
        now = time.time()
        wait = MIN_SECONDS_BETWEEN_REQUESTS - (now - _last_request_time[0])
        if wait > 0:
            time.sleep(wait)
        _last_request_time[0] = time.time()


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
    print(f"[background] Starting upload of part {part_num} to Litterbox ({LITTERBOX_TIME})...", flush=True)

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


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: python3 multi_upload.py <file_url> "
            "[chunk_size_mb] [max_workers] [seconds_between_requests]"
        )
        print(f"All links expire after {LITTERBOX_TIME} (Litterbox).")
        print("Defaults: 200MB chunks, 4 parallel workers, 1s min delay (fast).")
        sys.exit(1)

    url = sys.argv[1]
    chunk_size_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    max_workers = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    global MIN_SECONDS_BETWEEN_REQUESTS
    MIN_SECONDS_BETWEEN_REQUESTS = float(sys.argv[4]) if len(sys.argv) > 4 else MIN_SECONDS_BETWEEN_REQUESTS

    CHUNK_SIZE = chunk_size_mb * 1024 * 1024
    os.makedirs(TEMP_DIR, exist_ok=True)

    print(
        f"Starting - chunk size {chunk_size_mb}MB - service: litterbox (expires in {LITTERBOX_TIME}) - "
        f"workers: {max_workers} - min delay between requests: {MIN_SECONDS_BETWEEN_REQUESTS}s",
        flush=True,
    )

    futures = []
    executor = ThreadPoolExecutor(max_workers=max_workers)

    part_num = 1
    current_size = 0

    try:
        with session.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            part_filename = f"part_{part_num:03d}.bin"
            part_path = os.path.join(TEMP_DIR, part_filename)
            part_file = open(part_path, "wb")

            try:
                for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                    if not chunk:
                        continue
                    part_file.write(chunk)
                    current_size += len(chunk)

                    if current_size >= CHUNK_SIZE:
                        part_file.close()
                        future = executor.submit(upload_worker, part_num, part_path)
                        futures.append(future)

                        part_num += 1
                        current_size = 0
                        part_filename = f"part_{part_num:03d}.bin"
                        part_path = os.path.join(TEMP_DIR, part_filename)
                        part_file = open(part_path, "wb")

                part_file.close()

                if current_size > 0:
                    future = executor.submit(upload_worker, part_num, part_path)
                    futures.append(future)
                else:
                    if os.path.exists(part_path):
                        os.remove(part_path)

            finally:
                if not part_file.closed:
                    part_file.close()

    except Exception as e:
        print(f"[error] Failed downloading the source file: {e}", flush=True)
        executor.shutdown(wait=False)
        sys.exit(1)

    results = []
    for future in as_completed(futures):
        res = future.result()
        if res and res[1]:
            results.append(res)
        else:
            print("[error] One of the background uploads failed!", flush=True)

    executor.shutdown(wait=True)
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
