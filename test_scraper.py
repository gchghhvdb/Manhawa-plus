#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import re
import traceback
from urllib.parse import urlparse
from curl_cffi import requests
from cloud_monitor import get_latest

INDEX_URL = "https://raw.githubusercontent.com/keiyoushi/extensions/repo/index.min.json"
GITHUB_API_SEARCH = "https://api.github.com/search/code"
RAW_SOURCE_BASE = "https://raw.githubusercontent.com/keiyoushi/extensions-source/main"


def inspect_keiyoushi_trick(url: str):
    """کشف خودکار ترفند، قالب و API سایت از روی سورس‌کد اصلی Keiyoushi"""
    host = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    print(f"🕵️‍♂️ در حال استعلام «{host}» از مخزن سورس Keiyoushi...")

    try:
        r = requests.get(INDEX_URL, impersonate="chrome124", timeout=15)
        if r.status_code != 200:
            return
        index_data = r.json()
    except Exception as e:
        print(f"  ⚠️ خطا در دریافت ایندکس Keiyoushi: {e}")
        return

    matched_ext = None
    matched_src = None
    for ext in index_data:
        for src in ext.get("sources") or []:
            if host in (src.get("baseUrl") or "").lower():
                matched_ext = ext
                matched_src = src
                break
        if matched_ext:
            break

    if not matched_ext:
        print("  ℹ️ این دامنه در لیست رسمی Keiyoushi ثبت نشده است (استفاده از موتور عمومی).")
        return

    pkg = matched_ext.get("pkg", "")
    # تبدیل نام پکیج اندروید به مسیر پوشه در سورس گیت‌هاب
    # مثال: eu.kanade.tachiyomi.extension.en.asurascans -> src/en/asurascans
    parts = pkg.split(".")
    if len(parts) >= 2:
        lang_folder, site_folder = parts[-2], parts[-1]
        gradle_url = f"{RAW_SOURCE_BASE}/src/{lang_folder}/{site_folder}/build.gradle"
        print(f"  ✅ افزونه پیدا شد: {matched_src.get('name')} (مسیر سورس: src/{lang_folder}/{site_folder})")

        try:
            rg = requests.get(gradle_url, impersonate="chrome124", timeout=10)
            if rg.status_code == 200:
                gradle_txt = rg.text
                theme = re.search(r"themePkg\s*=\s*['\"]([^'\"]+)['\"]", gradle_txt)
                base_url = re.search(r"baseUrl\s*=\s*['\"]([^'\"]+)['\"]", gradle_txt)
                override_ver = re.search(r"overrideVersionCode\s*=\s*(\d+)", gradle_txt)

                if theme:
                    print(f"  🧩 قالب/موتور سایت در Keiyoushi: 【 {theme.group(1).upper()} 】")
                else:
                    print("  🧩 نوع سورس: اختصاصی (Custom Kotlin Source)")
                if base_url:
                    print(f"  🌐 آدرس پایه ثبت‌شده: {base_url.group(1)}")
        except Exception:
            pass


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("❌ لطفا یک لینک برای تست وارد کنید.")
        sys.exit(1)

    url = sys.argv[1].strip()
    include_paid = (len(sys.argv) > 2 and sys.argv[2].strip().lower() == "true")
    selector = sys.argv[3].strip() if len(sys.argv) > 3 else ""

    print("=" * 60)
    print("🔍 شروع کالبدشکافی و تست لینک در Manhawa-Plus:")
    print(f"🔗 آدرس: {url}")
    print(f"🔓 شامل چپترهای قفل/پولی: {include_paid}")
    print("-" * 60)

    # ۱. کالبدشکافی ترفند سایت از روی سورس‌کد Keiyoushi
    inspect_keiyoushi_trick(url)
    print("-" * 60)

    # ۲. استخراج آخرین چپتر
    try:
        ch, link = get_latest(url, selector=selector, include_paid=include_paid)
        print("\n✅ نتیجه تست موفقیت‌آمیز بود!")
        print(f"📖 آخرین چپتر تشخیص داده شده: {ch}")
        print(f"🔗 لینک چپتر: {link}")
        print("=" * 60)
    except Exception as e:
        print(f"\n❌ خطا در تشخیص چپتر: {e}")
        print("-" * 60)
        traceback.print_exc()
        print("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
