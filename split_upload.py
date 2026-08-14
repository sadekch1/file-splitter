#!/usr/bin/env python3
import sys
import os
import json
import shutil
import subprocess
import requests

TEMP_DIR = "gofile_parts"
LINKS_FILE = "gofile_links.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

def upload_with_curl(filepath, filename, retries=3):
    """استخدام curl المستقر لتفادي انقطاع اتصال SSL مع الملفات الضخمة"""
    url = f"https://pixeldrain.com/api/file/{filename}"
    
    for attempt in range(1, retries + 1):
        try:
            cmd = [
                "curl", "-s",
                "-X", "PUT",
                "-T", filepath,
                "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                url
            ]
            
            # تنفيذ أمر curl مباشرة من نظام التشغيل
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            
            if result.returncode == 0 and result.stdout:
                try:
                    data = json.loads(result.stdout)
                    if data.get("success"):
                        file_id = data.get("id")
                        return f"https://pixeldrain.com/u/{file_id}"
                except json.JSONDecodeError:
                    pass
            
            print(f"⚠️ محاولة رفع {attempt}/{retries} فشلت، جاري الإعادة...", flush=True)
            
        except Exception as e:
            print(f"⚠️ خطأ أثناء تنفيذ محاولة {attempt}: {e}", flush=True)
            
    raise RuntimeError(f"فشل الرفع بعد {retries} محاولات")

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

    print(f"⬇️ جاري بدء تحميل الملف وتجزئته بحجم {chunk_size_mb} ميجابايت...", flush=True)

    try:
        with requests.get(url, stream=True, headers=HEADERS, timeout=60) as r:
            r.raise_for_status()
            part_filename = f"part_{part_num:03d}.bin"
            part_path = os.path.join(TEMP_DIR, part_filename)
            part_file = open(part_path, "wb")

            try:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    part_file.write(chunk)
                    current_size += len(chunk)

                    if current_size >= CHUNK_SIZE:
                        part_file.close()
                        print(f"📤 جاري رفع الجزء {part_num} عبر curl...", flush=True)
                        link = upload_with_curl(part_path, part_filename)
                        
                        # الرمز ✅ ضروري ليتعرف عليه سكربت Termux
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
                    print(f"📤 جاري رفع الجزء {part_num} عبر curl...", flush=True)
                    link = upload_with_curl(part_path, part_filename)
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
