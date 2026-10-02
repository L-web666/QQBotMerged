/* ==========================================================================
   公共工具（命名空间：Util / App / Chat / Settings / Admin / Groups）
   注意：原两个程序的前端脚本存在 7 组同名全局函数（loadConfig/saveConfig/show/url/TOKEN 等），
   合并时已全部收进各自的命名空间对象，避免互相覆盖。
   ========================================================================== */
(function (global) {
  'use strict';

  var BOOT = global.__BOOT__ || {};
  var TOKEN = global.__URL_TOKEN__ || '';

  function apiUrl(path) {
    if (!TOKEN) return path;
    return path + (path.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN);
  }

  function el(id) { return document.getElementById(id); }

  function esc(text) {
    return String(text === undefined || text === null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function getPath(obj, path) {
    var parts = String(path || '').split('.');
    var node = obj;
    for (var i = 0; i < parts.length; i++) {
      if (node === null || node === undefined) return undefined;
      node = node[parts[i]];
    }
    return node;
  }

  function setPath(obj, path, value) {
    var parts = String(path || '').split('.');
    var node = obj;
    for (var i = 0; i < parts.length - 1; i++) {
      if (typeof node[parts[i]] !== 'object' || node[parts[i]] === null) node[parts[i]] = {};
      node = node[parts[i]];
    }
    node[parts[parts.length - 1]] = value;
    return obj;
  }

  /* ---------------- 网络 ---------------- */
  function getJSON(path) {
    return fetch(apiUrl(path), { credentials: 'same-origin' }).then(function (response) {
      if (response.status === 401) {
        throw new Error('访问令牌无效：请在网址后面加上 ?token=你的令牌');
      }
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.json();
    });
  }

  /* ---------------- 当前机器人（全局） ----------------
     左侧导航栏顶部的选择器决定"整个后台针对哪个机器人"：
     设置页写进它的覆盖、聊天窗口只显示它的会话、群管理用它调接口。
  */
  var ActiveBot = {
    id: '',
    listeners: [],
    onChange: function (fn) { if (typeof fn === 'function') ActiveBot.listeners.push(fn); },
    emit: function () {
      for (var i = 0; i < ActiveBot.listeners.length; i++) {
        try { ActiveBot.listeners[i](ActiveBot.id); } catch (e) { /* 忽略单个订阅者错误 */ }
      }
    },
  };

  function qs(name) {
    if (ActiveBot.id) return name + (name.indexOf('?') >= 0 ? '&' : '?') + 'bot=' +
      encodeURIComponent(ActiveBot.id);
    return name;
  }

  function setActiveBot(botId) {
    ActiveBot.id = botId || '';
    try { localStorage.setItem('qbm-active-bot', ActiveBot.id); } catch (e) { /* 忽略 */ }
    ActiveBot.emit();
  }

  function restoreActiveBot() {
    try { ActiveBot.id = localStorage.getItem('qbm-active-bot') || ''; } catch (e) { ActiveBot.id = ''; }
    if (ActiveBot.id === 'all') ActiveBot.id = '';
    return ActiveBot.id;
  }

  function postJSON(path, body) {
    var payload = body || {};
    if (ActiveBot.id && payload.bot_id === undefined) payload.bot_id = ActiveBot.id;
    return fetch(apiUrl(path), {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          var error = new Error(data && data.message ? data.message : ('HTTP ' + response.status));
          error.payload = data;
          throw error;
        }
        return data;
      });
    });
  }

  /* 上传表单（支持进度回调：大文件走官方分片上传时，用户能看到"传到哪了"） */
  function upload(path, formData, onProgress) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open('POST', apiUrl(path), true);
      xhr.withCredentials = true;
      if (onProgress && xhr.upload) {
        xhr.upload.onprogress = function (event) {
          if (!event.lengthComputable) return;
          try { onProgress(event.loaded, event.total); } catch (e) { /* 忽略 */ }
        };
      }
      xhr.onerror = function () { reject(new Error('网络错误，上传失败')); };
      xhr.ontimeout = function () { reject(new Error('上传超时')); };
      xhr.onabort = function () { reject(new Error('上传已取消')); };
      xhr.onload = function () {
        var data = {};
        try { data = JSON.parse(xhr.responseText || '{}') || {}; }
        catch (e) { data = {}; }
        if (xhr.status < 200 || xhr.status >= 300) {
          var message = (data && data.message) || ('HTTP ' + xhr.status);
          if (xhr.status === 413) message = '文件太大，服务端拒收（上限 256MB）';
          var error = new Error(message);
          error.payload = data;
          reject(error);
          return;
        }
        resolve(data);
      };
      xhr.send(formData);
    });
  }

  /* ---------------- 提示 ---------------- */
  function toast(message, kind, timeout) {
    var wrap = el('toastWrap');
    if (!wrap) return;
    var node = document.createElement('div');
    node.className = 'toast ' + (kind || '');
    node.innerHTML = esc(message);
    wrap.appendChild(node);
    setTimeout(function () {
      node.classList.add('hide');
      setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, 240);
    }, timeout || 3200);
  }

  function confirmDialog(options) {
    var opts = options || {};
    return new Promise(function (resolve) {
      var modal = el('confirmModal');
      el('confirmTitle').textContent = opts.title || '请确认';
      el('confirmBody').innerHTML = opts.html || esc(opts.text || '确定要执行该操作吗？');
      var okBtn = el('confirmOk');
      var cancelBtn = el('confirmCancel');
      okBtn.textContent = opts.okText || '确认';
      okBtn.className = 'btn ' + (opts.danger === false ? 'primary' : 'danger');
      modal.style.display = 'flex';

      function cleanup(result) {
        modal.style.display = 'none';
        okBtn.removeEventListener('click', onOk);
        cancelBtn.removeEventListener('click', onCancel);
        el('confirmClose').removeEventListener('click', onCancel);
        modal.removeEventListener('click', onBackdrop);
        document.removeEventListener('keydown', onKey);
        resolve(result);
      }
      function onOk() { cleanup(true); }
      function onCancel() { cleanup(false); }
      function onBackdrop(event) { if (event.target === modal) cleanup(false); }
      function onKey(event) { if (event.key === 'Escape') cleanup(false); }

      okBtn.addEventListener('click', onOk);
      cancelBtn.addEventListener('click', onCancel);
      el('confirmClose').addEventListener('click', onCancel);
      modal.addEventListener('click', onBackdrop);
      document.addEventListener('keydown', onKey);
    });
  }

  function fmtTime(value) {
    if (!value) return '';
    var text = String(value);
    return text.length >= 16 ? text.slice(11, 16) : text;
  }

  function humanSize(bytes) {
    var value = Number(bytes || 0);
    if (value < 1024) return value + ' B';
    if (value < 1048576) return (value / 1024).toFixed(1) + ' KB';
    if (value < 1073741824) return (value / 1048576).toFixed(1) + ' MB';
    return (value / 1073741824).toFixed(2) + ' GB';
  }

  function humanDuration(seconds) {
    var s = Math.max(0, Math.floor(Number(seconds) || 0));
    if (s < 60) return s + ' 秒';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟';
    if (s < 86400) return Math.floor(s / 3600) + ' 小时' + Math.floor((s % 3600) / 60) + ' 分';
    return Math.floor(s / 86400) + ' 天' + Math.floor((s % 86400) / 3600) + ' 小时';
  }

  function debounce(fn, wait) {
    var timer = null;
    return function () {
      var args = arguments, self = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(self, args); }, wait || 200);
    };
  }

  global.Util = {
    BOOT: BOOT,
    TOKEN: TOKEN,
    apiUrl: apiUrl,
    qs: qs,
    ActiveBot: ActiveBot,
    setActiveBot: setActiveBot,
    restoreActiveBot: restoreActiveBot,
    el: el,
    esc: esc,
    getPath: getPath,
    setPath: setPath,
    getJSON: getJSON,
    postJSON: postJSON,
    upload: upload,
    toast: toast,
    confirm: confirmDialog,
    fmtTime: fmtTime,
    humanSize: humanSize,
    humanDuration: humanDuration,
    debounce: debounce
  };
})(window);
