#!/usr/bin/env python3
import sys
import os
import time
import shutil
import subprocess
import requests

TEMP_DIR = "gofile_parts"
LINKS_FILE = "gofile_links.txt"

# هيدر مخصص لمنع تصنيف الطلب كـ Spam
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

def upload_litterbox_safe(filepath, filename, max_retries=5):
    """رفع آمن مع نظام الانتظار التراكمي لتفادي الحظر"""
    cmd = [
        "curl", "-s",
        "-A", HEADERS["User-Agent"],
        "-F", "reqtype=fileupload",
        "-F", "time=72h",
        "-F", f"fileToUpload=@{filepath}",
        "https://litterbox.catbox.moe/resources/internals/api.php"
    ]

    wait_time = 15  # البداية بانتظار 15 ثانية عند حدوث مشكلة

    for attempt in range(1, max_retries + 1):
        try:
            print(f"🔄 محاولة الرفع ({attempt}/{max_retries})...", flush=True)
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            url = res.stdout.strip()
            
            if url.startswith("http"):
                print("✅ تم الرفع بنجاح دون حظر!", flush=True)
                return url
            else:
                print(f"⚠️ السيرفر مشغول أو يفرض حظراً مؤقتاً. استجابة: {url[:50]}", flush=True)

        except Exception as e:
            print(f"⚠️ خطأ في الاتصال: {e}", flush=True)

        if attempt < max_retries:
            print(f"⏳ انتظار حماية للحظر لمدة {wait_time} ثانية قبل الإعادة...", flush=True)
            time.sleep(wait_time)
            wait_time *= 2  # مضاعفة الوقت (15s -> 30s -> 60s -> 120s)

    raise RuntimeError("تعذر الرفع بعد استنفاد محاولات الحماية")

def main():
    if len(sys.argv) < 2:
        print("❌ يرجى تزويد رابط الملف")
        sys.exit(1)

    url = sys.argv[1]
    # افتراضياً 1000MB (1GB) لتقليل عدد الأجزاء والابتعاد عن الحظر
    chunk_size_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
    CHUNK_SIZE = chunk_size_mb * 1024 * 1024
    os.makedirs(TEMP_DIR, exist_ok=True)

    links = []
    part_num = 1
    current_size = 0

    print(f"⬇️ جاري المعالجة بحجم جزء {chunk_size_mb}MB (نظام أمان Litterbox)...", flush=True)

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

                        # استراحة أمان 10 ثوانٍ بين كل جزء
                        print("💤 استراحة آمنة لمنع الحظر...", flush=True)
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
