#!/usr/bin/env python3
import sys
import os
import time
import shutil
import subprocess
import requests

TEMP_DIR = "gofile_parts"
LINKS_FILE = "gofile_links.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

def upload_0x0(filepath):
    """رفع خفيف وسريع على 0x0.st (يدعم حتى 512MB ويرجع رابطاً نصياً مباشرة)"""
    cmd = [
        "curl", "-s", "-F", f"file=@{filepath}", "https://0x0.st"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        url = res.stdout.strip()
        if url.startswith("http://") or url.startswith("https://"):
            return url
    except Exception as e:
        print(f"⚠️ خطأ في 0x0.st: {e}", flush=True)
    return None

def upload_transfersh(filepath, filename):
    """بديل خفيف ثانٍ: transfer.sh"""
    cmd = [
        "curl", "-s", "--upload-file", filepath, f"https://transfer.sh/{filename}"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        url = res.stdout.strip()
        if url.startswith("http://") or url.startswith("https://"):
            return url
    except Exception as e:
        print(f"⚠️ خطأ في transfer.sh: {e}", flush=True)
    return None

def upload_part_fast(filepath, filename):
    # المحاولة الأولى على 0x0.st
    url = upload_0x0(filepath)
    if url:
        print("✅ تم الرفع عبر 0x0.st", flush=True)
        return url
    
    # المحاولة الثانية على transfer.sh
    print("⚠️ تجربة السيرفر الخفيف الثاني (transfer.sh)...", flush=True)
    url = upload_transfersh(filepath, filename)
    if url:
        print("✅ تم الرفع عبر transfer.sh", flush=True)
        return url
        
    return None

def main():
    if len(sys.argv) < 2:
        print("❌ يرجى تزويد رابط الملف")
        sys.exit(1)

    url = sys.argv[1]
    chunk_size_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    CHUNK_SIZE = chunk_size_mb * 1024 * 1024
    os.makedirs(TEMP_DIR, exist_ok=True)

    links = []
    part_num = 1
    current_size = 0

    print(f"⬇️ جاري المعالجة بحجم {chunk_size_mb}MB عبر سيرفر خفيف وسريع...", flush=True)

    try:
        with requests.get(url, stream=True, headers=HEADERS, timeout=60) as r:
            r.raise_for_status()
            part_filename = f"part_{part_num:03d}.bin"
            part_path = os.path.join(TEMP_DIR, part_filename)
            part_file = open(part_path, "wb")

            try:
                for chunk in r.iter_content(chunk_size=2 * 1024 * 1024):
                    if not chunk:
                        continue
                    part_file.write(chunk)
                    current_size += len(chunk)

                    if current_size >= CHUNK_SIZE:
                        part_file.close()
                        print(f"📤 رفع الجزء {part_num}...", flush=True)
                        link = upload_part_fast(part_path, part_filename)
                        if not link:
                            raise RuntimeError(f"فشل رفع الجزء {part_num}")
                        
                        print(f"✅ الجزء {part_num}: {link}", flush=True)
                        links.append((part_num, link))
                        os.remove(part_path)

                        part_num += 1
                        current_size = 0
                        part_filename = f"part_{part_num:03d}.bin"
                        part_path = os.path.join(TEMP_DIR, part_filename)
                        part_file = open(part_path, "wb")

                part_file.close()

                if current_size > 0:
                    print(f"📤 رفع الجزء {part_num}...", flush=True)
                    link = upload_part_fast(part_path, part_filename)
                    if not link:
                        raise RuntimeError(f"فشل رفع الجزء {part_num}")
                    print(f"✅ الجزء {part_num}: {link}", flush=True)
                    links.append((part_num, link))
                    os.remove(part_path)
                else:
                    if os.path.exists(part_path):
                        os.remove(part_path)

            finally:
                if not part_file.closed:
                    part_file.close()

    except Exception as e:
        print(f"❌ خطأ أثناء العملية: {e}", flush=True)
        sys.exit(1)

    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        for num, link in links:
            f.write(f"الجزء {num}: {link}\n")

    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

if __name__ == "__main__":
    main()
