#!/usr/bin/env python3
import sys
import os
import time
import shutil
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

TEMP_DIR = "gofile_parts"
LINKS_FILE = "gofile_links.txt"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
})

cached_token = None
cached_server = None

def init_gofile_session():
    """تهيئة الجلسة وجلب Token وسيرفر الرفع"""
    global cached_token, cached_server
    try:
        acc_res = session.post("https://api.gofile.io/accounts", timeout=15)
        if acc_res.status_code == 200 and acc_res.json().get("status") == "ok":
            cached_token = acc_res.json()["data"]["token"]
            session.cookies.set("accountToken", cached_token)

        srv_res = session.get("https://api.gofile.io/servers", timeout=15)
        if srv_res.status_code == 200 and srv_res.json().get("status") == "ok":
            servers = srv_res.json()["data"].get("servers", [])
            if servers:
                cached_server = servers[0]["name"]
    except Exception as e:
        print(f"⚠️ تنبيه أثناء التهيئة: {e}", flush=True)

def fetch_direct_link(folder_code, filename):
    """جلب الرابط المباشر الصريح فقط"""
    if not cached_token or not folder_code:
        return None
    try:
        url = f"https://api.gofile.io/contents/{folder_code}?token={cached_token}"
        res = session.get(url, timeout=15)
        if res.status_code == 200 and res.json().get("status") == "ok":
            children = res.json()["data"].get("children", {})
            for item_id, item in children.items():
                if item.get("link"):
                    return item.get("link")
    except Exception:
        pass
    return None

def upload_worker(part_num, filepath, filename, max_retries=3):
    """رفع الجزء وجلب رابط التنزيل المباشر"""
    server_name = cached_server or "store1"
    upload_url = f"https://{server_name}.gofile.io/contents/uploadfile"
    
    payload = {}
    if cached_token:
        payload["token"] = cached_token

    print(f"🚀 رفع الجزء {part_num}...", flush=True)
    
    for attempt in range(1, max_retries + 1):
        try:
            with open(filepath, "rb") as f:
                files = {"file": f}
                u_res = session.post(upload_url, data=payload, files=files, timeout=900)
            
            if u_res.status_code == 200:
                data = u_res.json()
                if data.get("status") == "ok":
                    fdata = data.get("data", {})
                    folder_code = fdata.get("code")
                    download_page = fdata.get("downloadPage")

                    direct_link = fetch_direct_link(folder_code, filename)
                    # إذا تعذر استخراج الرابط المباشر يتم إرجاع رابط التنزيل الأساسي لضمان عدم القفز عن الجزء
                    final_link = direct_link if direct_link else download_page

                    if os.path.exists(filepath):
                        os.remove(filepath)
                    
                    return part_num, final_link
        except Exception:
            pass
        
        time.sleep(2)

    if os.path.exists(filepath):
        os.remove(filepath)
    return part_num, None

def main():
    if len(sys.argv) < 2:
        print("❌ يرجى تزويد رابط الملف")
        sys.exit(1)

    url = sys.argv[1]
    chunk_size_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    CHUNK_SIZE = chunk_size_mb * 1024 * 1024
    os.makedirs(TEMP_DIR, exist_ok=True)

    init_gofile_session()

    print(f"⚡ بدء التقسيم والرفع (حجم الجزء: {chunk_size_mb}MB)...", flush=True)

    futures = []
    executor = ThreadPoolExecutor(max_workers=3)

    part_num = 1
    current_size = 0

    try:
        with session.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            part_filename = f"part_{part_num:03d}.bin"
            part_path = os.path.join(TEMP_DIR, part_filename)
            part_file = open(part_path, "wb")

            try:
                for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                    if not chunk:
                        continue
                    part_file.write(chunk)
                    current_size += len(chunk)

                    if current_size >= CHUNK_SIZE:
                        part_file.close()
                        future = executor.submit(upload_worker, part_num, part_path, part_filename)
                        futures.append(future)

                        part_num += 1
                        current_size = 0
                        part_filename = f"part_{part_num:03d}.bin"
                        part_path = os.path.join(TEMP_DIR, part_filename)
                        part_file = open(part_path, "wb")

                part_file.close()

                if current_size > 0:
                    future = executor.submit(upload_worker, part_num, part_path, part_filename)
                    futures.append(future)
                else:
                    if os.path.exists(part_path):
                        os.remove(part_path)

            finally:
                if not part_file.closed:
                    part_file.close()

    except Exception as e:
        print(f"❌ خطأ أثناء العملية: {e}", flush=True)
        executor.shutdown(wait=False)
        sys.exit(1)

    results = []
    for future in as_completed(futures):
        res = future.result()
        if res and res[1]:
            results.append(res)

    executor.shutdown(wait=True)
    
    # فرز النتيجة حسب رقم الجزء لضمان الترتيب
    results.sort(key=lambda x: x[0])

    print("\n🔗 --- الروابط المباشرة المرتبة ---", flush=True)
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        for num, link in results:
            out_str = f"الجزء {num}: {link}"
            print(out_str, flush=True)
            f.write(out_str + "\n")

    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

    print("\n✅ اكتملت العملية وتم حفظ الروابط المباشرة المرتبة بنجاح!", flush=True)

if __name__ == "__main__":
    main()
