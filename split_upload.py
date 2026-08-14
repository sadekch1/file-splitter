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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

def upload_litterbox_safe(filepath, filename, max_retries=5):
    """رفع الملف إلى Litterbox مع معالجة ذكية لحظر Cloudflare"""
    cmd = [
        "curl", "-s", "-L",
        "-A", HEADERS["User-Agent"],
        "-F", "reqtype=fileupload",
        "-F", "time=72h",
        "-F", f"fileToUpload=@{filepath}",
        "https://litterbox.catbox.moe/resources/internals/api.php"
    ]

    for attempt in range(1, max_retries + 1):
        try:
            print(f"🔄 محاولة الرفع على Litterbox ({attempt}/{max_retries})...", flush=True)
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            url = res.stdout.strip()
            
            # التأكد من الحصول على رابط مباشر وليس صفحة خطأ HTML
            if url.startswith("http"):
                print("✅ تم الرفع بنجاح دون حظر!", flush=True)
                return url
            else:
                print(f"⚠️ استجابة غير معتادة (حظر مؤقت من السيرفر).", flush=True)

        except Exception as e:
            print(f"⚠️ خطأ في الاتصال: {e}", flush=True)

        if attempt < max_retries:
            wait_time = 45  # انتظار 45 ثانية لتصفير عداد الحظر
            print(f"⏳ جاري الانتظار {wait_time} ثانية لتصفير عداد الحظر لدى السيرفر...", flush=True)
            time.sleep(wait_time)

    raise RuntimeError("فشل الرفع على Litterbox بعد عدة محاولات")

def main():
    if len(sys.argv) < 2:
        print("❌ يرجى تزويد رابط الملف")
        sys.exit(1)

    url = sys.argv[1]
    # تثبيت الحجم الإفتراضي على 200MB بناءً على طلبك
    chunk_size_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    CHUNK_SIZE = chunk_size_mb * 1024 * 1024
    os.makedirs(TEMP_DIR, exist_ok=True)

    links = []
    part_num = 1
    current_size = 0

    print(f"⬇️ جاري المعالجة بحجم جزء {chunk_size_mb}MB (Litterbox حصراً مع نظام التبريد)...", flush=True)

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

                        # نظام التبريد الدوري: راحة 45 ثانية بعد كل 3 أجزاء، و 10 ثوانٍ بين الأجزاء العادية
                        if part_num % 3 == 0:
                            print("💤 [استراحة تبريد الـ IP] انتظار 45 ثانية لتفادي الحظر نهائياً...", flush=True)
                            time.sleep(45)
                        else:
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

    # حفظ الروابط
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        for num, link in links:
            f.write(f"الجزء {num}: {link}\n")

    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

if __name__ == "__main__":
    main()
