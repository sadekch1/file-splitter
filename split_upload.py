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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
}

def upload_litterbox(filepath, filename):
    """رفع الملف إلى Litterbox (72 ساعة)"""
    try:
        cmd = [
            "curl", "-s", "-L",
            "-A", HEADERS["User-Agent"],
            "-F", "reqtype=fileupload",
            "-F", "time=72h",
            "-F", f"fileToUpload=@{filepath}",
            "https://litterbox.catbox.moe/resources/internals/api.php"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        url = res.stdout.strip()
        if url.startswith("http://") or url.startswith("https://"):
            return url
    except Exception as e:
        print(f"Litterbox log: {e}", flush=True)
    return None

def upload_catbox(filepath, filename):
    """رفع الملف إلى Catbox الرسمية (روابط دائمة لأجزاء 200MB)"""
    try:
        cmd = [
            "curl", "-s", "-L",
            "-A", HEADERS["User-Agent"],
            "-F", "reqtype=fileupload",
            "-F", f"fileToUpload=@{filepath}",
            "https://catbox.moe/user/api.php"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        url = res.stdout.strip()
        if url.startswith("http://") or url.startswith("https://"):
            return url
    except Exception as e:
        print(f"Catbox log: {e}", flush=True)
    return None

def upload_part_safe(filepath, filename, max_retries=5):
    """الرفع مع التبديل الذكي بين Litterbox و Catbox لحجم 200MB"""
    providers = [
        ("Litterbox", upload_litterbox),
        ("Catbox", upload_catbox)
    ]

    for attempt in range(1, max_retries + 1):
        for name, provider in providers:
            print(f"🔄 محاولة الرفع عبر {name} (المحاولة {attempt}/{max_retries})...", flush=True)
            url = provider(filepath, filename)
            if url:
                print(f"✅ تم الرفع بنجاح عبر {name}!", flush=True)
                return url
            print(f"⚠️ متعذر على {name}، تجربة السيرفر الآخر...", flush=True)

        if attempt < max_retries:
            print("⏳ انتظار 30 ثانية قبل إعادة المحاولة لتجنب الحظر...", flush=True)
            time.sleep(30)

    raise RuntimeError("فشل الرفع على Litterbox و Catbox بعد عدة محاولات")

def main():
    if len(sys.argv) < 2:
        print("❌ يرجى تزويد رابط الملف")
        sys.exit(1)

    url = sys.argv[1]
    # ضبط الحجم على 190MB ليصقل الحجم النهائي تحت 200MB بالضبط مع الهيدر
    chunk_size_mb = 190
    CHUNK_SIZE = chunk_size_mb * 1024 * 1024
    os.makedirs(TEMP_DIR, exist_ok=True)

    links = []
    part_num = 1
    current_size = 0

    print(f"⬇️ جاري المعالجة بحجم أجزاء آمن تحت 200MB (Litterbox / Catbox)...", flush=True)

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
                        link = upload_part_safe(part_path, part_filename)
                        
                        print(f"✅ الجزء {part_num}: {link}", flush=True)
                        links.append((part_num, link))
                        os.remove(part_path)

                        # استراحة 15 ثانية بين كل جزء
                        time.sleep(15)

                        part_num += 1
                        current_size = 0
                        part_filename = f"part_{part_num:03d}.bin"
                        part_path = os.path.join(TEMP_DIR, part_filename)
                        part_file = open(part_path, "wb")

                part_file.close()

                if current_size > 0:
                    print(f"📤 رفع الجزء {part_num}...", flush=True)
                    link = upload_part_safe(part_path, part_filename)
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
