#!/usr/bin/env python3
import sys
import os
import shutil
import requests

TEMP_DIR = "gofile_parts"
LINKS_FILE = "gofile_links.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def upload_pixeldrain(filepath):
    """الرفع إلى Pixeldrain (يدعم حزم كبيرة ومستقر جداً مع GitHub Actions)"""
    try:
        url = "https://pixeldrain.com/api/file"
        with open(filepath, "rb") as f:
            res = requests.post(url, files={"file": f}, timeout=600)
            
        if res.status_code == 200 or res.status_code == 201:
            data = res.json()
            if data.get("success"):
                file_id = data.get("id")
                return f"https://pixeldrain.com/u/{file_id}"
        print(f"⚠️ Pixeldrain response ({res.status_code}): {res.text[:100]}", flush=True)
    except Exception as e:
        print(f"⚠️ استثناء Pixeldrain: {e}", flush=True)
    return None

def upload_tmpfiles(filepath):
    """بديل احتياطي: Tmpfiles.org"""
    try:
        url = "https://tmpfiles.org/api/v1/upload"
        with open(filepath, "rb") as f:
            res = requests.post(url, files={"file": f}, timeout=600)
            
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == "success":
                raw_url = data["data"]["url"]
                # تحويل الرابط إلى رابط تنزيل مباشر
                direct_url = raw_url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
                return direct_url
        print(f"⚠️ Tmpfiles response ({res.status_code}): {res.text[:100]}", flush=True)
    except Exception as e:
        print(f"⚠️ استثناء Tmpfiles: {e}", flush=True)
    return None

def upload_part(filepath, filename):
    print("🔄 جاري الرفع على Pixeldrain...", flush=True)
    link = upload_pixeldrain(filepath)
    if link:
        return link
        
    print("⚠️ تجربة السيرفر الاحتياطي (Tmpfiles)...", flush=True)
    link = upload_tmpfiles(filepath)
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

    print(f"⬇️ جاري معالجة الملف وتقسيمه إلى أجزاء بحجم {chunk_size_mb}MB...", flush=True)

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
                    link = upload_part(part_path, part_filename)
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
