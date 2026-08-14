#!/usr/bin/env python3
import sys
import os
import shutil
import subprocess
import uuid
import requests

TEMP_DIR = "gofile_parts"
LINKS_FILE = "gofile_links.txt"

# إنشاء حاوية خفيفة وفريدة خصيصاً لملفاتك على Filebin
BIN_ID = uuid.uuid4().hex[:12]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

def upload_filebin_direct(filepath, filename):
    """رفع عبر Filebin - يوفر رابط تنزيل مباشر وصريح بدون صفحة تحميل"""
    url = f"https://filebin.net/{BIN_ID}/{filename}"
    cmd = ["curl", "-s", "-T", filepath, url]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if res.returncode == 0:
            # الرابط الناتج مباشر 100% للتحميل الفوري
            return url
        print(f"⚠️ استجابة Filebin: {res.stderr[:100]}", flush=True)
    except Exception as e:
        print(f"⚠️ خطأ Filebin: {e}", flush=True)
    return None

def upload_temp_sh_direct(filepath):
    """بديل احتياطي يوفر رابط تنزيل مباشر"""
    cmd = ["curl", "-s", "-F", f"file=@{filepath}", "https://temp.sh/upload"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        url = res.stdout.strip()
        if url.startswith("http://") or url.startswith("https://"):
            return url
        print(f"⚠️ استجابة temp.sh: {url[:100]}", flush=True)
    except Exception as e:
        print(f"⚠️ خطأ temp.sh: {e}", flush=True)
    return None

def upload_part(filepath, filename):
    print("🔄 جاري الرفع لتوليد رابط مباشر...", flush=True)
    link = upload_filebin_direct(filepath, filename)
    if link:
        return link

    print("⚠️ تجربة السيرفر الاحتياطي ذات الروابط المباشرة (temp.sh)...", flush=True)
    link = upload_temp_sh_direct(filepath)
    if link:
        return link

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

    print(f"⬇️ جاري المعالجة والتقسيم إلى أجزاء بحجم {chunk_size_mb}MB (روابط تنزيل مباشرة)...", flush=True)

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
                        link = upload_part(part_path, part_filename)
                        if not link:
                            raise RuntimeError(f"فشل رفع الجزء {part_num}")
                        
                        print(f"✅ رابط مباشر للجزء {part_num}: {link}", flush=True)
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
                    link = upload_part(part_path, part_filename)
                    if not link:
                        raise RuntimeError(f"فشل رفع الجزء {part_num}")
                    print(f"✅ رابط مباشر للجزء {part_num}: {link}", flush=True)
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
