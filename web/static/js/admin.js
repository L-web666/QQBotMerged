/* ==========================================================================
   运维页面：运行状态 / 统计 / 插件管理 / 日志查看 / 上下文 / 指令面板 / 媒体留存
   ========================================================================== */
(function (global) {
  'use strict';
  var U = global.Util;

  var Admin = {
    plugins: [],
    pluginChanges: {},
    pluginServer: {},
    logTimer: null,
    statusTimer: null
  };

  /* ============================ 运行状态 ============================ */
  function kv(label, value, cls) {
    return '<div class="kv"><div class="k">' + U.esc(label) + '</div><div class="v ' +
      (cls || '') + '">' + U.esc(value === undefined || value === null ? '--' : value) + '</div></div>';
  }

  function loadStatus() {
    return U.getJSON('/api/admin/status').then(function (data) {
      var grid = U.el('statusGrid');
      grid.innerHTML = [
        kv('运行时长', U.humanDuration(data.uptime_seconds)),
        kv('当前访问地址', data.web_url || '--'),
        kv('机器人', (data.online_count || 0) + ' / ' + (data.bot_count || 0) + ' 在线',
           data.online_count ? 'ok-text' : 'warn-text'),
        kv('内置 AI', data.ai && data.ai.usable ? (data.ai.model || '可用') :
           (data.ai && data.ai.enabled ? '未配置完整' : '已关闭'),
           data.ai && data.ai.usable ? 'ok-text' : 'warn-text'),
        kv('存储', (data.storage && data.storage.kind === 'sqlite' ? 'SQLite' : '内存') +
           ' · ' + ((data.storage && data.storage.messages) || 0) + ' 条消息'),
        kv('会话数', (data.storage && data.storage.conversations) || 0),
        kv('插件', (data.plugins || 0) + ' 个'),
        kv('消息队列', (data.queue || 0) + ' 条待处理'),
        kv('媒体留存', (data.media && data.media.stats && Object.keys(data.media.stats).length)
           ? Object.keys(data.media.stats).map(function (key) {
               return key + ':' + data.media.stats[key].count;
             }).join(' ') : '无'),
        kv('存图开关', data.media && data.media.save_enabled ? '已开启' : '已关闭',
           data.media && data.media.save_enabled ? 'ok-text' : 'warn-text'),
        kv('服务器时间', data.beijing_time),
        kv('版本', data.version)
      ].join('');

      if (data.bots_disabled) {
        U.el('statusGrid').insertAdjacentHTML('beforeend',
          '<div class="hint warn-text">⚠️ 当前以调试模式（--no-bots）启动：不会连接 QQ，' +
          '保存设置也不会自动连上。要真正收发消息，请关掉程序后用 python run.py 重新启动。</div>');
      }

      var cards = (data.bots || []).map(function (bot) {
        var events = bot.events || {};
        var eventKeys = Object.keys(events);
        var eventText = eventKeys.map(function (name) {
          return name + ' ×' + events[name];
        }).join(' · ');
        return '<div class="card" style="margin:0">' +
          '<h3>' + (bot.online ? '🟢' : '⚪') + ' ' + U.esc(bot.name) + '</h3>' +
          '<div class="tiny">AppID：' + U.esc(bot.app_id || '未配置') +
          (bot.sandbox ? ' · 沙箱' : '') + '</div>' +
          '<div class="tiny">状态：' + U.esc(bot.state) + ' · intents ' + U.esc(bot.intents) + '</div>' +
          '<div class="tiny">会话：' + U.esc(String(bot.session_id || '--').slice(0, 18)) +
          ' · seq ' + U.esc(bot.last_seq) + ' · 心跳 ' + U.esc(bot.heartbeat_interval) + 's</div>' +
          '<div class="tiny">Token 剩余：' + U.esc(bot.token_remaining) + ' 秒 · 已发送 ' +
          U.esc(bot.sent) + ' · 错误 ' + U.esc(bot.errors) + '</div>' +
          (bot.last_error ? '<div class="tiny err-text">最近错误：' + U.esc(bot.last_error) + '</div>' : '') +
          (eventText ? '<div class="tiny">已收事件：' + U.esc(eventText) + '</div>' : '') +
          (bot.all_message_mode
            ? '<div class="tiny warn-text">ℹ️ 已开启「接收所有消息」：群里每条消息都会推给机器人，' +
              '此时机器人只在自己被 @ 时回复（可在「群管理」里关掉“需要 @ 才回复”让它参与全部聊天）。</div>'
            : '') +
          '<div class="row" style="margin-top:8px;display:flex;gap:6px">' +
          '<button class="btn tiny" data-botact="restart" data-botid="' + U.esc(bot.id) + '">重连</button>' +
          '<button class="btn tiny" data-botact="stop" data-botid="' + U.esc(bot.id) + '">断开</button>' +
          '<button class="btn tiny" data-botact="start" data-botid="' + U.esc(bot.id) + '">连接</button>' +
          '</div></div>';
      }).join('');
      U.el('botCards').innerHTML = cards || '<div class="tiny">还没有配置任何机器人</div>';
      U.el('statusLogs').textContent = data.logs || '（暂无日志）';
      bindBotActions();
      loadCloud();
      return data;
    }).catch(function (error) {
      U.el('statusGrid').innerHTML = '<div class="err-text">加载失败：' + U.esc(error.message) + '</div>';
    });
  }

  function bindBotActions() {
    var nodes = U.el('botCards').querySelectorAll('[data-botact]');
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].addEventListener('click', function () {
        var action = this.getAttribute('data-botact');
        var botId = this.getAttribute('data-botid');
        U.postJSON('/api/admin/receiver/' + encodeURIComponent(botId) + '/' + action, {})
          .then(function (data) {
            U.toast(data.message || '已执行', 'ok');
            setTimeout(loadStatus, 1200);
          })
          .catch(function (error) { U.toast(error.message, 'err'); });
      });
    }
  }

  /* ============================ 云同步 ============================ */
  function loadCloud() {
    return U.getJSON('/api/admin/cloud').then(function (data) {
      var grid = U.el('cloudGrid');
      if (!data.enabled) {
        grid.innerHTML = kv('状态', '未启用', 'warn-text') +
          kv('说明', '在「设置 → 云同步」里填写 Cloudflare D1 凭据并开启');
        U.el('cloudHint').textContent = '云同步会把上下文、插件数据、统计等同步到 Cloudflare D1，' +
          '换服务器后可自动恢复；config.json（含密钥）与消息库/媒体默认不上云。';
        return data;
      }
      var last = data.last_result || {};
      grid.innerHTML = [
        kv('状态', data.paused ? '已暂停' : '正常', data.paused ? 'warn-text' : 'ok-text'),
        kv('同步间隔', data.interval_text),
        kv('上次同步', last.message || '尚未同步'),
        kv('累计上传', data.uploaded),
        kv('累计恢复', data.downloaded),
        kv('累计删除', data.deleted),
        kv('跳过', data.skipped),
        kv('失败', data.errors, data.errors ? 'err-text' : ''),
        kv('首次同步', data.first_sync_done ? '已完成' : '进行中'),
        kv('日志上云', data.upload_logs ? '是' : '否')
      ].join('');
      U.el('cloudHint').textContent = data.configured
        ? ('参与同步：' + (data.sync_paths || []).join('、'))
        : '⚠️ 凭据不完整，请到「设置 → 云同步」填写 account_id / database_id / api_token';
      if (last.out_of_scope) {
        // 同一个 D1 库如果被别的程序共用，里面会有别的程序的日志/锁文件记录，这些会被跳过
        U.el('cloudHint').textContent += ' · 云端另有 ' + last.out_of_scope +
          ' 条记录不属于本程序（其它程序写的日志/锁文件等），已跳过、不算失败';
      }
      if (data.paused && data.pause_reason) {
        U.el('cloudHint').textContent += ' · ' + data.pause_reason + ' ' + (data.resume_text || '');
      }
      return data;
    }).catch(function (error) {
      U.el('cloudGrid').innerHTML = '<div class="err-text">云同步状态加载失败：' +
        U.esc(error.message) + '</div>';
    });
  }

  function cloudAction(action, label) {
    U.postJSON('/api/admin/cloud/' + action, {})
      .then(function (data) {
        U.toast((data.message || label || '已执行'), data.ok === false ? 'warn' : 'ok', 6000);
        loadCloud();
        loadStatus();
      })
      .catch(function (error) { U.toast(error.message, 'err', 6000); });
  }

  /* ============================ 统计 ============================ */
  var STAT_LABELS = {
    messages: '收到消息', messages_group: '群聊消息', messages_c2c: '私聊消息',
    messages_with_file: '带附件消息', ai_calls: 'AI 调用', ai_errors: 'AI 失败',
    keyword_hits: '关键词命中', commands: '指令执行', plugin_replies: '插件回复',
    filtered: '过滤无意义', rate_limited: '限速拦截', sensitive_blocked: '敏感词拦截',
    sensitive_masked: '输出打码', replies: '已回复', busy_replies: '繁忙提示',
    process_errors: '处理异常', media_saved: '媒体已留存', media_failed: '媒体留存失败',
    muted_blocked: '禁言成员拦截', sent_text: '发送文本', sent_image: '发送图片',
    sent_file: '发送文件', duplicates: '重复消息已忽略',
    recalled: '撤回自己消息', recalled_member: '撤回群成员消息', recall_failed: '撤回失败'
  };

  function statsKv(obj) {
    var keys = Object.keys(obj || {});
    if (!keys.length) return '<div class="tiny">暂无数据</div>';
    keys.sort(function (a, b) { return (obj[b] || 0) - (obj[a] || 0); });
    return keys.map(function (key) {
      return kv(STAT_LABELS[key] || key, obj[key]);
    }).join('');
  }

  function loadStats() {
    return U.getJSON('/api/admin/stats').then(function (data) {
      U.el('statsToday').innerHTML = statsKv(data.today || {});
      U.el('statsTotal').innerHTML = statsKv(data.totals || {});
      var recent = data.recent || [];
      var maxMessages = 1;
      for (var i = 0; i < recent.length; i++) {
        maxMessages = Math.max(maxMessages, recent[i].messages || 0, recent[i].ai_calls || 0,
          recent[i].replies || 0);
      }
      var chart = recent.map(function (day) {
        function bar(value, color) {
          var height = Math.round((value || 0) / maxMessages * 110);
          return '<div class="chart-bar" style="width:12px;height:' + height +
            'px;border-radius:6px 6px 2px 2px;background:' + color +
            ';align-self:flex-end" title="' + value + '"></div>';
        }
        return '<div style="display:flex;flex-direction:column;align-items:center;gap:4px;flex:1">' +
          '<div style="display:flex;gap:3px;align-items:flex-end;height:120px">' +
          bar(day.messages, 'linear-gradient(180deg,#818cf8,#4f46e5)') +
          bar(day.ai_calls, 'linear-gradient(180deg,#c084fc,#7c3aed)') +
          bar(day.replies, 'linear-gradient(180deg,#86efac,#16a34a)') +
          '</div><div class="tiny">' + U.esc(day.label) + '</div></div>';
      }).join('');
      U.el('statsChart').innerHTML =
        '<div style="display:flex;gap:6px;align-items:flex-end;height:130px">' + chart + '</div>' +
        '<div class="tiny" style="margin-top:8px">🟦 消息　🟪 AI 调用　🟩 回复</div>';
      var storage = data.storage || {};
      if (storage.total !== undefined) {
        U.el('statsTotal').insertAdjacentHTML('afterbegin',
          kv('消息库总条数', storage.total) + kv('今日消息', storage.today) +
          kv('媒体文件', storage.media_count) + kv('媒体占用', U.humanSize(storage.media_bytes)) +
          kv('会话数', storage.conversations));
      }
      return data;
    }).catch(function (error) {
      U.toast('统计加载失败：' + error.message, 'err');
    });
  }

  /* ============================ 插件 ============================ */
  function loadPlugins() {
    return U.getJSON('/api/admin/plugins').then(function (data) {
      Admin.plugins = data.plugins || [];
      Admin.pluginServer = {};
      for (var i = 0; i < Admin.plugins.length; i++) {
        Admin.pluginServer[Admin.plugins[i].name] = !!Admin.plugins[i].disabled;
      }
      renderPluginDiagnostics(data);
      renderPlugins();
      updatePluginPending();      // 重新加载后要刷新“待保存”提示，否则保存完还挂着旧提示
      return data;
    }).catch(function (error) {
      U.el('pluginsTable').innerHTML = '<tr><td class="err-text">加载失败：' +
        U.esc(error.message) + '</td></tr>';
    });
  }

  function renderPluginDiagnostics(data) {
    var diag = data.diagnostics || {};
    var parts = [];
    parts.push('插件目录：<code>' + U.esc(data.dir || '') + '</code>' +
      (diag.dir_exists === false ? ' <b class="err-text">（目录不存在）</b>' : ''));
    parts.push('插件系统' + (data.enabled ? '已启用' : '<b class="warn-text">已禁用</b>'));
    parts.push('已加载 ' + Admin.plugins.length + ' 个');
    if (diag.counts) {
      var kinds = [];
      for (var key in diag.counts) { if (diag.counts.hasOwnProperty(key)) kinds.push(key + ' ' + diag.counts[key] + ' 个'); }
      if (kinds.length) parts.push(kinds.join(' / '));
    }
    if (diag.astrbot_ready === false) {
      parts.push('<b class="warn-text">AstrBot 兼容层未就绪</b>');
    }
    if (diag.discovered && diag.discovered.length) {
      parts.push('扫描到 ' + diag.discovered.length + ' 个候选文件');
    }
    U.el('pluginsDir').innerHTML = parts.join(' · ') +
      '<div class="tiny" style="margin-top:4px">本程序只支持 <b>AstrBot 官方插件格式</b>：' +
      '把插件目录放进 <code>plugins/</code>（目录里要有 <code>metadata.yaml</code> 与 ' +
      '<code>main.py</code>，可选 <code>_conf_schema.json</code>）。' +
      '插件配置在 <code>data/config/&lt;插件名&gt;_config.json</code>，' +
      '插件数据在 <code>data/plugin_data/&lt;插件名&gt;/</code>。' +
      '插件是任意 Python 代码，请只放你信任的插件；' +
      '插件页会列出每个插件注册的处理器与用到的、本程序暂不支持的能力。</div>';

    // 加载失败 / 被跳过的文件：直接展示原因（以前只写日志，页面上完全看不出来）
    var issues = diag.issues || [];
    var box = U.el('pluginIssues');
    if (box) {
      var html = '';
      if (issues.length) {
        html += '<div class="card" style="border-color:rgba(220,38,38,.35)">' +
          '<h3>⚠️ 有 ' + issues.length + ' 处问题需要处理</h3>' +
          issues.map(function (item) {
            return '<div class="field-tip"><b>' + U.esc(item.name) + '</b> ' +
              U.esc(item.reason) + '</div>';
          }).join('') +
          '<div class="field-tip">修好后点右上角「丢弃修改并重载」重新扫描。</div></div>';
      }
      // 目录里有插件但一个都没加载出来 → 给出可能原因与强制加载入口
      if (!Admin.plugins.length) {
        var liveScan = diag.live_scan || [];
        html += '<div class="card" style="border-color:rgba(217,119,6,.4)">' +
          '<h3>没有加载到任何插件</h3>' +
          '<div class="field-tip">插件目录：<code>' + U.esc(diag.dir || '') + '</code>' +
          (diag.dir_exists === false ? '（<b class="err-text">目录不存在</b>）' : '') + '</div>' +
          (liveScan.length
            ? '<div class="field-tip">该目录里识别到 ' + liveScan.map(function (item) {
                return '<code>' + U.esc(item.file) + '</code>';
              }).join('、') + '，但都没能加载 —— 请看上面的问题说明。</div>'
            : '<div class="field-tip">该目录里没有识别到任何插件。' +
              '请确认插件是放在 <b>本程序</b> 插件目录下的标准 AstrBot 插件目录' +
              '（<code>&lt;插件名&gt;/metadata.yaml</code> + <code>main.py</code>）。</div>') +
          (data.enabled === false
            ? '<div class="field-tip warn-text">插件系统当前是关闭状态，' +
              '请到「设置 → 插件系统」勾选启用，或点下面的强制加载。</div>' +
              '<button class="btn tiny" id="pluginsForce">⚡ 忽略开关强制加载一次</button>'
            : '') +
          '</div>';
      }
      box.innerHTML = html;
      var force = U.el('pluginsForce');
      if (force) {
        force.addEventListener('click', function () {
          U.postJSON('/api/admin/plugins/force_load', {}).then(function (result) {
            U.toast(result.message || '已强制加载', result.count ? 'ok' : 'warn', 6000);
            loadPlugins();
          }).catch(function (error) { U.toast(error.message, 'err'); });
        });
      }
    }
  }

  function renderPlugins() {
    var rows = Admin.plugins.map(function (plugin) {
      var staged = Admin.pluginChanges.hasOwnProperty(plugin.name);
      var disabled = staged ? Admin.pluginChanges[plugin.name] : Admin.pluginServer[plugin.name];
      var statusBadge = staged
        ? '<span class="badge warn">' + (disabled ? '待停用' : '待启用') + '</span>'
        : (disabled ? '<span class="badge err">已停用</span>' : '<span class="badge ok">运行中</span>');
      var rules = [];
      if (plugin.commands && plugin.commands.length) rules.push('指令：' + plugin.commands.join(' '));
      if (plugin.keywords && plugin.keywords.length) rules.push('关键词：' + plugin.keywords.join(' '));
      if (plugin.has_match) rules.push('自定义 match()');
      if (plugin.handler_kinds && plugin.handler_kinds.length) {
        rules.push('处理器：' + plugin.handler_kinds.join(' / '));
      }
      if (plugin.unsupported && plugin.unsupported.length) {
        rules.push('<span class="warn-text">不支持：' + plugin.unsupported.join('、') + '</span>');
      }
      var formatBadge = '<span class="badge ok">AstrBot</span>';
      var riskBadge = '';
      var metaBits = [];
      if (plugin.astrbot_version) metaBits.push('要求 AstrBot ' + plugin.astrbot_version);
      if (plugin.support_platforms && plugin.support_platforms.length) {
        metaBits.push('平台：' + plugin.support_platforms.join('/'));
      }
      if (plugin.repo) {
        metaBits.push('<a href="' + U.esc(plugin.repo) + '" target="_blank" rel="noopener">仓库</a>');
      }
      var kindText = 'AstrBot 目录';
      return '<tr' + (staged ? ' style="background:rgba(217,119,6,.08)"' : '') + '>' +
        '<td><b>' + U.esc(plugin.title || plugin.name) + '</b> ' + formatBadge + riskBadge +
        '<div class="tiny">' + U.esc(plugin.file || '') + ' · v' + U.esc(plugin.version || '') +
        (plugin.author ? ' · ' + U.esc(plugin.author) : '') +
        (metaBits.length ? ' · ' + metaBits.join(' · ') : '') + '</div></td>' +
        '<td>' + U.esc(plugin.description || '（无说明）') + '</td>' +
        '<td class="nowrap">' + U.esc(kindText) + '</td>' +
        '<td>' + (rules.length ? rules.join('；') : '<span class="tiny">无匹配规则</span>') + '</td>' +
        '<td class="nowrap">' + statusBadge + '</td>' +
        '<td class="cell-act"><button class="btn tiny" data-plugin="' + U.esc(plugin.name) +
        '" data-disable="' + (disabled ? '0' : '1') + '">' +
        (disabled ? '▶️ 启用' : '⏸️ 停用') + '</button></td></tr>';
    }).join('');
    U.el('pluginsTable').innerHTML =
      '<colgroup><col style="width:22%"><col style="width:30%"><col style="width:9%">' +
      '<col style="width:20%"><col style="width:9%"><col style="width:10%"></colgroup>' +
      '<thead><tr><th>插件</th><th>说明</th><th>类型</th><th>匹配规则</th><th>状态</th><th>操作</th></tr></thead>' +
      '<tbody>' + (rows || '<tr><td colspan="6" class="tiny">plugins/ 目录里还没有插件</td></tr>') + '</tbody>';
    var buttons = U.el('pluginsTable').querySelectorAll('[data-plugin]');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener('click', function () {
        var name = this.getAttribute('data-plugin');
        var disable = this.getAttribute('data-disable') === '1';
        if (Admin.pluginServer[name] === disable) delete Admin.pluginChanges[name];
        else Admin.pluginChanges[name] = disable;
        renderPlugins();
        updatePluginPending();
      });
    }
  }

  function updatePluginPending() {
    var count = Object.keys(Admin.pluginChanges).length;
    var hint = U.el('pluginsPending');
    if (count) {
      hint.innerHTML = '<span class="warn-text">⚠️ 有 ' + count +
        ' 项修改待保存（点右上角「保存插件设置」才会生效）</span>';
      U.el('pluginsSave').textContent = '💾 保存插件设置（' + count + ' 项）';
    } else {
      hint.textContent = '';
      U.el('pluginsSave').textContent = '💾 保存插件设置';
    }
  }

  function savePlugins() {
    var changes = Admin.pluginChanges;
    if (!Object.keys(changes).length) { U.toast('没有需要保存的插件修改', 'warn'); return Promise.resolve(); }
    return U.postJSON('/api/admin/plugins/apply', { changes: changes }).then(function (data) {
      U.toast(data.message || '已保存', 'ok');
      Admin.pluginChanges = {};
      updatePluginPending();      // 立刻清掉“有 N 项修改待保存”
      return loadPlugins();
    }).catch(function (error) { U.toast(error.message, 'err'); });
  }

  function reloadPlugins() {
    Admin.pluginChanges = {};
    return U.postJSON('/api/admin/plugins/reload', {}).then(function (data) {
      U.toast(data.message || '已重载', data.count ? 'ok' : 'warn', 6000);
      Admin.plugins = data.plugins || [];
      Admin.pluginServer = {};
      for (var i = 0; i < Admin.plugins.length; i++) {
        Admin.pluginServer[Admin.plugins[i].name] = !!Admin.plugins[i].disabled;
      }
      renderPluginDiagnostics({
        dir: data.diagnostics && data.diagnostics.dir,
        enabled: true,
        plugins: Admin.plugins,
        diagnostics: data.diagnostics,
      });
      renderPlugins();
      updatePluginPending();
      return data;
    }).catch(function (error) { U.toast(error.message, 'err'); });
  }

  /* ============================ 日志 ============================ */
  function loadLogs() {
    var level = U.el('logLevel').value;
    var lines = U.el('logLines').value;
    var keyword = (U.el('logKeyword').value || '').trim();
    var file = U.el('logFile').value || '';
    var url = '/api/admin/logs?lines=' + encodeURIComponent(lines) +
      '&level=' + encodeURIComponent(level) +
      '&keyword=' + encodeURIComponent(keyword) +
      '&file=' + encodeURIComponent(file);
    return U.getJSON(url).then(function (data) {
      var text = (data.lines || []).join('\n');
      U.el('logsView').textContent = text || '（该筛选条件下暂无日志）';
      U.el('logMeta').textContent = '文件 ' + (data.file || '--') + ' · 共 ' + (data.total || 0) +
        ' 行 · 命中 ' + (data.matched || 0) + ' 行';
      if (data.file) U.el('logFile').value = data.file;
      return data;
    }).catch(function (error) { U.el('logsView').textContent = '加载失败：' + error.message; });
  }

  function loadLogFiles() {
    return U.getJSON('/api/admin/logs/list').then(function (data) {
      var select = U.el('logFile');
      var current = select.value;
      select.innerHTML = (data.files || []).map(function (file) {
        return '<option value="' + U.esc(file.name) + '">' + U.esc(file.name) +
          ' (' + U.esc(file.size_text) + ')' + (file.current ? ' · 当前' : '') + '</option>';
      }).join('');
      if (current) select.value = current;
      return data;
    }).catch(function () { /* 忽略 */ });
  }

  function downloadLog() {
    var name = U.el('logFile').value;
    if (!name) { U.toast('请先选择日志文件', 'warn'); return; }
    window.location.href = U.apiUrl('/api/admin/logs/download?name=' + encodeURIComponent(name));
  }

  function toggleLogAuto() {
    if (U.el('logAuto').checked) {
      Admin.logTimer = setInterval(function () {
        var active = document.querySelector('.nav-item.active');
        if (active && active.getAttribute('data-page') === 'logs' && !document.hidden) loadLogs();
      }, 5000);
    } else if (Admin.logTimer) {
      clearInterval(Admin.logTimer);
      Admin.logTimer = null;
    }
  }

  /* ============================ 上下文 ============================ */
  function contextTable(rows, scope) {
    if (!rows || !rows.length) return '<tbody><tr><td class="tiny">暂无文件</td></tr></tbody>';
    return '<thead><tr><th>文件</th><th>条数</th><th>大小</th><th>最后修改</th><th></th></tr></thead><tbody>' +
      rows.map(function (row) {
        return '<tr><td><b>' + U.esc(row.openid || row.name) + '</b>' +
          '<div class="tiny">机器人 ' + U.esc(row.bot_id || '--') + ' · ' + U.esc(row.name) + '</div></td>' +
          '<td class="nowrap">' + row.entries + '</td>' +
          '<td class="nowrap">' + U.humanSize(row.size) + '</td>' +
          '<td class="nowrap">' + U.esc(row.mtime_text) + '</td>' +
          '<td class="cell-act"><button class="btn tiny" data-ctx="' + U.esc(row.name) +
          '" data-scope="' + U.esc(scope) + '">删除</button></td></tr>';
      }).join('') + '</tbody>';
  }

  function loadContext() {
    return U.getJSON(U.qs('/api/admin/context')).then(function (data) {
      U.el('ctxPrivateCount').textContent = (data.private || []).length;
      U.el('ctxGroupCount').textContent = (data.group || []).length;
      U.el('ctxPrivate').innerHTML = contextTable(data.private, 'private');
      U.el('ctxGroup').innerHTML = contextTable(data.group, 'group');
      var hint = U.el('ctxHint');
      if (hint) {
        hint.innerHTML = '当前机器人：<b>' + U.esc(data.bot_name || data.bot_id || '（未选择）') +
          '</b>（在左侧导航栏顶部切换）· 这里只列出<b>该机器人</b>的上下文，' +
          '删除 / 清空都只影响它，不会动到其它机器人的文件。';
      }
      var buttons = U.el('page-context').querySelectorAll('[data-ctx]');
      for (var i = 0; i < buttons.length; i++) {
        buttons[i].addEventListener('click', function () {
          var name = this.getAttribute('data-ctx');
          var scope = this.getAttribute('data-scope');
          U.confirm({ title: '删除上下文', text: '确定删除 ' + name + ' 吗？该会话将失去记忆。' })
            .then(function (ok) {
              if (!ok) return;
              U.postJSON('/api/admin/context/delete',
                         { scope: scope, name: name, bot_id: data.bot_id || '' })
                .then(function (result) { U.toast(result.message || '已删除', 'ok'); loadContext(); })
                .catch(function (error) {
                  U.toast(error.message, 'err', 8000);
                  // 被拒绝通常是因为页面还是切换机器人之前的旧列表 → 立刻重新加载
                  loadContext();
                });
            });
        });
      }
      return data;
    }).catch(function (error) { U.toast('上下文加载失败：' + error.message, 'err'); });
  }

  function clearAllContext() {
    var botId = (U.ActiveBot && U.ActiveBot.id) || '';
    U.confirm({
      title: '清空当前机器人的上下文',
      html: '将删除<b>当前机器人（' + U.esc(botId || '未选择') + '）</b>所有会话的对话记忆' +
        '（消息记录不受影响）。<br>' +
        '<span class="tiny">其它机器人的上下文不会受影响；要清别的机器人，' +
        '先在左侧导航栏顶部切换过去。</span><br>' +
        '<label class="chk" style="margin-top:8px"><input type="checkbox" id="ctxAllChk"> ' +
        '我确认清空当前机器人的上下文</label>',
      okText: '清空当前机器人'
    }).then(function (ok) {
      if (!ok) return;
      var chk = U.el('ctxAllChk');
      if (!chk || !chk.checked) { U.toast('请先勾选确认', 'warn'); return; }
      U.postJSON('/api/admin/context/delete', { all: true, bot_id: botId })
        .then(function (data) { U.toast(data.message || '已清空', 'ok'); loadContext(); })
        .catch(function (error) { U.toast(error.message, 'err'); });
    });
  }

  /* ============================ 指令面板 ============================ */
  function loadPanels() {
    return U.getJSON('/api/admin/panels').then(function (data) {
      var panels = data.panels || [];
      var rows = panels.map(function (panel) {
        var scope = panel.scope || panel.panel_scope || '';
        return '<tr><td>' + U.esc(scope === 'group' ? '群聊' : (scope === 'c2c' ? '私聊' : scope || '--')) +
          '</td><td class="nowrap"><code>' + U.esc(panel.panel_id || panel.id || '') + '</code></td>' +
          '<td>' + U.esc(panel.remark || panel.panel_remark || '') + '</td>' +
          '<td class="cell-act"><button class="btn tiny danger" data-panel="' +
          U.esc(panel.panel_id || panel.id || '') + '">删除</button></td></tr>';
      }).join('');
      U.el('panelsTable').innerHTML =
        '<thead><tr><th>场景</th><th>面板 ID</th><th>备注</th><th>操作</th></tr></thead><tbody>' +
        (rows || '<tr><td colspan="4" class="tiny">还没有注册任何指令面板</td></tr>') + '</tbody>';
      var diag = data.diag || {};
      var diagText = Object.keys(diag).filter(function (key) { return diag[key]; })
        .map(function (key) { return '【' + key + '】' + diag[key]; }).join('\n\n');
      U.el('panelsDiag').textContent = diagText;
      var buttons = U.el('panelsTable').querySelectorAll('[data-panel]');
      for (var i = 0; i < buttons.length; i++) {
        buttons[i].addEventListener('click', function () {
          var panelId = this.getAttribute('data-panel');
          U.confirm({ title: '删除指令面板', text: '确定删除面板 ' + panelId + ' 吗？', okText: '删除' })
            .then(function (ok) {
              if (!ok) return;
              U.postJSON('/api/admin/panels/delete', { panel_id: panelId })
                .then(function (result) { U.toast(result.message || '已删除', 'ok'); loadPanels(); })
                .catch(function (error) { U.toast(error.message, 'err'); });
            });
        });
      }
      return data;
    }).catch(function (error) { U.toast('面板查询失败：' + error.message, 'err'); });
  }

  function reloadPanels() {
    return U.postJSON('/api/admin/panels/reload', {}).then(function (data) {
      U.toast(data.message || '已重新注册', 'ok');
      return loadPanels();
    }).catch(function (error) { U.toast(error.message, 'err', 6000); });
  }

  /* ============================ 媒体留存 ============================ */
  function loadMedia() {
    return U.getJSON('/api/admin/media?limit=200').then(function (data) {
      var stats = data.stats || {};
      U.el('mediaStats').innerHTML = Object.keys(stats).map(function (state) {
        return kv(state === 'done' ? '已留存' : (state === 'failed' ? '留存失败' : state),
          stats[state].count + ' 个 · ' + U.humanSize(stats[state].bytes),
          state === 'done' ? 'ok-text' : 'warn-text');
      }).join('') || '<div class="tiny">暂无媒体记录</div>';
      U.el('mediaHint').innerHTML = '保存目录：<code>' + U.esc(data.dir) + '</code> · ' +
        (data.save_enabled ? '收到图片会自动下载留存' :
          '<b class="warn-text">自动留存已关闭</b>（设置 → 功能开关）');
      var items = data.media || [];
      U.el('mediaGrid').innerHTML = items.length ? items.map(function (item) {
        var isImage = /\.(png|jpe?g|gif|webp|bmp|ico)$/i.test(item.local_name || '');
        return '<div class="media-item">' +
          (isImage && item.local_url
            ? '<img src="' + U.esc(U.apiUrl(item.local_url)) + '" loading="lazy" ' +
              'data-url="' + U.esc(item.local_url) + '" alt="媒体">'
            : '<div style="height:110px;display:grid;place-items:center;font-size:26px">📄</div>') +
          '<div class="meta">' + U.esc(item.file_name || item.local_name || '') +
          '<br>' + U.humanSize(item.size) + ' · ' + U.esc(item.state) +
          '<br>' + U.esc(item.source === 'sent' ? '发送' : '收到') + '</div></div>';
      }).join('') : '<div class="tiny">还没有留存任何媒体文件</div>';
      var images = U.el('mediaGrid').querySelectorAll('img[data-url]');
      for (var i = 0; i < images.length; i++) {
        images[i].addEventListener('click', function () {
          U.el('lightboxImg').src = U.apiUrl(this.getAttribute('data-url'));
          U.el('lightboxCap').textContent = this.getAttribute('data-url');
          U.el('lightbox').style.display = 'flex';
        });
      }
      return data;
    }).catch(function (error) { U.toast('媒体列表加载失败：' + error.message, 'err'); });
  }

  function cleanMedia() {
    U.confirm({
      title: '清理过期媒体',
      html: '将删除超过保留天数（设置 → 发送策略 → 上传文件保留天数）的本地媒体文件。' +
        '<br><label class="chk" style="margin-top:8px"><input type="number" id="mediaDays" value="7" ' +
        'min="1" style="width:80px"> 天前的文件</label>',
      okText: '开始清理'
    }).then(function (ok) {
      if (!ok) return;
      var days = parseInt((U.el('mediaDays') || {}).value || '7', 10) || 7;
      var url = U.apiUrl('/api/admin/media?days=' + days);
      fetch(url, { method: 'DELETE', credentials: 'same-origin' })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          U.toast(data.message || '已清理', 'ok');
          loadMedia();
        })
        .catch(function (error) { U.toast('清理失败：' + error.message, 'err'); });
    });
  }

  /* ============================ 初始化 ============================ */
  function init() {
    U.el('statusRefresh').addEventListener('click', loadStatus);
    U.el('statsRefresh').addEventListener('click', loadStats);
    U.el('cloudTest').addEventListener('click', function () { cloudAction('test', '测试完成'); });
    U.el('cloudSyncNow').addEventListener('click', function () { cloudAction('sync', '同步完成'); });
    U.el('cloudResume').addEventListener('click', function () { cloudAction('resume', '已恢复'); });
    U.el('pluginsRefresh').addEventListener('click', reloadPlugins);
    U.el('pluginsSave').addEventListener('click', savePlugins);
    U.el('logRefresh').addEventListener('click', function () { loadLogFiles().then(loadLogs); });
    U.el('logLevel').addEventListener('change', loadLogs);
    U.el('logLines').addEventListener('change', loadLogs);
    U.el('logKeyword').addEventListener('input', U.debounce(loadLogs, 400));
    U.el('logFile').addEventListener('change', loadLogs);
    U.el('logAuto').addEventListener('change', toggleLogAuto);
    U.el('logDownload').addEventListener('click', downloadLog);
    U.el('contextRefresh').addEventListener('click', loadContext);
    U.el('contextClearAll').addEventListener('click', clearAllContext);
    // 切换机器人后：上下文页要跟着换成新机器人的文件（以前必须手动刷新，
    // 而且不刷新时还留着上一个机器人的列表，容易删错对象）
    U.ActiveBot.onChange(function () {
      loadContext();
      if (U.el('ctxHint')) {
        U.el('ctxHint').innerHTML = '<span class="tiny">已切换机器人，正在重新加载上下文…</span>';
      }
    });
    U.el('panelsLoad').addEventListener('click', loadPanels);
    U.el('panelsReload').addEventListener('click', reloadPanels);
    U.el('mediaRefresh').addEventListener('click', loadMedia);
    U.el('mediaClean').addEventListener('click', cleanMedia);
  }

  Admin.init = init;
  Admin.loadStatus = loadStatus;
  Admin.loadCloud = loadCloud;
  Admin.loadStats = loadStats;
  Admin.loadPlugins = loadPlugins;
  Admin.loadLogs = loadLogs;
  Admin.loadLogFiles = loadLogFiles;
  Admin.loadContext = loadContext;
  Admin.loadPanels = loadPanels;
  Admin.loadMedia = loadMedia;
  global.Admin = Admin;
})(window);
