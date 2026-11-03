#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Manhawa-Plus: Multi-Engine Autonomous Comic & Manhwa Monitor
مجهز به ۴ لایه تشخیص خودکار:
1. ماژول‌های اختصاصی و API مستقیم (Naver, Asura, ComicWalker, HeanCMS/Keyoapp, MangaHub)
2. موتور قالب‌های جهانی Keiyoushi/Tachiyomi (Madara AJAX, MangaThemesia,WP-Manga, NextData/JSON)
3. موتور ایزوله‌سازی DOM (حذف سایدبار/تبلیغات و فیلتر هوشمند Slug برای جلوگیری از تشخیص اشتباه)
4. مرورگر مخفی Camoufox (در پروسه ایزوله برای عبور از Cloudflare Turnstile و شنود ترافیک شبکه)
"""
import json
import os
import re
import sys
import subprocess
import unicodedata
from urllib.parse import urljoin, urlparse
from curl_cffi import requests
from bs4 import BeautifulSoup

try:
    import warnings
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    warnings.filterwarnings("ignore")
except ImportError:
    pass

LIST_FILE = "manhwa_list.json"
SUBS_FILE = "subscribers.json"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TIMEOUT = 25

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko,ja,en-US;q=0.9,en;q=0.8",
}

# الگوهای استاندارد استخراج شماره چپتر
CHAPTER_RE = re.compile(
    r"(?<![a-z])(?:chapter|chap|ch|episode|ep|ملف|فصل|چپتر)[\s\-_.:#/]*(\d{1,5}(?:\.\d+)?)(?!\d)",
    re.I,
)
HASH_NUM_RE = re.compile(r"(?:^|[\s\#])#?(\d{1,4}(?:\.\d+)?)(?:\s|$)")
JP_RE = re.compile(r"第\s*(\d+(?:\.\d+)?)\s*話(?:\s*(?:その|パート|part|\.|[-_])?\s*(\d+))?", re.I)
KR_RE = re.compile(r"(?:제\s*)?(\d+(?:\.\d+)?)\s*화")
RSS_N_RE = re.compile(r"(?:^|[\[\(【\s])(\d+(?:\.\d+)?)\s*話(?:\s*(?:その|パート)?\s*(\d+))?")
KEY_RE = re.compile(r"\s*(\d+(?:\.\d+)?)(?:\s*その\s*(\d+))?")

# الگوهای تشخیص چپترهای پولی/قفل‌دار و بخش‌های پیشنهادی
RECO_RE = re.compile(
    r"recommend|related|similar|you may also|more like|trending|popular|weekly|monthly|hot\s*manga|latest\s*updates|new\s*releases|sidebar",
    re.I,
)
PAID_RE = re.compile(
    r"\b(?:lock(?:ed)?|coins?|premium|paywall|early\s*access|vip\s*only)\b"
    r"|asura\+|lucide-lock|fa-lock|icon-lock|🔒|🪙|\b\d+\s*<svg",
    re.I,
)
OPEN_RE = re.compile(r"[\w-]*(?:unlock|lock[\w-]*open|free)[\w-]*", re.I)
EMB_RE = re.compile(r"\{[^{}]*?chapter_(?:name|slug)[^{}]*\}")
LOCKED_RE = re.compile(
    r"locked chapter|waiting to be unlocked|chapter is locked|login to unlock chapter"
    r"|cost to unlock|sign in to unlock|unlock this (?:exciting )?chapter"
    r"|early access|asura\+|subscribe to read|purchase this chapter|unlock with"
    r"|this chapter is locked|premium chapter|read early with",
    re.I,
)

# سلکتورهای استاندارد مخزن Keiyoushi (برای قالب‌های Madara, MangaThemesia, WP-Manga, FoolSlide, Mangabox و...)
KEIYOUSHI_CHAPTER_SELECTORS = [
    "li.wp-manga-chapter > a",                   # Madara Standard
    "#chapterlist ul li a",                      # MangaThemesia / Mangastream
    "div.eph-num a",                             # MangaThemesia Variant
    "ul.main.version-chap li.wp-manga-chapter a",# Madara Variant
    "div.chapter-list div.row span a",           # Mangakakalot / Mangabox
    "ul.row-content-chapter li a.chapter-name",  # Manganelo / Manganato
    "div#list-chapter ul.list-chapter li a",     # Truyen / Novel / Manhua templates
    "div.list-chapter ul li a",
    "table.table-chapter tbody tr td a",
    "div.chapters-list a[href*='chapter']",
    "div#chapters a[href*='chapter']",
    "ul.chapter-list li a",
    "div.episode-list a",
    "a.chapter-link",
]


def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", str(s or ""))


def _jp(m):
    num_str = m.group(1)
    n = float(num_str)
    sub = int(m.group(2) or 0)
    base = str(int(n)) if n.is_integer() else num_str
    return (n, sub), (f"{base} その{sub}" if sub else base)


def chap_key(s):
    m = KEY_RE.match(norm(s))
    return (float(m.group(1)), int(m.group(2) or 0)) if m else None


def _paid_text(x) -> bool:
    return bool(PAID_RE.search(OPEN_RE.sub(" ", str(x))))


def _chap_anchor(x) -> bool:
    if getattr(x, "name", None) != "a":
        return False
    text = norm(x.get_text(" ", strip=True) + " " + (x.get("href") or ""))
    return bool(CHAPTER_RE.search(text) or JP_RE.search(text))


def is_paid(tag) -> bool:
    if _paid_text(tag):
        return True
    # بررسی تگ والد در قالب‌های MangaThemesia / Madara (مثل کلاس‌های قفل یا آیکون سکه)
    row, node = tag, tag.parent
    while node is not None and getattr(node, "name", None) not in (None, "body", "html", "[document]"):
        if sum(1 for x in node.find_all("a") if _chap_anchor(x)) > 1:
            break
        row, node = node, node.parent
    if row is not tag and _paid_text(row):
        return True
    for n, sib in enumerate(tag.next_siblings):
        if n >= 3 or (getattr(sib, "name", None) and (_chap_anchor(sib) or any(_chap_anchor(x) for x in sib.find_all("a")))):
            break
        if _paid_text(sib):
            return True
    return False


def _paid_row(ch: dict) -> bool:
    for k, v in ch.items():
        key = str(k).lower()
        if "price" in key or "coin" in key or "cost" in key:
            try:
                if float(v or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass
        elif key in ("is_premium", "premium", "locked", "is_locked", "paid", "is_paid", "is_early_access"):
            if v is True or str(v).lower() in ("1", "true", "yes"):
                return True
    return False


def fix_chap_url(base_url: str, href: str) -> str:
    if not href:
        return base_url
    if href.startswith("http"):
        return href
    if "asurascans" in base_url.lower() and not href.startswith("/"):
        if not href.startswith("comics/"):
            href = "/comics/" + href.lstrip("/")
        else:
            href = "/" + href
    return urljoin(base_url, href)


def extract_slug_tokens(url: str) -> set:
    """استخراج نام اصلی مانهوا (Slug) برای فیلتر کردن چپترهای متفرقه سایدبار"""
    p = urlparse(url)
    parts = [x for x in p.path.rstrip("/").split("/") if x and x.lower() not in ("manga", "series", "comic", "comics", "webtoon", "title", "detail", "en", "ko", "ja")]
    slugs = set()
    if parts:
        raw_slug = parts[-1].lower()
        # حذف کدهای هش آخر آدرس مثل -a1b2c3d4
        clean_slug = re.sub(r"-[0-9a-f]{6,10}$", "", raw_slug)
        if len(raw_slug) >= 3:
            slugs.add(raw_slug)
        if len(clean_slug) >= 3:
            slugs.add(clean_slug)
    return slugs


def clean_dom_noise(soup: BeautifulSoup):
    """حذف سایدبارها، فوتر، هدر و لیست‌های پیشنهادی (Popular / Related) قبل از اسکرپ"""
    for tag in soup.find_all(["aside", "footer", "nav", "header"]):
        tag.decompose()
    for el in soup.find_all(["div", "section", "ul"]):
        attrs = " ".join([
            str(el.get("id") or ""),
            " ".join(el.get("class") or []) if isinstance(el.get("class"), list) else str(el.get("class") or ""),
        ])
        if RECO_RE.search(attrs):
            el.decompose()


# ==============================================================================
# لایه ۱: ماژول‌های اختصاصی (Naver, Asura, ComicWalker, HeanCMS, MangaHub)
# ==============================================================================
def mangahub_latest(url: str):
    """پشتیبانی اختصاصی و ضد خطا از mangahub.io (جلوگیری از قاطی شدن با سایدبار)"""
    if "mangahub.io" not in url.lower():
        return None
    p = urlparse(url)
    m_slug = re.search(r"/(?:manga|chapter)/([^/?#]+)", p.path, re.I)
    if not m_slug:
        return None
    slug = m_slug.group(1).lower()

    # ۱. استخراج از API رسمی GraphQL سایت MangaHub
    try:
        gql_query = {
            "query": f'{{manga(x:m01,slug:"{slug}"){{latestChapter}}}}'
        }
        r_api = requests.post(
            "https://api.mghcdn.com/graphql",
            json=gql_query,
            headers={**HEADERS, "Content-Type": "application/json", "Origin": "https://mangahub.io", "Referer": url},
            impersonate="chrome124",
            timeout=15,
        )
        if r_api.status_code == 200:
            data = r_api.json() or {}
            latest_ch = ((data.get("data") or {}).get("manga") or {}).get("latestChapter")
            if latest_ch is not None:
                ch_str = str(int(latest_ch)) if float(latest_ch).is_integer() else str(latest_ch)
                return ch_str, f"https://mangahub.io/chapter/{slug}/chapter-{ch_str}"
    except Exception:
        pass

    # ۲. استخراج از HTML صفحه فقط با تطابق دقیق Slug
    try:
        r = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=20)
        if r.status_code == 200:
            soup = BeautifulSoup(norm(r.text), "html.parser")
            cands = []
            pat = re.compile(rf"/chapter/{re.escape(slug)}/chapter[-_](\d+(?:\.\d+)?)", re.I)
            for a in soup.find_all("a"):
                href = a.get("href", "") or ""
                m = pat.search(href)
                if m:
                    ch_str = m.group(1)
                    cands.append((float(ch_str), ch_str, urljoin("https://mangahub.io", href)))
            if cands:
                best = max(cands, key=lambda x: x[0])
                return best[1], best[2]
    except Exception:
        pass
    return None


def naver_webtoon_latest(url: str, free_only: bool = True):
    if "comic.naver.com" not in url.lower():
        return None
    m_id = re.search(r"titleId=(\d+)", url, re.I)
    if not m_id:
        return None
    title_id = m_id.group(1)
    try:
        api_url = f"https://comic.naver.com/api/article/list?titleId={title_id}&page=1"
        r = requests.get(
            api_url,
            headers={**HEADERS, "Accept": "application/json", "Referer": f"https://comic.naver.com/webtoon/list?titleId={title_id}"},
            impersonate="chrome124",
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            articles = list(data.get("articleList") or [])
            if not free_only and data.get("chargeFolderArticleList"):
                articles = list(data.get("chargeFolderArticleList")) + articles
            cands = []
            for art in articles:
                if free_only and art.get("charge"):
                    continue
                no = art.get("no")
                subtitle = norm(art.get("subtitle") or "")
                m_kr = KR_RE.search(subtitle)
                label = m_kr.group(1) if m_kr else str(no)
                if not label:
                    continue
                link = f"https://m.comic.naver.com/webtoon/detail?titleId={title_id}&no={no}"
                cands.append((float(label), label, link))
            if cands:
                best = max(cands, key=lambda x: x[0])
                return best[1], best[2]
    except Exception:
        pass
    return None


def asurascans_latest(url: str, free_only: bool = True):
    if "asurascans" not in url.lower():
        return None
    try:
        resp = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        raw = norm(resp.text)
        soup = BeautifulSoup(raw, "html.parser")
        p_path = urlparse(resp.url).path.rstrip("/")
        sl = p_path.split("/")[-1].lower()
        base_sl = re.sub(r"-[0-9a-f]{6,10}$", "", sl)
        chap_map = {}
        for a in soup.find_all("a"):
            href = a.get("href", "") or ""
            low_href = href.lower()
            if "/chapter/" not in low_href:
                continue
            if base_sl and len(base_sl) >= 3 and base_sl not in low_href:
                continue
            m = re.search(r"/chapter/(\d+(?:\.\d+)?)", low_href) or CHAPTER_RE.search(norm(a.get_text(" ", strip=True)))
            if not m:
                continue
            label = m.group(1)
            key = float(label)
            full_u = fix_chap_url(url, href)
            in_first_last_btn = bool(re.search(r"first\s*chapter|new\s*chapter|latest\s*chapter", norm(a.get_text(" ", strip=True)), re.I))
            row_paid = False
            if not in_first_last_btn:
                row_html = str(a) + " " + (str(a.parent) if a.parent else "")
                if a.find("svg") or (a.parent and a.parent.find("svg")) or re.search(r"lock|coin|premium|early|asura\+|<svg", row_html, re.I):
                    row_paid = True
            if key not in chap_map:
                chap_map[key] = [label, full_u, row_paid]
            elif row_paid:
                chap_map[key][2] = True
        if not chap_map:
            return None
        ordered = sorted(chap_map.keys(), reverse=True)
        if not free_only or len(ordered) == 1:
            top = chap_map[ordered[0]]
            return top[0], top[1]
        paid_flags = [chap_map[k][2] for k in ordered]
        if any(paid_flags) and not all(paid_flags):
            for k in ordered:
                if not chap_map[k][2]:
                    return chap_map[k][0], chap_map[k][1]
        top = chap_map[ordered[0]]
        return top[0], top[1]
    except Exception:
        return None


def comic_walker_latest(url: str):
    if "comic-walker.com" not in url:
        return None
    m_code = re.search(r"(KC_\d+_S)", url)
    if not m_code:
        return None
    work_code = m_code.group(1)
    detail_url = f"https://comic-walker.com/detail/{work_code}"
    try:
        api_url = f"https://comic-walker.com/api/contents/details/episode?workCode={work_code}&episodeType=latest"
        r_api = requests.get(api_url, headers={**HEADERS, "Accept": "application/json"}, impersonate="chrome124", timeout=15)
        if r_api.status_code == 200 and "json" in (r_api.headers.get("content-type") or ""):
            cands = []
            for m in JP_RE.finditer(norm(r_api.text)):
                key, label = _jp(m)
                cands.append((key, label))
            if cands:
                best = max(cands, key=lambda x: x[0])
                return best[1], detail_url
    except Exception:
        pass
    return None


def heancms_latest(url: str, free_only: bool = True):
    p = urlparse(url)
    m = re.fullmatch(r"/(?:series|comic)/([^/]+)/?", p.path)
    if not m:
        return None
    slug = m.group(1)
    api = f"{p.scheme}://api.{p.netloc.removeprefix('www.')}"
    hdr = {**HEADERS, "Accept": "application/json"}
    try:
        r_series = requests.get(f"{api}/series/{slug}", headers=hdr, impersonate="chrome124", timeout=15)
        if r_series.status_code != 200:
            return None
        sid = r_series.json().get("id")
        if not sid:
            return None
        r_ch = requests.get(f"{api}/chapter/query", headers=hdr, params={"page": 1, "perPage": 100, "series_id": sid}, impersonate="chrome124", timeout=15)
        if r_ch.status_code == 200:
            rows = r_ch.json().get("data", [])
            best = None
            for ch in rows:
                if free_only and _paid_row(ch):
                    continue
                name = f"{ch.get('chapter_name', '')} {ch.get('chapter_slug', '')}"
                mm = CHAPTER_RE.search(name) or re.search(r"\d+(?:\.\d+)?", name)
                if mm:
                    label = mm.group(1) if mm.lastindex else mm.group(0)
                    if best is None or float(label) > best[0]:
                        best = (float(label), label, ch.get("chapter_slug", "") or "")
            if best:
                return best[1], f"{p.scheme}://{p.netloc}/series/{slug}/{best[2]}"
    except Exception:
        pass
    return None


# ==============================================================================
# لایه ۲: موتور قالب‌های جهانی Keiyoushi (Madara AJAX + MangaThemesia + Selectors)
# ==============================================================================
def keiyoushi_template_engine(soup: BeautifulSoup, url: str, free_only: bool = True):
    """تشخیص خودکار قالب سایت بر اساس الگوهای استاندارد مخزن Keiyoushi/Tachiyomi"""
    slugs = extract_slug_tokens(url)

    # الف) قالب MangaThemesia: دارای تگ <li data-num="..."> داخل #chapterlist
    ts_items = soup.select("#chapterlist li[data-num], ul.clstyle li[data-num]")
    if ts_items:
        cands = []
        for li in ts_items:
            a = li.find("a")
            href = (a.get("href", "") if a else "") or ""
            data_num = norm(li.get("data-num", "")).strip()
            m = re.search(r"(\d+(?:\.\d+)?)", data_num) or CHAPTER_RE.search(norm(li.get_text(" ", strip=True)))
            if not m:
                continue
            ch_str = m.group(1)
            paid = is_paid(li)
            cands.append((float(ch_str), ch_str, fix_chap_url(url, href), paid))
        if cands:
            if free_only and any(not c[3] for c in cands):
                cands = [c for c in cands if not c[3]]
            best = max(cands, key=lambda x: x[0])
            return best[1], best[2]

    # ب) بررسی سلکتورهای استاندارد Keiyoushi (Madara / Mangabox / WP-Manga)
    for sel in KEIYOUSHI_CHAPTER_SELECTORS:
        els = soup.select(sel)
        if not els:
            continue
        cands = []
        for a in els:
            href = a.get("href", "") or ""
            txt = norm(a.get_text(" ", strip=True) + " " + href)
            m = CHAPTER_RE.search(txt) or HASH_NUM_RE.search(norm(a.get_text(" ", strip=True)))
            if not m:
                continue
            ch_str = m.group(1)
            cands.append((float(ch_str), ch_str, fix_chap_url(url, href), is_paid(a)))
        if cands:
            if free_only and any(not c[3] for c in cands):
                cands = [c for c in cands if not c[3]]
            best = max(cands, key=lambda x: x[0])
            return best[1], best[2]

    # پ) درخواست AJAX قالب Madara (برای سایت‌هایی که لیست چپتر را با POST لود می‌کنند)
    is_madara = bool(
        soup.select_one("div#manga-chapters-holder, body.wp-manga-template-default, input.rating-post-id")
    )
    if is_madara:
        try:
            ajax_url = url.rstrip("/") + "/ajax/chapters/"
            r_ajax = requests.post(
                ajax_url,
                headers={**HEADERS, "X-Requested-With": "XMLHttpRequest", "Referer": url},
                impersonate="chrome124",
                timeout=15,
            )
            if r_ajax.status_code == 200 and len(r_ajax.text) > 50:
                ajax_soup = BeautifulSoup(norm(r_ajax.text), "html.parser")
                cands = []
                for a in ajax_soup.select("li.wp-manga-chapter a, a"):
                    href = a.get("href", "") or ""
                    txt = norm(a.get_text(" ", strip=True) + " " + href)
                    m = CHAPTER_RE.search(txt)
                    if m:
                        cands.append((float(m.group(1)), m.group(1), fix_chap_url(url, href), is_paid(a)))
                if cands:
                    if free_only and any(not c[3] for c in cands):
                        cands = [c for c in cands if not c[3]]
                    best = max(cands, key=lambda x: x[0])
                    return best[1], best[2]
        except Exception:
            pass

    return None


# ==============================================================================
# لایه ۴: موتور ایزوله Camoufox برای کلودفلر سنگین و سایت‌های جاوااسکریپتی (SPA)
# ==============================================================================
def camoufox_isolated_latest(url: str, free_only: bool = True):
    """اجرای Camoufox در یک پروسه کاملاً مستقل برای جلوگیری از تداخل با curl_cffi"""
    print(f"[*] 🦊 فعال‌سازی موتور ضد کلودفلر Camoufox برای: {url}")
    code = r'''
import sys, re, json
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from camoufox.sync_api import Camoufox
from cloud_monitor import CHAPTER_RE, is_paid, norm, fix_chap_url, _paid_row, clean_dom_noise, extract_slug_tokens

url = sys.argv[1]
free_only = sys.argv[2] == "1"
captured_chapters = []

def handle_response(response):
    try:
        if any(k in response.url.lower() for k in ("api.", "chapter", "series", "episode", "graphql")):
            if "json" in (response.headers.get("content-type") or ""):
                data = response.json()
                rows = []
                if isinstance(data, dict):
                    rows = data.get("data") or data.get("chapters") or []
                    if isinstance(rows, dict):
                        rows = rows.get("chapters") or rows.get("data") or []
                    for s in (data.get("seasons") or []):
                        if isinstance(s, dict) and s.get("chapters"):
                            rows.extend(s.get("chapters"))
                elif isinstance(data, list):
                    rows = data
                if isinstance(rows, list):
                    for r in rows:
                        if isinstance(r, dict) and any(k in r for k in ("chapter_name", "chapter_slug", "number", "chap")):
                            captured_chapters.append(r)
    except Exception:
        pass

with Camoufox(headless=True, os="linux", humanize=True) as browser:
    page = browser.new_page()
    page.on("response", handle_response)
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    for _ in range(8):
        page.wait_for_timeout(3000)
        title = page.title()
        content = page.content()
        if captured_chapters or ("just a moment" not in title.lower() and re.search(r"chapter\s*\d+", content, re.I)):
            break
        try:
            for frame in page.frames:
                if "cloudflare" in frame.url or "turnstile" in frame.url:
                    box = frame.locator("input[type='checkbox'], body").first
                    if box:
                        box.click(timeout=1500)
        except Exception:
            pass
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
        page.wait_for_timeout(1500)
    except Exception:
        pass
    html = page.content()

if captured_chapters:
    p = urlparse(url)
    slug = p.path.rstrip("/").split("/")[-1]
    best = None
    for ch in captured_chapters:
        if free_only and _paid_row(ch):
            continue
        name = f"{ch.get('chapter_name', '')} {ch.get('chapter_slug', '')} {ch.get('number', '')}"
        mm = CHAPTER_RE.search(name) or re.search(r"\d+(?:\.\d+)?", name)
        if not mm:
            continue
        label = mm.group(1) if mm.lastindex else mm.group(0)
        if best is None or float(label) > best[0]:
            best = (float(label), label, ch.get("chapter_slug", "") or "")
    if best:
        link = f"{p.scheme}://{p.netloc}/series/{slug}/{best[2]}" if best[2] else url
        print("RESULT_JSON:" + json.dumps([best[1], link]))
        sys.exit(0)

raw = norm(html)
soup = BeautifulSoup(raw, "html.parser")
clean_dom_noise(soup)
slugs = extract_slug_tokens(url)
cands = []
for a in soup.find_all("a"):
    href = a.get("href", "") or ""
    if slugs and not any(s in href.lower() for s in slugs) and "chapter" not in href.lower():
        continue
    row_text = norm(a.get_text(" ", strip=True) + " " + (a.parent.get_text(" ", strip=True) if a.parent else ""))
    m = CHAPTER_RE.search(row_text) or CHAPTER_RE.search(href)
    if not m:
        continue
    ch_num = m.group(1)
    row_html = str(a) + " " + (str(a.parent) if a.parent else "")
    paid_flag = is_paid(a) or bool(re.search(r"coin|lock|premium|paywall|🪙|🔒|\b\d+\s*<svg", row_html, re.I))
    cands.append((float(ch_num), ch_num, fix_chap_url(url, href), paid_flag))

if cands:
    if free_only and any(not c[3] for c in cands):
        cands = [c for c in cands if not c[3]]
    best = max(cands, key=lambda x: x[0])
    print("RESULT_JSON:" + json.dumps([best[1], best[2]]))
    sys.exit(0)

sys.exit(1)
'''
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code, url, "1" if free_only else "0"],
            capture_output=True,
            text=True,
            timeout=95,
        )
        for line in (proc.stdout or "").splitlines():
            if line.startswith("RESULT_JSON:"):
                res = json.loads(line.split("RESULT_JSON:", 1)[1])
                return res[0], res[1]
    except Exception as e:
        print(f"Camoufox subprocess info: {e}")
    return None


# ==============================================================================
# تابع مرکزی تشخیص چپتر (ترکیب هر ۴ لایه)
# ==============================================================================
def get_latest(url: str, selector: str = "", include_paid: bool = False):
    free_only = not include_paid

    if not selector.strip():
        # اگر سایت کلودفلر سنگین مثل luacomic دارد، مستقیم با Camoufox باز کن
        if "luacomic." in url.lower():
            cf = camoufox_isolated_latest(url, free_only=free_only)
            if cf:
                return cf[0], cf[1]

        # بررسی ماژول‌های اختصاصی
        mh = mangahub_latest(url)
        if mh:
            return mh[0], mh[1]
        nv = naver_webtoon_latest(url, free_only=free_only)
        if nv:
            return nv[0], nv[1]
        asu = asurascans_latest(url, free_only=free_only)
        if asu:
            return asu[0], asu[1]
        cw = comic_walker_latest(url)
        if cw:
            return cw[0], cw[1]
        hc = heancms_latest(url, free_only=free_only)
        if hc:
            return hc[0], hc[1]

    try:
        resp = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        cf = camoufox_isolated_latest(url, free_only=free_only)
        if cf:
            return cf[0], cf[1]
        raise e

    raw = norm(resp.text)
    soup = BeautifulSoup(raw, "html.parser")

    # ۱. اگر کاربر سلکتور دستی داده باشد
    if selector.strip():
        el = soup.select_one(selector)
        if not el:
            raise ValueError("Selector پیدا نشد")
        m = re.search(r"\d+(?:\.\d+)?", norm(el.get_text(" ", strip=True)))
        if not m:
            raise ValueError("عددی در متن نبود")
        a = el if el.name == "a" else (el.find("a") or el.find_parent("a"))
        href = a.get("href", "") if a else ""
        return m.group(0), fix_chap_url(url, href)

    # ۲. بررسی قالب‌های استاندارد Keiyoushi / Tachiyomi (قبل از حذف تگ‌ها)
    ky = keiyoushi_template_engine(soup, url, free_only=free_only)
    if ky:
        return ky[0], ky[1]

    # ۳. ایزوله‌سازی DOM (حذف سایدبارها و بخش‌های پیشنهادی)
    clean_dom_noise(soup)
    slugs = extract_slug_tokens(url)

    anchors = soup.find_all("a")
    if slugs:
        own = [
            a for a in anchors
            if any(s in (a.get("href", "") or "").lower() for s in slugs)
            and (CHAPTER_RE.search(a.get("href", "") or "") or CHAPTER_RE.search(norm(a.get_text(" ", strip=True))))
        ]
        if own:
            anchors = own

    cands = []
    for a in anchors:
        href = a.get("href", "") or ""
        for text in (norm(a.get_text(" ", strip=True)), href):
            m = JP_RE.search(text or "")
            if m:
                key, label = _jp(m)
                cands.append((key, label, href, is_paid(a)))
                break
            m = CHAPTER_RE.search(text or "")
            if m:
                cands.append(((float(m.group(1)), 0), m.group(1), href, is_paid(a)))
                break

    if free_only and any(not c[3] for c in cands):
        cands = [c for c in cands if not c[3]]

    if not cands:
        # اگر سایت با جاوااسکریپت رندر می‌شود، با Camoufox باز کن
        cf = camoufox_isolated_latest(url, free_only=free_only)
        if cf:
            return cf[0], cf[1]
        raise ValueError("چپتری پیدا نشد")

    best = max(cands, key=lambda x: x[0])
    return best[1], fix_chap_url(url, best[2])


def sync_telegram_subscribers() -> set:
    subs = set()
    if TG_CHAT_ID:
        for cid in re.split(r"[\s,]+", TG_CHAT_ID):
            if cid:
                subs.add(str(cid))
    if os.path.exists(SUBS_FILE):
        try:
            with open(SUBS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    subs.update(str(x) for x in data if x)
        except Exception:
            pass
    return subs


def notify(title: str, message: str, link: str = "", tg_subs: set = None) -> bool:
    if os.environ.get("NOTIFICATIONS_ENABLED", "true").strip().lower() in ("false", "0", "no", "off"):
        return False
    ok = False
    if NTFY_TOPIC:
        payload = {"topic": NTFY_TOPIC, "title": title, "message": message, "priority": 4, "tags": ["books"]}
        if link:
            payload["click"] = link
        try:
            r = requests.post(NTFY_SERVER, json=payload, timeout=15)
            ok = ok or r.ok
        except Exception:
            pass
    if TG_TOKEN and tg_subs:
        text = f"📚 <b>{title}</b>\n{message}"
        if link:
            text += f'\n\n<a href="{link}">📖 باز کردن چپتر</a>'
        for cid in tg_subs:
            try:
                r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": cid, "text": text, "parse_mode": "HTML"}, timeout=15)
                ok = ok or r.ok
            except Exception:
                pass
    return ok


def main():
    tg_subs = sync_telegram_subscribers()
    if not os.path.exists(LIST_FILE):
        print("[!] فایل manhwa_list.json یافت نشد.")
        return

    with open(LIST_FILE, "r", encoding="utf-8") as f:
        items = json.load(f)

    changed = False
    for it in items:
        if it.get("paused") or not it.get("url"):
            continue
        title = it.get("title", "بدون نام")
        try:
            ch, link = get_latest(it["url"], it.get("selector", ""), bool(it.get("include_paid")))
        except Exception as e:
            print(f"[!] خطا در {title}: {e}")
            continue

        last = str(it.get("last_chapter", "") or "")
        if not last:
            it["last_chapter"] = ch
            it["last_link"] = link
            changed = True
            print(f"[*] مقدار اولیه برای {title}: چپتر {ch}")
        elif ch != last:
            kc, kl = chap_key(ch), chap_key(last)
            if kc and kl and kc > kl and (kc[0] - kl[0] <= 50):
                print(f"[+] چپتر جدید برای {title}: {last} -> {ch}")
                notify(f"چپتر جدید: {title}", f"جدید: چپتر {ch}\nقبلی: چپتر {last}", link, tg_subs)
                it["last_chapter"] = ch
                it["last_link"] = link
                changed = True

    if changed:
        with open(LIST_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
            f.write("\n")


if __name__ == "__main__":
    main()
