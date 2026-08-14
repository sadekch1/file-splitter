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

def upload_litterbox(filepath, filename):
    """رفع إلى Litterbox (يدعم حتى 1GB للملف وتدوم الروابط 24 ساعة)"""
    try:
        cmd = [
            "curl", "-s",
            "-F", "reqtype=fileupload",
            "-F", "time=24h",
            "-F", f"fileToUpload=@{filepath}",
            "https://litterbox.catbox.moe/resources/internals/api.php"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        url = res.stdout.strip()
        if url.startswith("http"):
            return url
    except Exception as e:
        print(f"Litterbox log: {e}", flush=True)
    return None

def upload_transfersh(filepath, filename):
    """رفع إلى Transfer.sh (سريع جداً ومستقر)"""
    try:
        cmd = [
            "curl", "-s",
            "--upload-file", filepath,
            f"https://transfer.sh/{filename}"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        url = res.stdout.strip()
        if url.startswith("http"):
            return url
    except Exception as e:
        print(f"Transfer.sh log: {e}", flush=True)
    return None

def upload_pixeldrain(filepath, filename):
    """رفع إلى Pixeldrain عبر POST multipart"""
    try:
        cmd = [
            "curl", "-s",
            "-F", f"file=@{filepath};filename={filename}",
            "https://pixeldrain.com/api/file"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if res.stdout:
            data = json.loads(res.stdout)
            if data.get("success"):
                return f"https://pixeldrain.com/u/{data.get('id')}"
    except Exception as e:
        print(f"Pixeldrain log: {e}", flush=True)
    return None

def upload_part_with_fallback(filepath, filename):
    """تجربة السيرفرات بالترتيب لضمان نجاح الرفع دائماً"""
    providers = [
        ("Litterbox", upload_litterbox),
        ("Transfer.sh", upload_transfersh),
        ("Pixeldrain", upload_pixeldrain),
    ]
    
    for name, provider in providers:
        print(f"🔄 جاري التجربة على {name}...", flush=True)
        url = provider(filepath, filename)
        if url:
            print(f"✅ تم الرفع بنجاح على {name}!", flush=True)
            return url
        print(f"⚠️ متعذر على {name}، الانتقال للبديل...", flush=True)
        
    raise RuntimeError("فشلت جميع سيرفرات الرفع المتاحة")

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

    print(f"⬇️ جاري تحميل وتجزئة الملف بحجم {chunk_size_mb}MB...", flush=True)

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
                        print(f"📤 رفع الجزء {part_num}...", flush=True)
                        link = upload_part_with_fallback(part_path, part_filename)
                        
                        # الرمز ✅ ليتعرف عليه سكربت Termux
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
                    link = upload_part_with_fallback(part_path, part_filename)
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
