# -*- coding: utf-8 -*-
"""启动信息：在控制台显示管理员账号密码，方便课堂部署时登录。

密码来源（按顺序）：
  1. Account.json —— 旧版明文账号文件，迁移后仍保留明文
  2. accounts.db 中尚未升级的旧明文密码
  3. 都没有 —— 只显示管理员账号名（库里是 PBKDF2 哈希，不可逆，不能当密码打印）

可用 config.json 里的 "show_admin_password": false 关闭本提示。
"""
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LEGACY_JSON = os.path.join(BASE_DIR, "Account.json")
ADMIN_PRIV = "Account ban"

try:
    import account_db
except Exception as e:                       # 账号库不可用时仍尽量给出提示
    print(f"[WARN] 无法加载账号库: {e}")
    account_db = None


def show_enabled():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("show_admin_password") is not False
    except Exception:
        return True


def is_admin(acc):
    return ADMIN_PRIV in str(acc.get("special_privileges") or "")


def admins_from_json():
    """从 Account.json 读管理员（明文）。"""
    try:
        with open(LEGACY_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[WARN] 读取 Account.json 失败: {e}")
        return []

    if not isinstance(data, list):
        return []
    return [a for a in data if isinstance(a, dict) and is_admin(a) and a.get("password")]


def admins_from_db():
    """从 accounts.db 读管理员账号。"""
    if account_db is None:
        return []
    try:
        with account_db.get_conn() as conn:
            rows = conn.execute(
                "SELECT account_name, username, password, special_privileges FROM accounts"
            ).fetchall()
    except Exception as e:
        print(f"[WARN] 查询账号库失败: {e}")
        return []
    return [dict(r) for r in rows if is_admin(dict(r))]


def main():
    if not show_enabled():
        print("[INFO] 已按 config.json 设置隐藏管理员密码")
        return 0

    shown = set()
    db_admins = {a["account_name"]: a for a in admins_from_db()}

    # 1) Account.json 里的明文（先校验与账号库是否一致，避免显示已失效的旧密码）
    for acc in admins_from_json():
        name = acc.get("account_name", "")
        pwd = acc.get("password", "")
        db_acc = db_admins.get(name)

        if db_acc and account_db is not None and account_db.is_hashed(db_acc.get("password") or ""):
            if not account_db.verify_password(db_acc, pwd):
                print(f"[INFO] 已检测到管理员账号：{name}（{db_acc['username']}）")
                print("[WARN] Account.json 里的明文密码已失效（与账号库不一致），不予显示")
                shown.add(name)
                continue

        print(f"[INFO] 已检测到管理员账号，密码:{pwd}")
        shown.add(name)

    # 2) 库里残留的旧明文密码
    if account_db is not None:
        for acc in admins_from_db():
            pwd = acc.get("password") or ""
            if acc["account_name"] in shown or not pwd:
                continue
            if account_db.is_hashed(pwd):
                continue
            print(f"[INFO] 已检测到管理员账号，密码:{pwd}")
            shown.add(acc["account_name"])

    # 3) 只剩哈希密码：无法还原明文
    rest = [a for a in admins_from_db() if a["account_name"] not in shown]
    if rest:
        names = "、".join(f"{a['account_name']}（{a['username']}）" for a in rest)
        print(f"[INFO] 已检测到管理员账号：{names}")
        print("[INFO] 该账号密码已加密存储，且 Account.json 缺失，无法还原明文")
        print('[INFO] 如需重设：python\\python.exe account_db.py set-password <账号名> <新密码>')
    elif not shown:
        print("[INFO] 未检测到管理员账号")

    return 0


if __name__ == "__main__":
    sys.exit(main())
