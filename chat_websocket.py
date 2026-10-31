# chat_websocket.py
import asyncio
import websockets
import http
import json
import os
import re
import secrets
import sys
import time
import uuid
import base64
from datetime import datetime
from urllib.parse import quote as urlquote, urlsplit

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
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")
_raw_origins = CONFIG.get("allowed_origins") or []
ALLOWED_ORIGINS = set(_raw_origins) if isinstance(_raw_origins, list) else set()

# ========== 全局状态 ==========
CLIENTS = {}        # ws -> {account_name, username, privileges, token}
SESSIONS = {}       # token -> account_name，断线重连用（避免前端保存明文密码）
CHAT_HISTORY = []
MAX_HISTORY = 500

# ========== 防护参数 ==========
# 单条 WebSocket 消息上限：10MB 文件经 base64 后约 13.3MB，再留些余量
MAX_WS_MESSAGE = 16 * 1024 * 1024
MAX_MESSAGE_LEN = 2000          # 单条聊天/公告文本长度
RATE_LIMIT_COUNT = 10           # 窗口内允许的消息数
RATE_LIMIT_WINDOW = 5.0         # 限流窗口（秒）
MIN_PASSWORD_LEN = 6            # 注册密码最小长度
LOGIN_FAIL_LIMIT = 5            # 连续登录失败次数
LOGIN_LOCK_SECONDS = 60         # 超限后锁定时长（秒）
RATE_LIMIT_STATE = {}           # ws -> [时间戳...]
LOGIN_FAILS = {}                # account_name -> (失败次数, 解锁时间戳)

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


def now_str():
    return datetime.now().strftime("%H:%M:%S")


# ========== 历史持久化 ==========
def load_history():
    """启动时从 history.json 恢复聊天记录（重启不再丢历史）。"""
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            CHAT_HISTORY[:] = data[-MAX_HISTORY:]
            if CHAT_HISTORY:
                print(f"[HISTORY] 已恢复 {len(CHAT_HISTORY)} 条历史消息")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[WARN] 读取历史记录失败: {e}")


def save_history():
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(CHAT_HISTORY, f, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] 保存历史记录失败: {e}")


def record_history(msg):
    """写入历史（含系统消息/公告/文件）并落盘。"""
    CHAT_HISTORY.append(msg)
    if len(CHAT_HISTORY) > MAX_HISTORY:
        del CHAT_HISTORY[0]
    save_history()


# ========== 防护 ==========
def check_rate_limit(ws):
    """简单的滑动窗口限流，返回 True 表示放行。"""
    now = time.time()
    stamps = RATE_LIMIT_STATE.setdefault(ws, [])
    stamps[:] = [t for t in stamps if now - t < RATE_LIMIT_WINDOW]
    if len(stamps) >= RATE_LIMIT_COUNT:
        return False
    stamps.append(now)
    return True


def cleanup_ws_state(ws):
    RATE_LIMIT_STATE.pop(ws, None)


def login_lock_left(account_name):
    """返回账号剩余锁定时长（秒），0 表示未锁定。"""
    info = LOGIN_FAILS.get(account_name)
    if not info:
        return 0
    _, until = info
    left = int(until - time.time())
    return left if left > 0 else 0


def note_login_fail(account_name):
    count, until = LOGIN_FAILS.get(account_name, (0, 0.0))
    count += 1
    if count >= LOGIN_FAIL_LIMIT:
        print(f"[WARN] 账号 {account_name} 连续登录失败 {count} 次，锁定 {LOGIN_LOCK_SECONDS} 秒")
        count, until = 0, time.time() + LOGIN_LOCK_SECONDS
    LOGIN_FAILS[account_name] = (count, until)


async def process_request(path, request_headers):
    """Origin 校验：挡住任意网页的跨站 WebSocket 连接。

    放行：无 Origin（非浏览器客户端）、同源、以及 config.json 中 allowed_origins 白名单。
    """
    origin = request_headers.get("Origin")
    if not origin:
        return None

    if origin in ALLOWED_ORIGINS:
        return None

    host = request_headers.get("Host", "")
    try:
        origin_host = urlsplit(origin).netloc
    except Exception:
        origin_host = ""

    if origin_host and origin_host == host:
        return None

    print(f"[WARN] 拒绝跨域连接: Origin={origin}, Host={host}")
    return http.HTTPStatus.FORBIDDEN, [], b"Forbidden origin\n"

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
    """净化文件名：去目录部分（../ / \\）、去控制字符、只保留安全字符。

    长度超限时先截主干、再拼回扩展名，避免把 .pdf 之类的后缀截掉。
    """
    name = str(filename or "").replace("\\", "/")
    name = os.path.basename(name)                       # 丢弃任何路径成分
    name = "".join(ch for ch in name if ch.isprintable())  # 去控制字符
    name = _UNSAFE_NAME_RE.sub("_", name)               # 特殊字符转下划线
    name = name.strip(".").strip()

    stem, ext = os.path.splitext(name)
    max_len = 100
    if len(name) > max_len:
        stem = stem[:max(1, max_len - len(ext))]        # 保留扩展名
        name = stem + ext[:max_len]
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
    # asyncio.wait(协程) 自 3.8 弃用、3.11 起报错；gather 还能取回单个连接的发送异常
    results = await asyncio.gather(
        *[ws.send(payload) for ws in list(CLIENTS)],
        return_exceptions=True,
    )
    for ws, res in zip(list(CLIENTS), results):
        if isinstance(res, Exception):
            print(f"[WARN] 广播失败: {res}")


async def system_message(text):
    """系统消息：广播并写入历史。"""
    msg = {"type": "system", "message": text, "time": now_str()}
    record_history(msg)
    await broadcast(msg)


async def kick_old_session(account_name, reason):
    """踢掉该账号的旧连接：先发 kicked，再真正关闭 socket。"""
    for old_ws, info in list(CLIENTS.items()):
        if info["account_name"] == account_name:
            SESSIONS.pop(info.get("token"), None)
            await send_to(old_ws, {"type": "kicked", "message": reason})
            try:
                await old_ws.close()
            except Exception as e:
                print(f"[WARN] 关闭旧连接失败: {e}")
            CLIENTS.pop(old_ws, None)
            cleanup_ws_state(old_ws)


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

        if len(new_account) < 3 or len(new_account) > 20 or not re.fullmatch(r"[\w.\-]+", new_account):
            await send_to(ws, {
                "type": "register_failed",
                "message": "账号名需为3-20位字母、数字、下划线、点或连字符"
            })
            return

        if len(new_password) < MIN_PASSWORD_LEN:
            await send_to(ws, {"type": "register_failed", "message": f"密码长度至少{MIN_PASSWORD_LEN}位"})
            return

        if len(new_username) < 1 or len(new_username) > 20:
            await send_to(ws, {"type": "register_failed", "message": "昵称长度需在1-20之间"})
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
        token = str(data.get("token", "")).strip()
        account_name = data.get("account_name", "").strip()
        password = data.get("password", "") or ""

        if token:
            # 断线重连：凭服务端下发的 token，不需要前端保存明文密码
            token_account = SESSIONS.get(token)
            acc = account_db.get_account_by_name(token_account) if token_account else None
            if not acc:
                await send_to(ws, {"type": "login_failed", "message": "登录状态已失效，请重新登录"})
                return
            if acc["is_banned"]:
                await send_to(ws, {"type": "login_failed", "message": "该账号已被封禁"})
                return
        else:
            if not account_name or not password:
                await send_to(ws, {"type": "login_failed", "message": "请输入账号和密码"})
                return

            locked = login_lock_left(account_name)
            if locked:
                await send_to(ws, {
                    "type": "login_failed",
                    "message": f"登录失败次数过多，请 {locked} 秒后再试"
                })
                return

            acc = account_db.get_account_by_name(account_name)
            if not acc:
                note_login_fail(account_name)
                await send_to(ws, {"type": "login_failed", "message": "账号或密码错误"})
                return

            if acc["is_banned"]:
                await send_to(ws, {"type": "login_failed", "message": "该账号已被封禁"})
                return

            if not account_db.verify_password(acc, password):
                note_login_fail(account_name)
                await send_to(ws, {"type": "login_failed", "message": "账号或密码错误"})
                return

            LOGIN_FAILS.pop(account_name, None)

        # 踢掉旧登录（同时作废其 token）
        await kick_old_session(acc["account_name"], "账号在其他地方登录")

        if not token:
            token = secrets.token_hex(16)
        SESSIONS[token] = acc["account_name"]

        privileges = parse_privileges(acc.get("special_privileges", "empty"))
        CLIENTS[ws] = {
            "account_name": acc["account_name"],
            "username": acc["username"],
            "privileges": privileges,
            "token": token,
        }

        await send_to(ws, {
            "type": "login_success",
            "username": acc["username"],
            "account_name": acc["account_name"],
            "privileges": privileges,
            "is_admin": "Account ban" in privileges,
            "token": token,
            "history": CHAT_HISTORY[-50:]
        })

        await system_message(f"{acc['username']} 加入了聊天室")
        await broadcast({
            "type": "user_list",
            "users": get_online_users()
        })
        print(f"[LOGIN] {acc['username']} ({acc['account_name']})")

    # ---------- 聊天 ----------
    elif msg_type == "chat":
        if ws not in CLIENTS:
            return

        user_info = CLIENTS[ws]
        content = data.get("message", "").strip()
        if not content:
            return

        if len(content) > MAX_MESSAGE_LEN:
            await send_to(ws, {
                "type": "error",
                "message": f"消息过长（最多 {MAX_MESSAGE_LEN} 字，当前 {len(content)} 字）"
            })
            return

        if not check_rate_limit(ws):
            await send_to(ws, {"type": "error", "message": "发送过于频繁，请稍后再试"})
            return

        msg_obj = {
            "type": "chat",
            "id": uuid.uuid4().hex[:12],
            "account_name": user_info["account_name"],
            "username": user_info["username"],
            "message": content,
            "mentions": resolve_mentions(find_mention_names(content)),
            "time": now_str()
        }

        quote = build_quote(data.get("quote_id"))
        if quote:
            msg_obj["quote"] = quote

        record_history(msg_obj)
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
        if not content:
            await send_to(ws, {"type": "error", "message": "公告内容不能为空"})
            return

        if len(content) > MAX_MESSAGE_LEN:
            await send_to(ws, {
                "type": "error",
                "message": f"公告过长（最多 {MAX_MESSAGE_LEN} 字）"
            })
            return

        ann = {
            "type": "announcement",
            "id": uuid.uuid4().hex[:12],
            "message": content,
            "from": user_info["username"],
            "time": now_str()
        }
        record_history(ann)
        await broadcast(ann)

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
        await kick_old_session(target, "你已被管理员封禁")

        await send_to(ws, {"type": "success", "message": f"已封禁账号: {target}"})
        await system_message(f"账号 {target} 已被管理员封禁")

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
        LOGIN_FAILS.pop(target, None)
        await send_to(ws, {"type": "success", "message": f"已解封账号: {target}"})
        await system_message(f"账号 {target} 已被管理员解封")

    # ---------- 清空聊天记录 ----------
    elif msg_type == "clear_chat_history":
        if ws not in CLIENTS:
            return
        user_info = CLIENTS[ws]
        if "delete_chat_history" not in user_info["privileges"]:
            await send_to(ws, {"type": "error", "message": "无权限清空聊天记录"})
            return

        CHAT_HISTORY.clear()
        save_history()
        await broadcast({"type": "history_cleared"})     # 让所有在线客户端同步清屏
        await system_message("聊天记录已被管理员清空")

    # ---------- 文件上传 ----------
    elif msg_type == "file_upload":
        if ws not in CLIENTS:
            return

        user_info = CLIENTS[ws]
        filename = data.get("filename", "unknown")
        file_data = data.get("data") or data.get("filedata")

        if not check_rate_limit(ws):
            await send_to(ws, {"type": "upload_failed", "message": "上传过于频繁，请稍后再试"})
            return

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
        except Exception as e:
            print(f"[ERROR] 文件保存失败: {e}")
            await send_to(ws, {"type": "upload_failed", "message": f"文件保存失败: {e}"})
            return

        # 广播给所有人（含上传者），聊天里能看到/下载这个文件
        saved_name = os.path.basename(filepath)
        file_msg = {
            "type": "file",
            "id": uuid.uuid4().hex[:12],
            "account_name": user_info["account_name"],
            "username": user_info["username"],
            "filename": saved_name,
            "size": len(content),
            "url": "/file/" + urlquote(saved_name),
            "time": now_str()
        }
        record_history(file_msg)
        await broadcast(file_msg)
        await send_to(ws, {
            "type": "upload_success",
            "message": f"文件 {saved_name} 上传成功"
        })

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
        cleanup_ws_state(websocket)
        if websocket in CLIENTS:
            user_info = CLIENTS.pop(websocket)
            await system_message(f"{user_info['username']} 离开了聊天室")
            await broadcast({
                "type": "user_list",
                "users": get_online_users()
            })
            print(f"[LOGOUT] {user_info['username']}")

# ========== 启动 ==========
async def main():
    os.makedirs(FILE_DIR, exist_ok=True)
    account_db.init_db()
    load_history()
    print(f"[DB] 账号数据库: {DB_FILE}（共 {account_db.count_accounts()} 个账号）")
    async with websockets.serve(
        chat_handler, HOST, PORT,
        max_size=MAX_WS_MESSAGE,          # 默认 1MiB 会让 >750KB 的上传在传输层被断开
        process_request=process_request,  # Origin 校验
    ):
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
