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

def upload_gofile(filepath, filename):
    """رفع إلى GoFile الرسمي"""
    try:
        srv_resp = requests.get("https://api.gofile.io/servers", headers=HEADERS, timeout=15)
        if srv_resp.status_code == 200:
            srv_data = srv_resp.json()
            if srv_data.get("status") == "ok":
                servers = srv_data.get("data", {}).get("servers", [])
                if servers:
                    server_name = servers[0]["name"]
                    upload_url = f"https://{server_name}.gofile.io/contents/uploadfile"
                    with open(filepath, "rb") as f:
                        up_resp = requests.post(upload_url, files={"file": f}, headers=HEADERS, timeout=600)
                    if up_resp.status_code == 200:
                        res_json = up_resp.json()
                        if res_json.get("status") == "ok":
                            return res_json["data"]["downloadPage"]
    except Exception as e:
        print(f"GoFile log: {e}", flush=True)
    return None

def upload_litterbox(filepath, filename):
    """رفع إلى Litterbox (يدعم حتى 1GB للملف)"""
    try:
        cmd = [
            "curl", "-s",
            "-F", "reqtype=fileupload",
            "-F", "time=72h",
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
    """رفع إلى Transfer.sh"""
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

def upload_part_with_fallback(filepath, filename):
    """تجربة السيرفرات مع إعادة المحاولة والمهلة لمنع الحظر"""
    providers = [
        ("Litterbox", upload_litterbox),
        ("GoFile", upload_gofile),
        ("Transfer.sh", upload_transfersh),
    ]
    
    for name, provider in providers:
        print(f"🔄 جاري التجربة على {name}...", flush=True)
        for attempt in range(1, 3):
            url = provider(filepath, filename)
            if url:
                print(f"✅ تم الرفع بنجاح على {name}!", flush=True)
                return url
            if attempt < 2:
                print(f"⚠️ فشلت محاولة {attempt} على {name}، الانتظار 10 ثوانٍ للإعادة...", flush=True)
                time.sleep(10)
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
                        
                        print(f"✅ الجزء {part_num}: {link}", flush=True)
                        links.append((part_num, link))
                        os.remove(part_path)

                        # استراحة 8 ثوانٍ لتفادي حظر الطلبات السريعة
                        time.sleep(8)

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
