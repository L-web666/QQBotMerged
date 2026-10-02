/* ==========================================================================
   设置页：按类别聚合两个程序的所有可配置项（由后端字段元信息驱动渲染）
   支持控件：text / number / bool / textarea / secret / list / dict / enum /
             bots（机器人账号）/ scheduler_tasks（定时任务）/ panel_commands（指令面板）
   ========================================================================== */
(function (global) {
  'use strict';
  var U = global.Util;

  var CLEAR_MARKER = '__CLEAR__';

  var Settings = {
    spec: null,
    dirty: false
  };

  /* ============================ 字段渲染 ============================ */
  function fieldHtml(path, meta, value) {
    var label = meta.label || path;
    var badge = '';
    if (Settings.spec && Settings.spec.restart_required &&
        Settings.spec.restart_required.indexOf(path) >= 0) {
      badge += '<span class="badge warn">需重启</span>';
    }
    if (Settings.spec && Settings.spec.env_overrides &&
        Settings.spec.env_overrides.indexOf(path) >= 0) {
      badge += '<span class="badge">被环境变量覆盖</span>';
    }
    var head = '<div class="field-head"><span class="field-label">' + U.esc(label) + '</span>' +
      badge + '<span class="field-path">' + U.esc(path) + '</span></div>';
    var tip = meta.hint ? '<div class="field-tip">' + U.esc(meta.hint) + '</div>' : '';
    var control = controlHtml(path, meta, value);
    return '<div class="field" data-field="' + U.esc(path) + '">' + head + control + tip + '</div>';
  }

  function controlHtml(path, meta, value) {
    var type = meta.type || 'str';
    if (type === 'bool') {
      return '<label class="chk"><input type="checkbox" data-bool' +
        (value ? ' checked' : '') + '> ' + U.esc(meta.label || '') + '</label>';
    }
    if (type === 'enum') {
      var options = meta.options || [];
      return '<select data-enum>' + options.map(function (pair) {
        var optionValue = Array.isArray(pair) ? pair[0] : pair;
        var optionLabel = Array.isArray(pair) ? pair[1] : pair;
        return '<option value="' + U.esc(optionValue) + '"' +
          (String(value) === String(optionValue) ? ' selected' : '') + '>' +
          U.esc(optionLabel) + '</option>';
      }).join('') + '</select>';
    }
    if (type === 'int' || type === 'float') {
      var step = type === 'float' ? '0.1' : '1';
      return '<input type="number" data-number step="' + step + '"' +
        (meta.min !== undefined ? ' min="' + meta.min + '"' : '') +
        (meta.max !== undefined ? ' max="' + meta.max + '"' : '') +
        ' value="' + U.esc(value === undefined || value === null ? '' : value) + '">';
    }
    if (type === 'textarea') {
      return '<textarea data-text rows="5">' + U.esc(value || '') + '</textarea>';
    }
    if (type === 'secret') {
      return '<div class="row"><input type="password" data-text autocomplete="new-password" ' +
        'placeholder="留空则不修改（当前：' + (value ? '已设置' : '未设置') + '）">' +
        '<label class="chk"><input type="checkbox" data-clear> 清空此值</label></div>';
    }
    if (type === 'list') {
      var items = Array.isArray(value) ? value : [];
      if (!items.length) items = [''];
      return '<div data-listbox>' + items.map(itemRowHtml).join('') +
        '<button class="row-add" type="button" data-addrow="item">➕ 添加一行</button></div>';
    }
    if (type === 'dict') {
      var dict = (value && typeof value === 'object') ? value : {};
      var keys = Object.keys(dict);
      if (!keys.length) {
        return '<div data-dictbox>' + dictRowHtml('', '') +
          '<button class="row-add" type="button" data-addrow="dict">➕ 添加一条</button></div>';
      }
      return '<div data-dictbox>' + keys.map(function (key) {
        return dictRowHtml(key, dict[key]);
      }).join('') + '<button class="row-add" type="button" data-addrow="dict">➕ 添加一条</button></div>';
    }
    if (type === 'bots') {
      return botsEditorHtml(value);
    }
    if (type === 'scheduler_tasks') {
      return tasksEditorHtml(value);
    }
    if (type === 'panel_commands') {
      return commandsEditorHtml(value);
    }
    return '<input type="text" data-text value="' +
      U.esc(value === undefined || value === null ? '' : value) + '">';
  }

  function itemRowHtml(value) {
    return '<div class="item-row"><input type="text" class="item" value="' + U.esc(value || '') +
      '" placeholder="每行一项"><button class="row-del" type="button" data-delrow>✕</button></div>';
  }

  function dictRowHtml(key, value) {
    return '<div class="kv-row"><input type="text" class="kv-key" value="' + U.esc(key || '') +
      '" placeholder="关键词"><input type="text" class="kv-val" value="' + U.esc(value || '') +
      '" placeholder="回复内容"><button class="row-del" type="button" data-delrow>✕</button></div>';
  }

  /* ---------------- 机器人账号 ---------------- */
  function botsEditorHtml(bots) {
    var list = Array.isArray(bots) ? bots : [];
    return '<div data-botsbox>' + list.map(botEditorHtml).join('') + '</div>' +
      '<button class="row-add" type="button" data-addbot>➕ 添加一个机器人</button>' +
      '<div class="field-tip">每个启用的机器人会建立一条独立的 WebSocket 连接，' +
      '可以同时管理多个 QQ 机器人账号。留空的 AppSecret 表示不修改已有值。</div>';
  }

  function botEditorHtml(bot) {
    var data = bot || {};
    return '<div class="bot-editor" data-bot>' +
      '<div class="bot-editor-head">' +
      '<label class="chk"><input type="checkbox" data-bot-enabled' +
      (data.enabled ? ' checked' : '') + '> 启用</label>' +
      '<input class="name" data-bot-name value="' + U.esc(data.name || '') + '" placeholder="显示名称">' +
      '<input data-bot-id value="' + U.esc(data.id || '') + '" placeholder="内部 id（如 bot1）" style="width:110px">' +
      '<button class="row-del" type="button" data-delbot>✕</button>' +
      '</div>' +
      '<div class="row">' +
      '<input data-bot-appid value="' + U.esc(data.app_id || '') + '" placeholder="AppID" style="min-width:150px">' +
      '<input type="password" data-bot-secret placeholder="AppSecret（留空=不修改）" style="min-width:190px">' +
      '<label class="chk" title="勾选后保存会真的清掉这个机器人的 AppSecret（用于停用某段凭据）">' +
      '<input type="checkbox" data-bot-secret-clear> 清空密钥</label>' +
      '<label class="chk"><input type="checkbox" data-bot-sandbox' +
      (data.sandbox ? ' checked' : '') + '> 沙箱</label>' +
      '</div>' +
      '<div class="row" style="margin-top:6px">' +
      '<label class="tiny">intents <input type="number" data-bot-intents value="' +
      U.esc(data.intents === undefined || data.intents === null ? 100663296 : data.intents) +
      '" style="width:160px"></label>' +
      '<label class="tiny">重连次数 <input type="number" data-bot-attempts value="' +
      U.esc(data.reconnect_attempts || 5) + '" style="width:80px"></label>' +
      '<label class="tiny">重连间隔(秒) <input type="number" data-bot-interval value="' +
      U.esc(data.reconnect_interval || 10) + '" style="width:80px"></label>' +
      '</div>' +
      '<div class="field-tip">intents 默认 100663296（群@ + 单聊）。' +
      '要接收群内所有消息需在 QQ 后台申请权限并让群管理员开启“接收所有消息”；' +
      '群成员变动事件可加 1&lt;&lt;24（16777216）。订阅未授权的位置会导致网关拒绝连接，' +
      '程序会自动降级重试。</div>' +
      '</div>';
  }

  /* ---------------- 定时任务 ---------------- */
  function tasksEditorHtml(tasks) {
    var list = Array.isArray(tasks) ? tasks : [];
    var rows = list.length ? list.map(taskRowHtml).join('') : taskRowHtml({});
    return '<div data-tasksbox>' + rows + '</div>' +
      '<button class="row-add" type="button" data-addtask>➕ 添加任务</button>' +
      '<div class="field-tip">时间为北京时间 HH:MM。目标 id 为群 openid 或用户 openid；' +
      '机器人 id 留空表示用第一个可用的机器人。</div>';
  }

  function taskRowHtml(task) {
    var data = task || {};
    return '<div class="task-row">' +
      '<input type="text" class="t-time" value="' + U.esc(data.time || '08:00') + '" placeholder="08:00" style="width:80px">' +
      '<select class="t-type"><option value="group"' + (data.target_type === 'group' ? ' selected' : '') +
      '>群聊</option><option value="c2c"' + (data.target_type === 'c2c' ? ' selected' : '') +
      '>私聊</option></select>' +
      '<input type="text" class="t-bot" value="' + U.esc(data.bot_id || '') + '" placeholder="机器人 id" style="width:90px">' +
      '<input type="text" class="t-id" value="' + U.esc(data.target_id || '') + '" placeholder="目标 openid">' +
      '<input type="text" class="t-content" value="' + U.esc(data.content || '') + '" placeholder="发送内容">' +
      '<button class="row-del" type="button" data-delrow>✕</button></div>';
  }

  /* ---------------- 指令面板 ---------------- */
  function commandsEditorHtml(commands) {
    var list = Array.isArray(commands) ? commands : [];
    var rows = list.length ? list.map(commandRowHtml).join('') : commandRowHtml({});
    return '<div data-cmdsbox>' + rows + '</div>' +
      '<button class="row-add" type="button" data-addcmd>➕ 添加指令</button>' +
      '<div class="field-tip">type=command 填名称与描述；type=link 填名称与链接。' +
      '名称最长 14 字符，描述最长 30 字符（超出会被平台截断）。</div>';
  }

  function commandRowHtml(command) {
    var data = command || {};
    var isLink = String(data.type || 'command') === 'link';
    return '<div class="cmd-row">' +
      '<select class="cmd-type"><option value="command"' + (isLink ? '' : ' selected') +
      '>指令</option><option value="link"' + (isLink ? ' selected' : '') + '>链接</option></select>' +
      '<input type="text" class="cmd-name" value="' + U.esc(data.name || '') + '" placeholder="名称" style="max-width:150px">' +
      '<input type="text" class="cmd-desc" value="' + U.esc(isLink ? (data.link || '') : (data.desc || '')) +
      '" placeholder="描述 / 链接">' +
      '<button class="row-del" type="button" data-delrow>✕</button></div>';
  }

  /* ============================ 整页渲染 ============================ */
  function render() {
    var spec = Settings.spec;
    if (!spec) return;
    var form = U.el('settingsForm');
    var keyword = (U.el('settingsSearch').value || '').trim().toLowerCase();
    var html = [];
    // 提示"设置作用于哪个机器人"：避免改了半天下错对象
    html.push('<div class="card" style="border-color:rgba(79,70,229,.35)">' +
      '<h3>🤖 当前机器人：<b>' + U.esc(spec.active_bot || '（未选择）') + '</b>' +
      ' <span class="badge">可在左侧导航栏顶部切换</span></h3>' +
      '<div class="field-tip">带 <b>「作用于：…」</b> 标记的分组只影响当前机器人；' +
      '带 <b>「全局」</b> 标记的分组对所有机器人共用。' +
      '每个分组右上角的「📋 应用到所有机器人」可把该机器人的设置一键复制给其它机器人。</div>' +
      '</div>');
    // 顶部提示"程序实际在用的地址"：改了端口没重启时最容易在这里看明白
    var runtimeWeb = spec.runtime_web;
    if (runtimeWeb && runtimeWeb.port) {
      var configuredPort = U.getPath(spec.config, 'web.port');
      var mismatch = configuredPort && Number(configuredPort) !== Number(runtimeWeb.port);
      html.push('<div class="card" style="border-color:' +
        (mismatch ? 'rgba(220,38,38,.45)' : 'var(--line)') + '">' +
        '<h3>🌐 当前网页地址</h3>' +
        '<div class="field-tip">程序正在监听：<code>' + U.esc(runtimeWeb.url) + '</code>' +
        (mismatch
          ? '<br><b class="err-text">注意：你配置的端口是 ' + U.esc(configuredPort) +
            '，但程序实际还在 ' + U.esc(runtimeWeb.port) +
            ' 上运行。端口/地址改动必须<b>重启程序</b>才生效 —— 请继续用上面的地址访问，' +
            '重启后再用新端口。</b>'
          : '') +
        '<br>' + U.esc(runtimeWeb.note || '') + '</div></div>');
    }
    // 机器人 id 重复：改设置只会写进第一个，必须让用户看到
    var dupIds = spec.duplicate_bot_ids || [];
    if (dupIds.length) {
      html.push('<div class="card" style="border-color:rgba(220,38,38,.45)">' +
        '<h3>⚠️ 有机器人用了重复的 id</h3>' +
        '<div class="field-tip">这些 id 出现了多次：<b>' +
        dupIds.map(function (id) { return '<code>' + U.esc(id) + '</code>'; }).join('、') +
        '</b>。<br>程序按 id 匹配机器人，重复时<b>只有第一个会生效</b>' +
        '（改设置看起来"没反应"，或者"改一个另一个也变了"）。' +
        '请到下面的「机器人账号」把 id 改成互不相同（例如 bot1 / bot2）。</div></div>');
    }
    // 环境变量没匹配上：以前是静默忽略，用户根本查不出为什么没生效
    var envUnknown = spec.env_unknown || [];
    if (envUnknown.length) {
      html.push('<div class="card" style="border-color:rgba(217,119,6,.4)">' +
        '<h3>⚠️ 有环境变量没匹配到配置项</h3>' +
        '<div class="field-tip">' +
        envUnknown.map(function (name) { return '<code>' + U.esc(name) + '</code>'; }).join('、') +
        ' 已被忽略。写法示例：<code>QQBOT_WEB_PORT</code>、<code>QQBOT_AI_API_KEY</code>、' +
        '<code>QQBOT_CLOUD_SYNC_ENABLED</code>，也可以用双下划线表示层级' +
        '（<code>QQBOT_AI__API_KEY</code>）。</div></div>');
    }
    var sections = spec.sections || [];
    for (var i = 0; i < sections.length; i++) {
      var section = sections[i];
      var perBot = !!section.per_bot;
      // 按机器人的分组读"该机器人的生效配置"，全局分组读全局配置
      var source = (perBot && spec.bot_config) ? spec.bot_config : spec.config;
      var fieldsHtml = [];
      for (var j = 0; j < (section.fields || []).length; j++) {
        var path = section.fields[j];
        var meta = spec.fields[path];
        if (!meta) continue;
        if (keyword && (path + ' ' + (meta.label || '') + ' ' + (meta.hint || '')).toLowerCase()
            .indexOf(keyword) < 0) continue;
        var value = U.getPath(source, path);
        if (value === undefined) value = U.getPath(spec.defaults, path);
        fieldsHtml.push(fieldHtml(path, meta, value));
      }
      if (!fieldsHtml.length) continue;
      var scopeBadge = perBot
        ? '<span class="badge ok" title="只作用于当前选中的机器人">作用于：' +
          U.esc(spec.active_bot || '当前机器人') + '</span>'
        : '<span class="badge" title="所有机器人共用">全局</span>';
      var actions = perBot
        ? '<button class="btn tiny" data-copyall="' + U.esc(section.key) +
          '" title="把当前机器人在这个分组里的设置复制给其它所有机器人">📋 应用到所有机器人</button>'
        : '';
      html.push('<div class="card cfg-section" data-section="' + U.esc(section.key) + '">' +
        '<h3>' + U.esc(section.icon || '') + ' ' + U.esc(section.title) + ' ' + scopeBadge +
        '</h3>' +
        (section.scope_hint || section.tip
          ? '<div class="field-tip" style="margin-bottom:8px">' +
            U.esc(section.scope_hint || section.tip) + '</div>' : '') +
        (actions ? '<div style="margin-bottom:8px">' + actions + '</div>' : '') +
        fieldsHtml.join('') + '</div>');
    }
    form.innerHTML = html.join('') || '<div class="card"><div class="tiny">没有匹配的配置项</div></div>';
    U.el('settingsPath').innerHTML = '配置文件：<code>' + U.esc(spec.path) + '</code>' +
      ' · 密钥类字段以掩码显示，留空保存表示不修改';
    bindFormEvents();
  }

  function bindFormEvents() {
    var form = U.el('settingsForm');
    form.addEventListener('click', function (event) {
      var target = event.target;
      if (target.matches('[data-copyall]')) {
        applyToAllBots();
        return;
      }
      if (target.matches('[data-addrow]')) {
        var kind = target.getAttribute('data-addrow');
        var box = target.parentNode;
        var html = kind === 'dict' ? dictRowHtml('', '') : itemRowHtml('');
        box.insertAdjacentHTML('beforeend', html);
        return;
      }
      if (target.matches('[data-delrow]')) {
        var row = target.closest('.item-row, .kv-row, .task-row, .cmd-row');
        if (row) row.parentNode.removeChild(row);
        return;
      }
      if (target.matches('[data-addbot]')) {
        U.el('settingsForm').querySelector('[data-botsbox]').insertAdjacentHTML('beforeend',
          botEditorHtml({ id: 'bot' + (Date.now() % 1000), name: '新机器人', enabled: false,
                          intents: 100663296, reconnect_attempts: 5, reconnect_interval: 10 }));
        return;
      }
      if (target.matches('[data-delbot]')) {
        var editor = target.closest('[data-bot]');
        if (editor) editor.parentNode.removeChild(editor);
        return;
      }
      if (target.matches('[data-addtask]')) {
        U.el('settingsForm').querySelector('[data-tasksbox]').insertAdjacentHTML('beforeend',
          taskRowHtml({}));
        return;
      }
      if (target.matches('[data-addcmd]')) {
        U.el('settingsForm').querySelector('[data-cmdsbox]').insertAdjacentHTML('beforeend',
          commandRowHtml({}));
      }
    });
    form.addEventListener('change', function () {
      Settings.dirty = true;
    });
  }

  /* ============================ 收集 ============================ */
  function collect() {
    var spec = Settings.spec;
    var out = {};
    var fields = U.el('settingsForm').querySelectorAll('.field');
    for (var i = 0; i < fields.length; i++) {
      var node = fields[i];
      var path = node.getAttribute('data-field');
      var meta = spec.fields[path] || {};
      var type = meta.type || 'str';
      try {
        if (type === 'bool') {
          U.setPath(out, path, node.querySelector('[data-bool]').checked);
        } else if (type === 'enum') {
          U.setPath(out, path, node.querySelector('[data-enum]').value);
        } else if (type === 'int' || type === 'float') {
          var raw = node.querySelector('[data-number]').value;
          if (raw !== '') U.setPath(out, path, type === 'int' ? parseInt(raw, 10) : parseFloat(raw));
        } else if (type === 'secret') {
          var clearBox = node.querySelector('[data-clear]');
          var input = node.querySelector('[data-text]');
          if (clearBox && clearBox.checked) U.setPath(out, path, CLEAR_MARKER);
          else if (input && input.value) U.setPath(out, path, input.value);
        } else if (type === 'list') {
          var items = [];
          var rows = node.querySelectorAll('.item');
          for (var j = 0; j < rows.length; j++) {
            if (rows[j].value.trim()) items.push(rows[j].value.trim());
          }
          U.setPath(out, path, items);
        } else if (type === 'dict') {
          var dict = {};
          var kvRows = node.querySelectorAll('.kv-row');
          for (var k = 0; k < kvRows.length; k++) {
            var key = kvRows[k].querySelector('.kv-key').value.trim();
            var value = kvRows[k].querySelector('.kv-val').value;
            if (key) dict[key] = value;
          }
          U.setPath(out, path, dict);
        } else if (type === 'bots') {
          out.bots = collectBots(node);
        } else if (type === 'scheduler_tasks') {
          U.setPath(out, path, collectTasks(node));
        } else if (type === 'panel_commands') {
          U.setPath(out, path, collectCommands(node));
        } else if (type === 'textarea') {
          U.setPath(out, path, node.querySelector('[data-text]').value);
        } else {
          U.setPath(out, path, node.querySelector('[data-text]').value);
        }
      } catch (error) {
        throw new Error('配置项「' + (meta.label || path) + '」填写有误：' + error.message);
      }
    }
    return out;
  }

  function collectBots(node) {
    var editors = node.querySelectorAll('[data-bot]');
    var bots = [];
    for (var i = 0; i < editors.length; i++) {
      var editor = editors[i];
      var name = editor.querySelector('[data-bot-name]').value.trim();
      var id = editor.querySelector('[data-bot-id]').value.trim() || ('bot' + (i + 1));
      var secretInput = editor.querySelector('[data-bot-secret]');
      var clearBox = editor.querySelector('[data-bot-secret-clear]');
      var secret = secretInput ? secretInput.value.trim() : '';
      if (clearBox && clearBox.checked && !secret) secret = CLEAR_MARKER;   // 显式清空
      bots.push({
        id: id,
        name: name || id,
        enabled: editor.querySelector('[data-bot-enabled]').checked,
        app_id: editor.querySelector('[data-bot-appid]').value.trim(),
        app_secret: secret,
        sandbox: editor.querySelector('[data-bot-sandbox]').checked,
        intents: parseInt(editor.querySelector('[data-bot-intents]').value, 10) || 100663296,
        reconnect_attempts: parseInt(editor.querySelector('[data-bot-attempts]').value, 10) || 5,
        reconnect_interval: parseInt(editor.querySelector('[data-bot-interval]').value, 10) || 10
      });
    }
    return bots;
  }

  function collectTasks(node) {
    var rows = node.querySelectorAll('.task-row');
    var tasks = [];
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var time = row.querySelector('.t-time').value.trim();
      var targetId = row.querySelector('.t-id').value.trim();
      var content = row.querySelector('.t-content').value.trim();
      if (!time || !targetId || !content) continue;
      tasks.push({
        time: time,
        target_type: row.querySelector('.t-type').value,
        bot_id: row.querySelector('.t-bot').value.trim(),
        target_id: targetId,
        content: content
      });
    }
    return tasks;
  }

  function collectCommands(node) {
    var rows = node.querySelectorAll('.cmd-row');
    var commands = [];
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var type = row.querySelector('.cmd-type').value;
      var name = row.querySelector('.cmd-name').value.trim();
      var extra = row.querySelector('.cmd-desc').value.trim();
      if (!name) continue;
      commands.push(type === 'link'
        ? { type: 'link', name: name, link: extra }
        : { type: 'command', name: name, desc: extra });
    }
    return commands;
  }

  /* ============================ 一键应用到所有机器人 ============================ */
  function applyToAllBots() {
    var botId = (Settings.spec && Settings.spec.active_bot) || U.ActiveBot.id || '';
    U.confirm({
      title: '应用到所有机器人',
      html: '把 <b>' + U.esc(botId) + '</b> 的设置（AI 人设、回复、过滤、功能开关、发送、' +
        '定时任务、指令面板）复制给**其它所有机器人**，覆盖它们各自的这些设置。<br>' +
        '<span class="tiny">全局设置（网页后台、存储、日志、云同步等）本来就不分机器人，不受影响。</span>',
      okText: '确认复制'
    }).then(function (ok) {
      if (!ok) return;
      // 先把当前表单存一遍，确保复制的是最新值
      var payload;
      try {
        payload = collect();
      } catch (error) {
        U.toast('有配置项填写有误：' + error.message, 'err', 7000);
        return;
      }
      U.postJSON('/api/admin/config', { config: payload, bot_id: botId }).then(function () {
        return U.postJSON('/api/admin/config/copy_to_all', { bot_id: botId });
      }).then(function (data) {
        U.toast(data.message || '已应用到所有机器人', 'ok', 6000);
        load();
      }).catch(function (error) {
        U.toast('复制失败：' + error.message, 'err', 7000);
      });
    });
  }

  /* ============================ 加载 / 保存 ============================ */
  function load() {
    return U.getJSON('/api/admin/config').then(function (data) {
      Settings.spec = data;
      Settings.dirty = false;
      render();
      return data;
    }).catch(function (error) {
      U.el('settingsForm').innerHTML = '<div class="card"><div class="err-text">加载配置失败：' +
        U.esc(error.message) + '</div></div>';
      throw error;
    });
  }

  function save() {
    var message = U.el('settingsMsg');
    var payload;
    try {
      payload = collect();
    } catch (error) {
      message.className = 'cfg-msg err';
      message.textContent = '❌ ' + error.message;
      return Promise.resolve();
    }
    var button = U.el('settingsSave');
    button.disabled = true;
    message.className = 'cfg-msg';
    message.textContent = '保存中…';
    return U.postJSON('/api/admin/config', {
      config: payload,
      bot_id: (Settings.spec && Settings.spec.active_bot) || U.ActiveBot.id || ''
    }).then(function (data) {
      message.className = 'cfg-msg ok';
      message.textContent = '✅ ' + (data.message || '已保存');
      Settings.spec = data;
      render();
      var ui = data.config && data.config.ui;
      if (ui) {
        document.body.setAttribute('data-anim', ui.animation || 'full');
        document.body.setAttribute('data-theme', ui.theme || 'auto');
        document.body.setAttribute('data-compact', ui.compact_mode ? '1' : '0');
        if (ui.poll_interval_ms && global.App) global.App.restartPolling(ui.poll_interval_ms);
      }
      // 改了端口/地址：必须让用户知道"现在还得用旧地址访问"
      if (data.restart_notice) {
        U.toast(data.restart_notice, 'warn', 15000);
        message.className = 'cfg-msg err';
        message.textContent = data.restart_notice;
      } else {
        U.toast(data.message || '配置已保存', 'ok', 4000);
      }
    }).catch(function (error) {
      message.className = 'cfg-msg err';
      message.textContent = '❌ ' + error.message;
    }).then(function () {
      button.disabled = false;
    });
  }

  function init() {
    U.el('settingsSave').addEventListener('click', save);
    U.el('settingsReload').addEventListener('click', function () { load(); });
    U.el('settingsSearch').addEventListener('input', U.debounce(function () { render(); }, 200));
    window.addEventListener('beforeunload', function (event) {
      if (Settings.dirty) {
        event.preventDefault();
        event.returnValue = '';
      }
    });
  }

  Settings.init = init;
  Settings.load = load;
  Settings.save = save;
  Settings.render = render;
  global.Settings = Settings;
})(window);
