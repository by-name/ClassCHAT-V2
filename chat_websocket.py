# chat_websocket.py
import asyncio
import websockets
import json
import os
import re
import sys
import uuid
import base64
from datetime import datetime

# 本项目用的是 embeddable 版 Python，sys.path 不含脚本所在目录，
# 需要手动加入，否则同目录的 account_db 无法导入。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import account_db

# ========== 配置 ==========
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DEFAULT_PORT = 8765


def load_config():
    """读取 config.json（与前端共用同一份，端口不再两处硬编码）。"""
    cfg = {"ws_port": DEFAULT_PORT}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg.update(loaded)
    except FileNotFoundError:
        print(f"[WARN] 未找到 config.json，使用默认端口 {DEFAULT_PORT}")
    except Exception as e:
        print(f"[WARN] 读取 config.json 失败({e})，使用默认端口 {DEFAULT_PORT}")

    try:
        port = int(cfg.get("ws_port", DEFAULT_PORT))
        if not 1 <= port <= 65535:
            raise ValueError("端口超出范围")
    except (TypeError, ValueError) as e:
        print(f"[WARN] config.json 中 ws_port 非法({e})，使用默认端口 {DEFAULT_PORT}")
        port = DEFAULT_PORT
    cfg["ws_port"] = port
    return cfg


CONFIG = load_config()

HOST = "0.0.0.0"
PORT = CONFIG["ws_port"]
DB_FILE = account_db.DB_FILE
FILE_DIR = os.path.join(BASE_DIR, "file")

# ========== 全局状态 ==========
CLIENTS = {}
CHAT_HISTORY = []
MAX_HISTORY = 500

# ========== 上传安全配置 ==========
MAX_UPLOAD_SIZE = 10 * 1024 * 1024                       # 单文件最大 10MB
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "pdf", "txt", "md", "zip"}
# 文件头魔数（用于扩展名一致性轻量校验）；txt/md 无魔数，跳过
MAGIC_SIGNATURES = {
    "jpg":  [b"\xff\xd8\xff"],
    "jpeg": [b"\xff\xd8\xff"],
    "png":  [b"\x89PNG\r\n\x1a\n"],
    "gif":  [b"GIF87a", b"GIF89a"],
    "pdf":  [b"%PDF-"],
    "zip":  [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"],
}
_UNSAFE_NAME_RE = re.compile(r"[^\w.\-]+")   # \w 含中文，保留可读文件名

# ========== 工具函数 ==========
def parse_privileges(priv_str):
    if not priv_str or priv_str == "empty":
        return []
    return [p.strip() for p in priv_str.split(",")]

def get_online_users():
    return [
        {
            "account_name": info["account_name"],
            "username": info["username"]
        }
        for info in CLIENTS.values()
    ]

# ========== 引用回复 ==========
QUOTE_PREVIEW_LEN = 60          # 引用块里原文摘要的最大长度

def build_quote(quote_id):
    """按 quote_id 在聊天历史里查原消息，生成引用块。

    只认服务端自己生成的 id，引用内容一律取自历史记录，不接受客户端伪造的文本。
    查不到（已被清空/超出 500 条上限）时返回 None，该条消息就不带引用。
    """
    if not isinstance(quote_id, str) or not quote_id:
        return None

    for msg in CHAT_HISTORY:
        if msg.get("type") == "chat" and msg.get("id") == quote_id:
            text = msg.get("message", "")
            preview = text if len(text) <= QUOTE_PREVIEW_LEN else text[:QUOTE_PREVIEW_LEN] + "…"
            return {
                "id": msg.get("id"),
                "username": msg.get("username", ""),
                "message": preview,
                "time": msg.get("time", ""),
            }
    return None


# ========== @提及 ==========
MENTION_RE = re.compile(r"@([^\s@，。,\.!！？?；;]{1,32})")

def find_mention_names(content):
    """提取消息里 @ 后面的名字（去重、保持出现顺序）。"""
    names, seen = [], set()
    for match in MENTION_RE.finditer(content or ""):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def resolve_mentions(names):
    """把 @ 后的名字解析成在线用户（昵称或账号名均可，忽略大小写）。"""
    if not names:
        return []

    online = {}
    for info in CLIENTS.values():
        online.setdefault(info["username"].lower(), info)
        online.setdefault(info["account_name"].lower(), info)

    result, seen = [], set()
    for name in names:
        info = online.get(name.lower())
        if info and info["account_name"] not in seen:
            seen.add(info["account_name"])
            result.append({
                "username": info["username"],
                "account_name": info["account_name"],
            })
    return result


# ========== 上传校验 ==========
def get_file_ext(filename):
    return os.path.splitext(str(filename or ""))[1].lstrip(".").lower()


def sanitize_filename(filename):
    """净化文件名：去目录部分（../ / \\）、去控制字符、只保留安全字符。"""
    name = str(filename or "").replace("\\", "/")
    name = os.path.basename(name)                       # 丢弃任何路径成分
    name = "".join(ch for ch in name if ch.isprintable())  # 去控制字符
    name = _UNSAFE_NAME_RE.sub("_", name)               # 特殊字符转下划线
    name = name.strip(".").strip()[:100]                # 去首尾点/空格，限长
    return name


def build_upload_path(filename):
    """在 FILE_DIR 下生成安全且不覆盖已有文件的落盘路径。"""
    safe = sanitize_filename(filename) or "upload"
    stem, ext = os.path.splitext(safe)
    candidate = safe
    index = 1
    while os.path.exists(os.path.join(FILE_DIR, candidate)):
        candidate = f"{stem}_{index}{ext}"
        index += 1
    return os.path.join(FILE_DIR, candidate)


def is_path_inside_file_dir(filepath):
    """realpath 校验：确保最终落盘位置仍在 file/ 目录内。"""
    return os.path.dirname(os.path.realpath(filepath)) == os.path.realpath(FILE_DIR)


def check_magic(ext, content):
    """文件头魔数与扩展名一致性校验（纯文本类直接放行）。"""
    if ext in ("txt", "md"):
        return True
    if ext == "webp":
        return content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    for sig in MAGIC_SIGNATURES.get(ext, []):
        if content.startswith(sig):
            return True
    return False


def decode_upload_data(raw):
    """解析前端传来的 data URL 或纯 base64，返回 (bytes, error_message)。"""
    if not isinstance(raw, str) or not raw.strip():
        return None, "缺少文件数据"

    payload = raw.strip()
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]              # 去掉 data URL 前缀

    # 解码前先按编码后长度粗筛，避免为超大载荷浪费内存
    if len(payload) * 3 // 4 > MAX_UPLOAD_SIZE + 1024:
        return None, "文件大小超过 10MB 限制"

    try:
        return base64.b64decode(re.sub(r"\s+", "", payload)), None
    except Exception as e:
        return None, f"文件数据不是合法的 base64: {e}"

# ========== 广播 ==========
async def broadcast(message: dict):
    if not CLIENTS:
        return
    payload = json.dumps(message, ensure_ascii=False)
    await asyncio.wait([ws.send(payload) for ws in CLIENTS.keys()])

async def send_to(ws, message: dict):
    try:
        await ws.send(json.dumps(message, ensure_ascii=False))
    except Exception as e:
        # 连接已断开等情况：只记日志，避免把异常抛到消息处理流程里
        print(f"[WARN] send failed: {e}")

# ========== 消息处理 ==========
async def handle_message(ws, raw_data):
    try:
        data = json.loads(raw_data)
    except json.JSONDecodeError:
        await send_to(ws, {"type": "error", "message": "无效的JSON格式"})
        return

    msg_type = data.get("type")

    # ---------- 注册 ----------
    if msg_type == "register":
        new_account = data.get("account_name", "").strip()
        new_password = data.get("password", "").strip()
        new_username = data.get("username", "").strip()

        if not new_account or not new_password or not new_username:
            await send_to(ws, {"type": "register_failed", "message": "请填写完整信息"})
            return

        if len(new_account) < 3 or len(new_account) > 20:
            await send_to(ws, {"type": "register_failed", "message": "账号名长度需在3-20之间"})
            return

        if len(new_password) < 3:
            await send_to(ws, {"type": "register_failed", "message": "密码长度至少3位"})
            return

        # 存在性校验（UNIQUE 约束作为并发兜底）
        if account_db.account_exists(new_account):
            await send_to(ws, {"type": "register_failed", "message": "账号名已存在"})
            return

        if account_db.username_exists(new_username):
            await send_to(ws, {"type": "register_failed", "message": "昵称已被使用"})
            return

        ok, err = account_db.create_account(new_account, new_username, new_password)
        if ok:
            await send_to(ws, {"type": "register_success", "message": "注册成功，请登录"})
            print(f"[REGISTER] 新用户: {new_username} ({new_account})")
        else:
            await send_to(ws, {"type": "register_failed", "message": err})

    # ---------- 登录 ----------
    elif msg_type == "login":
        account_name = data.get("account_name", "").strip()
        password = data.get("password", "").strip()

        acc = account_db.get_account_by_name(account_name)
        if not acc:
            await send_to(ws, {"type": "login_failed", "message": "账号或密码错误"})
            return

        if acc["is_banned"]:
            await send_to(ws, {"type": "login_failed", "message": "该账号已被封禁"})
            return

        if not account_db.verify_password(acc, password):
            await send_to(ws, {"type": "login_failed", "message": "账号或密码错误"})
            return

        # 踢掉旧登录
        for old_ws, info in list(CLIENTS.items()):
            if info["account_name"] == account_name:
                await send_to(old_ws, {"type": "kicked", "message": "账号在其他地方登录"})
                del CLIENTS[old_ws]

        privileges = parse_privileges(acc.get("special_privileges", "empty"))
        CLIENTS[ws] = {
            "account_name": acc["account_name"],
            "username": acc["username"],
            "privileges": privileges
        }

        await send_to(ws, {
            "type": "login_success",
            "username": acc["username"],
            "account_name": acc["account_name"],
            "privileges": privileges,
            "is_admin": "Account ban" in privileges,
            "history": CHAT_HISTORY[-50:]
        })

        await broadcast({
            "type": "system",
            "message": f"{acc['username']} 加入了聊天室"
        })
        await broadcast({
            "type": "user_list",
            "users": get_online_users()
        })
        print(f"[LOGIN] {acc['username']} ({account_name})")

    # ---------- 聊天 ----------
    elif msg_type == "chat":
        if ws not in CLIENTS:
            return

        user_info = CLIENTS[ws]
        content = data.get("message", "").strip()
        if not content:
            return

        msg_obj = {
            "type": "chat",
            "id": uuid.uuid4().hex[:12],
            "account_name": user_info["account_name"],
            "username": user_info["username"],
            "message": content,
            "mentions": resolve_mentions(find_mention_names(content)),
            "time": datetime.now().strftime("%H:%M:%S")
        }

        quote = build_quote(data.get("quote_id"))
        if quote:
            msg_obj["quote"] = quote

        CHAT_HISTORY.append(msg_obj)
        if len(CHAT_HISTORY) > MAX_HISTORY:
            CHAT_HISTORY.pop(0)

        await broadcast(msg_obj)

    # ---------- 公告 ----------
    elif msg_type == "announcement":
        if ws not in CLIENTS:
            return
        user_info = CLIENTS[ws]
        if "make_an_announcement" not in user_info["privileges"]:
            await send_to(ws, {"type": "error", "message": "无权限发布公告"})
            return

        content = data.get("message", "").strip()
        await broadcast({
            "type": "announcement",
            "message": content,
            "from": user_info["username"],
            "time": datetime.now().strftime("%H:%M:%S")
        })

    # ---------- 封禁 ----------
    elif msg_type == "ban_account":
        if ws not in CLIENTS:
            return
        user_info = CLIENTS[ws]
        if "Account ban" not in user_info["privileges"]:
            await send_to(ws, {"type": "error", "message": "无权限封禁账号"})
            return

        target = data.get("target_account", "").strip()
        if not target:
            await send_to(ws, {"type": "error", "message": "请输入要封禁的账号名"})
            return

        if target == user_info["account_name"]:
            await send_to(ws, {"type": "error", "message": "不能封禁自己"})
            return

        # 存在性校验：账号必须真实存在才能封禁
        target_acc = account_db.get_account_by_name(target)
        if not target_acc:
            await send_to(ws, {"type": "error", "message": f"账号不存在: {target}"})
            return

        if target_acc["is_banned"]:
            await send_to(ws, {"type": "error", "message": "该账号已被封禁"})
            return

        account_db.set_ban(target, True)
        for client_ws, info in list(CLIENTS.items()):
            if info["account_name"] == target:
                await send_to(client_ws, {"type": "kicked", "message": "你已被管理员封禁"})
                del CLIENTS[client_ws]

        await send_to(ws, {"type": "success", "message": f"已封禁账号: {target}"})
        await broadcast({
            "type": "system",
            "message": f"账号 {target} 已被管理员封禁"
        })

    # ---------- 解封 ----------
    elif msg_type == "unban_account":
        if ws not in CLIENTS:
            return
        user_info = CLIENTS[ws]
        if "Account ban" not in user_info["privileges"]:
            await send_to(ws, {"type": "error", "message": "无权限解封账号"})
            return

        target = data.get("target_account", "").strip()
        if not target:
            await send_to(ws, {"type": "error", "message": "请输入要解封的账号名"})
            return

        target_acc = account_db.get_account_by_name(target)
        if not target_acc:
            await send_to(ws, {"type": "error", "message": f"账号不存在: {target}"})
            return

        if not target_acc["is_banned"]:
            await send_to(ws, {"type": "error", "message": "该账号未被封禁"})
            return

        account_db.set_ban(target, False)
        await send_to(ws, {"type": "success", "message": f"已解封账号: {target}"})
        await broadcast({
            "type": "system",
            "message": f"账号 {target} 已被管理员解封"
        })

    # ---------- 清空聊天记录 ----------
    elif msg_type == "clear_chat_history":
        if ws not in CLIENTS:
            return
        user_info = CLIENTS[ws]
        if "delete_chat_history" not in user_info["privileges"]:
            await send_to(ws, {"type": "error", "message": "无权限清空聊天记录"})
            return

        CHAT_HISTORY.clear()
        await broadcast({
            "type": "system",
            "message": "聊天记录已被管理员清空"
        })

    # ---------- 文件上传 ----------
    elif msg_type == "file_upload":
        if ws not in CLIENTS:
            return

        filename = data.get("filename", "unknown")
        file_data = data.get("data") or data.get("filedata")

        os.makedirs(FILE_DIR, exist_ok=True)

        # 校验 1：扩展名白名单
        ext = get_file_ext(filename)
        if ext not in ALLOWED_EXTENSIONS:
            await send_to(ws, {
                "type": "upload_failed",
                "message": f"不支持的文件类型: .{ext or '未知'}，仅允许 {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            })
            return

        # 校验 2：base64 解析
        content, err = decode_upload_data(file_data)
        if err:
            await send_to(ws, {"type": "upload_failed", "message": err})
            return

        # 校验 3：空文件 / 大小限制
        if not content:
            await send_to(ws, {"type": "upload_failed", "message": "文件为空"})
            return

        if len(content) > MAX_UPLOAD_SIZE:
            await send_to(ws, {
                "type": "upload_failed",
                "message": f"文件大小超过 10MB 限制（{len(content) / 1024 / 1024:.2f}MB）"
            })
            return

        # 校验 4：文件头魔数与扩展名一致性
        if not check_magic(ext, content):
            await send_to(ws, {
                "type": "upload_failed",
                "message": f"文件内容与扩展名 .{ext} 不符，已拒绝"
            })
            return

        # 校验 5：文件名净化 + 目录逃逸防护
        filepath = build_upload_path(filename)
        if not is_path_inside_file_dir(filepath):
            await send_to(ws, {"type": "upload_failed", "message": "非法的文件存储路径"})
            return

        try:
            with open(filepath, "wb") as f:
                f.write(content)
            await send_to(ws, {
                "type": "success",
                "message": f"文件 {os.path.basename(filepath)} 上传成功"
            })
        except Exception as e:
            print(f"[ERROR] 文件保存失败: {e}")
            await send_to(ws, {"type": "error", "message": f"文件保存失败: {e}"})

    # ---------- 心跳 ----------
    elif msg_type == "ping":
        await send_to(ws, {"type": "pong"})

    else:
        await send_to(ws, {"type": "error", "message": f"未知消息类型: {msg_type}"})

# ========== WebSocket ==========
async def chat_handler(websocket):
    try:
        async for message in websocket:
            await handle_message(websocket, message)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if websocket in CLIENTS:
            user_info = CLIENTS.pop(websocket)
            await broadcast({
                "type": "system",
                "message": f"{user_info['username']} 离开了聊天室"
            })
            await broadcast({
                "type": "user_list",
                "users": get_online_users()
            })
            print(f"[LOGOUT] {user_info['username']}")

# ========== 启动 ==========
async def main():
    os.makedirs(FILE_DIR, exist_ok=True)
    account_db.init_db()
    print(f"[DB] 账号数据库: {DB_FILE}（共 {account_db.count_accounts()} 个账号）")
    async with websockets.serve(chat_handler, HOST, PORT):
        print(f"""
╔══════════════════════════════════════╗
║     Class-Chat V2 WebSocket Server      ║
║     Listening on ws://{HOST}:{PORT}        ║
║     Press Ctrl+C to stop             ║
╚══════════════════════════════════════╝
""")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
