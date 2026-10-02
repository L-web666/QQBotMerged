/* ==========================================================================
   群管理：群列表、群信息、成员列表、禁言/解禁（官方接口 + 本地兜底）、群级配置
   ========================================================================== */
(function (global) {
  'use strict';
  var U = global.Util;

  var Groups = {
    list: [],
    active: '',
    activeName: '',
    detail: null,
    botId: '',
    botName: '',
    muteMode: 'local',        // 本次操作使用的禁言方式（在群管理页选择）
    muteMinutes: 10,
    pollTimer: null,
    detailTimer: null,
    signature: '',            // 列表内容指纹：没变就不重绘，页面不会闪
    lastLoadedAt: 0
  };

  var LIST_POLL_MS = 15000;   // 群列表（含群名）自动刷新间隔
  var DETAIL_POLL_MS = 20000; // 打开的群详情自动刷新间隔（含官方禁言名单）

  var MUTE_MODES = [
    ['local', '本地禁言（机器人不再回复该成员，无需平台权限）'],
    ['api', '官方接口（QQ 群里真实禁言，需要机器人是群管理员）'],
    ['both', '两者都做（本地立即生效 + 官方真实禁言）']
  ];

  function muteModeBar() {
    return '<div class="mute-box">' +
      '<span class="tiny">禁言方式：</span>' +
      '<select id="muteModeSel">' + MUTE_MODES.map(function (pair) {
        return '<option value="' + pair[0] + '"' +
          (Groups.muteMode === pair[0] ? ' selected' : '') + '>' + U.esc(pair[1]) + '</option>';
      }).join('') + '</select>' +
      '<span class="tiny">时长(分钟)</span>' +
      '<input type="number" id="muteMinutesInput" value="' + Groups.muteMinutes +
      '" min="0" max="43200">' +
      '<span class="tiny">0 = 平台默认</span>' +
      '</div>';
  }

  function load(options) {
    options = options || {};
    return U.getJSON(U.qs('/api/groups')).then(function (data) {
      Groups.list = data.groups || [];
      Groups.botId = data.bot_id || '';
      Groups.botName = data.bot_name || data.bot_id || '';
      Groups.muteMode = data.mute_mode || Groups.muteMode;
      Groups.lastLoadedAt = Date.now();
      U.el('groupsHint').innerHTML = '当前机器人：<b>' + U.esc(Groups.botName) + '</b>' +
        '（在左侧导航栏顶部可切换）· 这里只列出<b>该机器人</b>看到过的群，' +
        '避免用错机器人导致请求失败。' +
        ' · 群名会<b>自动刷新</b>（每 30 分钟一次，新群名出现后列表自动更新）。' +
        ' · 禁言方式在群详情里按群选择（官方接口要求机器人是群管理员，最长 30 天）。';
      var signature = listSignature(Groups.list);
      // 内容没变就不重绘：避免自动刷新时表格闪烁、选中状态丢失
      if (signature !== Groups.signature || options.force) {
        Groups.signature = signature;
        renderList();
      }
      updateFreshness();
      // 当前群不再属于这个机器人时，清空右侧详情
      if (Groups.active && !Groups.list.some(function (g) {
        return g.group_openid === Groups.active;
      })) {
        Groups.active = '';
        U.el('groupDetail').innerHTML = '<div class="empty-hint"><div class="empty-icon">👥</div>' +
          '<p>这个群不属于当前机器人</p><p class="tiny">请切换到对应的机器人，或从左侧重新选择群</p></div>';
      }
      return data;
    }).catch(function (error) {
      U.el('groupsTable').innerHTML = '<tr><td class="err-text">加载失败：' +
        U.esc(error.message) + '</td></tr>';
    });
  }

  /* 列表内容指纹（群名 / 消息数 / 禁言数），用来判断要不要重绘 */
  function listSignature(list) {
    return list.map(function (group) {
      return [group.group_openid, group.name || '', group.message_count || 0,
              group.muted_count || 0, group.name_updated || 0].join('|');
    }).join(';');
  }

  function updateFreshness() {
    var node = U.el('groupsFreshness');
    if (!node) return;
    var unnamed = Groups.list.filter(function (g) { return !g.name; }).length;
    var refreshing = Groups.list.filter(function (g) { return g.name_refreshing; }).length;
    var failed = Groups.list.filter(function (g) { return g.name_error; }).length;
    var text = '上次检查：' + new Date(Groups.lastLoadedAt).toLocaleTimeString() +
      '（每 ' + Math.round(LIST_POLL_MS / 1000) + ' 秒自动刷新）';
    if (refreshing) text += ' · 正在获取群名…';
    if (unnamed) text += ' · 还有 ' + unnamed + ' 个群没有群名，正在后台获取';
    if (failed) text += ' · ' + failed + ' 个群名获取失败（10 分钟后自动重试，也可点「刷新全部群名」）';
    node.textContent = text;
  }


  function renderList() {
    var rows = Groups.list.map(function (group) {
      var nameHint = '';
      var shownName = group.name;
      if (!group.name) {
        // 群名以 QQ 为准：没拿到时用短标识占位，绝不用"最后发言者的昵称"
        shownName = '群 ' + U.esc(String(group.group_openid || '').slice(-6)) + '（暂无群名）';
        nameHint = group.name_error
          ? '<span class="badge err">群名获取失败</span>'
          : (group.name_refreshing
            ? '<span class="badge warn">正在获取群名…</span>'
            : '<span class="badge warn">暂无群名</span>');
      } else if (group.name_stale) {
        nameHint = '<span class="badge">群名待更新</span>';
      }
      return '<tr class="group-row' + (group.group_openid === Groups.active ? ' active' : '') +
        '" data-group="' + U.esc(group.group_openid) + '">' +
        '<td><b>' + U.esc(shownName) + '</b> ' + nameHint +
        '<div class="tiny">' + U.esc(group.group_openid) + '</div></td>' +
        '<td class="nowrap">' + (group.message_count || 0) + '</td>' +
        '<td class="nowrap">' + (group.muted_count ? '<span class="badge warn">' +
          group.muted_count + ' 人禁言中</span>' : '<span class="tiny">--</span>') + '</td>' +
        '<td class="nowrap tiny">' + U.esc(U.fmtTime(group.last_time)) + '</td>' +
        '<td class="cell-act"><button class="btn tiny" data-open="' + U.esc(group.group_openid) +
        '">管理</button></td></tr>';
    }).join('');
    U.el('groupsTable').innerHTML =
      '<thead><tr><th>群</th><th>消息</th><th>禁言</th><th>最后活跃</th><th></th></tr></thead><tbody>' +
      (rows || '<tr><td colspan="5" class="tiny">这个机器人还没有收到任何群消息</td></tr>') + '</tbody>';
    var buttons = U.el('groupsTable').querySelectorAll('[data-open]');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener('click', function () {
        openGroup(this.getAttribute('data-open'));
      });
    }
    var rowNodes = U.el('groupsTable').querySelectorAll('.group-row');
    for (var j = 0; j < rowNodes.length; j++) {
      rowNodes[j].addEventListener('click', function (event) {
        if (event.target.closest('button')) return;
        openGroup(this.getAttribute('data-group'));
      });
    }
  }

  function openGroup(groupOpenid, refresh, silent) {
    var changed = groupOpenid !== Groups.active;
    Groups.active = groupOpenid;
    if (changed) renderList();
    var detail = U.el('groupDetail');
    // silent（自动刷新）时不显示"正在加载"占位，避免每 20 秒闪一下
    if (!silent) {
      detail.innerHTML = '<div class="card"><div class="loading-row"><span class="spinner"></span>' +
        '正在加载群信息…</div></div>';
    }
    return U.getJSON('/api/groups/detail?group_openid=' + encodeURIComponent(groupOpenid) +
      (refresh ? '&refresh=1' : ''))
      .then(function (data) {
        // 自动刷新期间用户可能已经切到别的群，丢弃过期响应
        if (Groups.active !== groupOpenid) return data;
        Groups.detail = data;
        Groups.activeName = data.name || '';
        // 详情接口返回的是"当前机器人"，以它为准（避免切换机器人后残留上一个）
        Groups.botId = data.bot_id || Groups.botId;
        if (data.bot_name) Groups.botName = data.bot_name;
        renderDetail(data);
        if (Groups.signature !== listSignature(Groups.list)) {
          Groups.signature = listSignature(Groups.list);
          renderList();
        }
        updateFreshness();
        return data;
      })
      .catch(function (error) {
        if (silent) return;
        detail.innerHTML = '<div class="card"><div class="err-text">加载失败：' +
          U.esc(error.message) + '</div></div>';
      });
  }

  function renderDetail(data) {
    var settings = (data.settings || {}).effective || {};
    var stored = (data.settings || {}).stored || {};
    var members = data.members || [];
    var mutings = data.mutings || [];                     // 本地禁言（机器人侧）
    var platformMutings = data.platform_mutings || [];    // QQ 群内真实禁言（官方接口）
    var globalMute = data.global_mute || {};
    var muteMap = {};
    for (var i = 0; i < mutings.length; i++) muteMap[mutings[i].member_openid] = mutings[i];

    var memberRows = members.map(function (member) {
      var mute = muteMap[member.member_openid];
      var badges = '';
      if (mute) {
        badges += '<span class="badge warn">本地禁言 ' + U.esc(mute.remaining_text || '') + '</span> ';
      }
      if (member.platform_muted) {
        badges += '<span class="badge err">QQ群禁言 ' +
          U.esc(member.platform_remaining_text || '') + '</span>';
      }
      return '<tr><td><b>' + U.esc(member.username || member.member_openid) + '</b>' +
        '<div class="tiny">' + U.esc(member.member_openid) + '</div></td>' +
        '<td class="nowrap">' + U.esc(member.role === 'owner' ? '群主' :
          (member.role === 'admin' ? '管理员' : '成员')) + '</td>' +
        '<td class="nowrap">' + (member.message_count || 0) + '</td>' +
        '<td class="nowrap">' + (badges || '<span class="tiny">--</span>') + '</td>' +
        '<td class="cell-act">' +
        '<button class="btn tiny" data-mute="' + U.esc(member.member_openid) + '" data-name="' +
        U.esc(member.username || '') + '">🔇 禁言</button> ' +
        '<button class="btn tiny" data-unmute="' + U.esc(member.member_openid) + '">🔊 解禁</button>' +
        (member.platform_muted
          ? ' <button class="btn tiny" data-official-unmute="' + U.esc(member.member_openid) +
            '" data-name="' + U.esc(member.username || '') + '">🛡️ 官方解禁</button>'
          : '') +
        '</td></tr>';
    }).join('');

    var apiError = data.api_error
      ? '<div class="hint warn-text">⚠️ 平台成员接口不可用：' + U.esc(data.api_error) +
        '（列表已用历史消息中识别到的成员补齐，禁言/解禁仍可尝试）</div>' : '';
    // 官方「查询群禁言状态」失败时把平台原话显示出来（通常是"机器人不是群管理员"）
    var muteError = (!globalMute.ok && globalMute.error)
      ? '<div class="hint warn-text">⚠️ 无法读取 QQ 群内的真实禁言状态：' +
        U.esc(globalMute.error) + '</div>' : '';

    U.el('groupDetail').innerHTML =
      '<div class="card">' +
      '<h3>👥 ' + U.esc(data.name || '（未获取到群名）') + '</h3>' +
      '<div class="hint">群 openid：<code>' + U.esc(data.group_openid) + '</code>' +
      ' · <b>机器人：' + U.esc(Groups.botName || Groups.botId || '未知') + '</b>' +
      ' · 成员来源：' + U.esc(data.source === 'api' ? '平台接口' : '本地缓存/历史消息') +
      (data.name_updated_text ? ' · 群名更新于 ' + U.esc(data.name_updated_text) : '') +
      (data.updated_text ? ' · 成员更新于 ' + U.esc(data.updated_text) : '') + '</div>' +
      '<div class="head-actions" style="margin-bottom:8px">' +
      '<button class="btn tiny" id="grpRefreshName">🏷️ 刷新群名</button>' +
      '<button class="btn tiny" id="grpRefreshMembers">🔄 拉取成员</button>' +
      '<button class="btn tiny" id="grpRefreshMutes">🛡️ 刷新官方禁言状态</button>' +
      '<button class="btn tiny" id="grpOpenChat">💬 打开聊天</button>' +
      '</div>' + muteModeBar() + apiError + muteError +
      '<div class="member-list tbl-wrap"><table>' +
      '<thead><tr><th>成员</th><th>身份</th><th>发言</th><th>禁言</th><th>操作</th></tr></thead>' +
      '<tbody>' + (memberRows || '<tr><td colspan="5" class="tiny">暂时没有识别到成员</td></tr>') +
      '</tbody></table></div></div>' +

      platformMuteCard(globalMute, platformMutings) +

      '<div class="card">' +
      '<h3>🤖 当前本地禁言名单 <span class="badge">' + mutings.length + '</span>' +
      '<span class="tiny">（只影响机器人是否回复，不涉及 QQ 群内真实禁言）</span></h3>' +
      (mutings.length ? '<div class="tbl-wrap"><table><thead><tr><th>成员</th><th>时长</th>' +
        '<th>到期</th><th>原因</th><th></th></tr></thead><tbody>' +
        mutings.map(function (mute) {
          return '<tr><td>' + U.esc(mute.username || mute.member_openid) + '</td>' +
            '<td class="nowrap">' + U.esc(mute.remaining_text || '') + '</td>' +
            '<td class="nowrap">' + (mute.until ? new Date(mute.until * 1000).toLocaleString() : '永久') + '</td>' +
            '<td>' + U.esc(mute.reason || '--') + '</td>' +
            '<td class="cell-act"><button class="btn tiny" data-unmute="' +
            U.esc(mute.member_openid) + '">解除</button></td></tr>';
        }).join('') + '</tbody></table></div>'
        : '<div class="tiny">当前没有被本地禁言的成员</div>') +
      '</div>' +

      '<div class="card">' +
      '<h3>⚙️ 群级配置 <span class="badge' + (Object.keys(stored).length ? ' ok' : '') + '">' +
      (Object.keys(stored).length ? '已自定义' : '跟随全局') + '</span></h3>' +
      '<div class="settings-grid">' +
      settingRow('require_mention', '需要 @ 才回复', settings.require_mention, 'bool') +
      settingRow('auto_reply', '群内自动回复', settings.auto_reply, 'bool') +
      settingRow('keywords_enabled', '关键词回复', settings.keywords_enabled, 'bool') +
      settingRow('save_media', '图片自动留存', settings.save_media, 'bool') +
      settingRow('rate_limit_seconds', '回复限速（秒）', settings.rate_limit_seconds, 'number') +
      settingRow('max_history', '上下文条数', settings.max_history, 'number') +
      '</div>' +
      '<div class="row" style="margin-top:10px;display:flex;gap:8px">' +
      '<button class="btn primary tiny" id="grpSaveSettings">💾 保存群配置</button>' +
      '<button class="btn tiny" id="grpResetSettings">↩️ 恢复跟随全局</button>' +
      '</div></div>';

    bindDetail(data);
  }

  /* QQ 群里真实的禁言状态（官方接口：全员禁言模式 + 成员级禁言名单） */
  function platformMuteCard(globalMute, platformMutings) {
    var modeText = globalMute.text || '未知';
    var modeClass = globalMute.active_now ? 'err' : (globalMute.ok ? 'ok' : '');
    var rows = platformMutings.map(function (mute) {
      return '<tr><td><b>' + U.esc(mute.username || mute.member_openid) + '</b>' +
        '<div class="tiny">' + U.esc(mute.member_openid) + '</div></td>' +
        '<td class="nowrap">' + U.esc(mute.remaining_seconds
          ? U.humanDuration(mute.remaining_seconds) : '永久') + '</td>' +
        '<td class="nowrap">' + U.esc(mute.expire_text || mute.mute_expire_at || '--') + '</td>' +
        '<td class="cell-act"><button class="btn tiny" data-official-unmute="' +
        U.esc(mute.member_openid) + '" data-name="' + U.esc(mute.username || '') +
        '">🛡️ 官方解禁</button></td></tr>';
    }).join('');
    var rules = (globalMute.rules || []).map(function (rule) {
      return '<div class="tiny">· ' + U.esc(rule.text || '') + '</div>';
    }).join('');
    return '<div class="card">' +
      '<h3>🛡️ QQ 群内真实禁言 <span class="badge ' + modeClass + '">' +
      U.esc(modeText) + '</span>' +
      '<span class="tiny">（官方接口，需要机器人是群管理员）</span></h3>' +
      (rules ? '<div style="margin-bottom:6px">' + rules + '</div>' : '') +
      (globalMute.ok
        ? (platformMutings.length
          ? '<div class="tbl-wrap"><table><thead><tr><th>成员</th><th>剩余</th><th>到期</th>' +
            '<th></th></tr></thead><tbody>' + rows + '</tbody></table></div>'
          : '<div class="tiny">QQ 群内当前没有被禁言的成员</div>')
        : '<div class="hint">拿不到官方禁言名单：' +
          U.esc(globalMute.error || '平台未返回数据') +
          '<div class="tiny">常见原因：机器人不是群管理员，或应用未开通群成员管理能力（11253）。' +
          '你仍然可以用「本地禁言」让机器人不再回复该成员。</div></div>') +
      '</div>';
  }


  function settingRow(key, label, value, kind) {
    return '<label class="field" style="border:0;padding:6px 0"><span class="field-label">' +
      U.esc(label) + '</span>' +
      (kind === 'bool'
        ? '<input type="checkbox" data-setting="' + key + '"' + (value ? ' checked' : '') + '>'
        : '<input type="number" data-setting="' + key + '" step="0.5" value="' +
          U.esc(value === undefined ? '' : value) + '">') +
      '</label>';
  }

  function bindDetail(data) {
    var detail = U.el('groupDetail');
    // 记住这次选择的禁言方式与时长（重绘后保持不变）
    var modeSel = U.el('muteModeSel');
    if (modeSel) {
      modeSel.addEventListener('change', function () { Groups.muteMode = this.value; });
    }
    var minutesInput = U.el('muteMinutesInput');
    if (minutesInput) {
      minutesInput.addEventListener('change', function () {
        var value = parseInt(this.value, 10);
        Groups.muteMinutes = isNaN(value) ? 10 : value;
      });
    }
    var refreshName = U.el('grpRefreshName');
    if (refreshName) {
      refreshName.addEventListener('click', function () {
        U.postJSON('/api/groups/refresh', { group_openid: Groups.active, what: 'name' })
          .then(function (result) {
            U.toast(result.message || '已刷新', result.success ? 'ok' : 'warn');
            openGroup(Groups.active, true);
            load({ force: true });
          })
          .catch(function (error) { U.toast(error.message, 'err'); });
      });
    }
    var refreshMembers = U.el('grpRefreshMembers');
    if (refreshMembers) {
      refreshMembers.addEventListener('click', function () {
        U.toast('正在向平台请求成员列表…', 'ok');
        U.postJSON('/api/groups/refresh', { group_openid: Groups.active, what: 'members' })
          .then(function (result) {
            U.toast(result.message || '已刷新', result.success ? 'ok' : 'warn', 5000);
            openGroup(Groups.active);
          })
          .catch(function (error) { U.toast(error.message, 'err', 5000); });
      });
    }
    // 重新查询官方禁言状态（绕过 20 秒缓存）
    var refreshMutes = U.el('grpRefreshMutes');
    if (refreshMutes) {
      refreshMutes.addEventListener('click', function () {
        U.getJSON('/api/groups/mutings?group_openid=' +
          encodeURIComponent(Groups.active) + '&refresh=1')
          .then(function (data) {
            if (data.global_mute && data.global_mute.ok) {
              U.toast('官方禁言状态：' + (data.global_mute.text || '已更新') +
                '，禁言中 ' + (data.platform_mutings || []).length + ' 人', 'ok', 5000);
            } else {
              U.toast((data.global_mute && data.global_mute.error) || '查询失败', 'warn', 6000);
            }
            openGroup(Groups.active);
          })
          .catch(function (error) { U.toast(error.message, 'err', 5000); });
      });
    }
    var openChat = U.el('grpOpenChat');
    if (openChat) {
      openChat.addEventListener('click', function () {
        var botId = (data.bot_id || (Groups.detail && Groups.detail.bot_id) || '');
        var key = (botId ? botId + ':' : '') + 'group:' + Groups.active;
        if (global.App) global.App.go('chat');
        var conv = null;
        if (global.Chat) {
          for (var i = 0; i < global.Chat.conversations.length; i++) {
            if (global.Chat.conversations[i].key === key) conv = global.Chat.conversations[i];
          }
          if (!conv) conv = { key: key, type: 'group', name: Groups.activeName || Groups.active,
                              group_openid: Groups.active };
          global.Chat.renderConversations(global.Chat.conversations);
        }
        if (global.Chat && global.Chat.loadConversations) {
          global.Chat.loadConversations().then(function () {
            var target = global.Chat.byKey[key] || conv;
            if (target) {
              document.dispatchEvent(new CustomEvent('qbm-open-conv', { detail: target }));
            }
          });
        }
      });
    }
    var saveSettings = U.el('grpSaveSettings');
    if (saveSettings) {
      saveSettings.addEventListener('click', function () {
        var settings = {};
        var inputs = detail.querySelectorAll('[data-setting]');
        for (var i = 0; i < inputs.length; i++) {
          var node = inputs[i];
          var key = node.getAttribute('data-setting');
          if (node.type === 'checkbox') settings[key] = node.checked;
          else {
            var value = parseFloat(node.value);
            if (!isNaN(value)) settings[key] = value;
          }
        }
        U.postJSON('/api/groups/settings', { group_openid: Groups.active, settings: settings })
          .then(function (result) { U.toast(result.message || '已保存', 'ok'); openGroup(Groups.active, true); })
          .catch(function (error) { U.toast(error.message, 'err'); });
      });
    }
    var resetSettings = U.el('grpResetSettings');
    if (resetSettings) {
      resetSettings.addEventListener('click', function () {
        U.postJSON('/api/groups/settings', { group_openid: Groups.active, reset: true })
          .then(function (result) { U.toast(result.message || '已恢复', 'ok'); openGroup(Groups.active, true); })
          .catch(function (error) { U.toast(error.message, 'err'); });
      });
    }

    var muteButtons = detail.querySelectorAll('[data-mute]');
    for (var m = 0; m < muteButtons.length; m++) {
      muteButtons[m].addEventListener('click', function () {
        muteMember(this.getAttribute('data-mute'), this.getAttribute('data-name'));
      });
    }
    var unmuteButtons = detail.querySelectorAll('[data-unmute]');
    for (var n = 0; n < unmuteButtons.length; n++) {
      unmuteButtons[n].addEventListener('click', function () {
        unmuteMember(this.getAttribute('data-unmute'));
      });
    }
    // 官方解禁（QQ 群内真实解禁）
    var officialButtons = detail.querySelectorAll('[data-official-unmute]');
    for (var p = 0; p < officialButtons.length; p++) {
      officialButtons[p].addEventListener('click', function () {
        officialUnmute(this.getAttribute('data-official-unmute'),
                       this.getAttribute('data-name'));
      });
    }
  }

  function muteMember(memberOpenid, username) {
    var mode = Groups.muteMode;
    var minutes = Groups.muteMinutes;
    var modeText = (MUTE_MODES.filter(function (p) { return p[0] === mode; })[0] || [])[1] || mode;
    U.confirm({
      title: '禁言群成员',
      html: '<div>成员：<b>' + U.esc(username || memberOpenid) + '</b>' +
        '<div class="tiny">' + U.esc(memberOpenid) + '</div></div>' +
        '<div class="field-tip" style="margin-top:8px">方式：<b>' + U.esc(modeText) + '</b><br>' +
        '时长：<b>' + (minutes ? minutes + ' 分钟' : '平台默认') + '</b><br>' +
        '<span class="tiny">需要改方式/时长时，用群详情里的「禁言方式」栏目。</span></div>' +
        (mode === 'local'
          ? '<div class="tiny" style="margin-top:6px">本地禁言：机器人立刻不再回复该成员，' +
            '但他在 QQ 群里仍然可以发言。</div>'
          : '<div class="tiny" style="margin-top:6px">官方接口要求机器人是群管理员；' +
            '不是管理员时会返回平台错误。</div>'),
      okText: '确认禁言'
    }).then(function (ok) {
      if (!ok) return;
      U.postJSON('/api/groups/mute', {
        group_openid: Groups.active, member_openid: memberOpenid,
        username: username || '', minutes: minutes, mode: mode
      }).then(function (data) {
        U.toast(data.message || '已处理', data.success ? 'ok' : 'warn', 6000);
        openGroup(Groups.active, true);
      }).catch(function (error) { U.toast(error.message, 'err', 6000); });
    });
  }

  function unmuteMember(memberOpenid, official) {
    // 解禁默认只做本地解禁（把本地禁言记录释放掉），
    // 只有当前选的是"官方/两者都做"时才顺带调用官方接口
    var mode = official ? 'api' : ((Groups.muteMode === 'local') ? 'local' : Groups.muteMode);
    U.postJSON('/api/groups/unmute', {
      group_openid: Groups.active, member_openid: memberOpenid, mode: mode
    })
      .then(function (data) {
        var text = data.message || '已解禁';
        if (!official && data.api && data.api.success === false) {
          text += '（官方接口：' + data.api.message + '）';
        }
        U.toast(text, data.success ? 'ok' : 'warn', 6000);
        openGroup(Groups.active, true);
      })
      .catch(function (error) { U.toast(error.message, 'err'); });
  }

  function officialUnmute(memberOpenid, username) {
    U.confirm({
      title: '官方解禁（QQ 群内真实解禁）',
      html: '<div>成员：<b>' + U.esc(username || memberOpenid) + '</b>' +
        '<div class="tiny">' + U.esc(memberOpenid) + '</div></div>' +
        '<div class="tiny" style="margin-top:6px">将调用官方接口 ' +
        '<code>POST /v2/groups/{group_openid}/restrict_chat_setting</code>，' +
        '解除该成员在 QQ 群里的禁言（需要机器人是群管理员）。</div>',
      okText: '确认解禁'
    }).then(function (ok) {
      if (!ok) return;
      unmuteMember(memberOpenid, true);
    });
  }

  function init() {
    U.el('groupsRefresh').addEventListener('click', function () {
      if (Groups.active) openGroup(Groups.active, true);
      else load({ force: true });
    });
    var allNames = U.el('groupsRefreshNames');
    if (allNames) {
      allNames.addEventListener('click', function () {
        U.toast('正在向 QQ 重新获取群名…', 'ok');
        U.postJSON('/api/groups/refresh', { what: 'names', force: true, limit: 20 })
          .then(function (data) {
            U.toast(data.message || '已刷新', data.success ? 'ok' : 'warn', 6000);
            load({ force: true });
            if (Groups.active) openGroup(Groups.active);
          })
          .catch(function (error) { U.toast(error.message, 'err', 6000); });
      });
    }
    // 手动修正：清理"群名被写成最后发言者昵称"的历史数据（默认不会自动做，避免误伤）
    var fixNames = U.el('groupsFixNames');
    if (fixNames) {
      fixNames.addEventListener('click', function () {
        U.confirm({
          title: '修正群会话名',
          html: '按 QQ 返回的群名修正本地保存的群名，并清理"群名被写成最后发言者昵称"的历史坏数据。<br>' +
            '<span class="tiny">只有当群名确实不对时才需要点它；' +
            '这一步可能会清掉"群名恰好等于某人昵称"的少数群名，之后会自动重新获取。</span>',
          okText: '开始修正'
        }).then(function (ok) {
          if (!ok) return;
          U.postJSON('/api/admin/maintenance', { action: 'fix_group_names' })
            .then(function (data) {
              U.toast(data.message || '已修正', data.success ? 'ok' : 'warn', 8000);
              load({ force: true });
              if (Groups.active) openGroup(Groups.active);
            })
            .catch(function (error) { U.toast(error.message, 'err', 6000); });
        });
      });
    }
    startAutoRefresh();
    // 左侧导航栏切换机器人后：群列表按新机器人重新加载
    U.ActiveBot.onChange(function () { load({ force: true }); });
    document.addEventListener('qbm-open-conv', function (event) {
      if (global.Chat && global.Chat.conversations) {
        var target = event.detail;
        var found = null;
        for (var i = 0; i < global.Chat.conversations.length; i++) {
          if (global.Chat.conversations[i].key === target.key) found = global.Chat.conversations[i];
        }
        // Chat 模块内部 openConversation 未导出，这里通过点击会话项触发
        var node = document.querySelector('.conv-item[data-key="' + target.key + '"]');
        if (node) node.click();
        else if (found) U.toast('请在左侧会话列表中选择该群', 'warn');
      }
    });
  }

  /* 自动刷新：群名/群列表每 15 秒，打开的群详情（含官方禁言名单）每 20 秒。
     只在「群管理」页可见时执行；内容没变不会重绘，所以不会闪。 */
  function startAutoRefresh() {
    if (Groups.pollTimer) clearInterval(Groups.pollTimer);
    Groups.pollTimer = setInterval(function () {
      if (document.hidden || !global.App || global.App.page !== 'groups') return;
      load();
    }, LIST_POLL_MS);
    if (Groups.detailTimer) clearInterval(Groups.detailTimer);
    Groups.detailTimer = setInterval(function () {
      if (document.hidden || !global.App || global.App.page !== 'groups') return;
      if (!Groups.active) return;
      // 用户正在详情里操作（鼠标悬停/正在输入）时先不打扰
      var panel = U.el('groupDetail');
      if (panel && panel.querySelector(':focus')) return;
      openGroup(Groups.active, false, true);
    }, DETAIL_POLL_MS);
  }

  Groups.init = init;
  Groups.load = load;
  Groups.openGroup = openGroup;
  global.Groups = Groups;
})(window);
