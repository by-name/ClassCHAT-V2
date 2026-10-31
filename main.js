// main.js - Class-Chat 完整版

let ws = null;
let heartbeatTimer = null;
let reconnectTimer = null;

// ---------- @提及状态 ----------
let onlineUsers = [];                              // 在线用户，供 @ 自动补全
let currentUser = { account_name: '', username: '' };
let mentionCandidates = [];
let mentionIndex = 0;

// ---------- 引用回复状态 ----------
let pendingQuote = null;                           // 待发送的引用 {id, username, message}

// ---------- 配置（与服务端共用 config.json）----------
const DEFAULT_WS_PORT = 8765;   // 仅当 config.json 读不到时兜底
let wsConfig = { ws_port: DEFAULT_WS_PORT, ws_path: '' };
let wsCandidates = [];          // 候选连接地址：优先反代路径，失败自动回退直连端口
let wsCandidateIndex = 0;
let hasLoggedIn = false;
let myPrivileges = [];          // 服务端下发的权限列表，前端据此显示按钮

async function loadWsConfig() {
    try {
        const res = await fetch('config.json', { cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const cfg = await res.json();
        if (cfg && typeof cfg === 'object') wsConfig = Object.assign(wsConfig, cfg);
    } catch (e) {
        console.warn('[config] 读取 config.json 失败，使用默认端口 ' + DEFAULT_WS_PORT, e);
    }
}

// ---------- WebSocket ----------
function buildWsCandidates() {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const list = [];
    // 1) 配了 ws_path → 走 Nginx 反代，与页面同源（端口跟随页面）
    if (wsConfig.ws_path) {
        const port = location.port ? ':' + location.port : '';
        list.push(`${scheme}//${location.hostname}${port}${wsConfig.ws_path}`);
    }
    // 2) 兜底：直连 WebSocket 端口（页面不是由 nginx 提供时也能用）
    list.push(`${scheme}//${location.hostname}:${wsConfig.ws_port || DEFAULT_WS_PORT}`);
    return list;
}

function currentWsUrl() {
    if (!wsCandidates.length) wsCandidates = buildWsCandidates();
    return wsCandidates[Math.min(wsCandidateIndex, wsCandidates.length - 1)];
}

function clearReconnectTimer() {
    if (reconnectTimer) {
        clearInterval(reconnectTimer);
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
}

function connectWebSocket() {
    ws = new WebSocket(currentWsUrl());

    ws.onopen = () => {
        clearReconnectTimer();
        wsCandidateIndex = 0;   // 连上了就回到首选地址

        heartbeatTimer = setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'ping' }));
            }
        }, 30000);

        // 用服务端下发的 token 恢复会话，不再把明文密码放 sessionStorage
        const saved = sessionStorage.getItem('cc_account');
        const savedToken = sessionStorage.getItem('cc_token');
        if (saved && savedToken) {
            sendRaw({ type: 'login', token: savedToken });
        }
    };

    ws.onmessage = (event) => {
        try {
            handleMessage(JSON.parse(event.data));
        } catch (e) {
            console.error('解析失败:', e);
        }
    };

    ws.onclose = () => {
        clearInterval(heartbeatTimer);
        // 还没登录成功就断开：换下一个候选地址（例如 /ws 反代不通时直连端口）
        if (!hasLoggedIn && wsCandidateIndex < wsCandidates.length - 1) {
            wsCandidateIndex += 1;
            clearReconnectTimer();
            reconnectTimer = setTimeout(connectWebSocket, 300);
            return;
        }
        if (!reconnectTimer) {
            reconnectTimer = setInterval(connectWebSocket, 3000);
        }
    };

    ws.onerror = () => ws.close();
}

function sendRaw(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(obj));
    }
}

// ---------- 登录/注册切换 ----------
function toggleRegister() {
    document.getElementById('login-form').classList.toggle('hidden');
    document.getElementById('register-form').classList.toggle('hidden');
    document.getElementById('to-register').classList.toggle('hidden');
    document.getElementById('to-login').classList.toggle('hidden');
}

// ---------- 登录 ----------
function doLogin() {
    const account = document.getElementById('login-account').value.trim();
    const password = document.getElementById('login-password').value;
    if (!account || !password) {
        document.getElementById('login-error').textContent = '请输入账号和密码';
        return;
    }
    doLoginWith(account, password);
}

function doLoginWith(account, password) {
    sendRaw({ type: 'login', account_name: account, password: password });
}

function doRegister() {
    const username = document.getElementById('reg-username').value.trim();
    const account = document.getElementById('reg-account').value.trim();
    const password = document.getElementById('reg-password').value;

    if (!username || !account || !password) {
        document.getElementById('register-error').textContent = '请填写完整信息';
        return;
    }

    sendRaw({
        type: 'register',
        username: username,
        account_name: account,
        password: password
    });
}

// ---------- 聊天功能 ----------
function sendMessage() {
    hideMentionPopup();
    const input = document.getElementById('chat-input');
    const text = input.value.trim();
    if (!text) return;

    const payload = { type: 'chat', message: text };
    if (pendingQuote && pendingQuote.id) payload.quote_id = pendingQuote.id;   // 只发 id，内容由服务端回填
    sendRaw(payload);

    input.value = '';
    setPendingQuote(null);
    input.focus();
}

// ---------- 引用回复 ----------
function truncate(text, max = 60) {
    const s = String(text || '');
    return s.length > max ? s.slice(0, max) + '…' : s;
}

function setPendingQuote(quote) {
    pendingQuote = quote || null;
    const bar = document.getElementById('quote-bar');
    if (!bar) return;

    if (!pendingQuote) {
        bar.classList.add('hidden');
        bar.innerHTML = '';
        return;
    }

    bar.innerHTML = '';
    const label = document.createElement('span');
    label.className = 'quote-bar-text';
    label.textContent = `引用 ${pendingQuote.username || '某人'}：${truncate(pendingQuote.message, 40)}`;

    const close = document.createElement('button');
    close.className = 'quote-bar-close';
    close.type = 'button';
    close.textContent = '✕';
    close.title = '取消引用';
    close.onclick = () => setPendingQuote(null);

    bar.appendChild(label);
    bar.appendChild(close);
    bar.classList.remove('hidden');
    const input = document.getElementById('chat-input');
    if (input) input.focus();
}

function scrollToMessage(id) {
    if (!id) return;
    const target = document.querySelector('[data-msg-id="' + String(id).replace(/[^\w-]/g, '') + '"]');
    if (!target) return;
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    target.classList.add('msg-flash');
    setTimeout(() => target.classList.remove('msg-flash'), 1200);
}

// ---------- @提及 ----------
function getMentionQuery() {
    const input = document.getElementById('chat-input');
    const pos = input.selectionStart === null ? input.value.length : input.selectionStart;
    const text = input.value.slice(0, pos);
    const m = text.match(/@([^\s@]*)$/);
    return m ? { start: pos - m[0].length, query: m[1] } : null;
}

function showMentionPopup() {
    const popup = document.getElementById('mention-popup');
    const q = getMentionQuery();
    if (!popup || !q) {
        hideMentionPopup();
        return;
    }

    const kw = q.query.toLowerCase();
    mentionCandidates = onlineUsers.filter(u => u.username &&
        u.username.toLowerCase().includes(kw));
    if (!mentionCandidates.length) {
        hideMentionPopup();
        return;
    }
    if (mentionIndex >= mentionCandidates.length) mentionIndex = 0;

    popup.innerHTML = '';
    mentionCandidates.forEach((u, i) => {
        const item = document.createElement('div');
        item.className = 'mention-item' + (i === mentionIndex ? ' active' : '');
        item.textContent = u.username + (u.account_name ? '（' + u.account_name + '）' : '');
        item.onmousedown = (e) => {
            e.preventDefault();          // 防止输入框失焦
            mentionIndex = i;
            chooseMention();
        };
        popup.appendChild(item);
    });
    popup.classList.remove('hidden');
}

function hideMentionPopup() {
    const popup = document.getElementById('mention-popup');
    if (popup) popup.classList.add('hidden');
    mentionCandidates = [];
    mentionIndex = 0;
}

function isMentionPopupVisible() {
    const popup = document.getElementById('mention-popup');
    return !!popup && !popup.classList.contains('hidden');
}

function moveMention(step) {
    if (!mentionCandidates.length) return;
    mentionIndex = (mentionIndex + step + mentionCandidates.length) % mentionCandidates.length;
    showMentionPopup();
}

function chooseMention() {
    const item = mentionCandidates[mentionIndex];
    const q = getMentionQuery();
    if (!item || !q) return;

    const input = document.getElementById('chat-input');
    const before = input.value.slice(0, q.start);
    const after = input.value.slice(q.start + 1 + q.query.length);
    input.value = before + '@' + item.username + ' ' + after;
    hideMentionPopup();
    input.focus();
}

function handleChatInput() {
    showMentionPopup();
}

function handleChatKeydown(event) {
    if (isMentionPopupVisible()) {
        if (event.key === 'ArrowDown') { event.preventDefault(); moveMention(1); return; }
        if (event.key === 'ArrowUp') { event.preventDefault(); moveMention(-1); return; }
        if (event.key === 'Enter' || event.key === 'Tab') { event.preventDefault(); chooseMention(); return; }
        if (event.key === 'Escape') { event.preventDefault(); hideMentionPopup(); return; }
    }
    if (event.key === 'Enter') sendMessage();
}

function isMentioned(data) {
    if (!data.mentions || !data.mentions.length) return false;
    return data.mentions.some(m =>
        (currentUser.account_name && m.account_name === currentUser.account_name) ||
        (currentUser.username && m.username === currentUser.username));
}

function renderMessageText(text, mentions) {
    let html = escHtml(text);
    const names = (mentions || []).map(m => m.username).filter(Boolean);
    names.sort((a, b) => b.length - a.length);      // 长名优先，避免短名截断
    names.forEach(name => {
        const safe = escHtml(name);
        html = html.split('@' + safe).join('<span class="mention">@' + safe + '</span>');
    });
    return html;
}

function sendAnnouncement() {
    const text = document.getElementById('announce-input').value.trim();
    if (!text) return;
    sendRaw({ type: 'announcement', message: text });
    document.getElementById('announce-input').value = '';
    toggleAnnouncePanel();
}

function banAccount() {
    const target = document.getElementById('ban-input').value.trim();
    if (!target) return;
    if (confirm('确定封禁 ' + target + '？')) {
        sendRaw({ type: 'ban_account', target_account: target });
        document.getElementById('ban-input').value = '';
        toggleAdminPanel();
    }
}

function unbanAccount() {
    const target = document.getElementById('ban-input').value.trim();
    if (!target) return;
    sendRaw({ type: 'unban_account', target_account: target });
    document.getElementById('ban-input').value = '';
    toggleAdminPanel();
}

function clearChatHistory() {
    if (confirm('确定清空所有聊天记录？')) {
        sendRaw({ type: 'clear_chat_history' });
        toggleAdminPanel();
    }
}

// ---------- 文件上传 ----------
// 与服务端 chat_websocket.py 保持一致；仅作前端预检，最终以服务端校验为准
const MAX_UPLOAD_SIZE = 10 * 1024 * 1024;
const ALLOWED_UPLOAD_EXT = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'pdf', 'txt', 'md', 'zip'];
let uploadStatusTimer = null;

function getFileExt(name) {
    const i = String(name || '').lastIndexOf('.');
    return i < 0 ? '' : String(name).slice(i + 1).toLowerCase();
}

function showUploadStatus(message, isError = true) {
    const el = document.getElementById('upload-status');
    const text = (isError ? '❌ ' : '✅ ') + message;
    if (!el) {                       // 兜底：页面没有状态条时仍给提示
        alert(text);
        return;
    }
    el.textContent = text;
    el.classList.toggle('error', isError);
    el.classList.remove('hidden');
    clearTimeout(uploadStatusTimer);
    uploadStatusTimer = setTimeout(() => el.classList.add('hidden'), 6000);
}

function uploadFile() {
    const input = document.getElementById('file-input');
    const file = input.files[0];
    if (!file) return;

    // 预检 1：大小
    if (file.size > MAX_UPLOAD_SIZE) {
        showUploadStatus(`文件大小超过 10MB 限制（${(file.size / 1024 / 1024).toFixed(2)}MB）`);
        input.value = '';
        return;
    }
    if (file.size === 0) {
        showUploadStatus('文件为空');
        input.value = '';
        return;
    }

    // 预检 2：扩展名
    const ext = getFileExt(file.name);
    if (!ALLOWED_UPLOAD_EXT.includes(ext)) {
        showUploadStatus(`不支持的文件类型：${ext ? '.' + ext : '无扩展名'}`);
        input.value = '';
        return;
    }

    const reader = new FileReader();
    reader.onload = () => {
        sendRaw({
            type: 'file_upload',
            filename: file.name,
            data: reader.result
        });
    };
    reader.onerror = () => showUploadStatus('文件读取失败');
    reader.readAsDataURL(file);
    input.value = '';
}

// ---------- UI ----------
function toggleAnnouncePanel() {
    document.getElementById('announce-panel').classList.toggle('hidden');
}

function toggleAdminPanel() {
    document.getElementById('admin-panel').classList.toggle('hidden');
}

function doLogout() {
    hasLoggedIn = false;
    myPrivileges = [];
    pendingQuote = null;
    sessionStorage.clear();
    if (ws) ws.close();
    location.reload();
}

// ---------- 消息处理 ----------
function handleMessage(data) {
    switch (data.type) {
        case 'login_success':
            hasLoggedIn = true;
            currentUser = { account_name: data.account_name, username: data.username };
            myPrivileges = data.privileges || [];
            sessionStorage.setItem('cc_account', data.account_name);
            if (data.token) sessionStorage.setItem('cc_token', data.token);

            document.getElementById('login-page').classList.remove('active');
            document.getElementById('chat-page').classList.add('active');

            document.getElementById('current-user-info').textContent =
                (data.is_admin ? '👑 ' : '') + data.username;

            applyPrivileges();

            // 加载历史（先清空，避免重连时叠加一份）
            const historyBox = document.getElementById('chat-messages');
            historyBox.innerHTML = '';
            (data.history || []).forEach(msg => renderHistoryMsg(msg));
            break;

        case 'login_failed':
            document.getElementById('login-error').textContent = data.message;
            // 自动重登失败（被封/被踢/密码改了）：退回登录页，不要卡在空白界面
            if (document.getElementById('chat-page').classList.contains('active')) {
                alert('登录已失效：' + data.message);
                sessionStorage.clear();
                location.reload();
            }
            break;

        case 'kicked':
            alert('⚠️ ' + data.message);
            sessionStorage.clear();
            location.reload();
            break;

        case 'register_success':
            alert(data.message);
            toggleRegister();
            break;

        case 'register_failed':
            document.getElementById('register-error').textContent = data.message;
            break;

        case 'chat':
            appendChatMsg(data);
            break;

        case 'file':
            appendFileMsg(data);
            break;

        case 'user_list':
            updateUserList(data.users);
            break;

        case 'announcement':
            appendSystemMsg('📢 公告：' + data.message);
            break;

        case 'system':
            appendSystemMsg(data.message);
            break;

        case 'history_cleared':
            document.getElementById('chat-messages').innerHTML = '';
            break;

        case 'upload_failed':
            showUploadStatus(data.message, true);
            break;

        case 'upload_success':
            showUploadStatus(data.message, false);
            break;

        case 'success':
            showUploadStatus(data.message, false);
            break;

        case 'error':
            showUploadStatus(data.message, true);
            break;
    }
}

// 按服务端下发的权限显示按钮，避免"看得见却点不动/看不见却有权"
function applyPrivileges() {
    const can = (p) => myPrivileges.indexOf(p) !== -1;
    const show = (id, ok) => {
        const el = document.getElementById(id);
        if (el) el.classList.toggle('hidden', !ok);
    };

    show('announce-btn', can('make_an_announcement'));
    show('admin-btn', can('Account ban') || can('clear_files') || can('delete_chat_history'));
    show('ban-btn', can('Account ban'));
    show('unban-btn', can('Account ban'));
    show('clear-btn', can('delete_chat_history'));
    show('clear-files-btn', can('clear_files'));
}

function renderHistoryMsg(msg) {
    if (msg.type === 'chat') appendChatMsg(msg);
    else if (msg.type === 'file') appendFileMsg(msg);
    else if (msg.type === 'system') appendSystemMsg(msg.message);
    else if (msg.type === 'announcement') appendSystemMsg('📢 公告：' + msg.message);
}

function appendChatMsg(data) {
    const container = document.getElementById('chat-messages');
    const div = document.createElement('div');
    const mine = !!data.account_name && data.account_name === currentUser.account_name;
    div.className = (mine ? 'msg-mine' : 'msg-other') + (isMentioned(data) ? ' msg-mention' : '');
    if (data.id) div.dataset.msgId = data.id;

    div.innerHTML = `
        <span class="msg-user">${escHtml(data.username)}</span>
        <span class="msg-time">${escHtml(data.time || '')}</span>
        <button class="quote-btn" type="button" title="引用这条消息">↩</button>
        ${renderQuoteBlock(data.quote)}
        <div>${renderMessageText(data.message, data.mentions)}</div>
    `;

    const quoteBtn = div.querySelector('.quote-btn');
    if (quoteBtn) {
        quoteBtn.onclick = () => setPendingQuote({
            id: data.id,
            username: data.username,
            message: data.message
        });
    }

    const quoteBlock = div.querySelector('.msg-quote');
    if (quoteBlock) {
        quoteBlock.onclick = () => scrollToMessage(quoteBlock.dataset.quoteId);
    }

    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

// 文件消息：图片直接显示缩略图，其他类型给下载链接
function appendFileMsg(data) {
    const container = document.getElementById('chat-messages');
    const div = document.createElement('div');
    const mine = !!data.account_name && data.account_name === currentUser.account_name;
    div.className = 'msg-other file-msg-bubble' + (mine ? ' msg-mine' : '');
    if (data.id) div.dataset.msgId = data.id;

    const name = data.filename || '文件';
    const url = data.url || ('/file/' + encodeURIComponent(name));
    const size = data.size ? formatSize(data.size) : '';
    const isImage = /\.(jpg|jpeg|png|gif|webp)$/i.test(name);

    div.innerHTML = `
        <span class="msg-user">${escHtml(data.username || '')}</span>
        <span class="msg-time">${escHtml(data.time || '')}</span>
        <div class="file-msg">
            ${isImage ? `<a href="${escHtml(url)}" target="_blank" rel="noopener"><img class="file-thumb" src="${escHtml(url)}" alt="${escHtml(name)}"></a>` : ''}
            <a class="file-link" href="${escHtml(url)}" target="_blank" rel="noopener">📎 ${escHtml(name)}</a>
            ${size ? `<span class="file-size">${escHtml(size)}</span>` : ''}
        </div>
    `;

    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function formatSize(bytes) {
    if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(2) + ' MB';
    return (bytes / 1024).toFixed(1) + ' KB';
}

function renderQuoteBlock(quote) {
    if (!quote) return '';
    return `
        <div class="msg-quote" data-quote-id="${escHtml(quote.id || '')}">
            <span class="quote-user">${escHtml(quote.username || '未知用户')}</span>
            <span class="quote-time">${escHtml(quote.time || '')}</span>
            <div class="quote-text">${escHtml(quote.message || '')}</div>
        </div>
    `;
}

function appendSystemMsg(text) {
    const container = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = 'msg-system';
    div.textContent = text;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function updateUserList(users) {
    const list = document.getElementById('user-list');
    onlineUsers = users || [];
    document.getElementById('online-count').textContent = onlineUsers.length;
    list.innerHTML = '';
    onlineUsers.forEach(u => {
        const div = document.createElement('div');
        div.className = 'user-item';
        div.textContent = u.username || u.account_name;
        div.onclick = () => insertMention(u.username || u.account_name);
        list.appendChild(div);
    });
}

// 点击在线用户直接 @ 他
function insertMention(name) {
    const input = document.getElementById('chat-input');
    if (!name) return;
    const value = input.value;
    const needSpace = value.length > 0 && !/\s$/.test(value);
    input.value = value + (needSpace ? ' ' : '') + '@' + name + ' ';
    input.focus();
}

function escHtml(text) {
    const d = document.createElement('div');
    d.textContent = text;
    // 额外转义引号：这个值也可能被拼进 data-xxx="..." / href="..." 属性里
    return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ---------- 启动 ----------
loadWsConfig().then(connectWebSocket);