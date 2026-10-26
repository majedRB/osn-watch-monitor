import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from streamlit_autorefresh import st_autorefresh

APP_TITLE = "OSN+ Public Catalog Tracker"
DB_PATH = "osn_catalog.sqlite3"
SOURCES = {
    "OSN+ Saudi home": "https://osnplus.com/ar-sa",
    "OSN+ Saudi home category": "https://osnplus.com/ar-sa/category/home",
    "OSN+ series category": "https://osntv.osnplus.com/ar-sa/category/series",
    "OSN+ movies category": "https://osntv.osnplus.com/ar-sa/category/movies",
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
}

st.set_page_config(page_title=APP_TITLE, layout="wide")
st.title("OSN+ — متابعة الكتالوج العام")
st.caption("يراقب صفحات الكتالوج العامة وروابط الصور الظاهرة فيها. لا يفك تشفير الفيديو ولا يتجاوز تسجيل الدخول أو الحماية.")

def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS items (
            item_key TEXT PRIMARY KEY,
            title TEXT,
            item_url TEXT,
            image_url TEXT,
            source_url TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS scans (
            scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at TEXT,
            source_url TEXT,
            status TEXT,
            items_found INTEGER,
            note TEXT
        )
    """)
    con.commit()
    return con

def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()

def absolutize(base_url, value):
    if not value:
        return ""
    return urljoin(base_url, value.strip())

def scrape_page(source_url):
    resp = requests.get(source_url, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    found = []

    # Catalog cards are generally links containing an image and a title.
    for a in soup.select("a[href]"):
        href = absolutize(source_url, a.get("href", ""))
        if not href or urlparse(href).netloc not in ("osnplus.com", "www.osnplus.com", "osntv.osnplus.com"):
            continue

        img = a.find("img")
        title = ""
        if img:
            title = clean_text(img.get("alt") or img.get("title") or "")
        if not title:
            title = clean_text(a.get("aria-label") or a.get("title") or a.get_text(" ", strip=True))
        if not title:
            continue

        image_url = ""
        if img:
            image_url = (img.get("src") or img.get("data-src") or
                         img.get("data-lazy-src") or "")
            if not image_url and img.get("srcset"):
                image_url = img.get("srcset").split(",")[0].strip().split(" ")[0]
            image_url = absolutize(source_url, image_url)

        # Keep only likely content links, not generic navigation.
        path = urlparse(href).path.lower()
        if len(path.strip("/").split("/")) < 2 and not image_url:
            continue
        if not image_url and not re.search(r"/(title|movie|series|show|watch|content|program)", path):
            continue

        found.append({
            "title": title[:300],
            "item_url": href,
            "image_url": image_url,
            "source_url": source_url,
        })

    # Also preserve public artwork URLs visible in HTML even if the page layout
    # doesn't pair them cleanly with a title.
    seen = set()
    unique = []
    for item in found:
        key = (item["title"].casefold(), item["item_url"], item["image_url"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique, resp.status_code, f"HTML fetched ({len(resp.text)} bytes)"

def save_scan(con, source_url, status, items, note):
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    new_count = 0
    for item in items:
        key = item["item_url"] or (item["title"].casefold() + "|" + item["image_url"])
        row = con.execute("SELECT item_key FROM items WHERE item_key=?", (key,)).fetchone()
        if row:
            con.execute("""UPDATE items SET title=?, image_url=?, source_url=?, last_seen=?
                           WHERE item_key=?""",
                        (item["title"], item["image_url"], source_url, now, key))
        else:
            con.execute("""INSERT INTO items(item_key,title,item_url,image_url,source_url,first_seen,last_seen)
                           VALUES(?,?,?,?,?,?,?)""",
                        (key, item["title"], item["item_url"], item["image_url"], source_url, now, now))
            new_count += 1
    con.execute("INSERT INTO scans(scanned_at,source_url,status,items_found,note) VALUES(?,?,?,?,?)",
                (now, source_url, str(status), len(items), note))
    con.commit()
    return new_count

def run_scan():
    con = db()
    results = []
    new_total = 0
    for label, url in SOURCES.items():
        try:
            items, status, note = scrape_page(url)
            new_count = save_scan(con, url, status, items, note)
            new_total += new_count
            results.append((label, url, "OK", len(items), new_count, note))
        except Exception as e:
            now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            con.execute("INSERT INTO scans(scanned_at,source_url,status,items_found,note) VALUES(?,?,?,?,?)",
                        (now, url, "ERROR", 0, str(e)[:500]))
            con.commit()
            results.append((label, url, "ERROR", 0, 0, str(e)[:300]))
    con.close()
    return results, new_total

con = db()
left, right = st.columns([2, 1])
with left:
    refresh_minutes = st.selectbox("تحديث تلقائي أثناء بقاء الموقع مفتوحًا", [5, 15, 30, 60, 180], index=2)
with right:
    if st.button("افحص الآن", type="primary", use_container_width=True):
        with st.spinner("يجري فحص صفحات الكتالوج العامة..."):
            results, new_total = run_scan()
        st.session_state["last_scan_results"] = results
        st.session_state["last_new_total"] = new_total
        st.success(f"انتهى الفحص. سجلات جديدة: {new_total}")

st_autorefresh(interval=refresh_minutes * 60 * 1000, key="catalog_refresh")
# On first open, seed the database; on timed reruns scan at the selected cadence.
if "last_auto_scan" not in st.session_state:
    st.session_state["last_auto_scan"] = 0
if time.time() - st.session_state["last_auto_scan"] >= refresh_minutes * 60:
    with st.spinner("يتم تحديث الكتالوج العام..."):
        results, new_total = run_scan()
    st.session_state["last_scan_results"] = results
    st.session_state["last_new_total"] = new_total
    st.session_state["last_auto_scan"] = time.time()

con = db()
df = pd.read_sql_query("SELECT * FROM items ORDER BY last_seen DESC", con)
scans = pd.read_sql_query("SELECT * FROM scans ORDER BY scan_id DESC LIMIT 20", con)
con.close()

m1, m2, m3 = st.columns(3)
m1.metric("إجمالي السجلات المكتشفة", len(df))
m2.metric("سجلات جديدة في آخر فحص", st.session_state.get("last_new_total", 0))
m3.metric("آخر تحديث", df["last_seen"].max() if not df.empty else "لم يبدأ بعد")

tab1, tab2, tab3 = st.tabs(["الأعمال والصور", "سجل الفحوصات", "مصادر البيانات"])
with tab1:
    if df.empty:
        st.info("لم تُجمع بيانات بعد. جرّب زر «افحص الآن». إذا كان الموقع يعتمد على تحميل JavaScript، فقد نحتاج إلى موصل API أو متصفح آلي.")
    else:
        query = st.text_input("ابحث بالعنوان", "")
        if query:
            df = df[df["title"].str.contains(query, case=False, na=False)]
        c1, c2 = st.columns(2)
        with c1:
            only_images = st.checkbox("اعرض السجلات التي لها صورة فقط", value=True)
        with c2:
            sort = st.selectbox("الترتيب", ["آخر ظهور", "أول اكتشاف", "العنوان"])
        if only_images:
            df = df[df["image_url"].fillna("") != ""]
        if sort == "أول اكتشاف":
            df = df.sort_values("first_seen", ascending=False)
        elif sort == "العنوان":
            df = df.sort_values("title")
        else:
            df = df.sort_values("last_seen", ascending=False)

        st.caption("ملاحظة: «أول اكتشاف» يعني أول مرة رآها هذا البرنامج، وليس تاريخ إضافة العمل إلى OSN+.")
        for _, row in df.head(500).iterrows():
            with st.container(border=True):
                cols = st.columns([1, 4])
                with cols[0]:
                    if isinstance(row.get("image_url"), str) and row["image_url"].startswith("http"):
                        st.image(row["image_url"], use_container_width=True)
                with cols[1]:
                    st.markdown(f"**{row['title']}**")
                    if row.get("item_url"):
                        st.markdown(f"[فتح صفحة العمل]({row['item_url']})")
                    if row.get("image_url"):
                        st.markdown(f"[فتح رابط الصورة]({row['image_url']})")
                    st.caption(f"أول اكتشاف: {row['first_seen']} · آخر ظهور: {row['last_seen']}")
        st.download_button("تنزيل CSV", df.to_csv(index=False).encode("utf-8-sig"),
                           file_name="osn_public_catalog.csv", mime="text/csv")
with tab2:
    if scans.empty:
        st.info("لا توجد فحوصات مسجلة بعد.")
    else:
        st.dataframe(scans, use_container_width=True, hide_index=True)
with tab3:
    st.write("المصادر العامة التي يفحصها البرنامج:")
    for label, url in SOURCES.items():
        st.markdown(f"- **{label}:** {url}")
    st.warning("قد تتغير بنية الموقع أو تُحمّل البطاقات عبر JavaScript/API، وبالتالي قد لا يظهر كل الكتالوج. الفحص دوري ما دام التطبيق يعمل على جهازك؛ لا يعمل إذا أُغلق البرنامج أو أُطفئ الجهاز.")

st.divider()
st.caption("تنبيه: الأداة تجمع البيانات والروابط الظاهرة علنًا فقط. لا تحاول تخمين مسارات غير معلنة، أو تنزيل ملفات الفيديو، أو تجاوز تسجيل الدخول أو DRM أو قيود الوصول.")
