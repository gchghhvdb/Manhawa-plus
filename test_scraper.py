#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import traceback
from cloud_monitor import get_latest


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("❌ لطفا یک لینک برای تست وارد کنید.")
        sys.exit(1)

    url = sys.argv[1].strip()
    include_paid = (len(sys.argv) > 2 and sys.argv[2].strip().lower() == "true")
    selector = sys.argv[3].strip() if len(sys.argv) > 3 else ""

    print("=" * 55)
    print("🔍 شروع تست لینک در موتور ۴ لایه Manhawa-Plus:")
    print(f"🔗 آدرس: {url}")
    print(f"🔓 شامل چپترهای قفل/پولی: {include_paid}")
    print("=" * 55)

    try:
        ch, link = get_latest(url, selector=selector, include_paid=include_paid)
        print("\n✅ نتیجه تست موفقیت‌آمیز بود!")
        print(f"📖 آخرین چپتر تشخیص داده شده: {ch}")
        print(f"🔗 لینک چپتر: {link}")
        print("=" * 55)
    except Exception as e:
        print(f"\n❌ خطا در تشخیص چپتر: {e}")
        print("-" * 55)
        traceback.print_exc()
        print("=" * 55)
        sys.exit(1)


if __name__ == "__main__":
    main()
