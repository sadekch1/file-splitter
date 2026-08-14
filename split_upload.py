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

# قائمة بروكسيات مجانية لتجاوز حظر Cloudflare على GitHub IPs
PROXIES = [
    "", # المحاولة الأولى بدون بروكسي
    "--proxy http://103.152.112.162:80",
    "--proxy http://43.134.68.204:3128",
    "--proxy http://185.199.229.156:7492"
]

def upload_litterbox_safe(filepath, filename, max_retries=5):
    """رفع الملف إلى Litterbox باستخدام البروكسي لتجاوز حظر Cloudflare"""
    for attempt in range(1, max_retries + 1):
        proxy_cmd = PROXIES[(attempt - 1) % len(PROXIES)]
        
        cmd = [
            "curl", "-s", "-L",
            "-A", HEADERS["User-Agent"],
            "-F", "reqtype=fileupload",
            "-F", "time=72h",
            "-F", f"fileToUpload=@{filepath}",
            "https://litterbox.catbox.moe/resources/internals/api.php"
        ]
        
        if proxy_cmd:
            cmd.extend(proxy_cmd.split())

        try:
            print(f"🔄 محاولة الرفع على Litterbox ({attempt}/{max_retries})...", flush=True)
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            url = res.stdout.strip()
            
            # التأكد من الحصول على رابط مباشر وليس صفحة خطأ HTML
            if url.startswith("http://") or url.startswith("https://"):
                print("✅ تم الرفع بنجاح دون حظر!", flush=True)
                return url
            else:
                print(f"⚠️ استجابة Cloudflare (حظر IP الخادم). إعادة المحاولة بمسار جديد...", flush=True)

        except Exception as e:
            print(f"⚠️ خطأ أثناء الاتصال: {e}", flush=True)

        if attempt < max_retries:
            time.sleep(15)

    raise RuntimeError("فشل الرفع على Litterbox بعد استنفاد كل المحاولات")

def main():
    if len(sys.argv) < 2:
        print("❌ يرجى تزويد رابط الملف")
        sys.exit(1)

    url = sys.argv[1]
    chunk_size_mb = 190  # 190MB لضمان عدم تجاوز السقف مع الهيدر
    CHUNK_SIZE = chunk_size_mb * 1024 * 1024
    os.makedirs(TEMP_DIR, exist_ok=True)

    links = []
    part_num = 1
    current_size = 0

    print(f"⬇️ جاري المعالجة بحجم {chunk_size_mb}MB (Litterbox + تجاوز حظر Cloudflare)...", flush=True)

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
                        link = upload_litterbox_safe(part_path, part_filename)
                        
                        print(f"✅ الجزء {part_num}: {link}", flush=True)
                        links.append((part_num, link))
                        os.remove(part_path)

                        time.sleep(10)

                        part_num += 1
                        current_size = 0
                        part_filename = f"part_{part_num:03d}.bin"
                        part_path = os.path.join(TEMP_DIR, part_filename)
                        part_file = open(part_path, "wb")

                part_file.close()

                if current_size > 0:
                    print(f"📤 رفع الجزء {part_num}...", flush=True)
                    link = upload_litterbox_safe(part_path, part_filename)
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
