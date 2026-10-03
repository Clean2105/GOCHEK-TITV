import sqlite3
import re
import time
import random
from urllib.parse import urljoin

URL = "https://titv.vn"
DB_FILE = "titv.db"
U_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
LINE = "=" * 78
SWAP_NUMBERS = False

CATEGORY_RULES = [
    ("Python", ["python"]),
    ("C / C++", ["c++", "cpp", "lập trình c", "ngôn ngữ c", "c căn bản", "c cơ bản"]),
    ("Java", ["java", "jsp", "servlet", "spring"]),
    ("C# / .NET", ["c#", ".net", "asp"]),
    ("PHP", ["php", "laravel"]),
    ("Web Frontend", ["html", "css", "javascript", "js", "react", "vue", "angular",
                      "bootstrap", "jquery", "web"]),
    ("Cơ sở dữ liệu", ["sql", "database", "csdl", "cơ sở dữ liệu", "mongodb"]),
    ("Thuật toán / Toán", ["toán", "thuật toán", "cấu trúc dữ liệu", "giải thuật"]),
    ("Git / DevOps", ["git", "docker", "linux", "devops"]),
    ("Quản trị / Văn phòng", ["quản trị", "excel", "word", "powerpoint", "văn phòng"]),
    ("Đồ họa / Thiết kế", ["photoshop", "illustrator", "thiết kế", "đồ họa"]),
    ("Android / Mobile", ["android", "flutter", "kotlin", "swift", "mobile"]),
    ("AI / Data", ["ai", "machine learning", "data", "dữ liệu lớn"]),
]


def to_int(s: str) -> int:
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else 0


def don_dep_price(price_str: str) -> int:
    if not price_str:
        return 0
    price_str = price_str.strip().lower()
    if "free" in price_str or price_str == "0":
        return 0
    return to_int(price_str)


def detect_category(name: str) -> str:
    text = re.sub(r"\[video\]", "", name, flags=re.IGNORECASE).lower()
    for category, keywords in CATEGORY_RULES:
        for kw in keywords:
            # từ khóa ngắn (<=3 ký tự, vd: "ai", "js", "git") phải khớp nguyên từ
            if len(kw) <= 3 and kw.isalpha():
                if re.search(rf"\b{re.escape(kw)}\b", text):
                    return category
            elif kw in text:
                return category
    return "Khác"


# =========================
# DATABASE
# =========================

def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_database(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL DEFAULT 'Khác',
            current_price INTEGER NOT NULL DEFAULT 0,
            original_price INTEGER NOT NULL DEFAULT 0,
            discount_percentage REAL NOT NULL DEFAULT 0,
            type TEXT NOT NULL,
            viewers INTEGER NOT NULL DEFAULT 0,
            learners INTEGER NOT NULL DEFAULT 0,
            url TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            UNIQUE(course_id, url),
            FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_courses_name ON courses(name);
        CREATE INDEX IF NOT EXISTS idx_lessons_course ON lessons(course_id);
    """)
    # Nâng cấp DB cũ (thiếu cột category / viewers / learners)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(courses)")]
    if "category" not in cols:
        conn.execute("ALTER TABLE courses ADD COLUMN category TEXT NOT NULL DEFAULT 'Khác'")
        for cid, name in conn.execute("SELECT id, name FROM courses").fetchall():
            conn.execute("UPDATE courses SET category = ? WHERE id = ?",
                         (detect_category(name), cid))
    if "viewers" not in cols:
        conn.execute("ALTER TABLE courses ADD COLUMN viewers INTEGER NOT NULL DEFAULT 0")
    if "learners" not in cols:
        conn.execute("ALTER TABLE courses ADD COLUMN learners INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_courses_category ON courses(category)")
    conn.commit()


def insert_course(conn, course) -> int:
    conn.execute("""
        INSERT INTO courses (name, category, current_price, original_price,
                             discount_percentage, type, viewers, learners, url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            category = excluded.category,
            current_price = excluded.current_price,
            original_price = excluded.original_price,
            discount_percentage = excluded.discount_percentage,
            type = excluded.type,
            viewers = excluded.viewers,
            learners = excluded.learners,
            url = excluded.url
    """, (course["name"], course["category"], course["price"],
          course["original_price"], course["discount_percentage"],
          course["type"], course["viewers"], course["learners"], course["url"]))
    return conn.execute("SELECT id FROM courses WHERE name = ?",
                        (course["name"],)).fetchone()[0]


def insert_lesson(conn, course_id, title, url):
    conn.execute("INSERT OR IGNORE INTO lessons (course_id, title, url) VALUES (?, ?, ?)",
                 (course_id, title, url))


# =========================
# CRAWLER
# =========================

def parse_counts(block, title):
    """Đi ngược lên các thẻ cha, tìm 2 số đứng ngay trước điểm đánh giá (vd 4.7)."""
    node = block
    for _ in range(5):
        try:
            node = node.locator("xpath=./..")
            text = node.inner_text()
            start = text.find(title)
            text = text[start:] if start >= 0 else text
            m = re.search(r"(\d[\d,.]*)\s*\n\s*(\d[\d,.]*)\s*\n\s*\d\.\d\b", text)
            if m:
                a, b = to_int(m.group(1)), to_int(m.group(2))
                return (b, a) if SWAP_NUMBERS else (a, b)
        except Exception:
            break
    return 0, 0


def crawl_titv():
    from playwright.sync_api import sync_playwright

    conn = get_connection()
    create_database(conn)
    courses_list = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=U_AGENT)
        page = context.new_page()

        print("\n====== BƯỚC 1: Kết nối TITV.vn lấy danh sách khóa học ======")
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(1500)
        except Exception as e:
            print(f"Không thể tải trang: {e}")
            browser.close()
            conn.close()
            return

        seen_titles = set()
        for block in page.locator("h3").all():
            try:
                title = block.inner_text().strip()
                if not title or title in seen_titles:
                    continue
                if not ("[Video]" in title or "Toán rời rạc" in title or "Quản trị" in title):
                    continue
                seen_titles.add(title)

                current_price = original_price = 0
                discount = 0
                parent = block.locator("xpath=./..")

                try:
                    parent_text = parent.inner_text()
                    prices = re.findall(r"(\d{1,3}(?:,\d{3})*\s*đ|Free)",
                                        parent_text, re.IGNORECASE)
                    if len(prices) >= 2:
                        parsed = sorted(don_dep_price(x) for x in prices)
                        current_price, original_price = parsed[0], parsed[1]
                    elif len(prices) == 1:
                        current_price = original_price = don_dep_price(prices[0])
                    if original_price > current_price and original_price > 0:
                        discount = round((original_price - current_price)
                                         / original_price * 100, 2)
                except Exception:
                    pass

                viewers, learners = parse_counts(block, title)

                course_url = URL
                try:
                    href = block.get_attribute("href") or parent.get_attribute("href")
                    if not href:
                        anc = block.locator("xpath=./ancestor::a").first
                        if anc.count() > 0:
                            href = anc.get_attribute("href")
                    if href:
                        course_url = urljoin(URL, href)
                except Exception:
                    pass

                course = {
                    "name": title,
                    "category": detect_category(title),
                    "price": current_price,
                    "original_price": original_price,
                    "discount_percentage": discount,
                    "type": "Miễn phí" if current_price == 0 else "Có phí",
                    "viewers": viewers,
                    "learners": learners,
                    "url": course_url,
                }
                courses_list.append(course)
                print(f"Found: {title} [{course['category']}] -> "
                      f"Giá: {current_price:,}đ | Người xem: {viewers:,} | "
                      f"Học viên: {learners:,}")
            except Exception as e:
                print(f"Lỗi xử lý khóa học: {e}")

        print("\n====== BƯỚC 2: Cào bài học từng khóa ======")
        for idx, course in enumerate(courses_list, 1):
            c_url = course["url"]
            print(f"[{idx}/{len(courses_list)}] Đang quét: {course['name']}")
            course_id = insert_course(conn, course)

            if c_url == URL or "/courses-page/" not in c_url:
                insert_lesson(conn, course_id, "Nội dung trọn gói tại trang chính", c_url)
                conn.commit()
                continue

            try:
                page.goto(c_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1000)

                lessons = {}
                for node in page.locator(f'a[href^="{c_url}"]').all():
                    l_href = node.get_attribute("href")
                    l_text = node.inner_text().strip()
                    if l_href and l_text:
                        full = urljoin(c_url, l_href).split("?")[0].split("#")[0]
                        if full.strip("/") != c_url.strip("/") and full not in lessons:
                            lessons[full] = " ".join(l_text.split())

                if lessons:
                    print(f"  Tìm thấy {len(lessons)} bài học.")
                    for l_url, l_title in lessons.items():
                        insert_lesson(conn, course_id, l_title, l_url)
                else:
                    print("  Không tìm thấy bài học riêng lẻ.")
                    insert_lesson(conn, course_id, "Trọn bộ nội dung", c_url)
                conn.commit()
            except Exception as e:
                print(f"  Lỗi tải chi tiết: {e}")
                insert_lesson(conn, course_id, "Lỗi tải chi tiết bài học", c_url)
                conn.commit()

            time.sleep(random.uniform(1.0, 2.0))

        browser.close()

    conn.close()
    print(f"\n✅ Đã lưu dữ liệu vào SQLite: {DB_FILE}")


# =========================
# HIỂN THỊ / THỐNG KÊ
# =========================

def fmt_price(v) -> str:
    return "Miễn phí" if not v else f"{int(v):,}đ"


BASE_SELECT = "SELECT name, category, current_price, type, viewers FROM courses"


def print_courses(rows):
    if not rows:
        print("Không có khóa học nào.")
        return
    print(f"{'Khóa học':<40} {'Thể loại':<18} {'Giá':>11} {'Hình thức':<10} {'Người xem':>10}")
    print("-" * 94)
    for name, cat, price, ctype, viewers in rows:
        short = name if len(name) <= 38 else name[:37] + "…"
        print(f"{short:<40} {cat:<18} {fmt_price(price):>11} {ctype:<10} {viewers:>10,}")
    print("-" * 94)
    print(f"Tổng: {len(rows)} khóa học | Tổng người xem: {sum(r[4] for r in rows):,}")


def show_all(conn):
    print_courses(conn.execute(BASE_SELECT + " ORDER BY viewers DESC, name").fetchall())


def find_by_name(conn):
    kw = input("Nhập từ khóa tên khóa học: ").strip()
    rows = conn.execute(BASE_SELECT + " WHERE name LIKE ? ORDER BY name",
                        (f"%{kw}%",)).fetchall()
    print_courses(rows)


def show_by_category(conn):
    cats = [r[0] for r in conn.execute(
        "SELECT DISTINCT category FROM courses ORDER BY category")]
    if not cats:
        print("Chưa có dữ liệu. Hãy chọn 1 để cào trước.")
        return
    for i, c in enumerate(cats, 1):
        print(f"  {i}. {c}")
    raw = input("Chọn số thể loại: ").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(cats)):
        print("Lựa chọn không hợp lệ.")
        return
    rows = conn.execute(BASE_SELECT + " WHERE category = ? ORDER BY viewers DESC",
                        (cats[int(raw) - 1],)).fetchall()
    print_courses(rows)


def show_lessons(conn):
    kw = input("Nhập từ khóa tên khóa học để xem bài học: ").strip()
    courses = conn.execute("SELECT id, name FROM courses WHERE name LIKE ? ORDER BY name",
                           (f"%{kw}%",)).fetchall()
    if not courses:
        print("Không tìm thấy khóa học.")
        return
    for cid, name in courses:
        lessons = conn.execute(
            "SELECT title, url FROM lessons WHERE course_id = ? ORDER BY id", (cid,)
        ).fetchall()
        print(f"\nKhóa học: {name}  ({len(lessons)} bài)")
        for i, (title, url) in enumerate(lessons, 1):
            print(f"  {i:>3}. {title}\n       {url}")


def show_statistics(conn):
    total, free, paid, avg_price, views, learners = conn.execute("""
        SELECT COUNT(*),
               COALESCE(SUM(current_price = 0), 0),
               COALESCE(SUM(current_price > 0), 0),
               COALESCE(ROUND(AVG(CASE WHEN current_price > 0 THEN current_price END), 0), 0),
               COALESCE(SUM(viewers), 0),
               COALESCE(SUM(learners), 0)
        FROM courses
    """).fetchone()
    total_lessons = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]

    print(f"\n{LINE}\n{'THỐNG KÊ KHÓA HỌC':^78}\n{LINE}")
    print(f"Tổng số khóa học        : {total}")
    print(f"Tổng số bài học         : {total_lessons}")
    print(f"Tổng số lượng người xem : {views:,}")
    print(f"Tổng số học viên        : {learners:,}")
    print(f"Khóa học miễn phí       : {free}")
    print(f"Khóa học có phí         : {paid}")
    print(f"Giá trung bình (có phí) : {fmt_price(avg_price)}")

    print("\n--- Theo hình thức ---")
    print(f"  {'Hình thức':<14}{'Số khóa':>8}{'Người xem':>14}")
    for ctype, n, v in conn.execute("""
            SELECT type, COUNT(*), SUM(viewers) FROM courses
            GROUP BY type ORDER BY COUNT(*) DESC"""):
        print(f"  {ctype:<14}{n:>8}{v:>14,}")

    print("\n--- Theo thể loại ---")
    print(f"  {'Thể loại':<22}{'Số khóa':>8}{'Miễn phí':>10}{'Có phí':>8}"
          f"{'Số bài':>8}{'Người xem':>13}")
    rows = conn.execute("""
        SELECT c.category,
               COUNT(*),
               SUM(c.current_price = 0),
               SUM(c.current_price > 0),
               (SELECT COUNT(*) FROM lessons l
                  JOIN courses c2 ON c2.id = l.course_id
                 WHERE c2.category = c.category),
               SUM(c.viewers)
        FROM courses c
        GROUP BY c.category
        ORDER BY SUM(c.viewers) DESC, c.category
    """).fetchall()
    for cat, n, f, p, nl, v in rows:
        print(f"  {cat:<22}{n:>8}{f:>10}{p:>8}{nl:>8}{v:>13,}")
    print(f"\nTổng số thể loại: {len(rows)}")

    top = conn.execute(
        "SELECT name, viewers FROM courses ORDER BY viewers DESC LIMIT 1").fetchone()
    if top and top[1] > 0:
        print(f"Khóa nhiều người xem nhất: {top[0]} ({top[1]:,})")


def delete_all(conn):
    if input("Xóa TOÀN BỘ dữ liệu? (y/n): ").strip().lower() == "y":
        conn.execute("DELETE FROM lessons")
        conn.execute("DELETE FROM courses")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('courses','lessons')")
        conn.commit()
        print("Đã xóa toàn bộ dữ liệu.")


# =========================
# MENU
# =========================

def print_menu():
    print(f"\n{'TITV COURSE MANAGEMENT':^78}")
    print(LINE)
    print("1. Crawl Courses (Cào khóa học + bài học + lượt xem)")
    print("2. Show All Courses")
    print("3. Find Course by Name")
    print("4. Show Courses by Category")
    print("5. Show Lessons of a Course")
    print("6. Statistics (Tổng khóa học, thể loại, người xem)")
    print("7. Delete All Data")
    print("0. Exit")
    print(LINE)


def main():
    conn = get_connection()
    create_database(conn)
    actions = {
        "2": show_all, "3": find_by_name, "4": show_by_category,
        "5": show_lessons, "6": show_statistics, "7": delete_all,
    }
    while True:
        print_menu()
        choice = input("Chọn chức năng: ").strip()
        if choice == "0":
            break
        if choice == "1":
            conn.close()
            crawl_titv()
            conn = get_connection()
            create_database(conn)
            show_statistics(conn)
        elif choice in actions:
            actions[choice](conn)
        else:
            print("Lựa chọn không hợp lệ!")
    conn.close()
    print("Tạm biệt!")


if __name__ == "__main__":
    main()