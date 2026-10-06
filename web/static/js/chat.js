/* ==========================================================================
   聊天窗口：会话列表、消息渲染、发送（文本/图片链接/上传图片/上传文件/群 @）、
   点击消息弹框（引用 / 复制 / 保存 / 原始数据 / 撤回 等）、按会话清空 + 二次确认
   ========================================================================== */
(function (global) {
  'use strict';
  var U = global.Util;
  var FX = global.Effects;

  var Chat = {
    botId: '',
    bots: [],
    conversations: [],
    byKey: {},
    activeKey: '',
    activeType: '',
    activeOpenid: '',
    activeName: '',
    messages: [],
    quote: null,
    pending: null,          // { blob, name, kind: 'image'|'file' }
    lastRendered: '',
    lastMsgId: null,        // 已渲染的最后一条消息 id（用于"只追加"优化）
    appendedKey: '',        // lastMsgId 对应的会话
    pendingNew: 0,          // 用户在看历史时新到的消息条数
    convSignature: '',       // 会话列表内容签名（相同则不重绘）
    knownConvKeys: {},       // 已经出现过的会话（只有新的才播放入场动画）
    timer: null,
    stream: null,
    live: true,
    sending: false,
    groupRole: ''      // 当前群聊里机器人的身份（owner/admin/member），决定能否撤回成员消息
  };

  /* ============================ 图片地址处理 ============================ */
  function isImageAttachment(att) {
    if (!att) return false;
    var ctype = String(att.content_type || '').split(';')[0].trim().toLowerCase();
    if (ctype.indexOf('image/') === 0) return true;
    // 明确说是别的类型（file / application/pdf ...）就不是图片
    if (ctype && ctype !== 'application/octet-stream' && ctype !== 'binary/octet-stream') {
      return false;
    }
    var url = String(att.local_url || att.url || att.file_name || '');
    return /\.(png|jpe?g|gif|webp|bmp|ico|svg)(\?|$)/i.test(url);
  }

  function imgSrc(url) {
    if (!url) return '';
    if (url.indexOf('/') === 0) return U.apiUrl(url);            // 本地留存，直接访问
    if (/^https?:\/\//i.test(url)) {
      return U.apiUrl('/api/chat/image?url=' + encodeURIComponent(url));
    }
    return url;
  }

  /* 附件（非图片）的下载地址：本地留存直接下；远端文件**不要**走图片代理 */
  function fileSrc(url) {
    if (!url) return '';
    if (url.indexOf('/') === 0) return U.apiUrl(url);
    return url;
  }

  var IMAGE_URL_RE = /\.(png|jpe?g|gif|webp|bmp|ico|svg)$/i;
  var NON_IMAGE_URL_RE = new RegExp('\\.(exe|msi|dll|py|pyc|js|ts|zip|rar|7z|gz|tar|bz2|txt|md|' +
    'json|xml|csv|log|pdf|doc|docx|xls|xlsx|ppt|pptx|apk|ipa|jar|bin|iso|dmg|db|sql|cfg|ini|' +
    'bat|cmd|ps1|sh|mp3|wav|m4a|flac|ogg|amr|mp4|avi|mkv|mov|wmv|flv|webm)$', 'i');

  /* 这个地址能不能当图片渲染？
     没有扩展名的（QQ 的临时链接）交给浏览器/代理去判断；
     扩展名明显不是图片的（.exe/.py/...）坚决不渲染成 <img>，
     否则会显示"图片加载失败"这种让人误解的提示。 */
  function isProbablyImageUrl(url) {
    var text = String(url || '').split('?')[0].split('#')[0];
    if (!text) return false;
    var name = text.split('/').pop() || '';
    if (name.indexOf('.') < 0) return true;
    if (IMAGE_URL_RE.test(name)) return true;
    if (NON_IMAGE_URL_RE.test(name)) return false;
    return true;
  }

  function imgHtml(url, caption) {
    if (!url) return '';
    var src = imgSrc(url);
    return '<img src="' + U.esc(src) + '" data-original="' + U.esc(url) +
      '" alt="' + U.esc(caption || '图片') + '" loading="lazy" title="点击放大 / 右键更多操作">';
  }

  /* ---------------- 图片去重 ----------------
     同一条消息里的图片可能来自三个地方：attachments、image_url、content_images（表情标记里的图）。
     它们常常指向**同一张图**（远端地址与本地留存地址各一份），不去重就会出现"一条消息两张一样的图"。
     这里用"本地文件名 / 远端地址"作为去重键，并优先使用**本地留存**的那份。
  */
  function imageKeys(message) {
    /* 同一张图在一条消息里可能有多种表示：
         · attachments[].url        —— QQ 的临时下载地址（会过期）
         · attachments[].local_url  —— 我们下载留存后的本地地址
         · image_url / media_local  —— 上面两者的快捷字段
         · content_images[]         —— QQ 表情标记里带的图片地址
       它们指向同一张图时必须只显示一次。

       关键点：**本地文件名里不含远端的 fileid，无法互相推断**，
       所以去重键优先用"远端地址"（有 fileid 时按 fileid 归一，rkey 不同也算同一张），
       只有完全没有远端地址时才退回用本地文件名。
       local -> remote 的映射同时记录下来，展示时优先用本地留存（不会过期）。
    */
    var found = {};
    var order = [];

    function add(local, remote) {
      local = local || '';
      remote = remote || '';
      if (!local && !remote) return;
      var key = imageKey(remote || local);
      if (!key) return;
      if (!found[key]) { found[key] = {}; order.push(key); }
      if (local && !found[key].local) found[key].local = local;
      if (remote && !found[key].remote) found[key].remote = remote;
    }

    var attachments = message.attachments || [];
    // 非图片附件的地址：绝不能被当成图片显示（否则会提示"图片加载失败"）
    var blocked = {};
    for (var b = 0; b < attachments.length; b++) {
      var attItem = attachments[b];
      if (!isImageAttachment(attItem)) {
        if (attItem.local_url) blocked[attItem.local_url] = true;
        if (attItem.url) blocked[attItem.url] = true;
      }
    }
    for (var i = 0; i < attachments.length; i++) {
      var att = attachments[i];
      if (!isImageAttachment(att)) continue;
      add(att.local_url, att.url);
    }
    // image_url / media_local 有时是远程地址、有时是本地地址，两种都试
    addRemoteOrLocal(message.image_url);
    addRemoteOrLocal(message.media_local);

    function addRemoteOrLocal(url) {
      if (!url) return;
      if (blocked[url]) return;                     // 这是文件附件的地址，不是图片
      if (!isProbablyImageUrl(url)) return;         // 扩展名明显不是图片
      if (/^https?:\/\//i.test(url)) add('', url);
      else add(url, '');
    }

    var contentImages = message.content_images || [];
    for (var c = 0; c < contentImages.length; c++) add('', contentImages[c]);

    var out = [];
    for (var k = 0; k < order.length; k++) {
      var entry = found[order[k]];
      var chosen = entry.local || entry.remote;
      if (chosen && out.indexOf(chosen) < 0) out.push(chosen);
    }
    return out;
  }

  function imageKey(url) {
    var text = String(url || '').trim();
    if (!text) return '';
    // 远端 QQ 地址：rkey 每次都不同，但 fileid 指向同一个文件 → 用 fileid 当身份
    var fileId = (text.match(/[?&]fileid=([^&]+)/i) || [])[1];
    if (fileId) return 'file:' + decodeURIComponent(fileId).split('&')[0];
    // 本地留存名里若带远端 fileid（例如 FILE_A_1759300000.png）也能对上
    var mediaMatch = text.match(/\/media\/([^/?#]+)/);
    var name = mediaMatch ? decodeURIComponent(mediaMatch[1])
                          : (text.split('?')[0].split('#')[0].split('/').pop() || '');
    if (!name) return 'raw:' + text;
    var embedded = name.match(/^([0-9a-f]{16,})/i) || name.match(/^(.+?)_\d{10,}\./);
    if (embedded && embedded[1]) return 'file:' + embedded[1].toLowerCase();
    return 'name:' + name.toLowerCase();
  }

  /* ============================ 文本渲染 ============================ */
  function formatContent(text) {
    var out = U.esc(text || '');
    out = out.replace(/&lt;face(Type|Id|Id=)[^&]*?&gt;/g, '');
    // 收到的消息里的 @ 标记：统一显示成 @某人 / @全体成员
    out = out.replace(/&lt;qqbot-at-everyone\s*\/?&gt;/gi,
      '<span class="mention">@全体成员</span>');
    out = out.replace(/&lt;qqbot-at-user[^]*?&gt;/gi,
      '<span class="mention">@某人</span>');
    out = out.replace(/&lt;@!?all&gt;/gi, '<span class="mention">@全体成员</span>');
    out = out.replace(/(^|\s)@everyone\b/g, '$1<span class="mention">@全体成员</span>');
    out = out.replace(/&lt;@!?([0-9A-Za-z_.\-]{4,80})&gt;/g,
      '<span class="mention">@某人</span>');
    return out;
  }

  function displayText(message) {
    if (message.content_display !== undefined && message.content_display !== null &&
        message.content_display !== '') return message.content_display;
    return message.content || '';
  }

  function messageId(message) {
    return message.msg_id || message.id || '';
  }

  /* ============================ 会话列表 ============================ */
  function conversationSignature(conversations, groups, privates) {
    // 只把"会影响显示"的字段纳入签名：内容没变就完全不碰 DOM。
    // 这解决了"每次轮询（2 秒）整块重建列表 + 重播入场动画"造成的上下抖动。
    var parts = [Chat.activeKey, groups.length, privates.length];
    for (var i = 0; i < conversations.length; i++) {
      var conv = conversations[i];
      parts.push(conv.key, conv.name || '', conv.last_time || '',
                 conv.last_content || '', conv.unread || 0,
                 (conv.message_count || conv.count || 0), conv.named === false ? 0 : 1);
    }
    return parts.join('\u0001');
  }

  function renderConversations(list) {
    Chat.conversations = list || [];
    Chat.byKey = {};
    var groups = [], privates = [];
    for (var i = 0; i < Chat.conversations.length; i++) {
      var conv = Chat.conversations[i];
      Chat.byKey[conv.key] = conv;
      if (conv.type === 'group') groups.push(conv); else privates.push(conv);
    }
    var signature = conversationSignature(Chat.conversations, groups, privates);
    if (signature === Chat.convSignature) return;      // 数据没变 → 一个 DOM 都不动
    var known = Chat.knownConvKeys || (Chat.knownConvKeys = {});
    Chat.convSignature = signature;

    U.el('groupCount').textContent = groups.length;
    U.el('privateCount').textContent = privates.length;
    U.el('groupList').innerHTML = groups.length ? groups.map(convHtml).join('') :
      '<div class="tiny" style="padding:4px 8px">暂无群聊</div>';
    U.el('privateList').innerHTML = privates.length ? privates.map(convHtml).join('') :
      '<div class="tiny" style="padding:4px 8px">暂无私聊</div>';

    // 只有"这一轮新出现的会话"才播放入场动画，已有会话不再重播（否则每 2 秒抖一次）
    var items = U.el('convScroll').querySelectorAll('.conv-item');
    for (var n = 0; n < items.length; n++) {
      var key = items[n].getAttribute('data-key');
      if (known[key]) {
        items[n].classList.add('conv-settled');
      } else {
        known[key] = 1;
        items[n].style.setProperty('--i', Math.min(n, 8));
      }
    }
    bindConversationClicks();
    var unread = 0;
    for (var j = 0; j < Chat.conversations.length; j++) unread += (Chat.conversations[j].unread || 0);
    U.el('badgeChat').textContent = unread ? String(unread) : '';
  }

  function convHtml(conv) {
    var name = conv.name || conv.display || conv.key;
    // QQ 没返回昵称时给出明确提示：双击会话或点标题栏 ✏️ 可以自己命名
    var unnamed = (conv.type !== 'group' && conv.named === false);
    var sub = conv.type === 'group'
      ? ('群 ' + (conv.group_openid || '').slice(0, 10) + '…')
      : ('用户 ' + (conv.openid || '').slice(0, 10) + '…');
    return '<div class="conv-item' + (conv.key === Chat.activeKey ? ' active' : '') +
      '" data-key="' + U.esc(conv.key) + '" title="双击可给这个' +
      (conv.type === 'group' ? '群' : '用户') + '起名字">' +
      '<div class="conv-line"><span class="conv-type ' +
      (conv.type === 'group' ? 'type-group' : 'type-private') + '">' +
      (conv.type === 'group' ? '群' : '私') + '</span>' +
      '<span class="conv-title">' + U.esc(name) + '</span>' +
      (conv.unread ? '<span class="conv-unread">' + conv.unread + '</span>' : '') +
      '<span class="conv-time">' + U.esc(U.fmtTime(conv.last_time)) + '</span></div>' +
      '<div class="conv-preview">' + U.esc(conv.last_content || '（暂无消息）') + '</div>' +
      '<div class="conv-sub">' + U.esc(sub) +
      (unnamed ? ' · <span class="warn-text">QQ 无昵称，双击命名</span>' : '') + '</div>' +
      '</div>';
  }

  function bindConversationClicks() {
    var items = U.el('convScroll').querySelectorAll('.conv-item');
    for (var i = 0; i < items.length; i++) {
      items[i].addEventListener('click', function () {
        var conv = Chat.byKey[this.getAttribute('data-key')];
        if (conv) openConversation(conv);
      });
      // 双击会话 → 给这个用户/群起个名字
      items[i].addEventListener('dblclick', function (event) {
        event.preventDefault();
        var conv = Chat.byKey[this.getAttribute('data-key')];
        if (conv) renameConversation(conv);
      });
    }
  }

  /* ============================ 手动命名 ============================ */
  function renameConversation(conv) {
    var target = conv || Chat.byKey[Chat.activeKey];
    if (!target) { setStatus('请先选择会话', 'err'); return; }
    var openid = target.type === 'group' ? (target.group_openid || '') : (target.openid || '');
    if (!openid) { setStatus('该会话缺少 openid，无法命名', 'err'); return; }
    var current = target.name || '';
    U.confirm({
      title: '给这个' + (target.type === 'group' ? '群' : '用户') + '起个名字',
      html: '<div class="tiny" style="margin-bottom:6px">openid：<code>' + U.esc(openid) + '</code></div>' +
        '<input type="text" id="aliasInput" value="' + U.esc(current) + '" placeholder="留空表示清除自定义名称" ' +
        'style="width:100%">' +
        '<div class="tiny" style="margin-top:6px">' +
        (target.type === 'group'
          ? '群名以 QQ 返回的为准；这里设置的名字会优先显示。'
          : 'QQ 的私聊事件通常不返回昵称，所以需要你手动命名（只保存在本地，不会改动 QQ 资料）。') +
        '</div>',
      okText: '保存名称',
      danger: false
    }).then(function (ok) {
      if (!ok) return;
      var input = U.el('aliasInput');
      var name = input ? input.value.trim() : '';
      U.postJSON('/api/chat/alias', { openid: openid, name: name })
        .then(function (data) {
          U.toast(data.message || '已保存', 'ok');
          if (Chat.activeKey === target.key) {
            Chat.activeName = name || target.display || openid;
            U.el('chatTitle').textContent = Chat.activeName;
          }
          loadConversations();
          loadMessages(true);
        })
        .catch(function (error) { U.toast(error.message, 'err'); });
    });
  }

  /* ============================ 打开会话 ============================ */
  function parseConv(key) {
    var parts = String(key || '').split(':');
    if (parts.length >= 3 && (parts[1] === 'group' || parts[1] === 'private')) {
      return { bot: parts[0], type: parts[1], openid: parts.slice(2).join(':') };
    }
    if (parts.length >= 2) return { bot: '', type: parts[0], openid: parts.slice(1).join(':') };
    return { bot: '', type: 'private', openid: key };
  }

  function openConversation(conv) {
    var parsed = parseConv(conv.key);
    Chat.activeKey = conv.key;
    Chat.activeType = conv.type || parsed.type;
    Chat.activeOpenid = conv.type === 'group' ? (conv.group_openid || parsed.openid) : (conv.openid || parsed.openid);
    Chat.activeName = conv.name || conv.display || conv.key;
    Chat.quote = null;
    renderQuoteBar();
    U.el('chatTitle').textContent = Chat.activeName;
    var bot = botById(parsed.bot);
    U.el('chatSub').innerHTML = (Chat.activeType === 'group' ? '群聊' : '私聊') +
      ' · openid: <code>' + U.esc(Chat.activeOpenid) + '</code>' +
      (bot ? ' · 机器人：' + U.esc(bot.name) : '');
    U.el('fetchGroupNameBtn').style.display = Chat.activeType === 'group' ? '' : 'none';
    FX.remember('lastConv', conv.key);
    // 切会话时重置渲染状态，避免"只追加"逻辑串到别的会话上
    Chat.lastRendered = '';
    Chat.lastMsgId = null;
    Chat.appendedKey = '';
    Chat.pendingNew = 0;
    hideNewHint();
    loadMessages(true);
    markRead(conv.key);
    renderConversations(Chat.conversations);
  }

  function botById(id) {
    for (var i = 0; i < Chat.bots.length; i++) if (Chat.bots[i].id === id) return Chat.bots[i];
    return null;
  }

  function openManualConversation() {
    var type = U.el('newType').value;
    var openid = (U.el('newOpenid').value || '').trim();
    if (!openid) { setStatus('请填写 openid', 'err'); return; }
    if (/\s/.test(openid)) { setStatus('openid 不能包含空格', 'err'); return; }
    var key = (Chat.botId ? Chat.botId + ':' : '') + type + ':' + openid;
    var conv = Chat.byKey[key] || {
      key: key, type: type, name: openid,
      group_openid: type === 'group' ? openid : '',
      openid: type === 'group' ? '' : openid
    };
    openConversation(conv);
    setStatus('已打开会话，可开始发送消息', 'ok');
  }

  function markRead(key) {
    U.postJSON('/api/chat/read', { key: key }).catch(function () { /* 忽略 */ });
  }

  /* ============================ 消息渲染 ============================ */
  function renderMessages(list, options) {
    var opts = options || {};
    var area = U.el('msgArea');
    var signature = JSON.stringify(list) + '|' + Chat.activeKey;
    if (!opts.force && signature === Chat.lastRendered) return;   // 数据没变 → 完全不碰 DOM
    Chat.messages = list || [];

    if (!Chat.activeKey) {
      Chat.lastRendered = signature;
      Chat.lastMsgId = null;
      area.innerHTML = '<div class="empty-hint"><div class="empty-icon">💬</div>' +
        '<p>右侧尚无会话</p><p class="tiny">从左侧选择会话，或在上方输入 openid 开启新会话</p></div>';
      return;
    }
    if (!Chat.messages.length) {
      Chat.lastRendered = signature;
      Chat.lastMsgId = null;
      area.innerHTML = '<div class="empty-hint"><div class="empty-icon">🕊️</div>' +
        '<p>这个会话还没有消息</p><p class="tiny">收到的图片/表情包会自动留存到本地</p></div>';
      return;
    }

    var lastMessage = Chat.messages[Chat.messages.length - 1];
    var lastId = lastMessage ? lastMessage.id : null;
    var index = buildIndex(Chat.messages);

    // 纯追加：只在末尾多了 1~N 条（轮询/实时推送的常见情况）→ 只插入新气泡，
    // 不动已有 DOM，也就不存在"整块重绘"造成的跳动
    var canAppend = !opts.force && Chat.appendedKey === Chat.activeKey
      && Chat.lastMsgId !== null && lastId !== Chat.lastMsgId
      && index['local:' + Chat.lastMsgId];
    if (canAppend) {
      var position = null;
      for (var i = 0; i < Chat.messages.length; i++) {
        if (Chat.messages[i].id === Chat.lastMsgId) { position = i; break; }
      }
      if (position !== null && position < Chat.messages.length - 1) {
        var stickBefore = FX.nearBottom(area, 80);
        var holder = document.createElement('div');
        var html = [];
        for (var n = position + 1; n < Chat.messages.length; n++) {
          html.push(messageHtml(Chat.messages[n], index));
        }
        holder.innerHTML = html.join('');
        while (holder.firstChild) area.appendChild(holder.firstChild);
        bindMessageClicks(area);
        FX.fadeImages(area);
        Chat.lastRendered = signature;
        Chat.lastMsgId = lastId;
        if (stickBefore) {
          FX.scrollToBottom(area);
        } else {
          Chat.pendingNew = (Chat.pendingNew || 0) + (Chat.messages.length - position - 1);
          showNewHint();
        }
        return;
      }
    }

    // 其余情况（切会话、强制刷新、历史回溯）→ 整体重绘，
    // 但要**保住用户当前的滚动位置**，不能把人从历史里拽到底部
    var stick = FX.nearBottom(area, 80);
    var previousHeight = area.scrollHeight;
    var previousTop = area.scrollTop;
    var htmlAll = [];
    for (var k = 0; k < Chat.messages.length; k++) {
      htmlAll.push(messageHtml(Chat.messages[k], index));
    }
    area.innerHTML = htmlAll.join('');
    bindMessageClicks(area);
    FX.fadeImages(area);
    Chat.lastRendered = signature;
    Chat.lastMsgId = lastId;
    Chat.appendedKey = Chat.activeKey;
    if (stick || opts.force) {
      FX.scrollToBottom(area);
      Chat.pendingNew = 0;
      hideNewHint();
    } else {
      // 保持视觉位置：按高度差补偿，避免内容变化时"跳一下"
      area.scrollTop = previousTop + (area.scrollHeight - previousHeight);
    }
  }

  function buildIndex(messages) {
    var index = {};
    for (var i = 0; i < messages.length; i++) {
      var item = messages[i];
      if (item.msg_id) index['id:' + item.msg_id] = item;
      if (item.msg_idx) index['idx:' + item.msg_idx] = item;
      index['local:' + item.id] = item;
    }
    return index;
  }

  function showNewHint() {
    var node = U.el('newMsgHint');
    if (!node) return;
    node.textContent = '↓ ' + (Chat.pendingNew || 0) + ' 条新消息';
    node.style.display = 'block';
  }

  function hideNewHint() {
    var node = U.el('newMsgHint');
    if (node) node.style.display = 'none';
    Chat.pendingNew = 0;
  }

  function messageHtml(message, index) {
    var isOut = message.direction === 'out';
    var who = isOut ? '我' : (message.sender_name || message.username || '对方');
    var meta = who + ' · ' + U.esc(message.time || '');
    if (isOut && message.bot_id) {
      var bot = botById(message.bot_id);
      if (bot) meta += ' · ' + U.esc(bot.name);
    }
    if (!isOut && message.type === 'group' && message.openid) {
      meta += ' · <span class="tiny">' + U.esc(String(message.openid).slice(0, 12)) + '…</span>';
    }

    var inner = '';
    var quote = message.quote || {};
    var quoteHtml = '';
    if (!isOut) {
      var target = null;
      if (quote.msg_id && index['id:' + quote.msg_id]) target = index['id:' + quote.msg_id];
      else if (quote.msg_idx && index['idx:' + quote.msg_idx]) target = index['idx:' + quote.msg_idx];
      var quoteText = quote.content_display || quote.content ||
        (target ? (displayText(target) || '[图片]') : '');
      if (!quoteText && (quote.attachments || (target && target.image_url))) quoteText = '[图片]';
      if (quoteText) {
        var short = quoteText.length > 120 ? quoteText.slice(0, 120) + '…' : quoteText;
        quoteHtml = '<div class="quoted' + (quoteText.length > 120 ? ' expandable' : '') +
          '" data-full="' + U.esc(quoteText) + '" data-short="' + U.esc(short) + '" title="点击展开/收起">' +
          U.esc(short) + '</div>';
      }
    } else if (message.reply_to && index['id:' + message.reply_to]) {
      var replied = index['id:' + message.reply_to];
      var repliedText = displayText(replied) || '[图片]';
      quoteHtml = '<div class="quoted">' + U.esc(repliedText.slice(0, 120)) + '</div>';
    }

    // 图片统一走 imageKeys()：自动去重（附件 / image_url / 表情图片常指向同一张图），
    // 并优先使用本地留存的那份，避免"一条消息显示两张一样的图"
    var images = imageKeys(message);
    var attachments = message.attachments || [];
    var attachmentHtml = '';
    for (var a = 0; a < attachments.length; a++) {
      var att = attachments[a];
      var attUrl = att.local_url || att.url || '';
      if (isImageAttachment(att)) continue;              // 图片已由 imageKeys 处理
      var attName = att.file_name || att.file_info || '附件';
      var attSize = att.size ? '（' + U.humanSize(att.size) + '）' : '';
      if (attUrl) {
        attachmentHtml += '<div><a class="raw-link" href="' + U.esc(fileSrc(attUrl)) +
          '" target="_blank" rel="noopener" download>📄 ' + U.esc(attName) + attSize +
          '（点击下载）</a></div>';
      } else if (att.file_name || att.file_info) {
        attachmentHtml += '<div class="att-placeholder">📎 有附件（未提供下载地址：' +
          U.esc(attName) + '）</div>';
      }
    }
    var imagesHtml = images.map(function (url) { return imgHtml(url, '图片/表情包'); }).join('');

    var body = formatContent(displayText(message));
    if (!images.length && !attachmentHtml && !body) {
      var found = String(message.content || '').match(/https?:\/\/[^\s"']+\.(png|jpe?g|gif|webp|bmp)(\?[^\s"']*)?/gi);
      if (found && found.length) {
        var unique = [];
        for (var f = 0; f < found.length; f++) {
          if (unique.indexOf(found[f]) < 0) unique.push(found[f]);
        }
        imagesHtml = unique.slice(0, 3).map(function (url) { return imgHtml(url, '链接图片'); }).join('');
      }
    }
    if (!body && (images.length || attachmentHtml)) body = '';

    inner = quoteHtml + imagesHtml + attachmentHtml +
      (body ? '<div class="msg-text">' + body + '</div>' : '');
    var rawLink = (!isOut && message.msg_id)
      ? '<div class="raw-link" data-raw="1">查看原始数据</div>' : '';

    return '<div class="msg ' + (isOut ? 'out' : 'in') + '" data-idx="' +
      Chat.messages.indexOf(message) + '" data-msgid="' + U.esc(message.msg_id || '') + '">' +
      '<div class="msg-wrap"><div class="msg-meta">' + meta + '</div>' +
      '<div class="bubble">' + (inner || '<span class="tiny">[空消息]</span>') + rawLink + '</div>' +
      '</div></div>';
  }

  /* ============================ 消息交互（点击弹框） ============================ */
  function bindMessageClicks(area) {
    // 只有点在「气泡」上才弹操作框：点空白处、时间行、消息之间的间隙都不弹，
    // 避免误触（用户反馈过"点非消息内容也出现操作框"）
    var bubbles = area.querySelectorAll('.msg .bubble');
    for (var i = 0; i < bubbles.length; i++) {
      bubbles[i].addEventListener('click', function (event) {
        var node = this.closest('.msg');
        if (!node) return;
        var message = Chat.messages[parseInt(node.getAttribute('data-idx'), 10)];
        if (!message) return;
        if (event.target.closest('[data-raw]')) {
          event.stopPropagation();
          showRaw(node, message);
          return;
        }
        if (event.target.closest('.quoted.expandable')) {
          event.stopPropagation();
          toggleQuote(event.target.closest('.quoted'));
          return;
        }
        if (event.target.tagName === 'IMG') {
          if (U.BOOT.ui && U.BOOT.ui.lightbox === false) return;
          event.stopPropagation();
          showLightbox(event.target.getAttribute('data-original') || event.target.src);
          return;
        }
        if (event.target.closest('a')) return;
        event.stopPropagation();
        showPopover(event.clientX, event.clientY, message);
      });
    }
    // 右键：仍可作用在整行上（右键本来就是"要操作这条消息"的强意图）
    var nodes = area.querySelectorAll('.msg');
    for (var j = 0; j < nodes.length; j++) {
      nodes[j].addEventListener('contextmenu', function (event) {
        var message = Chat.messages[parseInt(this.getAttribute('data-idx'), 10)];
        if (!message) return;
        event.preventDefault();
        showPopover(event.clientX, event.clientY, message);
      });
    }
    var images = area.querySelectorAll('img[data-original]');
    for (var k = 0; k < images.length; k++) {
      images[k].addEventListener('error', function () {
        var original = this.getAttribute('data-original');
        var holder = document.createElement('div');
        holder.className = 'img-fallback';
        holder.innerHTML = '🖼️ 图片加载失败，<a href="' + U.esc(imgSrc(original)) +
          '" target="_blank" rel="noopener">点此打开原图</a>';
        if (this.parentNode) this.parentNode.replaceChild(holder, this);
      });
    }
  }

  function toggleQuote(node) {
    var expanded = node.getAttribute('data-expanded') === '1';
    node.setAttribute('data-expanded', expanded ? '0' : '1');
    node.textContent = expanded ? node.getAttribute('data-short') : node.getAttribute('data-full');
  }

  /* ---------------- 浮动操作框 ---------------- */
  function showPopover(x, y, message) {
    var pop = U.el('msgPopover');
    var isOut = message.direction === 'out';
    var images = imageKeys(message);       // 与气泡渲染用同一套去重逻辑

    var rows = [];
    rows.push('<div class="title">' + (isOut ? '我发送的消息' : '对方的消息') +
      ' · ' + U.esc(U.fmtTime(message.time)) + '</div>');
    if (!isOut && message.msg_id) {
      rows.push('<button data-act="quote">💬 引用这条消息</button>');
    }
    if (images.length) {
      rows.push('<button data-act="fill">🔗 填入图片链接</button>');
      rows.push('<button data-act="open">🖼️ 查看大图</button>');
      rows.push('<button data-act="save">⬇️ 保存图片</button>');
    }
    if (message.content) {
      rows.push('<button data-act="copy">📋 复制文本内容</button>');
    }
    if (message.msg_id) {
      rows.push('<button data-act="copyid">🔑 复制消息 ID</button>');
      if (!isOut) rows.push('<button data-act="raw">🧾 查看原始数据</button>');
    }
    // 撤回：自己发出的消息可以撤回（群聊/私聊都走官方接口，2 分钟内有效）
    if (isOut && (message.id || message.msg_id)
        && (message.content || '').indexOf('已撤回') < 0) {
      var canRecall = !!message.msg_id;
      rows.push('<div class="sep"></div>');
      rows.push('<button data-act="recall"' + (canRecall ? '' : ' disabled') + '>' +
        '↩️ 撤回这条消息' +
        (canRecall ? '（2 分钟内）' : '（缺少消息 ID，无法撤回）') + '</button>');
    }
    if (!isOut && message.msg_id && Chat.activeType === 'group') {
      rows.push('<div class="sep"></div>');
      rows.push('<button data-act="mute">🔇 禁言发送者</button>');
      // 撤回群成员的消息：机器人是群管理员才行（官方限制 2 分钟内）
      if ((message.content || '').indexOf('已撤回') < 0) {
        var roleKnown = Chat.groupRole === 'owner' || Chat.groupRole === 'admin'
          || Chat.groupRole === 'member';
        var adminHint = Chat.groupRole === 'member'
          ? '（机器人不是管理员，会被平台拒绝）'
          : (roleKnown ? '（管理员权限，2 分钟内）' : '（需要机器人是群管理员）');
        rows.push('<button data-act="recall-member">🗑️ 撤回这条群消息' + adminHint + '</button>');
      }
    }
    rows.push('<div class="sep"></div>');
    rows.push('<button data-act="clearc">🧹 清空本会话聊天记录</button>');
    pop.innerHTML = rows.join('');
    pop.style.display = 'block';
    pop.style.left = '0px';
    pop.style.top = '0px';
    var rect = pop.getBoundingClientRect();
    var left = Math.min(x, window.innerWidth - rect.width - 12);
    var top = Math.min(y, window.innerHeight - rect.height - 12);
    pop.style.left = Math.max(8, left) + 'px';
    pop.style.top = Math.max(8, top) + 'px';

    pop.onclick = function (event) {
      var button = event.target.closest('button');
      if (!button) return;
      var action = button.getAttribute('data-act');
      hidePopover();
      handleAction(action, message, images);
    };
  }

  function hidePopover() {
    var pop = U.el('msgPopover');
    pop.style.display = 'none';
    pop.onclick = null;
  }

  function handleAction(action, message, images) {
    if (action === 'quote') {
      Chat.quote = {
        msg_id: message.msg_id,
        preview: (displayText(message) || '[图片]').slice(0, 200)
      };
      renderQuoteBar();
      setStatus('已引用该消息，发送时会带上引用', 'ok');
      U.el('content').focus();
      return;
    }
    if (action === 'fill') {
      U.el('imageUrl').value = images[0];
      setStatus('图片链接已填入输入栏，可直接发送', 'ok');
      return;
    }
    if (action === 'open') {
      showLightbox(images[0]);
      return;
    }
    if (action === 'save') {
      saveImage(images[0]);
      return;
    }
    if (action === 'copy') {
      copyText(message.content);
      return;
    }
    if (action === 'copyid') {
      copyText(message.msg_id);
      return;
    }
    if (action === 'raw') {
      var node = U.el('msgArea').querySelector('.msg[data-msgid="' + cssEscape(message.msg_id) + '"]');
      if (node) showRaw(node, message);
      return;
    }
    if (action === 'recall') {
      recallMessage(message);
      return;
    }
    if (action === 'recall-member') {
      recallMemberMessage(message);
      return;
    }
    if (action === 'mute') {
      quickMute(message);
      return;
    }
    if (action === 'clearc') {
      clearConversation();
    }
  }

  function cssEscape(text) {
    return String(text || '').replace(/["\\]/g, '\\$&');
  }

  function guessExt(url) {
    var match = String(url || '').match(/\.(png|jpe?g|gif|webp|bmp)(\?|$)/i);
    return match ? '.' + match[1].toLowerCase() : '.png';
  }

  function saveImage(url) {
    var link = document.createElement('a');
    link.href = imgSrc(url);
    link.download = 'qq-image' + guessExt(url);
    link.target = '_blank';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

  function copyText(text) {
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { U.toast('已复制', 'ok', 1600); },
        function () { U.toast('复制失败，请手动选择文本', 'err'); });
    } else {
      U.toast('当前浏览器不支持自动复制', 'warn');
    }
  }

  function recallMessage(message) {
    var isGroup = Chat.activeType === 'group';
    U.confirm({
      title: '撤回消息',
      html: '撤回这条<b>自己发出</b>的消息？<br>' +
        '<span class="tiny">QQ 官方限制：只能撤回 <b>2 分钟内</b>的消息。' +
        (isGroup ? '（要撤回<b>群成员</b>发的消息，用操作框里的「🗑️ 撤回这条群消息」，' +
          '需要机器人是群管理员。）' : '') +
        '撤回失败时不会改动任何记录。</span>',
      okText: '确认撤回'
    }).then(function (ok) {
      if (!ok) return;
      U.postJSON('/api/groups/recall', {
        message_id: message.id,
        msg_id: message.msg_id,
        bot_id: message.bot_id || Chat.botId
      }).then(function (data) {
        // 只有平台真的撤回成功才提示成功；失败就是把真实原因说出来
        U.toast(data.message || '已撤回', data.success ? 'ok' : 'err', 7000);
        if (data.success) loadMessages(true);
      }).catch(function (error) {
        U.toast(error.message || '撤回失败', 'err', 7000);
      });
    });
  }

  /* 撤回群成员（普通用户）的消息：官方要求机器人是群管理员，且 2 分钟内 */
  function recallMemberMessage(message) {
    var who = message.sender_name || message.username || '该成员';
    var role = Chat.groupRole;
    var warn = role === 'member'
      ? '<div class="hint warn-text" style="margin-top:6px">机器人当前<b>不是</b>该群管理员，' +
        '平台会拒绝这次撤回（只能撤回机器人自己发的消息）。请让群主把机器人设为管理员。</div>'
      : (role ? '' : '<div class="tiny" style="margin-top:6px">正在确认机器人在群里的身份…</div>');
    U.confirm({
      title: '撤回群成员的消息',
      html: '撤回 <b>' + U.esc(who) + '</b> 发的这条消息？<br>' +
        '<span class="tiny">调用官方接口 ' +
        '<code>DELETE /v2/groups/{group_openid}/messages/{message_id}</code>；' +
        'QQ 限制只能撤回 <b>2 分钟内</b>的消息，且机器人必须是<b>群管理员</b>。' +
        '撤回失败不会改动任何记录。</span>' + warn,
      okText: '确认撤回'
    }).then(function (ok) {
      if (!ok) return;
      U.postJSON('/api/groups/recall', {
        message_id: message.id,
        msg_id: message.msg_id,
        bot_id: message.bot_id || Chat.botId,
        as_admin: true
      }).then(function (data) {
        U.toast(data.message || '已撤回', data.success ? 'ok' : 'err', 8000);
        if (data.success) {
          loadMessages(true);
        } else if (data.need_admin) {
          Chat.groupRole = 'member';       // 平台说没权限，按钮提示同步更新
        }
      }).catch(function (error) {
        U.toast(error.message || '撤回失败', 'err', 8000);
      });
    });
  }

  function quickMute(message) {
    var minutes = 10;
    U.confirm({
      title: '禁言该成员',
      html: '禁言 <code>' + U.esc((message.username || message.openid || '').slice(0, 16)) +
        '</code><br><label class="chk">时长（分钟，0=平台默认，上限 30 天）：' +
        '<input type="number" id="muteMinutes" value="10" min="0" max="43200" style="width:110px"></label>' +
        '<div class="tiny" style="margin-top:6px">需要机器人是群管理员，官方接口才能真正禁言；' +
        '否则至少会在本地生效（机器人不回复该成员）。</div>',
      okText: '确认禁言'
    }).then(function (ok) {
      if (!ok) return;
      var input = U.el('muteMinutes');
      minutes = input ? parseInt(input.value, 10) : 10;
      U.postJSON('/api/groups/mute', {
        group_openid: Chat.activeOpenid,
        member_openid: message.openid,
        username: message.username || '',
        minutes: isNaN(minutes) ? 10 : minutes,
        bot_id: message.bot_id || Chat.botId
      }).then(function (data) {
        U.toast(data.message || '已处理', data.success ? 'ok' : 'warn', 5000);
      }).catch(function (error) { U.toast(error.message, 'err', 5000); });
    });
  }

  function showRaw(node, message) {
    var bubble = node.querySelector('.bubble');
    var existing = bubble.querySelector('.raw-pre');
    if (existing) {
      existing.style.display = existing.style.display === 'none' ? 'block' : 'none';
      return;
    }
    var pre = document.createElement('pre');
    pre.className = 'raw-pre';
    pre.textContent = '加载中…';
    bubble.appendChild(pre);
    U.getJSON('/api/chat/raw?key=' + encodeURIComponent(Chat.activeKey) +
      '&msg_id=' + encodeURIComponent(message.msg_id))
      .then(function (data) {
        var content = data.parsed ? JSON.stringify(data.parsed, null, 2) : (data.raw || '');
        pre.textContent = content.slice(0, 20000);
      })
      .catch(function (error) { pre.textContent = '获取失败：' + error.message; });
  }

  /* ---------------- 图片灯箱 ---------------- */
  function showLightbox(url) {
    U.el('lightboxImg').src = imgSrc(url);
    U.el('lightboxCap').textContent = url;
    U.el('lightbox').style.display = 'flex';
  }

  function hideLightbox() { U.el('lightbox').style.display = 'none'; }

  /* ============================ 引用栏 ============================ */
  function renderQuoteBar() {
    var bar = U.el('quoteBar');
    if (!Chat.quote) { bar.style.display = 'none'; return; }
    U.el('quoteText').textContent = Chat.quote.preview;
    bar.style.display = 'flex';
  }

  /* ============================ 发送 ============================ */
  function setStatus(text, kind) {
    var node = U.el('status');
    node.textContent = text || '';
    node.className = 'status ' + (kind || '');
  }

  function buildForm(targetType, openid) {
    var form = new FormData();
    form.append('targetType', targetType);
    form.append('openid', openid);
    if (Chat.botId) form.append('bot_id', Chat.botId);
    return form;
  }

  function sendMessage() {
    if (Chat.sending) return;
    var content = (U.el('content').value || '').trim();
    var imageUrl = (U.el('imageUrl').value || '').trim();
    var pending = Chat.pending;
    if (!Chat.activeKey) { setStatus('请先在左侧选择要发送到的会话', 'err'); return; }
    if (!content && !imageUrl && !pending) { setStatus('消息内容不能为空', 'err'); return; }
    if (!Chat.activeOpenid) { setStatus('会话缺少 openid，无法发送', 'err'); return; }

    var button = U.el('sendBtn');
    button.disabled = true;
    Chat.sending = true;
    setStatus('发送中…');

    var request;
    if (pending) {
      request = doUpload(pending.file, pending.kind, content);
    } else {
      var body = {
        targetType: Chat.activeType, openid: Chat.activeOpenid,
        content: content, bot_id: Chat.botId
      };
      if (imageUrl) body.image_url = imageUrl;
      if (Chat.quote) body.msg_id = Chat.quote.msg_id;
      request = U.postJSON('/api/chat/send', body);
    }

    request.then(function (data) {
      var modeText = { text: '文本', image: '图片', image_link: '图片链接',
                       file: '文件', link: '文件链接' }[data.mode] || '消息';
      setStatus('✅ ' + modeText + '发送成功' + (data.note ? '（' + data.note + '）' : ''), 'ok');
      U.el('content').value = '';
      U.el('imageUrl').value = '';
      clearPending();
      Chat.quote = null;
      renderQuoteBar();
      loadMessages(true);
      loadConversations();
    }).catch(function (error) {
      setStatus('❌ ' + error.message, 'err');
    }).then(function () {
      button.disabled = false;
      Chat.sending = false;
    });
  }

  function doUpload(file, kind, content) {
    var form = buildForm(Chat.activeType, Chat.activeOpenid);
    form.append('content', content || '');
    if (Chat.quote) form.append('msg_id', Chat.quote.msg_id);
    form.append('file', file, file.name || (kind === 'image' ? 'image.png' : 'file'));
    form.append('file_name', file.name || '');
    if (kind === 'image') form.append('as_image', '1');
    var lastPct = -1;
    return U.upload('/api/chat/send', form, function (loaded, total) {
      if (!total) return;
      var pct = Math.floor(loaded * 100 / total);
      if (pct === lastPct) return;
      lastPct = pct;
      setStatus('上传中… ' + pct + '%（' + U.humanSize(loaded) + ' / ' + U.humanSize(total) + '）');
      if (pct >= 100) {
        setStatus('已上传到本机 ' + U.humanSize(total) +
          '，正在发给 QQ…（大文件会用官方分片上传，请稍候，别关页面）');
      }
    });
  }

  function onFilePicked(input, kind) {
    var file = input.files && input.files[0];
    if (!file) return;
    var limitMb = kind === 'image'
      ? ((U.BOOT.limits || {}).max_image_mb || 20)
      : ((U.BOOT.limits || {}).max_file_mb || 200);
    if (file.size > limitMb * 1024 * 1024) {
      setStatus('❌ 文件过大（当前上限 ' + limitMb + ' MB；QQ 官方硬限制 200MB，' +
        '可在「设置 → 发送策略」调整）', 'err');
      input.value = '';
      return;
    }
    Chat.pending = { file: file, kind: kind, name: file.name };
    var hint = U.el('fileHint');
    if (kind === 'image' && /^image\//.test(file.type)) {
      var reader = new FileReader();
      reader.onload = function () {
        hint.innerHTML = '<img src="' + reader.result + '" alt="预览">' +
          '<span>' + U.esc(file.name) + '</span> ' +
          '<button class="icon-btn" id="rmPending" title="移除">✕</button>';
        var remove = U.el('rmPending');
        if (remove) remove.addEventListener('click', clearPending);
      };
      reader.readAsDataURL(file);
    } else {
      var big = file.size > 4 * 1024 * 1024;
      hint.innerHTML = '<span>📎 ' + U.esc(file.name) + '（' + U.humanSize(file.size) + '）' +
        (big ? ' · 大文件，将用分片上传' : '') + '</span> ' +
        '<button class="icon-btn" id="rmPending" title="移除">✕</button>';
      var rm = U.el('rmPending');
      if (rm) rm.addEventListener('click', clearPending);
    }
    setStatus('已选择文件，点击发送即可' +
      (file.size > 4 * 1024 * 1024 ? '（超过 4MB 会自动用 QQ 官方分片上传，需要等一会儿）' : ''), 'ok');
  }

  function clearPending() {
    Chat.pending = null;
    var hint = U.el('fileHint');
    if (hint) hint.innerHTML = '';
    if (U.el('fileInput')) U.el('fileInput').value = '';
  }

  /* ============================ 清空（二次确认） ============================ */
  function clearConversation() {
    if (!Chat.activeKey) { setStatus('请先选择会话', 'err'); return; }
    U.confirm({
      title: '清空本会话聊天记录',
      html: '即将清空会话 <b>' + U.esc(Chat.activeName) + '</b> 的本地聊天记录。<br>' +
        '<span class="tiny">只影响本机保存的消息，QQ 里的消息不会被删除；' +
        '该会话的对话上下文也会一并清空。</span><br>' +
        '<label class="chk" style="margin-top:8px"><input type="checkbox" id="clearConfirmChk"> ' +
        '我已确认要清空这个会话</label>',
      okText: '清空该会话'
    }).then(function (ok) {
      if (!ok) return;
      var chk = U.el('clearConfirmChk');
      if (!chk || !chk.checked) {
        U.toast('请先勾选确认，再点击清空', 'warn');
        return;
      }
      U.postJSON('/api/chat/clear', { key: Chat.activeKey, bot_id: Chat.botId })
        .then(function (data) {
          U.toast(data.message || '已清空', 'ok');
          loadMessages(true);
          loadConversations();
        })
        .catch(function (error) { U.toast(error.message, 'err'); });
    });
  }

  function clearAll() {
    U.confirm({
      title: '清空当前机器人的所有会话记录',
      danger: true,
      html: '这会删除<b>当前机器人</b>所有会话的本地聊天记录与上下文，且不可恢复。<br>' +
        '<span class="tiny">其它机器人的记录不会受影响；要清别的机器人，' +
        '先在左侧导航栏顶部切换过去。也建议优先用每个会话的「清空本会话」。</span><br>' +
        '<label class="chk" style="margin-top:8px"><input type="checkbox" id="clearAllChk"> ' +
        '我确认要清空当前机器人的所有会话记录</label>',
      okText: '清空当前机器人'
    }).then(function (ok) {
      if (!ok) return;
      var chk = U.el('clearAllChk');
      if (!chk || !chk.checked) { U.toast('请先勾选确认', 'warn'); return; }
      U.postJSON('/api/chat/clear', { all: true, bot_id: Chat.botId })
        .then(function (data) {
          U.toast(data.message || '已清空全部', 'ok');
          Chat.activeKey = '';
          loadConversations();
          renderMessages([], { force: true });
        })
        .catch(function (error) { U.toast(error.message, 'err'); });
    });
  }

  /* ============================ 数据加载 ============================ */
  function loadConversations() {
    Chat.botId = U.ActiveBot.id || '';
    return U.getJSON(U.qs('/api/chat/conversations'))
      .then(function (data) {
        Chat.bots = data.bots || [];
        renderBotPicker();
        renderConversations(data.conversations || []);
        updateFoot(data);
        updateConnBadge();
        return data;
      })
      .catch(function (error) {
        U.el('connText').textContent = '后台未响应';
        U.el('connDot').className = 'dot';
        if (String(error.message).indexOf('令牌') >= 0) U.toast(error.message, 'err', 6000);
        throw error;
      });
  }

  /* 左侧导航栏切换机器人后：清掉当前会话并重新加载（只显示该机器人的会话） */
  function onBotChanged() {
    Chat.botId = U.ActiveBot.id || '';
    Chat.activeKey = '';
    Chat.activeType = '';
    Chat.activeOpenid = '';
    Chat.activeName = '';
    Chat.messages = [];
    Chat.lastRendered = '';
    Chat.lastMsgId = null;
    Chat.convSignature = '';
    Chat.knownConvKeys = {};
    renderMessages([], { force: true });
    U.el('chatTitle').textContent = '未选择会话';
    U.el('chatSub').textContent = '从左侧选择一个会话';
    loadConversations().then(function () {
      if (Chat.conversations.length) openConversation(Chat.conversations[0]);
    }).catch(function () { /* 忽略 */ });
  }

  function updateFoot(data) {
    var online = (Chat.bots || []).filter(function (bot) { return bot.online; }).length;
    var total = (data.conversations || []).length;
    var botText = '机器人：' + online + '/' + (Chat.bots || []).length + ' 在线';
    var statText = total + ' 个会话 · 存储 ' +
      (data.storage === 'sqlite' ? 'SQLite' : '内存') +
      (data.unread_total ? ' · 未读 ' + data.unread_total : '');
    // 文本没变就不写 DOM（避免每次轮询触发无意义的重排）
    if (botText !== Chat.footBotText) {
      Chat.footBotText = botText;
      U.el('botSummary').textContent = botText;
    }
    if (statText !== Chat.footStatText) {
      Chat.footStatText = statText;
      U.el('footStat').textContent = statText;
    }
  }

  function updateConnBadge() {
    var online = (Chat.bots || []).filter(function (bot) { return bot.online; }).length;
    var configured = (Chat.bots || []).filter(function (bot) { return bot.configured; }).length;
    var dot = U.el('connDot');
    var text = U.el('connText');
    if (online > 0) {
      dot.className = 'dot on';
      text.textContent = online + ' 个机器人在线';
    } else if (configured > 0) {
      dot.className = 'dot';
      text.textContent = '已配置但未连接';
    } else {
      dot.className = 'dot';
      text.textContent = '未配置机器人';
    }
  }

  function renderBotPicker() {
    /* 机器人选择器已移到左侧导航栏（全局），这里只同步显示当前机器人的状态 */
    Chat.botId = U.ActiveBot.id || '';
    var bot = botById(Chat.botId);
    var title = U.el('chatSideTitle');
    if (title) title.textContent = bot ? ('会话 · ' + bot.name) : '会话';
  }

  function loadMessages(force) {
    if (!Chat.activeKey) { renderMessages([], { force: true }); return Promise.resolve(); }
    var limit = (U.BOOT.limits || {}).page_size || 200;
    return U.getJSON('/api/chat/messages?key=' + encodeURIComponent(Chat.activeKey) +
      '&limit=' + limit)
      .then(function (data) {
        // 群聊里机器人是不是管理员：决定"撤回群成员消息"按钮的提示
        if (data.group_role !== undefined) Chat.groupRole = data.group_role || '';
        renderMessages(data.messages || [], { force: force });
        var title = U.el('chatTitle');
        if (title && data.conversation && data.conversation.name && !Chat.activeName) {
          Chat.activeName = data.conversation.name;
          title.textContent = Chat.activeName;
        }
      })
      .catch(function (error) { setStatus('加载消息失败：' + error.message, 'err'); });
  }

  /* ============================ 实时推送 ============================ */
  function startStream() {
    if (!Chat.live) return;
    if (typeof EventSource === 'undefined') return;   // 老浏览器自动退回轮询
    try {
      if (Chat.stream) Chat.stream.close();
      Chat.stream = new EventSource(U.apiUrl('/api/chat/stream'));
      Chat.stream.onmessage = function (event) {
        var data;
        try { data = JSON.parse(event.data); } catch (e) { return; }
        if (data.type === 'message') {
          if (data.conversation === Chat.activeKey) loadMessages(true);
          loadConversations().catch(function () { /* 忽略 */ });
        } else if (data.type === 'bot_status' || data.type === 'conversations_changed') {
          loadConversations().catch(function () { /* 忽略 */ });
        } else if (data.type === 'group_role') {
          // 后台查明机器人在群里的身份：同步给操作框（决定能否撤回群成员消息）
          if (Chat.activeType === 'group' && data.group_openid === Chat.activeOpenid) {
            Chat.groupRole = data.role || '';
          }
        } else if (data.type === 'message_recalled') {
          if (data.conversation === Chat.activeKey) loadMessages(true);
        } else if (data.type === 'config_changed') {
          U.toast('配置已更新（' + (data.source || '') + '）', 'ok', 2200);
        } else if (data.type === 'port_changed') {
          // 改了监听端口但没重启：反复提示，避免用户以为"网站坏了"
          U.toast(data.message || '网页端口配置已变更，需要重启程序才生效', 'warn', 15000);
        }
      };
      Chat.stream.onerror = function () {
        // EventSource 会自动重连；这里只在彻底失败时提示
      };
    } catch (e) { /* 退回轮询 */ }
  }

  function startPolling() {
    if (Chat.timer) clearInterval(Chat.timer);
    var interval = (U.BOOT.ui || {}).poll_interval_ms || 2000;
    Chat.timer = setInterval(function () {
      if (!Chat.live) return;
      if (document.hidden) return;                 // 页面不可见时不轮询，省资源
      var active = document.querySelector('.nav-item.active');
      if (!active || active.getAttribute('data-page') !== 'chat') return;
      loadConversations().catch(function () { /* 忽略 */ });
      loadMessages(false);
    }, Math.max(1000, interval));
  }

  /* ============================ 事件绑定 ============================ */
  function init() {
    U.el('sendBtn').addEventListener('click', sendMessage);
    U.el('content').addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });
    U.el('newBtn').addEventListener('click', openManualConversation);
    U.el('refreshConvs').addEventListener('click', function () { loadConversations(); });
    U.el('refreshMsgs').addEventListener('click', function () {
      loadMessages(true);
      loadConversations();
    });
    U.el('quoteCancel').addEventListener('click', function () {
      Chat.quote = null;
      renderQuoteBar();
      setStatus('已取消引用');
    });
    U.el('clearConvBtn').addEventListener('click', clearConversation);
    U.el('clearAllBtn').addEventListener('click', clearAll);
    U.el('renameBtn').addEventListener('click', function () { renameConversation(null); });
    U.el('fetchGroupNameBtn').addEventListener('click', function () {
      if (!Chat.activeOpenid) return;
      setStatus('正在获取群名称…');
      U.postJSON('/api/chat/group_name', { openid: Chat.activeOpenid, bot_id: Chat.botId })
        .then(function (data) {
          setStatus(data.message || '已获取群名', data.success ? 'ok' : 'err');
          if (data.name) { Chat.activeName = data.name; U.el('chatTitle').textContent = data.name; }
          loadConversations();
        })
        .catch(function (error) { setStatus('获取失败：' + error.message, 'err'); });
    });
    U.el('fileInput').addEventListener('change', function () {
      var file = this.files && this.files[0];
      // 一个按钮搞定：是图片就按图片发（能在 QQ 里直接显示），否则按文件发
      var kind = (file && /^image\//.test(file.type || '')) ? 'image' : 'file';
      onFilePicked(this, kind);
    });
    U.el('liveToggle').addEventListener('change', function () {
      Chat.live = this.checked;
      if (Chat.live) startStream();
      else if (Chat.stream) Chat.stream.close();
      setStatus(Chat.live ? '实时刷新已开启' : '实时刷新已关闭（仍在低频轮询）', 'ok');
    });
    U.el('lightbox').addEventListener('click', hideLightbox);
    // 用户往回翻历史时，新消息不再把人拽到底部；点浮动提示才跳回最新
    U.el('msgArea').addEventListener('scroll', function () {
      if (FX.nearBottom(this, 80)) hideNewHint();
    });
    var hint = U.el('newMsgHint');
    if (hint) {
      hint.addEventListener('click', function () {
        hideNewHint();
        loadMessages(true);
        FX.scrollToBottom(U.el('msgArea'));
      });
    }
    document.addEventListener('click', function (event) {
      var pop = U.el('msgPopover');
      if (pop.style.display === 'block' && !event.target.closest('#msgPopover')) hidePopover();
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { hidePopover(); hideLightbox(); }
    });
    window.addEventListener('resize', hidePopover);

    Chat.botId = U.ActiveBot.id || '';
    startStream();
    startPolling();
    U.ActiveBot.onChange(function () { onBotChanged(); });
    loadConversations().then(function () {
      // 记不住上次的会话时，自动打开第一个（避免打开网页一片空白）
      var last = FX.recall('lastConv');
      if (last && Chat.byKey[last]) {
        openConversation(Chat.byKey[last]);
      } else if (Chat.conversations.length) {
        openConversation(Chat.conversations[0]);
      }
    }).catch(function () { /* 忽略 */ });
  }

  Chat.init = init;
  Chat.loadConversations = loadConversations;
  Chat.loadMessages = loadMessages;
  Chat.renderConversations = renderConversations;
  Chat.setStatus = setStatus;
  Chat.clearConversation = clearConversation;
  Chat.onBotChanged = onBotChanged;
  Chat.imageKeys = imageKeys;          // 供测试与调试使用
  Chat.messageHtmlForTest = messageHtml;
    // 防止整个对象被别人替换掉（对象内部字段照旧可改，见 util.js 的 protectGlobal）
  if (typeof global.__qbmProtect === 'function') global.__qbmProtect('Chat', Chat);
  else global.Chat = Chat;
})(window);
