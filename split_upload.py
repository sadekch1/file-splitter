#!/usr/bin/env python3
"""
تنزيل ملف كبير، تقسيمه لأجزاء، ورفع كل جزء بالتوازي على خدمة
ترجع رابط مباشر (Direct Link) بدل صفحة تحميل.

الخدمات المدعومة (كلها مجانية وبدون حساب/توكن):
  - catbox   -> https://catbox.moe        (رابط مباشر دائم)
  - pixeldrain -> https://pixeldrain.com  (رابط مباشر دائم، حجم أكبر)
  - 0x0      -> https://0x0.st            (رابط مباشر، الملفات مؤقتة حسب الحجم)

الاستخدام:
    python3 multi_upload.py <رابط الملف> [حجم_الجزء_ميجابايت] [service]

مثال:
    python3 multi_upload.py "https://example.com/file.zip" 200 pixeldrain
"""
import sys
import os
import time
import shutil
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

import threading

TEMP_DIR = "upload_parts"
LINKS_FILE = "direct_links.txt"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
})

# ---------------------------------------------------------------------------
# تنظيم معدل الطلبات (rate limiting) - يمنع الوصول لحد الحظر بدل محاولة الالتفاف عليه
# ---------------------------------------------------------------------------
MIN_SECONDS_BETWEEN_REQUESTS = 3.0  # فاصل أدنى بين أي طلبين متتاليين لنفس الخدمة
_rate_lock = threading.Lock()
_last_request_time = [0.0]


def throttle():
    """ينتظر الوقت اللازم قبل إرسال أي طلب جديد، لضمان عدم تجاوز معدل الخدمة."""
    with _rate_lock:
        now = time.time()
        wait = MIN_SECONDS_BETWEEN_REQUESTS - (now - _last_request_time[0])
        if wait > 0:
            time.sleep(wait)
        _last_request_time[0] = time.time()


# ---------------------------------------------------------------------------
# دوال الرفع الخاصة بكل خدمة - كل واحدة ترجع (نجاح: bool, رابط_مباشر أو رسالة خطأ)
# ---------------------------------------------------------------------------

def upload_catbox(filepath):
    with open(filepath, "rb") as f:
        files = {"fileToUpload": f}
        data = {"reqtype": "fileupload"}
        r = session.post("https://catbox.moe/user/api.php", data=data, files=files, timeout=900)
    if r.status_code == 200 and r.text.strip().startswith("http"):
        return True, r.text.strip()
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


def upload_pixeldrain(filepath):
    filename = os.path.basename(filepath)
    with open(filepath, "rb") as f:
        r = session.post(
            f"https://pixeldrain.com/api/file/{filename}",
            data=f,
            timeout=900,
        )
    if r.status_code in (200, 201):
        data = r.json()
        if data.get("success") and data.get("id"):
            return True, f"https://pixeldrain.com/api/file/{data['id']}?download"
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


def upload_0x0(filepath):
    with open(filepath, "rb") as f:
        files = {"file": f}
        r = session.post("https://0x0.st", files=files, timeout=900)
    if r.status_code == 200 and r.text.strip().startswith("http"):
        return True, r.text.strip()
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


SERVICES = {
    "catbox": upload_catbox,
    "pixeldrain": upload_pixeldrain,
    "0x0": upload_0x0,
}


# ---------------------------------------------------------------------------

def is_rate_limited(error_text):
    text = str(error_text).lower()
    return "429" in text or "rate" in text or "too many" in text


def upload_worker(part_num, filepath, service_name, max_retries=5):
    upload_fn = SERVICES[service_name]
    print(f"🚀 [خلفية] بدء رفع الجزء {part_num} على {service_name}...", flush=True)

    backoff = 5.0  # ثواني - يتضاعف عند كل حظر مؤقت (429)

    for attempt in range(1, max_retries + 1):
        throttle()  # احترام الحد الأدنى بين الطلبات قبل أي محاولة
        try:
            ok, result = upload_fn(filepath)
            if ok:
                print(f"✅ [اكتمل] الجزء {part_num}: {result}", flush=True)
                if os.path.exists(filepath):
                    os.remove(filepath)
                return part_num, result

            print(f"⚠️ محاولة {attempt} للجزء {part_num} فشلت: {result}", flush=True)
            if is_rate_limited(result):
                print(f"⏳ تم تجاوز حد الطلبات، الانتظار {backoff:.0f} ثانية قبل إعادة المحاولة...", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)  # exponential backoff بحد أقصى دقيقتين
                continue
        except Exception as e:
            print(f"⚠️ خطأ أثناء رفع الجزء {part_num} (محاولة {attempt}): {e}", flush=True)

        time.sleep(2)

    if os.path.exists(filepath):
        os.remove(filepath)
    return part_num, None


def main():
    if len(sys.argv) < 2:
        print(
            "❌ الاستخدام: python3 multi_upload.py <رابط الملف> "
            "[حجم_الجزء_ميجابايت] [service] [عدد_العمال] [فاصل_بالثواني]"
        )
        print(f"   الخدمات المتاحة: {', '.join(SERVICES.keys())}")
        sys.exit(1)

    url = sys.argv[1]
    chunk_size_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    service_name = sys.argv[3] if len(sys.argv) > 3 else "catbox"
    max_workers = int(sys.argv[4]) if len(sys.argv) > 4 else 2
    global MIN_SECONDS_BETWEEN_REQUESTS
    MIN_SECONDS_BETWEEN_REQUESTS = float(sys.argv[5]) if len(sys.argv) > 5 else MIN_SECONDS_BETWEEN_REQUESTS

    if service_name not in SERVICES:
        print(f"❌ خدمة غير معروفة: {service_name}")
        print(f"   الخدمات المتاحة: {', '.join(SERVICES.keys())}")
        sys.exit(1)

    CHUNK_SIZE = chunk_size_mb * 1024 * 1024
    os.makedirs(TEMP_DIR, exist_ok=True)

    print(
        f"⚡ بدء العملية - حجم الجزء {chunk_size_mb}MB - خدمة: {service_name} - "
        f"عدد العمال المتزامنين: {max_workers} - فاصل بين الطلبات: {MIN_SECONDS_BETWEEN_REQUESTS}ث",
        flush=True,
    )

    futures = []
    executor = ThreadPoolExecutor(max_workers=max_workers)

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
                        future = executor.submit(upload_worker, part_num, part_path, service_name)
                        futures.append(future)

                        part_num += 1
                        current_size = 0
                        part_filename = f"part_{part_num:03d}.bin"
                        part_path = os.path.join(TEMP_DIR, part_filename)
                        part_file = open(part_path, "wb")

                part_file.close()

                if current_size > 0:
                    future = executor.submit(upload_worker, part_num, part_path, service_name)
                    futures.append(future)
                else:
                    if os.path.exists(part_path):
                        os.remove(part_path)

            finally:
                if not part_file.closed:
                    part_file.close()

    except Exception as e:
        print(f"❌ خطأ أثناء تنزيل الملف الأصلي: {e}", flush=True)
        executor.shutdown(wait=False)
        sys.exit(1)

    results = []
    for future in as_completed(futures):
        res = future.result()
        if res and res[1]:
            results.append(res)
        else:
            print("❌ فشلت إحدى عمليات الرفع في الخلفية!", flush=True)

    executor.shutdown(wait=True)
    results.sort(key=lambda x: x[0])

    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        for num, link in results:
            f.write(f"الجزء {num}: {link}\n")

    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

    print(f"🏁 انتهت جميع العمليات - الروابط المباشرة محفوظة في {LINKS_FILE}", flush=True)


if __name__ == "__main__":
    main()
