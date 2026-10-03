import sqlite3
import random
import re
import time
import json
import urllib.robotparser
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# =========================================================
# CẤU HÌNH
# =========================================================

DATABASE_NAME = "gochek.db"
B_URL = "https://gochek.vn"
CO_URL = f"{B_URL}/collections/all"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# =========================================================
# DATABASE
# =========================================================

def connect_db():
    return sqlite3.connect(DATABASE_NAME)


def create_table():
    conn = connect_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            name TEXT NOT NULL,
            category TEXT,
            price INTEGER,
            old_price INTEGER,
            in_stock INTEGER DEFAULT 0,
            image TEXT,
            url TEXT PRIMARY KEY
        )
    """)

    # =====================================================
    # MIGRATION: Có gochek.db cũ thì không cần xóa
    # =====================================================
    cursor.execute("PRAGMA table_info(products)")
    columns = {row[1] for row in cursor.fetchall()}

    if "category" not in columns:
        cursor.execute("ALTER TABLE products ADD COLUMN category TEXT")
        print("Đã thêm cột category vào database cũ.")

    # Dữ liệu cũ chưa có thể loại thì gán Khác.
    cursor.execute("""
        UPDATE products
        SET category = 'Khác'
        WHERE category IS NULL OR TRIM(category) = ''
    """)

    conn.commit()
    conn.close()


# =========================================================
# XỬ LÝ GIÁ
# =========================================================

def parse_price(text):
    if not text:
        return None

    digits = re.sub(r"[^\d]", "", str(text))
    return int(digits) if digits else None


# =========================================================
# KIỂM TRA ROBOTS.TXT
# =========================================================

def check_robots():
    try:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{B_URL}/robots.txt")
        rp.read()

        allowed = rp.can_fetch("*", CO_URL)
        print(f"Robots.txt cho phép cào: {allowed}")
        return allowed

    except Exception as e:
        print(f"Không kiểm tra được robots.txt: {e}")
        return True


# =========================================================
# LẤY DANH SÁCH LINK SẢN PHẨM
# =========================================================

def get_product_links(page):
    links = set()
    page_number = 1

    while True:
        url = CO_URL if page_number == 1 else f"{CO_URL}?page={page_number}"

        try:
            print(f"Đang cào trang {page_number}: {url}")

            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=40000
            )
            page.wait_for_timeout(1500)

            locator = page.locator('a[href*="/products/"]')
            hrefs = locator.evaluate_all(
                "elements => elements.map(e => e.getAttribute('href'))"
            )

            new_links = {
                urljoin(B_URL, href)
                for href in hrefs
                if href
            }

            old_count = len(links)
            links.update(new_links)

            print(
                f"  Tìm thấy {len(new_links)} link, "
                f"tổng: {len(links)}"
            )

            if len(links) == old_count:
                break

            if page_number >= 30:
                break

            page_number += 1

        except PWTimeout:
            print("Timeout khi phân trang.")
            break

        except Exception as e:
            print(f"Lỗi khi cào trang {page_number}: {e}")
            break

    return sorted(links)


# =========================================================
# LẤY THỂ LOẠI SẢN PHẨM
# =========================================================

def get_category(page, product_name):
    # 1. Ưu tiên meta product:category
    try:
        category = page.get_attribute(
            'meta[property="product:category"]',
            "content"
        )
        if category and category.strip():
            return category.strip()
    except Exception:
        pass

    # 2. Thử JSON-LD: category hoặc productType
    try:
        scripts = page.locator('script[type="application/ld+json"]').all_inner_texts()

        for script_text in scripts:
            try:
                data = json.loads(script_text)

                items = data if isinstance(data, list) else [data]

                for item in items:
                    if not isinstance(item, dict):
                        continue

                    category = item.get("category") or item.get("productType")
                    if isinstance(category, str) and category.strip():
                        return category.strip()

            except Exception:
                continue
    except Exception:
        pass

    # 3. Lấy collection từ breadcrumb
    try:
        collection_links = page.locator('a[href*="/collections/"]').all()

        for link in collection_links:
            href = link.get_attribute("href") or ""
            text = link.inner_text().strip()

            if (
                text
                and "/collections/all" not in href
                and text.lower() not in {"tất cả sản phẩm", "all"}
            ):
                return text
    except Exception:
        pass

    # 4. Fallback theo tên sản phẩm
    name = (product_name or "").lower()

    if "micro" in name:
        return "Micro thu âm"
    if "tai nghe" in name:
        return "Tai nghe"
    if "loa" in name:
        return "Loa"
    if any(word in name for word in ["cáp", "cap", "chuyển đổi", "adapter", "phụ kiện"]):
        return "Phụ kiện"

    return "Khác"


# =========================================================
# CÀO CHI TIẾT SẢN PHẨM
# =========================================================

def scrape_product(page, url):
    product = {
        "name": None,
        "category": None,
        "price": None,
        "old_price": None,
        "in_stock": 0,
        "image": None,
        "url": url
    }

    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=30000
        )

        page.wait_for_selector("h1", timeout=10000)

        # Tên sản phẩm
        try:
            product["name"] = page.locator("h1").first.inner_text().strip()
        except Exception:
            product["name"] = None

        # Meta
        def get_meta(property_name):
            try:
                return page.get_attribute(
                    f'meta[property="{property_name}"]',
                    "content"
                )
            except Exception:
                return None

        product["image"] = get_meta("og:image")

        # Giá hiện tại
        price_amount = get_meta("og:price:amount")
        if price_amount:
            product["price"] = parse_price(price_amount)

        # Giá cũ
        try:
            old_price_text = page.locator(
                "s, del, .old-price, .price-old"
            ).first.inner_text(timeout=2000)

            product["old_price"] = parse_price(old_price_text)

        except Exception:
            product["old_price"] = None

        # Trạng thái
        body_text = page.locator("body").inner_text()

        if "Hết hàng" in body_text[:5000]:
            product["in_stock"] = 0
        else:
            product["in_stock"] = 1

        # Thể loại
        product["category"] = get_category(page, product["name"])

        print(
            f"OK | {product['name']} | "
            f"{product['category']} | "
            f"{product['price']}đ | "
            f"{'Còn hàng' if product['in_stock'] else 'Hết hàng'}"
        )

    except Exception as e:
        print(f"Lỗi sản phẩm {url}: {e}")

    return product


# =========================================================
# LƯU SẢN PHẨM VÀO SQLITE
# Không dùng ID, không UPDATE
# =========================================================

def save_product(product):
    if not product["name"]:
        return

    conn = connect_db()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO products (
            name,
            category,
            price,
            old_price,
            in_stock,
            image,
            url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(url) DO UPDATE SET
            name = excluded.name,
            category = excluded.category,
            price = excluded.price,
            old_price = excluded.old_price,
            in_stock = excluded.in_stock,
            image = excluded.image
    """, (
        product["name"],
        product["category"],
        product["price"],
        product["old_price"],
        product["in_stock"],
        product["image"],
        product["url"]
    ))

    conn.commit()
    conn.close()


# =========================================================
# CÀO GOCHEK
# =========================================================

def crawl_gochek():
    print("\n========== CRAWL GOCHEK ==========")

    if not check_robots():
        print("Robots.txt không cho phép cào.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            user_agent=USER_AGENT
        )

        page = context.new_page()

        print("\nBước 1: Lấy danh sách sản phẩm...")
        product_links = get_product_links(page)

        print(
            f"\nTổng cộng tìm thấy "
            f"{len(product_links)} sản phẩm."
        )

        print("\nBước 2: Cào chi tiết sản phẩm...")

        for index, url in enumerate(product_links, 1):
            print(f"\n[{index}/{len(product_links)}] {url}")

            product = scrape_product(page, url)

            if product["name"]:
                save_product(product)

            time.sleep(random.uniform(1, 2))

        browser.close()

    print("\nĐã lưu dữ liệu vào gochek.db")


# =========================================================
# HIỂN THỊ TẤT CẢ + % GIÁ
# =========================================================

def show_all_products():
    conn = connect_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            name,
            category,
            price,
            old_price,
            in_stock,
            ROUND(
                price * 100.0 /
                NULLIF((SELECT SUM(price) FROM products), 0),
                2
            ) AS price_percent,
            url
        FROM products
        ORDER BY price DESC
    """)

    products = cursor.fetchall()
    conn.close()

    if not products:
        print("Chưa có sản phẩm.")
        return

    print("\n================ ALL PRODUCTS ================")

    for product in products:
        stock = "Còn hàng" if product[4] else "Hết hàng"

        price = (
            f"{product[2]:,}đ"
            if product[2] is not None
            else "Không có"
        )

        old_price = (
            f"{product[3]:,}đ"
            if product[3] is not None
            else "Không có"
        )

        percent = (
            f"{product[5]:.2f}%"
            if product[5] is not None
            else "0.00%"
        )

        print(f"\nTên: {product[0]}")
        print(f"Thể loại: {product[1] or 'Khác'}")
        print(f"Giá: {price}")
        print(f"Giá cũ: {old_price}")
        print(f"Tỷ lệ giá: {percent} tổng giá")
        print(f"Trạng thái: {stock}")
        print(f"URL: {product[6]}")
        print("-" * 60)


# =========================================================
# TÌM THEO TÊN
# =========================================================

def find_product_by_name():
    keyword = input("Nhập tên sản phẩm cần tìm: ").strip()

    conn = connect_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            name,
            category,
            price,
            old_price,
            in_stock
        FROM products
        WHERE name LIKE ?
        ORDER BY name
    """, (f"%{keyword}%",))

    products = cursor.fetchall()
    conn.close()

    if not products:
        print("Không tìm thấy sản phẩm.")
        return

    print("\n========== SEARCH RESULT ==========")

    for product in products:
        stock = "Còn hàng" if product[4] else "Hết hàng"
        price = f"{product[2]:,}đ" if product[2] is not None else "Không có"

        print(
            f"Tên: {product[0]} | "
            f"Thể loại: {product[1] or 'Khác'} | "
            f"Giá: {price} | "
            f"{stock}"
        )


# =========================================================
# TÌM THEO GIÁ
# =========================================================

def find_product_by_price():
    try:
        min_price = int(input("Giá thấp nhất: "))
        max_price = int(input("Giá cao nhất: "))
    except ValueError:
        print("Giá phải là số.")
        return

    conn = connect_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            name,
            category,
            price,
            in_stock
        FROM products
        WHERE price BETWEEN ? AND ?
        ORDER BY price ASC
    """, (min_price, max_price))

    products = cursor.fetchall()
    conn.close()

    if not products:
        print("Không có sản phẩm trong khoảng giá.")
        return

    print("\n========== PRICE RESULT ==========")

    for product in products:
        stock = "Còn hàng" if product[3] else "Hết hàng"
        price = f"{product[2]:,}đ" if product[2] is not None else "Không có"

        print(
            f"Tên: {product[0]} | "
            f"Thể loại: {product[1] or 'Khác'} | "
            f"{price} | {stock}"
        )


# =========================================================
# TÌM THEO THỂ LOẠI
# =========================================================

def find_product_by_category():
    keyword = input("Nhập thể loại cần tìm: ").strip()

    conn = connect_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            name,
            category,
            price,
            in_stock
        FROM products
        WHERE category LIKE ?
        ORDER BY price DESC
    """, (f"%{keyword}%",))

    products = cursor.fetchall()
    conn.close()

    if not products:
        print("Không tìm thấy sản phẩm thuộc thể loại này.")
        return

    print("\n========== CATEGORY RESULT ==========")

    for product in products:
        stock = "Còn hàng" if product[3] else "Hết hàng"
        price = f"{product[2]:,}đ" if product[2] is not None else "Không có"

        print(
            f"Tên: {product[0]} | "
            f"Thể loại: {product[1] or 'Khác'} | "
            f"Giá: {price} | {stock}"
        )


# =========================================================
# XÓA THEO URL - KHÔNG DÙNG ID
# =========================================================

def delete_product():
    url = input("Nhập URL sản phẩm cần xóa: ").strip()

    if not url:
        print("URL không được để trống.")
        return

    conn = connect_db()
    cursor = conn.cursor()

    cursor.execute("""
        DELETE FROM products
        WHERE url = ?
    """, (url,))

    if cursor.rowcount > 0:
        print("Xóa sản phẩm thành công!")
    else:
        print("Không tìm thấy sản phẩm với URL này.")

    conn.commit()
    conn.close()


# =========================================================
# XÓA TOÀN BỘ
# =========================================================

def delete_all_products():
    confirm = input(
        "Bạn có chắc muốn xóa toàn bộ dữ liệu? (y/n): "
    ).strip().lower()

    if confirm != "y":
        print("Đã hủy.")
        return

    conn = connect_db()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM products")

    conn.commit()
    conn.close()

    print("Đã xóa toàn bộ sản phẩm.")


# =========================================================
# THỐNG KÊ TỔNG QUAN
# =========================================================

def statistics():
    conn = connect_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(
                CASE WHEN in_stock = 1 THEN 1 ELSE 0 END
            ), 0) AS in_stock,
            COALESCE(SUM(
                CASE WHEN in_stock = 0 THEN 1 ELSE 0 END
            ), 0) AS out_of_stock,
            ROUND(AVG(price), 0) AS average_price,
            MIN(price) AS min_price,
            MAX(price) AS max_price,
            COALESCE(SUM(price), 0) AS total_price
        FROM products
    """)

    result = cursor.fetchone()

    print("\n========== STATISTICS ==========")
    print(f"Tổng sản phẩm: {result[0] or 0}")
    print(f"Còn hàng:      {result[1] or 0}")
    print(f"Hết hàng:      {result[2] or 0}")

    if result[3] is not None:
        print(f"Giá trung bình: {result[3]:,.0f}đ")
    else:
        print("Giá trung bình: 0đ")

    print(f"Giá thấp nhất:  {result[4]:,}đ" if result[4] is not None else "Giá thấp nhất:  0đ")
    print(f"Giá cao nhất:   {result[5]:,}đ" if result[5] is not None else "Giá cao nhất:   0đ")
    print(f"Tổng giá sản phẩm: {result[6]:,}đ")

    # Thống kê theo thể loại
    print("\n---------- THEO THỂ LOẠI ----------")

    cursor.execute("""
        SELECT
            COALESCE(category, 'Khác') AS category,
            COUNT(*) AS product_count,
            COALESCE(SUM(price), 0) AS category_total,
            ROUND(
                COALESCE(SUM(price), 0) * 100.0 /
                NULLIF((SELECT SUM(price) FROM products), 0),
                2
            ) AS category_percent,
            ROUND(AVG(price), 0) AS category_avg
        FROM products
        GROUP BY COALESCE(category, 'Khác')
        ORDER BY category_total DESC
    """)

    categories = cursor.fetchall()

    for row in categories:
        print(
            f"Thể loại: {row[0]} | "
            f"Số SP: {row[1]} | "
            f"Tổng giá: {row[2]:,}đ | "
            f"% giá: {row[3] or 0:.2f}% | "
            f"Giá TB: {row[4]:,.0f}đ" if row[4] is not None
            else
            f"Thể loại: {row[0]} | "
            f"Số SP: {row[1]} | "
            f"Tổng giá: {row[2]:,}đ | "
            f"% giá: {row[3] or 0:.2f}% | "
            f"Giá TB: 0đ"
        )

    # =====================================================
    # THỐNG KÊ TỪNG SẢN PHẨM + TỔNG GIÁ
    # =====================================================
    print("\n---------- THỐNG KÊ TỪNG SẢN PHẨM ----------")

    cursor.execute("""
        SELECT
            name,
            category,
            price,
            ROUND(
                price * 100.0 /
                NULLIF((SELECT SUM(price) FROM products), 0),
                2
            ) AS price_percent
        FROM products
        WHERE price IS NOT NULL
        ORDER BY price DESC, name ASC
    """)

    rows = cursor.fetchall()

    if rows:
        for index, row in enumerate(rows, 1):
            print(
                f"{index}. {row[0]} | "
                f"Thể loại: {row[1] or 'Khác'} | "
                f"Giá: {row[2]:,}đ | "
                f"% tổng giá: {row[3] or 0:.2f}%"
            )
    else:
        print("Chưa có dữ liệu giá sản phẩm.")

    # =====================================================
    # THỐNG KÊ GỘP THEO GIÁ
    # Ví dụ: có 3 sản phẩm cùng giá 650.000đ
    # thì số lượng = 3 và tổng giá = 1.950.000đ
    # =====================================================
    print("\n---------- THỐNG KÊ THEO GIÁ SẢN PHẨM ----------")

    cursor.execute("""
        SELECT
            price,
            COUNT(*) AS product_count,
            SUM(price) AS total_by_price,
            ROUND(
                SUM(price) * 100.0 /
                NULLIF((SELECT SUM(price) FROM products), 0),
                2
            ) AS price_group_percent
        FROM products
        WHERE price IS NOT NULL
        GROUP BY price
        ORDER BY price DESC
    """)

    price_groups = cursor.fetchall()

    if price_groups:
        for row in price_groups:
            print(
                f"Giá: {row[0]:,}đ | "
                f"Số sản phẩm: {row[1]} | "
                f"Tổng giá: {row[2]:,}đ | "
                f"% tổng giá: {row[3] or 0:.2f}%"
            )
    else:
        print("Chưa có dữ liệu để thống kê theo giá.")

    # =====================================================
    # TỔNG GIÁ CỦA SẢN PHẨM CÒN HÀNG / HẾT HÀNG
    # =====================================================
    print("\n---------- TỔNG GIÁ THEO TRẠNG THÁI ----------")

    cursor.execute("""
        SELECT
            CASE
                WHEN in_stock = 1 THEN 'Còn hàng'
                ELSE 'Hết hàng'
            END AS stock_status,
            COUNT(*) AS product_count,
            COALESCE(SUM(price), 0) AS total_price
        FROM products
        GROUP BY in_stock
        ORDER BY in_stock DESC
    """)

    stock_rows = cursor.fetchall()

    for row in stock_rows:
        print(
            f"{row[0]} | "
            f"Số sản phẩm: {row[1]} | "
            f"Tổng giá: {row[2]:,}đ"
        )

    conn.close()


# =========================================================
# MENU
# BỎ UPDATE + BỎ ID
# =========================================================

def menu():
    while True:
        print("\n")
        print("=" * 55)
        print("             GOCHEK PRODUCT MANAGEMENT")
        print("=" * 55)
        print("1. Crawl GoChek Products")
        print("2. Show All Products + % Giá")
        print("3. Find Product by Name")
        print("4. Find Product by Price")
        print("5. Find Product by Category")
        print("6. Delete Product by URL")
        print("7. Delete All Products")
        print("8. Statistics - Thống kê sản phẩm & tổng giá")
        print("0. Exit")
        print("=" * 55)

        choice = input("Enter your choice: ").strip()

        if choice == "1":
            crawl_gochek()

        elif choice == "2":
            show_all_products()

        elif choice == "3":
            find_product_by_name()

        elif choice == "4":
            find_product_by_price()

        elif choice == "5":
            find_product_by_category()

        elif choice == "6":
            delete_product()

        elif choice == "7":
            delete_all_products()

        elif choice == "8":
            statistics()

        elif choice == "0":
            print("Goodbye!")
            break

        else:
            print("Invalid choice. Please try again.")


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    create_table()
    menu()