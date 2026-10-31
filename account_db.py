<<<<<<< HEAD
# -*- coding: utf-8 -*-
"""Class-Chat 账号存储层（SQLite）。

账号数据保存在 accounts.db 的 accounts 表中；
首次启动时会自动把旧的 Account.json 迁移进来（已迁移过的记录不会重复写入）。

密码使用 hashlib.pbkdf2_hmac('sha256') 加盐派生后存储，
格式为：pbkdf2_sha256$<迭代次数>$<salt_hex>$<hash_hex>。
从旧版本迁移来的明文密码会在首次启动时批量升级为哈希。
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
from contextlib import closing

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "accounts.db")
LEGACY_JSON = os.path.join(BASE_DIR, "Account.json")

# ========== 密码哈希参数 ==========
HASH_ALGO = "sha256"
ITERATIONS = 200000
SALT_BYTES = 16
PREFIX = "pbkdf2_sha256"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name       TEXT    NOT NULL UNIQUE,
    username           TEXT    NOT NULL UNIQUE,
    password           TEXT    NOT NULL,
    special_privileges TEXT    NOT NULL DEFAULT 'empty',
    is_banned          INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
)
"""


# ========== 连接 ==========
def get_conn():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ========== 密码哈希 ==========
def hash_password(password, salt=None, iterations=ITERATIONS):
    """生成 `pbkdf2_sha256$<迭代次数>$<salt_hex>$<hash_hex>` 形式的密文。"""
    if salt is None:
        salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(HASH_ALGO, password.encode("utf-8"), salt, iterations)
    return f"{PREFIX}${iterations}${salt.hex()}${dk.hex()}"


def is_hashed(stored):
    return isinstance(stored, str) and stored.startswith(PREFIX + "$")


def _verify_stored(stored, password):
    """校验明文密码：兼容旧库里的明文，新密文走 pbkdf2 + 常量时间比较。"""
    if not isinstance(stored, str):
        return False

    if is_hashed(stored):
        try:
            _, iter_str, salt_hex, hash_hex = stored.split("$")
            dk = hashlib.pbkdf2_hmac(
                HASH_ALGO,
                password.encode("utf-8"),
                bytes.fromhex(salt_hex),
                int(iter_str),
            )
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(dk.hex(), hash_hex)

    # 旧版本遗留的明文密码
    return hmac.compare_digest(stored, password)


def verify_password(acc, password):
    """校验账号密码。acc 为账号 dict 或账号名。

    命中旧明文密码且校验通过时，会顺带把该账号升级为哈希存储。
    """
    if not isinstance(acc, dict):
        acc = get_account_by_name(acc)
    if acc is None:
        return False

    stored = acc["password"]
    if not _verify_stored(stored, password):
        return False

    if not is_hashed(stored):
        upgrade_legacy_password(acc["account_name"], password, stored)
    return True


def upgrade_legacy_password(account_name, password, old_stored):
    """把单条明文密码升级为哈希（带旧值条件，避免并发重复升级）。"""
    with closing(get_conn()) as conn:
        cur = conn.execute(
            "UPDATE accounts SET password = ? WHERE account_name = ? AND password = ?",
            (hash_password(password), account_name, old_stored),
        )
        conn.commit()
        return cur.rowcount > 0


def upgrade_all_legacy_passwords():
    """批量把库里残留的明文密码升级为哈希，返回升级条数。"""
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT account_name, password FROM accounts"
            " WHERE password IS NOT NULL AND password NOT LIKE ?",
            (PREFIX + "$%",),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE accounts SET password = ? WHERE account_name = ? AND password = ?",
                (hash_password(row["password"]), row["account_name"], row["password"]),
            )
        conn.commit()
    if rows:
        print(f"[DB] 已将 {len(rows)} 个明文密码升级为 PBKDF2 哈希")
    return len(rows)


# ========== 初始化 / 迁移 ==========
def init_db():
    """建表并迁移旧数据，返回本次迁移的账号数量。"""
    with closing(get_conn()) as conn:
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()
        count = _migrate_from_json(conn)
    upgrade_all_legacy_passwords()
    return count


def _migrate_from_json(conn):
    if not os.path.exists(LEGACY_JSON):
        return 0

    try:
        with open(LEGACY_JSON, "r", encoding="utf-8") as f:
            accounts = json.load(f)
    except Exception as e:
        print(f"[WARN] 读取 Account.json 失败，跳过迁移: {e}")
        return 0

    if not isinstance(accounts, list):
        return 0

    count = 0
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        account_name = str(acc.get("account_name", "")).strip()
        username = str(acc.get("username", "")).strip()
        if not account_name or not username:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO accounts"
            " (account_name, username, password, special_privileges) VALUES (?, ?, ?, ?)",
            (
                account_name,
                username,
                str(acc.get("password", "")),
                str(acc.get("special_privileges") or "empty"),
            ),
        )
        count += cur.rowcount

    conn.commit()
    if count:
        print(f"[DB] 已从 Account.json 迁移 {count} 个账号")
    return count


def count_accounts():
    with closing(get_conn()) as conn:
        return conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]


# ========== 查询 ==========
def _row_to_account(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "account_name": row["account_name"],
        "username": row["username"],
        "password": row["password"],
        "special_privileges": row["special_privileges"],
        "is_banned": bool(row["is_banned"]),
        "created_at": row["created_at"],
    }


def get_account_by_name(account_name):
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE account_name = ?", (account_name,)
        ).fetchone()
        return _row_to_account(row)


def account_exists(account_name):
    return get_account_by_name(account_name) is not None


def username_exists(username):
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT 1 FROM accounts WHERE username = ?", (username,)
        ).fetchone()
        return row is not None


def get_account(account_name, password):
    """按账号名+密码取账号，密码错误返回 None（内部走 PBKDF2 校验）。"""
    acc = get_account_by_name(account_name)
    if acc is not None and verify_password(acc, password):
        return acc
    return None


# ========== 写入 ==========
def create_account(account_name, username, password, privileges="empty"):
    """新建账号，密码以 PBKDF2 哈希写入。UNIQUE 约束保证并发注册也不会写入重复数据。

    返回 (ok: bool, message: str)
    """
    try:
        with closing(get_conn()) as conn:
            conn.execute(
                "INSERT INTO accounts (account_name, username, password, special_privileges)"
                " VALUES (?, ?, ?, ?)",
                (account_name, username, hash_password(password), privileges or "empty"),
            )
            conn.commit()
        return True, "ok"
    except sqlite3.IntegrityError:
        return False, "账号名或昵称已被占用"
    except sqlite3.Error as e:
        return False, f"数据库写入失败: {e}"


def set_ban(account_name, banned):
    """设置封禁状态。返回 bool：True 表示账号存在且已更新，False 表示账号不存在。"""
    with closing(get_conn()) as conn:
        cur = conn.execute(
            "UPDATE accounts SET is_banned = ? WHERE account_name = ?",
            (1 if banned else 0, account_name),
        )
        conn.commit()
        return cur.rowcount > 0


def set_password(account_name, new_password):
    """重设密码（存为哈希）。返回 bool：True 表示账号存在且已更新。"""
    if not new_password:
        return False
    with closing(get_conn()) as conn:
        cur = conn.execute(
            "UPDATE accounts SET password = ? WHERE account_name = ?",
            (hash_password(new_password), account_name),
        )
        conn.commit()
        return cur.rowcount > 0


if __name__ == "__main__":
    n = init_db()

    # 命令行：python account_db.py set-password <账号名> <新密码>
    if len(sys.argv) >= 4 and sys.argv[1] == "set-password":
        target, new_pwd = sys.argv[2], sys.argv[3]
        if not account_exists(target):
            print(f"[ERROR] 账号不存在: {target}")
            sys.exit(1)
        if set_password(target, new_pwd):
            print(f"[DB] 已重设账号 {target} 的密码为: {new_pwd}")
        else:
            print(f"[ERROR] 重设失败: {target}")
            sys.exit(1)
        sys.exit(0)

    print(f"[DB] 数据库文件: {DB_FILE}")
    print(f"[DB] 迁移账号数: {n}，当前账号总数: {count_accounts()}")
=======
# -*- coding: utf-8 -*-
"""Class-Chat 账号存储层（SQLite）。

账号数据保存在 accounts.db 的 accounts 表中；
首次启动时会自动把旧的 Account.json 迁移进来（已迁移过的记录不会重复写入）。

密码使用 hashlib.pbkdf2_hmac('sha256') 加盐派生后存储，
格式为：pbkdf2_sha256$<迭代次数>$<salt_hex>$<hash_hex>。
从旧版本迁移来的明文密码会在首次启动时批量升级为哈希。
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
from contextlib import closing

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "accounts.db")
LEGACY_JSON = os.path.join(BASE_DIR, "Account.json")

# ========== 密码哈希参数 ==========
HASH_ALGO = "sha256"
ITERATIONS = 200000
SALT_BYTES = 16
PREFIX = "pbkdf2_sha256"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name       TEXT    NOT NULL UNIQUE,
    username           TEXT    NOT NULL UNIQUE,
    password           TEXT    NOT NULL,
    special_privileges TEXT    NOT NULL DEFAULT 'empty',
    is_banned          INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
)
"""


# ========== 连接 ==========
def get_conn():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ========== 密码哈希 ==========
def hash_password(password, salt=None, iterations=ITERATIONS):
    """生成 `pbkdf2_sha256$<迭代次数>$<salt_hex>$<hash_hex>` 形式的密文。"""
    if salt is None:
        salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(HASH_ALGO, password.encode("utf-8"), salt, iterations)
    return f"{PREFIX}${iterations}${salt.hex()}${dk.hex()}"


def is_hashed(stored):
    return isinstance(stored, str) and stored.startswith(PREFIX + "$")


def _verify_stored(stored, password):
    """校验明文密码：兼容旧库里的明文，新密文走 pbkdf2 + 常量时间比较。"""
    if not isinstance(stored, str):
        return False

    if is_hashed(stored):
        try:
            _, iter_str, salt_hex, hash_hex = stored.split("$")
            dk = hashlib.pbkdf2_hmac(
                HASH_ALGO,
                password.encode("utf-8"),
                bytes.fromhex(salt_hex),
                int(iter_str),
            )
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(dk.hex(), hash_hex)

    # 旧版本遗留的明文密码
    return hmac.compare_digest(stored, password)


def verify_password(acc, password):
    """校验账号密码。acc 为账号 dict 或账号名。

    命中旧明文密码且校验通过时，会顺带把该账号升级为哈希存储。
    """
    if not isinstance(acc, dict):
        acc = get_account_by_name(acc)
    if acc is None:
        return False

    stored = acc["password"]
    if not _verify_stored(stored, password):
        return False

    if not is_hashed(stored):
        upgrade_legacy_password(acc["account_name"], password, stored)
    return True


def upgrade_legacy_password(account_name, password, old_stored):
    """把单条明文密码升级为哈希（带旧值条件，避免并发重复升级）。"""
    with closing(get_conn()) as conn:
        cur = conn.execute(
            "UPDATE accounts SET password = ? WHERE account_name = ? AND password = ?",
            (hash_password(password), account_name, old_stored),
        )
        conn.commit()
        return cur.rowcount > 0


def upgrade_all_legacy_passwords():
    """批量把库里残留的明文密码升级为哈希，返回升级条数。"""
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT account_name, password FROM accounts"
            " WHERE password IS NOT NULL AND password NOT LIKE ?",
            (PREFIX + "$%",),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE accounts SET password = ? WHERE account_name = ? AND password = ?",
                (hash_password(row["password"]), row["account_name"], row["password"]),
            )
        conn.commit()
    if rows:
        print(f"[DB] 已将 {len(rows)} 个明文密码升级为 PBKDF2 哈希")
    return len(rows)


# ========== 初始化 / 迁移 ==========
def init_db():
    """建表并迁移旧数据，返回本次迁移的账号数量。"""
    with closing(get_conn()) as conn:
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()
        count = _migrate_from_json(conn)
    upgrade_all_legacy_passwords()
    return count


def _migrate_from_json(conn):
    if not os.path.exists(LEGACY_JSON):
        return 0

    try:
        with open(LEGACY_JSON, "r", encoding="utf-8") as f:
            accounts = json.load(f)
    except Exception as e:
        print(f"[WARN] 读取 Account.json 失败，跳过迁移: {e}")
        return 0

    if not isinstance(accounts, list):
        return 0

    count = 0
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        account_name = str(acc.get("account_name", "")).strip()
        username = str(acc.get("username", "")).strip()
        if not account_name or not username:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO accounts"
            " (account_name, username, password, special_privileges) VALUES (?, ?, ?, ?)",
            (
                account_name,
                username,
                str(acc.get("password", "")),
                str(acc.get("special_privileges") or "empty"),
            ),
        )
        count += cur.rowcount

    conn.commit()
    if count:
        print(f"[DB] 已从 Account.json 迁移 {count} 个账号")
    return count


def count_accounts():
    with closing(get_conn()) as conn:
        return conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]


# ========== 查询 ==========
def _row_to_account(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "account_name": row["account_name"],
        "username": row["username"],
        "password": row["password"],
        "special_privileges": row["special_privileges"],
        "is_banned": bool(row["is_banned"]),
        "created_at": row["created_at"],
    }


def get_account_by_name(account_name):
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE account_name = ?", (account_name,)
        ).fetchone()
        return _row_to_account(row)


def account_exists(account_name):
    return get_account_by_name(account_name) is not None


def username_exists(username):
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT 1 FROM accounts WHERE username = ?", (username,)
        ).fetchone()
        return row is not None


def get_account(account_name, password):
    """按账号名+密码取账号，密码错误返回 None（内部走 PBKDF2 校验）。"""
    acc = get_account_by_name(account_name)
    if acc is not None and verify_password(acc, password):
        return acc
    return None


# ========== 写入 ==========
def create_account(account_name, username, password, privileges="empty"):
    """新建账号，密码以 PBKDF2 哈希写入。UNIQUE 约束保证并发注册也不会写入重复数据。

    返回 (ok: bool, message: str)
    """
    try:
        with closing(get_conn()) as conn:
            conn.execute(
                "INSERT INTO accounts (account_name, username, password, special_privileges)"
                " VALUES (?, ?, ?, ?)",
                (account_name, username, hash_password(password), privileges or "empty"),
            )
            conn.commit()
        return True, "ok"
    except sqlite3.IntegrityError:
        return False, "账号名或昵称已被占用"
    except sqlite3.Error as e:
        return False, f"数据库写入失败: {e}"


def set_ban(account_name, banned):
    """设置封禁状态。返回 bool：True 表示账号存在且已更新，False 表示账号不存在。"""
    with closing(get_conn()) as conn:
        cur = conn.execute(
            "UPDATE accounts SET is_banned = ? WHERE account_name = ?",
            (1 if banned else 0, account_name),
        )
        conn.commit()
        return cur.rowcount > 0


def set_password(account_name, new_password):
    """重设密码（存为哈希）。返回 bool：True 表示账号存在且已更新。"""
    if not new_password:
        return False
    with closing(get_conn()) as conn:
        cur = conn.execute(
            "UPDATE accounts SET password = ? WHERE account_name = ?",
            (hash_password(new_password), account_name),
        )
        conn.commit()
        return cur.rowcount > 0


if __name__ == "__main__":
    n = init_db()

    # 命令行：python account_db.py set-password <账号名> <新密码>
    if len(sys.argv) >= 4 and sys.argv[1] == "set-password":
        target, new_pwd = sys.argv[2], sys.argv[3]
        if not account_exists(target):
            print(f"[ERROR] 账号不存在: {target}")
            sys.exit(1)
        if set_password(target, new_pwd):
            print(f"[DB] 已重设账号 {target} 的密码为: {new_pwd}")
        else:
            print(f"[ERROR] 重设失败: {target}")
            sys.exit(1)
        sys.exit(0)

    print(f"[DB] 数据库文件: {DB_FILE}")
    print(f"[DB] 迁移账号数: {n}，当前账号总数: {count_accounts()}")
>>>>>>> 6f2b46aeeca4f7905d570907d223211bb66ec17b
