#!/usr/bin/env python3
"""
سكربت تحميل + تقسيم + رفع لـ 0x0.st
الاستخدام: python split_upload.py <رابط_التحميل_المباشر>
"""

import sys
import os
import math
import requests

TEMP_DIR = "gofile_parts"
UPLOAD_URL = "https://0x0.st"
LINKS_FILE = "gofile_links.txt"
UPLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


def upload_part(filepath, retries=3):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with open(filepath, "rb") as f:
                resp = requests.post(
                    UPLOAD_URL,
                    files={"file": f},
                    headers=UPLOAD_HEADERS,
                    timeout=180,
                )
            resp.raise_for_status()
            link = resp.text.strip()
            if not link.startswith("http"):
                raise RuntimeError(f"استجابة غير متوقعة من 0x0.st: {link}")
            return link
        except Exception as e:
            last_err = e
            print(f"\n⚠️  محاولة {attempt}/{retries} فشلت: {e}")
    raise RuntimeError(f"فشل رفع {filepath} بعد {retries} محاولات: {last_err}")


def format_size(num_bytes):
    return f"{num_bytes / (1024 * 1024):.2f} MB"


def main():
    if len(sys.argv) < 2:
        print("الاستخدام: python split_upload.py <رابط_التحميل_المباشر> [حجم_الجزء_بالميغابايت]")
        sys.exit(1)

    url = sys.argv[1]
    chunk_size_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 190
    CHUNK_SIZE = chunk_size_mb * 1024 * 1024
    os.makedirs(TEMP_DIR, exist_ok=True)

    print(f"⬇️  بدء التحميل من:\n{url}\n")

    links = []
    part_num = 1
    current_size = 0
    downloaded_total = 0

    part_path = os.path.join(TEMP_DIR, f"part_{part_num:03d}")
    part_file = open(part_path, "wb")

    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total_size = int(r.headers.get("content-length", 0))
            if total_size:
                total_parts = math.ceil(total_size / CHUNK_SIZE)
                print(f"📦 الحجم الكلي: {format_size(total_size)} — سيقسم لـ ~{total_parts} جزء\n")

            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                part_file.write(chunk)
                current_size += len(chunk)
                downloaded_total += len(chunk)
                print(f"\r⬇️  تم تحميل: {format_size(downloaded_total)}", end="", flush=True)

                if current_size >= CHUNK_SIZE:
                    part_file.close()
                    print(f"\n📤 رفع الجزء {part_num} ({format_size(current_size)})...")
                    link = upload_part(part_path)
                    print(f"✅ رابط الجزء {part_num}: {link}")
                    links.append((part_num, link))
                    os.remove(part_path)

                    part_num += 1
                    current_size = 0
                    part_path = os.path.join(TEMP_DIR, f"part_{part_num:03d}")
                    part_file = open(part_path, "wb")

        part_file.close()

        if current_size > 0:
            print(f"\n📤 رفع الجزء الأخير {part_num} ({format_size(current_size)})...")
            link = upload_part(part_path)
            print(f"✅ رابط الجزء {part_num}: {link}")
            links.append((part_num, link))
            os.remove(part_path)
        else:
            os.remove(part_path)

    except Exception as e:
        part_file.close()
        print(f"\n❌ خطأ: {e}")
        sys.exit(1)

    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        for num, link in links:
            f.write(f"الجزء {num}: {link}\n")

    print("\n\n🎉 انتهى! جميع الروابط:")
    for num, link in links:
        print(f"  الجزء {num}: {link}")
    print(f"\n💾 الروابط محفوظة أيضًا في: {LINKS_FILE}")

    try:
        os.rmdir(TEMP_DIR)
    except OSError:
        pass


if __name__ == "__main__":
    main()
