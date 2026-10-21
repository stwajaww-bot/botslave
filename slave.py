import os
import html
import io
import time
import base64
import sqlite3
import functools
import threading
import requests
import telebot
from datetime import datetime, timedelta, timezone
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

# ======================================================================
# KONFIGURASI
# ======================================================================
TOKEN = os.getenv("BOT_TOKEN")
KLIQRIS_API_KEY = os.getenv("KLIQRIS_API_KEY")
KLIQRIS_MERCHANT_ID = os.getenv("KLIQRIS_MERCHANT_ID")
KLIQRIS_BASE_URL = os.getenv("KLIQRIS_BASE_URL", "https://klikqris.com/api")
PACKAGE_PRICE = 30000
PAYMENT_CHECK_INTERVAL = int(os.getenv("PAYMENT_CHECK_INTERVAL", "10"))
ORDER_MAX_AGE = 24 * 3600  # order PENDING lebih tua dari ini dianggap kedaluwarsa
DB_PATH = os.getenv("DB_PATH", "slave_bot.db")
WIB = timezone(timedelta(hours=7))

VIP_GROUP_IDS = [-1004451939488, -1004486985873, -1003813292350]
SLAVE_GROUP_IDS = [-1003745212632]
ALL_GROUP_IDS = VIP_GROUP_IDS + SLAVE_GROUP_IDS  # nomor grup untuk /kirimlink: 1..N

PACKAGES = {
    "vip": ("Tambahan Paket VIP 3 Grup", VIP_GROUP_IDS),
    "slave": ("Paket Slave", SLAVE_GROUP_IDS),
}

PAID_STATUSES = {"SUCCESS", "PAID", "SETTLED", "COMPLETED", "BERHASIL"}
EXPIRED_STATUSES = {"EXPIRED", "CANCELLED", "CANCELED"}


def parse_admin_ids(raw):
    ids = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


# ADMIN_IDS = beberapa admin dipisah koma. ADMIN_ID lama tetap didukung.
ADMIN_IDS = parse_admin_ids(os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID"))

bot = telebot.TeleBot(TOKEN) if TOKEN else None
pending_broadcasts = {}  # admin_id -> teks


def check_config():
    missing = []
    if not TOKEN:
        missing.append("BOT_TOKEN")
    if not ADMIN_IDS:
        missing.append("ADMIN_IDS")
    if not KLIQRIS_API_KEY:
        missing.append("KLIQRIS_API_KEY")
    if not KLIQRIS_MERCHANT_ID:
        missing.append("KLIQRIS_MERCHANT_ID")
    if missing:
        raise RuntimeError("Environment variable belum diisi: " + ", ".join(missing))


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ======================================================================
# DATABASE (SQLite)
# ======================================================================
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db_lock = threading.RLock()


def db_exec(sql, params=(), fetch=None):
    with _db_lock:
        cur = _db.execute(sql, params)
        _db.commit()
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        return cur


def init_db():
    with _db_lock:
        _db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                blocked INTEGER DEFAULT 0,
                first_seen INTEGER,
                last_seen INTEGER
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                user_id INTEGER,
                package TEXT,
                amount INTEGER,
                status TEXT,
                created_at INTEGER,
                paid_at INTEGER,
                expired_at TEXT,
                message_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                order_id TEXT,
                group_id INTEGER,
                invite_link TEXT,
                created_at INTEGER,
                revoked INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_links_user ON links(user_id);
            CREATE INDEX IF NOT EXISTS idx_links_order ON links(order_id);
            """
        )
        _db.commit()


def touch_user(u):
    now = int(time.time())
    name = " ".join(filter(None, [u.first_name, u.last_name]))
    db_exec(
        """INSERT INTO users(user_id, username, full_name, first_seen, last_seen)
           VALUES(?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
             username=excluded.username,
             full_name=excluded.full_name,
             last_seen=excluded.last_seen""",
        (u.id, u.username or "", name, now, now),
    )


def is_blocked(user_id):
    row = db_exec("SELECT blocked FROM users WHERE user_id=?", (user_id,), "one")
    return bool(row and row["blocked"])


def user_label(user_id):
    row = db_exec("SELECT username, full_name FROM users WHERE user_id=?", (user_id,), "one")
    if not row:
        return f"<code>{user_id}</code>"
    parts = []
    if row["full_name"]:
        parts.append(html.escape(row["full_name"]))
    if row["username"]:
        parts.append("@" + html.escape(row["username"]))
    parts.append(f"<code>{user_id}</code>")
    return " — ".join(parts)


# ======================================================================
# HELPER UMUM
# ======================================================================
def rupiah(value):
    try:
        return "Rp " + f"{int(float(value)):,}".replace(",", ".")
    except Exception:
        return f"Rp {value}"


def fmt_ts(ts):
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, WIB).strftime("%d/%m %H:%M")


def package_info(pkg_code):
    return PACKAGES.get(pkg_code, PACKAGES["slave"])


def group_number(group_id):
    return ALL_GROUP_IDS.index(group_id) + 1


def groups_legend():
    lines = []
    for i, gid in enumerate(ALL_GROUP_IDS, 1):
        lines.append(f"{i} = {'VIP' if gid in VIP_GROUP_IDS else 'Slave'}")
    return ", ".join(lines)


def notify_admins(text):
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as exc:
            print(f"[NOTIFY ADMIN ERROR] admin={admin_id}: {exc}")


def main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("🛒 Tambahan Paket VIP 3 Grup (Rp 30.000)"))
    markup.add(KeyboardButton("⛓️ Paket Slave (Rp 30.000)"))
    markup.add(KeyboardButton("⭐ Testimoni"), KeyboardButton("❓ Bantuan"))
    markup.add(KeyboardButton("📞 Hubungi Admin"))
    return markup


# ======================================================================
# KLIKQRIS
# ======================================================================
def klikqris_headers():
    return {"Content-Type": "application/json", "x-api-key": KLIQRIS_API_KEY, "id_merchant": KLIQRIS_MERCHANT_ID}


def create_order_id(user_id):
    return f"WD-{int(time.time())}-{user_id}"


def create_klikqris_transaction(order_id, pkg_label):
    url = f"{KLIQRIS_BASE_URL}/qris/create"
    payload = {
        "order_id": order_id,
        "id_merchant": KLIQRIS_MERCHANT_ID,
        "amount": PACKAGE_PRICE,
        "keterangan": f"Pembayaran {pkg_label}",
    }
    response = requests.post(url, json=payload, headers=klikqris_headers(), timeout=30)

    try:
        result = response.json()
    except Exception:
        raise RuntimeError(f"KlikQRIS HTTP {response.status_code}, response bukan JSON: {response.text[:500]}")

    # KlikQRIS berhasil membuat transaksi dengan HTTP 201.
    if not (200 <= response.status_code < 300):
        raise RuntimeError(f"KlikQRIS HTTP {response.status_code}: {result.get('message', result)}")

    if result.get("status") is False:
        raise RuntimeError(f"KlikQRIS HTTP {response.status_code}: {result.get('message', result)}")

    data = result.get("data") or {}
    if not data:
        data = {k: result.get(k) for k in ("order_id", "qris_image", "qris_url", "total_amount", "expired_at", "status") if result.get(k) is not None}

    if not data:
        raise RuntimeError(f"KlikQRIS berhasil membuat transaksi, tetapi data transaksi tidak ditemukan: {result}")

    data.setdefault("order_id", order_id)
    return data


def check_klikqris_status(order_id):
    url = f"{KLIQRIS_BASE_URL}/qris/status/{order_id}"
    response = requests.get(url, headers=klikqris_headers(), timeout=20)

    try:
        result = response.json()
    except Exception:
        raise RuntimeError(f"KlikQRIS status HTTP {response.status_code}, response bukan JSON.")

    if not (200 <= response.status_code < 300):
        raise RuntimeError(f"KlikQRIS status HTTP {response.status_code}: {result.get('message', result)}")

    if result.get("status") is False:
        raise RuntimeError(f"KlikQRIS status: {result.get('message', result)}")

    data = result.get("data") or {}
    if not data:
        data = {k: result.get(k) for k in ("order_id", "status", "payment_status", "transaction_status", "expired_at", "paid_at") if result.get(k) is not None}
    return data


def decode_qris_image(data):
    qris_image = data.get("qris_image")
    if qris_image:
        if isinstance(qris_image, str) and qris_image.startswith("data:image"):
            try:
                return io.BytesIO(base64.b64decode(qris_image.split(",", 1)[1]))
            except Exception as exc:
                raise RuntimeError(f"Gagal decode qris_image: {exc}")
        if isinstance(qris_image, str):
            try:
                return io.BytesIO(base64.b64decode(qris_image))
            except Exception:
                pass

    qris_url = data.get("qris_url")
    if qris_url:
        try:
            response = requests.get(qris_url, timeout=30)
            response.raise_for_status()
            return io.BytesIO(response.content)
        except Exception as exc:
            raise RuntimeError(f"Gagal mengambil qris_url: {exc}")

    raise RuntimeError(f"KlikQRIS tidak mengembalikan qris_image atau qris_url. Data: {data}")


def normalize_payment_status(data):
    for value in (data.get("status"), data.get("payment_status"), data.get("transaction_status")):
        if value is not None:
            return str(value).strip().upper()
    return ""


# ======================================================================
# ORDER & PENGIRIMAN LINK
# ======================================================================
def get_pending_order(user_id):
    return db_exec(
        "SELECT * FROM orders WHERE user_id=? AND status='PENDING' ORDER BY created_at DESC LIMIT 1",
        (user_id,), "one",
    )


def set_order_status(order_id, from_status, to_status):
    cur = db_exec("UPDATE orders SET status=? WHERE order_id=? AND status=?", (to_status, order_id, from_status))
    return cur.rowcount > 0


def send_payment_qr(user_id, pkg_code):
    pkg_label, _ = package_info(pkg_code)
    order_id = create_order_id(user_id)
    data = create_klikqris_transaction(order_id, pkg_label)
    total_amount = data.get("total_amount", PACKAGE_PRICE)
    expired_at = data.get("expired_at", "-")

    qr_file = decode_qris_image(data)
    qr_file.seek(0)

    caption = (
        f"💳 <b>{html.escape(pkg_label)}</b>\n\n"
        f"Nominal: <b>{rupiah(total_amount)}</b>\n"
        f"Order ID: <code>{html.escape(str(order_id))}</code>\n"
        f"Expired: <b>{html.escape(str(expired_at))}</b>\n\n"
        "Silakan scan QRIS di atas dan bayar sesuai nominal yang tertera.\n\n"
        "✅ Setelah pembayaran berhasil, bot akan mengecek pembayaran secara otomatis.\n"
        "❌ Tidak perlu mengirim screenshot bukti transfer."
    )
    sent = bot.send_photo(user_id, qr_file, caption=caption, parse_mode="HTML")

    try:
        amount_int = int(float(total_amount))
    except Exception:
        amount_int = PACKAGE_PRICE

    db_exec(
        """INSERT INTO orders(order_id, user_id, package, amount, status, created_at, expired_at, message_id)
           VALUES(?,?,?,?,?,?,?,?)""",
        (order_id, user_id, pkg_code, amount_int, "PENDING", int(time.time()), str(expired_at), sent.message_id),
    )


def cleanup_payment_message(order):
    try:
        if order["message_id"]:
            bot.delete_message(chat_id=order["user_id"], message_id=order["message_id"])
    except Exception:
        pass


def delivered_group_ids(order_id):
    """Grup yang linknya sudah pernah dibuat untuk order ini (termasuk yang kemudian dicabut)."""
    rows = db_exec("SELECT DISTINCT group_id FROM links WHERE order_id=?", (order_id,), "all")
    return {r["group_id"] for r in rows}


def missing_groups(order):
    _, groups = package_info(order["package"])
    done = delivered_group_ids(order["order_id"])
    return [g for g in groups if g not in done]


def incomplete_orders():
    result = []
    for order in db_exec("SELECT * FROM orders WHERE status='PAID' ORDER BY paid_at DESC", fetch="all"):
        missing = missing_groups(order)
        if missing:
            result.append((order, missing))
    return result


def create_and_record_link(user_id, group_id, order_id):
    invite = bot.create_chat_invite_link(chat_id=group_id, member_limit=1)
    db_exec(
        "INSERT INTO links(user_id, order_id, group_id, invite_link, created_at) VALUES(?,?,?,?,?)",
        (user_id, order_id, group_id, invite.invite_link, int(time.time())),
    )
    return invite.invite_link


def deliver_package(order):
    """Buat link hanya untuk grup yang belum terkirim, lalu kirim ke member. Return dict error."""
    pkg_label, groups = package_info(order["package"])
    order_id = order["order_id"]
    user_id = order["user_id"]
    done = delivered_group_ids(order_id)
    errors = {}

    for gid in groups:
        if gid in done:
            continue
        try:
            create_and_record_link(user_id, gid, order_id)
        except Exception as exc:
            errors[gid] = str(exc)
            print(f"[LINK ERROR] user={user_id} order={order_id} group={gid}: {exc}")

    rows = db_exec("SELECT invite_link FROM links WHERE order_id=? AND revoked=0 ORDER BY id", (order_id,), "all")
    links_text = "\n".join(f"• {r['invite_link']}" for r in rows)

    try:
        if rows:
            extra = ""
            if errors:
                extra = "\n\n⚠️ Sebagian link sedang diproses admin dan akan segera dikirim."
            bot.send_message(
                user_id,
                f"✅ <b>Pembayaran Berhasil!</b>\n\n"
                f"Paket: <b>{html.escape(pkg_label)}</b>\n"
                f"Order ID: <code>{html.escape(str(order_id))}</code>\n\n"
                f"Berikut link akses Anda:\n\n{links_text}\n\n"
                "<b>Catatan:</b>\n- Setiap link hanya dapat digunakan 1 kali.\n- Jangan bagikan link kepada orang lain."
                + extra,
                parse_mode="HTML",
            )
        else:
            bot.send_message(
                user_id,
                f"✅ <b>Pembayaran Berhasil!</b>\n\nOrder ID: <code>{html.escape(str(order_id))}</code>\n\n"
                "Link akses sedang diproses admin dan akan segera dikirim. Mohon tunggu sebentar.",
                parse_mode="HTML",
            )
    except Exception as exc:
        errors["kirim"] = str(exc)
        print(f"[SEND LINK ERROR] user={user_id} order={order_id}: {exc}")

    return errors


def notify_incomplete(order, errors):
    _, groups = package_info(order["package"])
    if "kirim" in errors:
        targets = groups  # link terbuat tapi gagal terkirim ke member
    else:
        targets = missing_groups(order)
    if not targets:
        return
    numbers = " ".join(str(group_number(g)) for g in targets)
    reasons = "\n".join(f"• {html.escape(str(v))[:200]}" for v in errors.values())
    notify_admins(
        "⚠️ <b>Pengiriman link belum tuntas</b>\n\n"
        f"Order: <code>{html.escape(order['order_id'])}</code>\n"
        f"Member: {user_label(order['user_id'])}\n"
        f"Grup belum terkirim: <b>{html.escape(numbers.replace(' ', ', '))}</b>\n\n"
        f"Penyebab:\n{reasons or '-'}\n\n"
        "Kirim manual dengan perintah ini:\n"
        f"/kirimlink {order['user_id']} {numbers}"
    )


def handle_paid(order):
    order_id = order["order_id"]
    cur = db_exec("UPDATE orders SET status='PAID', paid_at=? WHERE order_id=? AND status='PENDING'", (int(time.time()), order_id))
    if cur.rowcount == 0:
        return  # sudah diproses
    cleanup_payment_message(order)
    errors = deliver_package(order)

    pkg_label, _ = package_info(order["package"])
    status_link = "✅ terkirim lengkap" if not errors else "⚠️ belum tuntas"
    notify_admins(
        "💰 <b>Pembayaran masuk</b>\n\n"
        f"Paket: <b>{html.escape(pkg_label)}</b>\n"
        f"Nominal: <b>{rupiah(order['amount'])}</b>\n"
        f"Member: {user_label(order['user_id'])}\n"
        f"Order: <code>{html.escape(order_id)}</code>\n"
        f"Link: {status_link}"
    )
    if errors:
        notify_incomplete(order, errors)


def process_pending_order(order):
    order_id = order["order_id"]
    user_id = order["user_id"]

    if time.time() - order["created_at"] > ORDER_MAX_AGE:
        if set_order_status(order_id, "PENDING", "EXPIRED"):
            cleanup_payment_message(order)
        return

    data = check_klikqris_status(order_id)
    status = normalize_payment_status(data)
    print(f"[KLIQRIS] order={order_id} status={status or 'UNKNOWN'}")

    if status in PAID_STATUSES:
        handle_paid(order)
    elif status in EXPIRED_STATUSES:
        if set_order_status(order_id, "PENDING", "EXPIRED"):
            try:
                bot.send_message(user_id, "⏰ <b>QRIS sudah kedaluwarsa.</b>\n\nSilakan pilih paket kembali untuk membuat QRIS baru.", parse_mode="HTML")
            except Exception:
                pass
            cleanup_payment_message(order)


def payment_monitor():
    while True:
        time.sleep(PAYMENT_CHECK_INTERVAL)
        try:
            pending = db_exec("SELECT * FROM orders WHERE status='PENDING'", fetch="all")
        except Exception as exc:
            print(f"[MONITOR DB ERROR] {exc}")
            continue
        for order in pending:
            try:
                process_pending_order(order)
            except Exception as exc:
                print(f"[STATUS CHECK ERROR] user={order['user_id']} order={order['order_id']}: {exc}")


# ======================================================================
# GUARD: catat user + tolak user yang diblokir (harus didaftarkan PERTAMA)
# ======================================================================
def _track_and_check_blocked(message):
    u = message.from_user
    if u is None or message.chat.type != "private":
        return False
    touch_user(u)
    return is_blocked(u.id) and not is_admin(u.id)


@bot.message_handler(func=_track_and_check_blocked)
def handle_blocked(message):
    bot.send_message(message.chat.id, "🚫 Akun Anda diblokir. Hubungi admin jika ini keliru.")


@bot.callback_query_handler(func=lambda call: is_blocked(call.from_user.id) and not is_admin(call.from_user.id))
def handle_blocked_callback(call):
    bot.answer_callback_query(call.id, "Akun Anda diblokir.", show_alert=True)


# ======================================================================
# HANDLER MEMBER
# ======================================================================
@bot.message_handler(commands=["id"])
def cmd_id(message):
    status = "ADMIN ✅" if is_admin(message.from_user.id) else "bukan admin ❌"
    bot.send_message(
        message.chat.id,
        f"🆔 ID Telegram Anda: <code>{message.from_user.id}</code>\nStatus: <b>{status}</b>",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.send_chat_action(message.chat.id, "typing")
    bot.reply_to(
        message,
        "Halo! Selamat datang di bot WarungDosa.\n\n"
        "🔥 <b>Pilihan Paket Pembelian:</b>\n"
        "1. <b>Tambahan Paket VIP 3 Grup</b> — Rp 30.000\n"
        "2. <b>Paket Slave</b> — Rp 30.000\n\n"
        "QRIS akan dibuat dinamis untuk setiap transaksi.",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


@bot.message_handler(func=lambda message: message.text == "🛒 Tambahan Paket VIP 3 Grup (Rp 30.000)")
def handle_buy_vip(message):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("💳 Buat QRIS Dinamis", callback_data="create_qris_vip"))
    bot.send_message(message.chat.id, "Anda memilih <b>Tambahan Paket VIP 3 Grup</b> — Rp 30.000.\n\nKlik tombol di bawah untuk membuat QRIS pembayaran baru.", parse_mode="HTML", reply_markup=markup)


@bot.message_handler(func=lambda message: message.text == "⛓️ Paket Slave (Rp 30.000)")
def handle_buy_slave(message):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("💳 Buat QRIS Dinamis", callback_data="create_qris_slave"))
    bot.send_message(message.chat.id, "Anda memilih <b>Paket Slave</b> — Rp 30.000.\n\nKlik tombol di bawah untuk membuat QRIS pembayaran baru.", parse_mode="HTML", reply_markup=markup)


@bot.message_handler(func=lambda message: message.text == "⭐ Testimoni")
def handle_testimoni(message):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔗 Buka Channel Testimoni", url="https://t.me/testiwarungdosaa"))
    bot.send_message(message.chat.id, "⭐ <b>Testimoni Pelanggan WarungDosa</b>\n\nSilakan klik tombol di bawah untuk melihat testimoni.", parse_mode="HTML", reply_markup=markup)


@bot.message_handler(func=lambda message: message.text == "❓ Bantuan")
def handle_faq(message):
    bot.reply_to(message, "💡 <b>Panduan:</b>\n\n1. Pilih paket.\n2. Klik <b>Buat QRIS Dinamis</b>.\n3. Scan QRIS dan bayar sesuai nominal yang tampil.\n4. Bot mengecek pembayaran otomatis.\n5. Setelah sukses, link grup dikirim otomatis.\n\nTidak perlu mengirim screenshot bukti transfer.", parse_mode="HTML")


@bot.message_handler(func=lambda message: message.text == "📞 Hubungi Admin")
def handle_contact_admin(message):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("💬 Chat Admin Sekarang", url="https://t.me/WarungDosa"))
    bot.send_message(message.chat.id, "💬 Silakan hubungi Admin jika mengalami kendala pembayaran.", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data in ["create_qris_vip", "create_qris_slave"])
def process_create_qris(call):
    user_id = call.from_user.id
    pkg_code = "vip" if call.data == "create_qris_vip" else "slave"
    bot.answer_callback_query(call.id, "Membuat QRIS dinamis...")

    existing = get_pending_order(user_id)
    if existing:
        bot.send_message(
            user_id,
            "⚠️ Anda masih memiliki pembayaran yang menunggu.\n\n"
            f"Order ID: <code>{html.escape(existing['order_id'])}</code>\n"
            f"Nominal: <b>{rupiah(existing['amount'])}</b>\n"
            f"Expired: <b>{html.escape(str(existing['expired_at']))}</b>",
            parse_mode="HTML",
        )
        return

    try:
        send_payment_qr(user_id, pkg_code)
    except Exception as exc:
        print(f"[CREATE QR ERROR] {exc}")
        bot.send_message(
            user_id,
            "❌ <b>Gagal membuat QRIS.</b>\n\n"
            f"<code>{html.escape(str(exc))}</code>\n\n"
            "Silakan coba lagi atau hubungi admin.",
            parse_mode="HTML",
        )


# ======================================================================
# PERINTAH ADMIN
# ======================================================================
def admin_only(func):
    @functools.wraps(func)
    def wrapper(message):
        if not is_admin(message.from_user.id):
            return
        try:
            func(message)
        except Exception as exc:
            print(f"[ADMIN CMD ERROR] {func.__name__}: {exc}")
            bot.send_message(message.chat.id, f"❌ Error: <code>{html.escape(str(exc))}</code>", parse_mode="HTML")
    return wrapper


def reply(message, text):
    bot.send_message(message.chat.id, text, parse_mode="HTML", disable_web_page_preview=True)


def cmd_args(message):
    return message.text.split()[1:]


def parse_user_id(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_targets(tokens):
    gids = []
    for token in tokens:
        t = token.lower()
        if t in ("semua", "all"):
            new = ALL_GROUP_IDS
        elif t == "vip":
            new = VIP_GROUP_IDS
        elif t == "slave":
            new = SLAVE_GROUP_IDS
        elif t.isdigit() and 1 <= int(t) <= len(ALL_GROUP_IDS):
            new = [ALL_GROUP_IDS[int(t) - 1]]
        else:
            raise ValueError(f"Target tidak dikenal: {token}")
        for g in new:
            if g not in gids:
                gids.append(g)
    return gids


PERIOD_LABELS = {"hari": "Hari ini", "kemarin": "Kemarin", "minggu": "Minggu ini", "bulan": "Bulan ini", "semua": "Semua waktu"}


def period_range(name):
    now = datetime.now(WIB)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end = None
    if name == "hari":
        start = today
    elif name == "kemarin":
        start, end = today - timedelta(days=1), today
    elif name == "minggu":
        start = today - timedelta(days=today.weekday())
    elif name == "bulan":
        start = today.replace(day=1)
    return (int(start.timestamp()) if start else None, int(end.timestamp()) if end else None)


def sales_between(start=None, end=None):
    sql = "SELECT package, COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM orders WHERE status='PAID'"
    params = []
    if start is not None:
        sql += " AND paid_at>=?"
        params.append(start)
    if end is not None:
        sql += " AND paid_at<?"
        params.append(end)
    sql += " GROUP BY package"
    return db_exec(sql, params, "all")


@bot.message_handler(commands=["admin"])
@admin_only
def cmd_admin(message):
    reply(
        message,
        "🛠 <b>Panel Admin</b>\n\n"
        "/stats — ringkasan semua periode\n"
        "/laporan [hari|kemarin|minggu|bulan|semua] — laporan penjualan\n"
        "/cari &lt;kata&gt; — cari member (username, nama, awal ID)\n"
        "/cek &lt;id&gt; — info member\n"
        "/riwayat &lt;id&gt; — 10 order terakhir member\n"
        "/kirimlink &lt;id&gt; &lt;nomor...&gt; — kirim link sekali klik\n"
        "/reset &lt;id&gt; — cabut link tercatat &amp; bebaskan transaksi macet\n"
        "/blokir &lt;id&gt; — blokir member\n"
        "/unblokir &lt;id&gt; — buka blokir\n"
        "/broadcast &lt;teks&gt; — kirim pesan ke semua member\n"
        "/id — cek ID &amp; status admin\n\n"
        f"<b>Nomor grup:</b> {html.escape(groups_legend())}, atau <code>vip</code> / <code>slave</code> / <code>semua</code>\n"
        "Contoh: <code>/kirimlink 8953615855 1 3</code>",
    )


@bot.message_handler(commands=["stats"])
@admin_only
def cmd_stats(message):
    lines = ["📈 <b>Statistik</b>\n"]
    for name in ("hari", "kemarin", "minggu", "bulan", "semua"):
        rows = sales_between(*period_range(name))
        count = sum(r["c"] for r in rows)
        total = sum(r["s"] for r in rows)
        lines.append(f"• {PERIOD_LABELS[name]}: <b>{count}</b> order — {rupiah(total)}")

    total_users = db_exec("SELECT COUNT(*) AS c FROM users", fetch="one")["c"]
    blocked = db_exec("SELECT COUNT(*) AS c FROM users WHERE blocked=1", fetch="one")["c"]
    pending = db_exec("SELECT COUNT(*) AS c FROM orders WHERE status='PENDING'", fetch="one")["c"]
    lines.append(f"\n👥 Pengguna: <b>{total_users}</b> (diblokir: {blocked})")
    lines.append(f"⏳ Order menunggu: <b>{pending}</b>")

    incomplete = incomplete_orders()
    lines.append(f"⚠️ Lunas tapi link belum tuntas: <b>{len(incomplete)}</b>")
    for order, missing in incomplete[:5]:
        numbers = " ".join(str(group_number(g)) for g in missing)
        lines.append(f"/kirimlink {order['user_id']} {numbers}")
    reply(message, "\n".join(lines))


@bot.message_handler(commands=["laporan"])
@admin_only
def cmd_laporan(message):
    args = cmd_args(message)
    name = (args[0].lower() if args else "hari")
    if name not in PERIOD_LABELS:
        reply(message, "Pilihan: <code>hari</code>, <code>kemarin</code>, <code>minggu</code>, <code>bulan</code>, <code>semua</code>\nContoh: <code>/laporan bulan</code>")
        return
    rows = sales_between(*period_range(name))
    count = sum(r["c"] for r in rows)
    total = sum(r["s"] for r in rows)
    lines = [
        f"📊 <b>Laporan {PERIOD_LABELS[name]}</b>\n",
        f"Order lunas: <b>{count}</b>",
        f"Pendapatan: <b>{rupiah(total)}</b>",
    ]
    if rows:
        lines.append("\nRincian:")
        for r in rows:
            lines.append(f"• {html.escape(package_info(r['package'])[0])}: {r['c']} order — {rupiah(r['s'])}")
    reply(message, "\n".join(lines))


@bot.message_handler(commands=["cari"])
@admin_only
def cmd_cari(message):
    args = cmd_args(message)
    if not args:
        reply(message, "Contoh: <code>/cari budi</code>")
        return
    q = " ".join(args).lstrip("@")
    rows = db_exec(
        """SELECT * FROM users
           WHERE CAST(user_id AS TEXT) LIKE ? OR username LIKE ? OR full_name LIKE ?
           ORDER BY last_seen DESC LIMIT 15""",
        (q + "%", "%" + q + "%", "%" + q + "%"), "all",
    )
    if not rows:
        reply(message, "Member tidak ditemukan.")
        return
    lines = [f"🔎 <b>Hasil pencarian</b> ({len(rows)})\n"]
    for r in rows:
        flag = " 🚫" if r["blocked"] else ""
        lines.append(f"{user_label(r['user_id'])}{flag}")
    lines.append("\nLanjut: /cek &lt;id&gt; atau /riwayat &lt;id&gt;")
    reply(message, "\n".join(lines))


@bot.message_handler(commands=["cek"])
@admin_only
def cmd_cek(message):
    args = cmd_args(message)
    uid = parse_user_id(args[0]) if args else None
    if uid is None:
        reply(message, "Contoh: <code>/cek 8953615855</code>")
        return
    user = db_exec("SELECT * FROM users WHERE user_id=?", (uid,), "one")
    if not user:
        reply(message, "Member belum pernah memakai bot ini.")
        return
    spent = db_exec("SELECT COALESCE(SUM(amount),0) AS s, COUNT(*) AS c FROM orders WHERE user_id=? AND status='PAID'", (uid,), "one")
    active_links = db_exec("SELECT COUNT(*) AS c FROM links WHERE user_id=? AND revoked=0", (uid,), "one")["c"]
    reply(
        message,
        "👤 <b>Info Member</b>\n\n"
        f"{user_label(uid)}\n"
        f"Status: <b>{'🚫 DIBLOKIR' if user['blocked'] else '✅ Aktif'}</b>\n"
        f"Pertama kali: {fmt_ts(user['first_seen'])}\n"
        f"Terakhir aktif: {fmt_ts(user['last_seen'])}\n"
        f"Order lunas: <b>{spent['c']}</b>\n"
        f"Total belanja: <b>{rupiah(spent['s'])}</b>\n"
        f"Link tercatat aktif: <b>{active_links}</b>",
    )


@bot.message_handler(commands=["riwayat"])
@admin_only
def cmd_riwayat(message):
    args = cmd_args(message)
    uid = parse_user_id(args[0]) if args else None
    if uid is None:
        reply(message, "Contoh: <code>/riwayat 8953615855</code>")
        return
    orders = db_exec("SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (uid,), "all")
    if not orders:
        reply(message, "Belum ada order untuk member ini.")
        return
    lines = [f"🧾 <b>Riwayat</b> {user_label(uid)}\n"]
    for o in orders:
        line = (
            f"<code>{html.escape(o['order_id'])}</code>\n"
            f"{html.escape(package_info(o['package'])[0])} — {rupiah(o['amount'])} — <b>{o['status']}</b> — {fmt_ts(o['created_at'])}"
        )
        if o["status"] == "PAID":
            _, groups = package_info(o["package"])
            done = delivered_group_ids(o["order_id"])
            sent = [str(group_number(g)) for g in groups if g in done]
            miss = [str(group_number(g)) for g in groups if g not in done]
            line += f"\nGrup terkirim: {', '.join(sent) or '-'}"
            if miss:
                line += f"\n⚠️ Belum terkirim: {', '.join(miss)} → /kirimlink {uid} {' '.join(miss)}"
        lines.append(line + "\n")
    reply(message, "\n".join(lines))


def pick_order_for_group(user_id, group_id):
    rows = db_exec("SELECT * FROM orders WHERE user_id=? AND status='PAID' ORDER BY paid_at DESC", (user_id,), "all")
    for o in rows:
        _, groups = package_info(o["package"])
        if group_id in groups and group_id not in delivered_group_ids(o["order_id"]):
            return o["order_id"]
    return None


@bot.message_handler(commands=["kirimlink"])
@admin_only
def cmd_kirimlink(message):
    args = cmd_args(message)
    uid = parse_user_id(args[0]) if args else None
    if uid is None or len(args) < 2:
        reply(message, f"Contoh: <code>/kirimlink 8953615855 3</code>\nNomor grup: {html.escape(groups_legend())}\natau <code>vip</code> / <code>slave</code> / <code>semua</code>")
        return
    try:
        targets = parse_targets(args[1:])
    except ValueError as exc:
        reply(message, f"❌ {html.escape(str(exc))}")
        return

    ok, failed = [], []
    for gid in targets:
        try:
            order_id = pick_order_for_group(uid, gid)
            link = create_and_record_link(uid, gid, order_id)
            ok.append((gid, link))
        except Exception as exc:
            failed.append((gid, str(exc)))

    if ok:
        text = "🔗 <b>Link akses dari admin</b>\n\n" + "\n".join(f"• Grup {group_number(g)}: {l}" for g, l in ok)
        text += "\n\n<b>Catatan:</b>\n- Setiap link hanya dapat digunakan 1 kali.\n- Jangan bagikan link kepada orang lain."
        try:
            bot.send_message(uid, text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as exc:
            reply(message, f"❌ Link dibuat tapi gagal terkirim ke member (mungkin belum /start atau memblokir bot): <code>{html.escape(str(exc))}</code>")
            return

    lines = []
    if ok:
        lines.append("✅ Terkirim: grup " + ", ".join(str(group_number(g)) for g, _ in ok))
    for g, err in failed:
        lines.append(f"❌ Grup {group_number(g)} gagal: <code>{html.escape(err[:200])}</code>")
    reply(message, "\n".join(lines))


@bot.message_handler(commands=["reset"])
@admin_only
def cmd_reset(message):
    args = cmd_args(message)
    uid = parse_user_id(args[0]) if args else None
    if uid is None:
        reply(message, "Contoh: <code>/reset 8953615855</code>")
        return

    links = db_exec("SELECT * FROM links WHERE user_id=? AND revoked=0", (uid,), "all")
    revoked = 0
    for l in links:
        try:
            bot.revoke_chat_invite_link(l["group_id"], l["invite_link"])
        except Exception as exc:
            print(f"[REVOKE ERROR] {exc}")  # link mungkin sudah dipakai/kedaluwarsa
        db_exec("UPDATE links SET revoked=1 WHERE id=?", (l["id"],))
        revoked += 1

    freed = 0
    for o in db_exec("SELECT * FROM orders WHERE user_id=? AND status='PENDING'", (uid,), "all"):
        if set_order_status(o["order_id"], "PENDING", "CANCELLED"):
            cleanup_payment_message(o)
            freed += 1

    reply(
        message,
        f"♻️ <b>Reset selesai</b> untuk {user_label(uid)}\n\n"
        f"Link dicabut: <b>{revoked}</b>\n"
        f"Transaksi pending dibebaskan: <b>{freed}</b>\n\n"
        "Catatan: member yang sudah masuk grup lewat link tersebut tidak otomatis dikeluarkan.",
    )


def set_blocked(message, value):
    args = cmd_args(message)
    uid = parse_user_id(args[0]) if args else None
    cmd = "blokir" if value else "unblokir"
    if uid is None:
        reply(message, f"Contoh: <code>/{cmd} 8953615855</code>")
        return
    if is_admin(uid):
        reply(message, "❌ Tidak bisa memblokir admin.")
        return
    now = int(time.time())
    db_exec(
        """INSERT INTO users(user_id, username, full_name, blocked, first_seen, last_seen)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET blocked=excluded.blocked""",
        (uid, "", "", 1 if value else 0, now, now),
    )
    reply(message, f"{'🚫 Diblokir' if value else '✅ Blokir dibuka'}: {user_label(uid)}")


@bot.message_handler(commands=["blokir"])
@admin_only
def cmd_blokir(message):
    set_blocked(message, True)


@bot.message_handler(commands=["unblokir"])
@admin_only
def cmd_unblokir(message):
    set_blocked(message, False)


@bot.message_handler(commands=["broadcast"])
@admin_only
def cmd_broadcast(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        reply(message, "Contoh: <code>/broadcast Promo hari ini!</code>")
        return
    text = parts[1].strip()
    total = db_exec("SELECT COUNT(*) AS c FROM users WHERE blocked=0", fetch="one")["c"]
    pending_broadcasts[message.from_user.id] = text

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Kirim", callback_data="bc_send"),
        InlineKeyboardButton("❌ Batal", callback_data="bc_cancel"),
    )
    bot.send_message(
        message.chat.id,
        f"📢 Pratinjau broadcast (akan dikirim ke {total} member):\n\n{text}",
        reply_markup=markup,
    )


def run_broadcast(chat_id, text):
    rows = db_exec("SELECT user_id FROM users WHERE blocked=0", fetch="all")
    sent = failed = 0
    for r in rows:
        try:
            bot.send_message(r["user_id"], text)
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.05)  # jaga di bawah batas rate Telegram
    bot.send_message(chat_id, f"📢 <b>Broadcast selesai</b>\n\nTerkirim: <b>{sent}</b>\nGagal: <b>{failed}</b>", parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data in ("bc_send", "bc_cancel"))
def process_broadcast_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Khusus admin.", show_alert=True)
        return
    text = pending_broadcasts.pop(call.from_user.id, None)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    if call.data == "bc_cancel":
        bot.answer_callback_query(call.id, "Dibatalkan.")
        bot.send_message(call.message.chat.id, "❌ Broadcast dibatalkan.")
        return
    if not text:
        bot.answer_callback_query(call.id, "Tidak ada broadcast tertunda.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Mengirim...")
    bot.send_message(call.message.chat.id, "⏳ Broadcast sedang dikirim...")
    threading.Thread(target=run_broadcast, args=(call.message.chat.id, text), daemon=True).start()


if __name__ == "__main__":
    check_config()
    init_db()
    print("Bot QRIS Dinamis KlikQRIS berjalan...")
    threading.Thread(target=payment_monitor, daemon=True).start()
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
