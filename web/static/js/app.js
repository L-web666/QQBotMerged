/* ==========================================================================
   应用外壳：左侧导航切换（含过渡动画）、页面懒加载、状态轮询、全局快捷键
   注意：统一用一个 App.go() 切页，避免原两个程序里散落的 show()/tab 逻辑互相干扰。
   ========================================================================== */
(function (global) {
  'use strict';
  var U = global.Util;
  var FX = global.Effects;

  var App = {
    page: '',
    pollTimer: null,
    pollInterval: 0,
    statusTimer: null
  };

  var PAGE_TITLE = {
    chat: '聊天窗口', status: '运行状态', stats: '统计', settings: '设置',
    plugins: '插件管理', groups: '群管理', logs: '日志查看',
    context: '上下文', panels: '指令面板', media: '媒体留存', about: '使用说明'
  };

  function init() {
    // 先恢复"当前机器人"：所有接口都会带上它
    U.restoreActiveBot();
    FX.restoreTheme();
    FX.applyUiPrefs();
    loadBotSwitcher();

    // 页面切换
    var items = U.el('navList').querySelectorAll('.nav-item');
    for (var i = 0; i < items.length; i++) {
      items[i].addEventListener('click', function () {
        go(this.getAttribute('data-page'));
      });
    }
    U.el('navToggle').addEventListener('click', function () {
      U.el('app').classList.toggle('collapsed');
      if (window.innerWidth <= 900) U.el('app').classList.toggle('nav-open');
      FX.remember('navCollapsed', U.el('app').classList.contains('collapsed') ? '1' : '0');
    });
    U.el('themeBtn').addEventListener('click', FX.cycleTheme);
    var botPicker = U.el('botPicker');
    if (botPicker) {
      botPicker.addEventListener('change', function () { switchBot(this.value); });
    }

    // 网页上的「关闭程序」按钮（在"使用说明"页）
    var shutdownBtn = U.el('shutdownBtn');
    if (shutdownBtn) {
      shutdownBtn.addEventListener('click', function () {
        U.confirm({
          title: '关闭程序',
          html: '将停止所有机器人连接与网页服务，并退出程序。<br>' +
            '<span class="tiny">关闭后需要重新启动才能继续使用；' +
            '如果你是用 start.bat 启动的，脚本会自动把它再拉起来。</span>',
          okText: '确认关闭'
        }).then(function (ok) {
          if (!ok) return;
          U.postJSON('/api/admin/shutdown', { reason: '网页按钮' })
            .then(function (data) { U.toast(data.message || '正在关闭…', 'warn', 10000); })
            .catch(function (error) {
              // 进程正在退出时请求可能被中断，这属于正常现象
              U.toast('程序正在关闭：' + error.message, 'warn', 8000);
            });
        });
      });
    }

    // 恢复界面偏好
    FX.restoreTheme();
    FX.applyUiPrefs();
    if (FX.recall('navCollapsed') === '1' && window.innerWidth > 900) {
      U.el('app').classList.add('collapsed');
    }

    // 各模块初始化
    global.Chat.init();
    global.Settings.init();
    global.Admin.init();
    global.Groups.init();

    // 全局状态轮询（导航栏在线状态与未读数）
    startGlobalPoll();
    // 机器人在线状态定期刷新（选择器里的小圆点）
    setInterval(function () {
      if (!document.hidden) loadBotSwitcher();
    }, 15000);

    // 快捷键：Ctrl/Cmd + K 聚焦输入框；数字 1~9 切页
    document.addEventListener('keydown', function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        go('chat');
        setTimeout(function () { U.el('content').focus(); }, 60);
      }
    });

    var initial = FX.recall('lastPage') || 'chat';
    go(initial);
    window.addEventListener('hashchange', function () {
      var page = (location.hash || '').replace('#', '');
      if (page && PAGE_TITLE[page] && page !== App.page) go(page);
    });
  }

  /* ============================ 机器人选择器（全局） ============================ */
  function loadBotSwitcher() {
    return U.getJSON('/api/bots').then(function (data) {
      var bots = data.bots || [];
      var active = U.ActiveBot.id || data.active || '';
      if (!active || !bots.some(function (b) { return b.id === active; })) {
        var preferred = bots.filter(function (b) { return b.enabled && b.has_credentials; })[0]
          || bots.filter(function (b) { return b.enabled; })[0] || bots[0];
        active = preferred ? preferred.id : '';
        U.ActiveBot.id = active;
      }
      // 浏览器里记住的机器人要和**服务端**保持一致：
      // 两边不一致时（换了浏览器/重开过服务/清过缓存）以浏览器为准推给服务端，
      // 否则页面上的"当前机器人"与服务端实际用的会是两个。
      if (active && data.active !== active) {
        U.postJSON('/api/bots/active', { bot_id: active }).catch(function () { /* 忽略 */ });
      }
      renderBotSwitcher(bots, active);
      return active;
    }).catch(function () { /* 忽略 */ });
  }

  function renderBotSwitcher(bots, active) {
    var picker = U.el('botPicker');
    if (!picker) return;
    if (!bots.length) {
      picker.innerHTML = '<option value="">（还没有配置机器人）</option>';
      U.el('botSwitchHint').textContent = '请到「设置 → 机器人账号」添加一个机器人';
      return;
    }
    picker.innerHTML = bots.map(function (bot) {
      var flag = bot.online ? '🟢' : (bot.has_credentials ? '⚪' : '⚠️');
      return '<option value="' + U.esc(bot.id) + '"' + (bot.id === active ? ' selected' : '') +
        '>' + flag + ' ' + U.esc(bot.name) + '</option>';
    }).join('');
    var current = bots.filter(function (b) { return b.id === active; })[0];
    var hint = '';
    if (current) {
      hint = current.online ? '在线' : (current.has_credentials ? '已配置，未连接' : '未填写凭据');
      if (current.overrides) hint += ' · 有专属设置';
    }
    U.el('botSwitchHint').textContent = hint;
  }

  function switchBot(botId) {
    return U.postJSON('/api/bots/active', { bot_id: botId }).then(function (data) {
      U.setActiveBot(data.active || botId);
      U.toast(data.message || ('已切换到 ' + botId), 'ok', 2600);
      // 切机器人后：会话列表、设置、群管理都要按新机器人重新加载
      if (global.Chat && global.Chat.onBotChanged) global.Chat.onBotChanged();
      if (global.Settings && global.Settings.spec) global.Settings.load();
      if (global.Groups && global.Groups.active) global.Groups.openGroup(global.Groups.active);
      return loadBotSwitcher();
    }).catch(function (error) {
      U.toast('切换机器人失败：' + error.message, 'err');
    });
  }

  /* ============================ 页面切换 ============================ */
  function go(page) {
    if (!page || !PAGE_TITLE[page]) page = 'chat';
    if (page === App.page) {
      refreshPage(page, true);
      return;
    }
    App.page = page;
    var items = U.el('navList').querySelectorAll('.nav-item');
    for (var i = 0; i < items.length; i++) {
      items[i].classList.toggle('active', items[i].getAttribute('data-page') === page);
    }
    var pages = document.querySelectorAll('.page');
    for (var j = 0; j < pages.length; j++) {
      pages[j].classList.toggle('active', pages[j].getAttribute('data-page') === page);
    }
    document.body.setAttribute('data-page', page);
    if (U.el('app').classList.contains('nav-open')) U.el('app').classList.remove('nav-open');
    FX.remember('lastPage', page);
    if (location.hash !== '#' + page) {
      try { history.replaceState(null, '', '#' + page); } catch (e) { /* 忽略 */ }
    }
    refreshPage(page, false);
  }

  function refreshPage(page, isRepeat) {
    try {
      if (page === 'chat') {
        global.Chat.loadConversations().catch(function () { /* 忽略 */ });
        global.Chat.loadMessages(true);
      } else if (page === 'status') {
        global.Admin.loadStatus();
      } else if (page === 'stats') {
        global.Admin.loadStats();
      } else if (page === 'settings') {
        if (!global.Settings.spec) global.Settings.load();
      } else if (page === 'plugins') {
        global.Admin.loadPlugins();
      } else if (page === 'groups') {
        global.Groups.load();
      } else if (page === 'logs') {
        global.Admin.loadLogFiles().then(function () { global.Admin.loadLogs(); });
      } else if (page === 'context') {
        global.Admin.loadContext();
      } else if (page === 'panels') {
        global.Admin.loadPanels();
      } else if (page === 'media') {
        global.Admin.loadMedia();
      }
    } catch (error) {
      U.toast('页面加载出错：' + error.message, 'err');
    }
  }

  /* ============================ 全局轮询 ============================ */
  function startGlobalPoll(intervalMs) {
    if (App.pollTimer) clearInterval(App.pollTimer);
    App.pollInterval = intervalMs || App.pollInterval ||
      (U.BOOT.ui && U.BOOT.ui.poll_interval_ms) || 5000;
    App.pollTimer = setInterval(function () {
      if (document.hidden) return;
      if (App.page !== 'chat' && global.Chat && global.Chat.loadConversations) {
        // 不在聊天页时只更新导航栏的在线状态/未读角标，低频刷新
        global.Chat.loadConversations().catch(function () { /* 忽略 */ });
      }
    }, Math.max(3000, App.pollInterval));
  }

  function restartPolling(intervalMs) {
    startGlobalPoll(intervalMs);
  }

  /* ============================ 启动 ============================ */
  function boot() {
    FX.attachRipple(document);
    init();
    // 首次进入给出提示（配置缺失时）
    U.getJSON('/api/admin/status').then(function (data) {
      var configured = (data.bots || []).filter(function (bot) { return bot.configured; });
      if (!configured.length) {
        U.toast('还没有配置机器人：请在「设置 → 机器人账号」填写 AppID / AppSecret', 'warn', 8000);
      }
      if (data.ai && !data.ai.usable) {
        U.toast('内置 AI 未配置完整，普通消息将交给插件/关键词回复', 'warn', 6000);
      }
    }).catch(function () { /* 忽略 */ });
  }

  App.go = go;
  App.restartPolling = restartPolling;
  App.refreshPage = refreshPage;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
    // 防止整个对象被别人替换掉（对象内部字段照旧可改，见 util.js 的 protectGlobal）
  if (typeof global.__qbmProtect === 'function') global.__qbmProtect('App', App);
  else global.App = App;
})(window);
