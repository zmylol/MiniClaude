import { createConversation, applyEvent, interruptConversations, restorePermissions } from './state.js';
import { EventConnection } from './transport.js';
import { escapeHtml, markdown, historyMessages, readAttachments, composeContent } from './content.js';
import { WorkspaceViews } from './views.js';
import { ProjectPicker } from './projects.js';

const $ = selector => document.querySelector(selector);
const state = { conversations: [], runs: {}, activeId: null, events: {}, connected: false };
let storageKey = 'miniclaude.web.v1';
let info = {};
let renderFrame;
let saveTimer;
let toastTimer;
let storageWarning = false;
let visibleConversation;
const messageNodes = new Map();
const eventNodes = new Map();
let eventsConversation;
let currentPage = null;
let navigation = [];
let navigationIndex = -1;
let switchingProject = false;
let attachmentLoading = false;
let reconciling = null;
let preferences = {};
let modalVersion = 0;
let connectionVersion = 0;
let connectionEvents = [];
if (new URLSearchParams(location.search).has('desktop')) document.body.classList.add('desktop-app');

// 从自有本地缓存恢复对话，缓存异常不会阻止启动。
function restore() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
    if (!saved || !Array.isArray(saved.conversations)) return;
    state.conversations = saved.conversations.filter(c => typeof c.id === 'string' && Array.isArray(c.messages)).map(c => ({
      ...createConversation(c.id), ...c,
      messages: c.messages.filter(m => m && ['text', 'tool', 'permission', 'notice', 'agent', 'image'].includes(m.kind)),
      attachments: [],
      historyLoaded: false, loadingHistory: false, stopping: false,
    }));
    state.runs = saved.runs && typeof saved.runs === 'object' ? saved.runs : {};
    state.activeId = saved.activeId;
    preferences = saved.preferences || {};
    interruptConversations(state);
  } catch { toast('未能读取本地对话记录，已打开新的工作区。'); }
}

// 合并同一轮 token 的缓存写入，存储空间不足时显式告知用户。
function persist() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveNow, 400);
}

// 仅保存本机对话内容与路由信息，不保存接口密钥或全局外部事件。
function saveNow() {
  try {
    const conversations = state.conversations.map(c => ({ ...c, attachments: [], failedDraft: c.failedDraft ? { text: c.failedDraft.text, files: [] } : undefined, messages: c.messages.filter(m => m.kind !== 'image') }));
    localStorage.setItem(storageKey, JSON.stringify({ conversations, activeId: state.activeId, runs: state.runs, preferences }));
  } catch {
    if (!storageWarning) { storageWarning = true; toast('浏览器存储不可用或已满；本次对话仍可继续，但刷新后可能丢失。'); }
  }
}

// 返回当前会话。
function active() { return state.conversations.find(c => c.id === state.activeId); }

// 空工作区仍能管理项目，但不能向上一个项目发送命令。
function hasProject() { return info.project_selected !== false && Boolean(info.project_path); }

// 打开会话并恢复它自己的输入草稿。
function selectConversation(id, record = true) {
  if (!state.conversations.some(c => c.id === id)) return;
  if (active()) active().draft = $('#prompt').value;
  state.activeId = id;
  showChat();
  if (record) recordNavigation({ conversation: id });
  $('#prompt').value = active()?.draft || '';
  document.body.classList.remove('sidebar-open');
  syncSidebar();
  render();
  persist();
  $('#prompt').focus();
  if (active().sessionId && !active().historyLoaded && state.connected) loadHistory(active()).catch(error => toast(error.message));
}

// 新建本地空会话，首次发送时再创建远端 session。
function newConversation() {
  const empty = state.conversations.find(c => !c.sessionId && !c.messages.length);
  if (empty) { selectConversation(empty.id); return; }
  const conversation = createConversation(crypto.randomUUID());
  conversation.selectedModel = preferences.model || info.model || '';
  conversation.permissionMode = preferences.permissionMode || 'ask';
  state.conversations.unshift(conversation);
  selectConversation(conversation.id);
}

// 显示短暂状态提示。
function toast(message) {
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 5000);
}

// 打开原生对话框，焦点和 Escape 关闭由浏览器管理。
function modal(title, content) {
  projectPicker.close(false);
  modalVersion++;
  $('#modal-content').innerHTML = `<h2>${escapeHtml(title)}</h2>${content}`;
  $('#modal').setAttribute('aria-label', title);
  if (!$('#modal').open) $('#modal').showModal();
  return modalVersion;
}

// Escape、取消和替换后的旧异步响应不再控制当前弹窗。
function isCurrentModal(version) { return $('#modal').open && modalVersion === version; }

// 使用相同细线图标构建动态按钮。
function icon(name) { return `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`; }

// 更新会话列表，置顶按钮与会话按钮保持独立以支持键盘操作。
function renderSidebar() {
  const query = $('#search-input').value.trim().toLocaleLowerCase();
  const conversations = state.conversations.filter(c => (c.sessionId || c.messages.length) && `${c.title}\n${c.messages.filter(m => m.kind === 'text').map(m => m.text).join('\n')}`.toLocaleLowerCase().includes(query));
  for (const [selector, pinned] of [['#pinned-list', true], ['#recent-list', false]]) {
    const entries = conversations.filter(c => Boolean(c.pinned) === pinned);
    const html = entries.map(c => `<div class="conversation-item ${c.id === state.activeId && !currentPage ? 'active' : ''}"><button class="conversation-select" data-conversation="${escapeHtml(c.id)}" ${c.id === state.activeId && !currentPage ? 'aria-current="page"' : ''}>${icon('message')}<span>${escapeHtml(c.title)}</span>${['running', 'waiting', 'sending'].includes(c.status) ? '<span class="conversation-running" aria-label="运行中">·</span>' : ''}</button><button class="icon-button pin-button" data-menu="${escapeHtml(c.id)}" aria-label="管理对话 ${escapeHtml(c.title)}" title="对话操作">${icon('more')}</button></div>`).join('') || `<p class="sidebar-empty">${query ? '没有匹配的对话' : pinned ? '重要的对话，留在手边' : '你的对话会显示在这里'}</p>`;
    if ($(selector).innerHTML !== html) $(selector).innerHTML = html;
  }
}

// 把消息转换为安全的展示内容；审批按钮只在仍有效且在线时启用。
function messageHtml(message, index) {
  if (message.kind === 'text') return `<article class="message ${escapeHtml(message.role)}"><div class="message-body">${message.role === 'assistant' ? markdown(message.text) : escapeHtml(message.text)}${message.files?.length ? `<div class="sent-files">${message.files.map(name => `<span>${icon('paperclip')}${escapeHtml(name)}</span>`).join('')}</div>` : ''}</div><button class="message-copy" data-copy-message="${index}" aria-label="复制消息">复制</button></article>`;
  if (message.kind === 'image' && /^image\/(png|jpeg|webp|gif)$/.test(message.mediaType)) return `<article class="message user"><img class="message-image" src="data:${message.mediaType};base64,${escapeHtml(message.data)}" alt="${escapeHtml(message.name || '已添加的图片')}"></article>`;
  if (message.kind === 'tool') return `<details class="tool-card ${message.status === 'failed' ? 'error' : ''}"><summary>${icon('terminal')}${escapeHtml(message.name)}<span class="tool-status">${({ running: '执行中', success: '已完成', failed: '失败' })[message.status]}${message.elapsed != null ? ` · ${message.elapsed} ms` : ''}</span></summary><pre>${escapeHtml(JSON.stringify(message.params, null, 2))}</pre>${message.output ? `<pre>${escapeHtml(message.output)}</pre>` : ''}</details>`;
  if (message.kind === 'agent') return `<div class="tool-card"><div class="agent-summary">${icon('code')}${escapeHtml(message.text)} · ${message.status === 'running' ? '子代理执行中' : message.status === 'success' ? '已完成' : '未完成'}</div></div>`;
  if (message.kind === 'permission') {
    const decisions = { allow_once: '已允许本次执行', always_allow: '已始终允许此工具', deny_once: '已拒绝本次执行', always_deny: '已始终拒绝此工具', disconnected: '连接中断，审批状态未确认', expired: '此次审批已结束' };
    return `<section class="permission-card"><h3>允许执行 ${escapeHtml(message.name)}？</h3><p>MiniClaude 需要你的许可才能继续。</p><pre>${escapeHtml(message.preview || '')}</pre>${message.decision ? `<p>${escapeHtml(decisions[message.decision] || message.decision)}</p>` : `<div class="permission-actions">${[['allow_once', '允许一次'], ['deny_once', '拒绝'], ['always_allow', '始终允许此工具']].map(([decision, label]) => `<button type="button" data-approval="${index}" data-decision="${decision}" ${!state.connected || message.submitting ? 'disabled' : ''}>${label}</button>`).join('')}</div><p class="permission-footnote">“始终允许”会保存到本机工具权限策略。</p>`}</section>`;
  }
  return `<p class="message-meta message-notice">${escapeHtml(message.text)}</p>`;
}

// 只替换内容发生变化的消息节点，保留展开的工具详情和其他按钮的焦点。
function renderMessages(conversation) {
  const container = $('#messages');
  const switched = visibleConversation !== conversation.id;
  const follow = switched || container.scrollHeight - container.scrollTop - container.clientHeight < 100;
  if (switched) { container.replaceChildren(); messageNodes.clear(); visibleConversation = conversation.id; }
  conversation.messages.forEach((message, index) => {
    const html = messageHtml(message, index);
    let entry = messageNodes.get(index);
    if (!entry) {
      const node = document.createElement('div');
      container.append(node);
      entry = { node, html: null };
      messageNodes.set(index, entry);
    }
    if (entry.html !== html) {
      const open = entry.node.querySelector('details')?.open;
      entry.node.innerHTML = html;
      if (open && entry.node.querySelector('details')) entry.node.querySelector('details').open = true;
      entry.html = html;
    }
  });
  for (const [index, entry] of messageNodes) if (index >= conversation.messages.length) { entry.node.remove(); messageNodes.delete(index); }
  let status = container.querySelector('.conversation-status');
  if (!status) { status = document.createElement('div'); status.className = 'conversation-status'; }
  container.append(status);
  const labels = { sending: '正在发送…', running: 'MiniClaude 正在处理…', waiting: '等待你的审批', interrupted: '连接曾中断，本轮结果可能不完整。' };
  const statusHtml = (conversation.needsHistorySync
    ? `<div class="sync-notice">${labels.interrupted}<button data-action="sync-history">恢复完整对话</button></div>` : '')
    + (conversation.failedDraft ? '<div class="sync-notice"><button data-action="recover-draft">恢复上次输入和附件</button></div>' : '')
    + (labels[conversation.status] && conversation.status !== 'interrupted' ? `<div class="run-status" role="status">${labels[conversation.status]}</div>` : '');
  if (status.innerHTML !== statusHtml) status.innerHTML = statusHtml;
  if (follow) container.scrollTop = container.scrollHeight;
}

// 事件面板只展示当前会话最近 200 条原始事件。
function renderEvents() {
  if ($('#event-panel').hidden) return;
  const events = state.events[state.activeId] || [];
  if (eventsConversation !== state.activeId) { eventNodes.clear(); $('#event-list').replaceChildren(); eventsConversation = state.activeId; }
  if (!events.length) { $('#event-list').innerHTML = '<li class="empty-state">发送消息后，事件将显示在这里。</li>'; return; }
  $('#event-list .empty-state')?.remove();
  for (const event of events) if (!eventNodes.has(event)) {
    const node = document.createElement('li');
    node.className = 'event-item';
    node.innerHTML = `<details><summary><code>${escapeHtml(event.type)}</code><time>${escapeHtml(event.ts?.slice(11, 19) || '')}</time></summary><pre>${escapeHtml(JSON.stringify(event, null, 2))}</pre></details>`;
    eventNodes.set(event, node);
    $('#event-list').prepend(node);
  }
  const retained = new Set(events);
  for (const [event, node] of eventNodes) if (!retained.has(event)) { node.remove(); eventNodes.delete(event); }
}

// 同步消息区、侧栏和输入状态。
function render() {
  const conversation = active();
  if (!conversation) return;
  renderSidebar();
  if (!currentPage) $('#conversation-title').textContent = conversation.messages.length || conversation.sessionId ? conversation.title : '';
  $('#welcome').hidden = conversation.messages.length > 0;
  $('#empty-workspace').hidden = hasProject();
  $('#prompt').disabled = !hasProject() || switchingProject;
  $('#prompt').placeholder = hasProject() ? '随心输入，开始构建' : '先打开一个项目文件夹';
  $('#attach-button').disabled = !hasProject();
  $('#file-picker').disabled = !hasProject();
  $('#messages').hidden = !conversation.messages.length;
  renderMessages(conversation);
  $('#model-name').textContent = conversation.selectedModel || conversation.model || info.model || '默认模型';
  const busy = ['running', 'waiting', 'sending'].includes(conversation.status);
  $('#send-button').disabled = !hasProject() || !state.connected || conversation.stopping || attachmentLoading || (busy ? !conversation.sessionId : !$('#prompt').value.trim() && !conversation.attachments?.length);
  $('#send-button').title = conversation.stopping ? '正在停止…' : busy ? '停止当前任务' : !state.connected ? '正在连接本地服务' : '发送消息';
  $('#send-button').setAttribute('aria-label', busy ? '停止当前任务' : '发送消息');
  $('#send-button use').setAttribute('href', busy ? '#i-stop' : '#i-arrow-up');
  $('#permission-mode span').textContent = { ask: '按需审批', read_only: '只读', full_access: '完全访问' }[conversation.permissionMode || 'ask'];
  $('#permission-mode').dataset.mode = conversation.permissionMode || 'ask';
  $('#model-button').disabled = !hasProject() || busy;
  $('#permission-mode').disabled = !hasProject() || busy;
  $('#conversation-menu').hidden = !hasProject() || Boolean(currentPage);
  renderAttachments();
  renderEvents();
}

// 同一动画帧内合并流式事件渲染。
function scheduleRender() {
  if (renderFrame) return;
  renderFrame = requestAnimationFrame(() => { renderFrame = null; render(); });
}

// 重连核对项目期间不允许业务命令落到另一个项目的 Core。
function command(method, params) {
  if (!state.connected && !['workspace.list', 'workspace.pick', 'workspace.select', 'workspace.remove'].includes(method)) return Promise.reject(new Error('正在恢复项目连接，请稍后再试。'));
  return connection.command(method, params);
}

// 事件订阅和项目身份都确认后，才把界面标为可用。
function renderConnection(status) {
  $('#connection-status').dataset.state = status === 'disconnected' ? 'error' : status;
  $('#connection-status').innerHTML = `<span class="status-dot" aria-hidden="true"></span><span>${({ connected: '已连接', connecting: '连接中', disconnected: '未连接' })[status]}</span>`;
  $('#connection-status').title = status === 'connected' ? '本地服务已连接' : '正在恢复本地连接';
}

const connection = new EventConnection(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`, {
  onEvent: event => {
    if (event.type === 'workspace.changed') { changeProject(event); return; }
    if (event.type === 'workspace.projects_changed') { projectPicker.update(event); return; }
    if (!state.connected) { connectionEvents.push(event); return; }
    if (!hasProject()) return;
    if (event.type === 'schedules.changed') {
      if (currentPage === 'scheduled') views.show('scheduled');
      reconcileSessions().catch(error => toast(error.message));
      return;
    }
    if (event.type === 'session.created' && !state.conversations.some(c => c.sessionId === event.session_id)) {
      const c = createConversation(event.session_id); c.sessionId = event.session_id; c.title = event.title || '任务对话'; state.conversations.unshift(c);
    }
    const conversation = applyEvent(state, event);
    if (conversation) conversation.eventVersion = (conversation.eventVersion || 0) + 1;
    if (event.type === 'subagent.finished') reconcileSessions().catch(error => toast(error.message));
    if (!conversation) return;
    const events = state.events[conversation.id] ||= [];
    events.push(event);
    if (events.length > 200) events.shift();
    scheduleRender();
    persist();
  },
  onStatus: async status => {
    const version = ++connectionVersion;
    state.connected = false;
    renderConnection(status === 'connected' ? 'connecting' : status);
    if (status !== 'connected') connectionEvents = [];
    if (status === 'disconnected') { interruptConversations(state); persist(); }
    scheduleRender();
    if (status !== 'connected' || switchingProject) return;
    try {
      const result = await projectPicker.refresh();
      if (version !== connectionVersion || switchingProject) return;
      changeProject({ project_path: result.current_path, project_selected: result.project_selected });
      if (switchingProject) return;
      state.connected = true;
      renderConnection('connected');
      for (const event of connectionEvents.splice(0)) connection.onEvent(event);
      if (hasProject()) reconcileSessions().catch(error => toast(`对话同步失败：${error.message}`));
    } catch (error) {
      if (version !== connectionVersion || switchingProject) return;
      toast(`项目连接恢复失败：${error.message}`);
      connection.socket?.close();
    }
    scheduleRender();
  },
});

const projectPicker = new ProjectPicker({ command, onChange: changeProject, toast });

// 创建远端会话后发送消息，命令等待期间事件处理与权限审批继续运行。
async function sendMessage(event) {
  event.preventDefault();
  if (!hasProject()) return;
  const conversation = active();
  const text = $('#prompt').value.trim();
  if (['running', 'waiting', 'sending'].includes(conversation.status)) { await stopConversation(conversation); return; }
  const files = conversation.attachments || [];
  if ((!text && !files.length) || !state.connected || attachmentLoading) return;
  conversation.status = 'sending';
  conversation.mainStatus = 'sending';
  render();
  let submitted = false;
  try {
    if (!conversation.sessionId) {
      const result = await command('session.create', { mode: 'chat', title: (text || files[0]?.name || '新对话').slice(0, 40), model: conversation.selectedModel || info.model, permission_mode: conversation.permissionMode || 'ask' });
      state.conversations = state.conversations.filter(c => c.id === conversation.id || c.sessionId !== result.session_id);
      conversation.sessionId = result.session_id;
    }
    if (!conversation.messages.length) conversation.title = (text || files[0]?.name || '新对话').slice(0, 40);
    conversation.messages.push({ kind: 'text', role: 'user', text, files: files.filter(file => file.kind === 'text').map(file => file.name) });
    for (const file of files.filter(file => file.kind === 'image')) conversation.messages.push({ kind: 'image', mediaType: file.mediaType, data: file.data, name: file.name });
    conversation.draft = '';
    conversation.attachments = [];
    conversation.historyLoaded = true;
    if (active()?.id === conversation.id) $('#prompt').value = '';
    submitted = true;
    persist(); render();
    $('#messages').scrollTop = $('#messages').scrollHeight;
    await command('session.send_message', { session_id: conversation.sessionId, content: composeContent(text || '请查看附件。', files), attachments: files.filter(file => file.kind === 'image').map(file => ({ name: file.name, media_type: file.mediaType, data: file.data })) });
    if (['sending', 'running'].includes(conversation.status)) { conversation.status = 'idle'; conversation.mainStatus = 'idle'; }
  } catch (error) {
    conversation.status = state.connected ? 'error' : 'interrupted';
    conversation.mainStatus = conversation.status;
    if (submitted) { conversation.messages.push({ kind: 'notice', text: `消息未确认完成：${error.message}` }); conversation.failedDraft = { text, files }; }
    else toast(`创建对话失败：${error.message}`);
  }
  persist(); render();
}

// 审批按事件携带的 tool_use_id 回复，服务器确认后才能标为已处理。
async function approve(index, decision) {
  const conversation = active();
  const message = conversation.messages[Number(index)];
  if (!message || message.kind !== 'permission' || message.decision || message.submitting) return;
  message.submitting = true;
  render();
  try {
    await command('permission.respond', { tool_use_id: message.id, decision });
    message.decision ||= decision;
  } catch (error) { toast(`审批未成功：${error.message}`); }
  message.submitting = false;
  persist(); render();
}

// 重新载入已完成的持久化会话，恢复后可以在同一会话继续。
async function syncHistory() {
  const conversation = active();
  if (!conversation.sessionId) return;
  try {
    await reconcileSessions();
    if (['running', 'waiting', 'sending'].includes(conversation.status)) { toast('任务仍在执行，完成后会恢复完整记录。'); return; }
    await loadHistory(conversation, true);
    toast('对话已恢复，可以继续。');
  } catch (error) { toast(`恢复失败：${error.message}`); }
}

// 桌面收起侧栏，移动端通过遮罩展开，同时移除隐藏内容的键盘焦点。
function syncSidebar() {
  const mobile = matchMedia('(max-width: 760px)').matches;
  const open = mobile ? document.body.classList.contains('sidebar-open') : !document.body.classList.contains('sidebar-collapsed');
  $('#sidebar').inert = !open;
  $('#sidebar-toggle').setAttribute('aria-expanded', String(open));
  $('#sidebar-open').setAttribute('aria-expanded', String(open));
}

// 页面和会话共用前进后退历史。
function recordNavigation(entry) {
  if (JSON.stringify(navigation[navigationIndex]) === JSON.stringify(entry)) return;
  navigation = navigation.slice(0, navigationIndex + 1);
  navigation.push(entry); navigationIndex = navigation.length - 1;
  updateHistoryButtons();
}
function updateHistoryButtons() {
  $('#history-back').disabled = navigationIndex <= 0;
  $('#history-forward').disabled = navigationIndex >= navigation.length - 1;
}
function traverseHistory(delta) {
  const next = navigationIndex + delta;
  if (next < 0 || next >= navigation.length) return;
  navigationIndex = next;
  const entry = navigation[next];
  if (entry.page) showPage(entry.page, false);
  else selectConversation(entry.conversation, false);
  updateHistoryButtons();
}

// 打开管理页面时保留当前会话草稿和后台运行。
function showChat() {
  projectPicker.close(false);
  currentPage = null; views.page = null; views.generation++;
  $('#workspace-page').hidden = true;
  $('.conversation-stage').hidden = false;
  $('.composer-dock').hidden = false;
  for (const nav of document.querySelectorAll('.primary-nav [data-action]')) nav.classList.remove('selected');
}
function showPage(page, record = true) {
  projectPicker.close(false);
  currentPage = page;
  $('#workspace-page').hidden = false;
  $('.conversation-stage').hidden = true;
  $('.composer-dock').hidden = true;
  $('#event-panel').hidden = true;
  $('#event-toggle').setAttribute('aria-expanded', 'false');
  $('#conversation-title').textContent = '';
  for (const nav of document.querySelectorAll('.primary-nav [data-action]')) nav.classList.toggle('selected', nav.dataset.action === page);
  if (record) recordNavigation({ page });
  document.body.classList.remove('sidebar-open'); syncSidebar(); render();
  views.show(page);
}

// 项目变化后重新建立对应工作区缓存和事件订阅。
function changeProject(result) {
  if (switchingProject || !Object.hasOwn(result, 'project_path') || result.project_path === info.project_path) return;
  switchingProject = true;
  saveNow(); connection.close();
  $('#prompt').disabled = true;
  toast(result.project_path ? '正在打开项目…' : '项目已移除');
  location.reload();
}
// 合并 Core 会话目录，并只在没有流式更新时载入历史。
async function reconcileSessions() {
  if (!hasProject()) return;
  if (reconciling) return reconciling;
  reconciling = (async () => {
    const versions = new Map(state.conversations.map(c => [c.sessionId, c.eventVersion || 0]));
    const result = await command('session.list');
    for (const remote of result.sessions || []) {
      let conversation = state.conversations.find(c => c.sessionId === remote.session_id);
      if (!conversation) {
        conversation = createConversation(remote.session_id);
        conversation.sessionId = remote.session_id;
        state.conversations.push(conversation);
      }
      conversation.title = remote.title || '新对话';
      conversation.selectedModel = remote.model || conversation.selectedModel || info.model;
      conversation.permissionMode = remote.permission_mode || 'ask';
      conversation.updatedAt = remote.updated_at;
      const unchanged = (conversation.eventVersion || 0) === (versions.get(remote.session_id) || 0);
      if (unchanged && remote.running) {
        conversation.mainStatus = 'running'; conversation.status = 'running';
        if (remote.active_run_id) state.runs[remote.active_run_id] = { conversationId: conversation.id, parent: null };
      } else if (unchanged && conversation.mainStatus !== 'sending') {
        conversation.mainStatus = 'idle'; conversation.status = 'idle';
      }
      conversation.remoteRunning = Boolean(remote.running);
      if (unchanged && remote.pending_permissions) {
        restorePermissions(conversation, remote.pending_permissions);
        for (const item of remote.pending_permissions) if (item.run_id && !state.runs[item.run_id]) state.runs[item.run_id] = { conversationId: conversation.id, parent: item.run_id === remote.active_run_id ? null : 'restored-subagent' };
      }
    }
    state.conversations.sort((a, b) => (b.updatedAt || '').localeCompare(a.updatedAt || ''));
    render(); persist();
    if (active()?.sessionId && !active().remoteRunning && (active().eventVersion || 0) === (versions.get(active().sessionId) || 0)) await loadHistory(active(), true);
  })();
  try { await reconciling; } finally { reconciling = null; }
}
async function loadHistory(conversation, force = false) {
  if (!conversation.sessionId || conversation.loadingHistory || (conversation.historyLoaded && !force)) return;
  if (['running', 'waiting', 'sending'].includes(conversation.status) && conversation.messages.length) return;
  const version = conversation.eventVersion || 0;
  conversation.loadingHistory = true;
  try {
    const result = await command('session.get_history', { session_id: conversation.sessionId });
    if ((conversation.eventVersion || 0) !== version) return;
    conversation.messages = historyMessages(result.messages);
    conversation.historyLoaded = true;
    conversation.needsHistorySync = false;
    persist(); render();
  } finally { conversation.loadingHistory = false; }
}
async function openSession(sessionId, draft) {
  if (!sessionId) {
    newConversation();
    if (draft) { active().draft = draft; $('#prompt').value = draft; render(); persist(); }
    return;
  }
  await reconcileSessions();
  const conversation = state.conversations.find(c => c.sessionId === sessionId);
  if (conversation) selectConversation(conversation.id);
  else toast('这次运行的会话尚未创建，请稍后刷新。');
}

// 原生文件选择和拖放共用内容读取，发送前允许逐个移除。
function renderAttachments() {
  const files = active()?.attachments || [];
  const html = files.map(file => `<div class="attachment-chip">${file.kind === 'image' ? `<img src="data:${file.mediaType};base64,${escapeHtml(file.data)}" alt="">` : icon('paperclip')}<span>${escapeHtml(file.name)}<small>${Math.max(1, Math.ceil(file.size / 1024))} KB</small></span><button class="icon-button" data-remove-file="${escapeHtml(file.id)}" type="button" aria-label="移除 ${escapeHtml(file.name)}">${icon('x')}</button></div>`).join('');
  $('#attachment-list').hidden = !files.length;
  if ($('#attachment-list').innerHTML !== html) $('#attachment-list').innerHTML = html;
}
async function addFiles(files) {
  if (!hasProject() || !files.length || attachmentLoading) return;
  const conversation = active();
  attachmentLoading = true; render();
  try { conversation.attachments = await readAttachments(files, conversation.attachments || []); }
  catch (error) { toast(error.message); }
  finally { attachmentLoading = false; $('#file-picker').value = ''; render(); }
}
async function stopConversation(conversation) {
  if (!conversation?.sessionId || conversation.stopping) return;
  conversation.stopping = true; render();
  try {
    const result = await command('session.cancel', { session_id: conversation.sessionId });
    if (!result.cancelled) toast('当前没有正在执行的任务。');
    conversation.mainStatus = 'idle'; conversation.status = 'idle';
    for (const message of conversation.messages) if (message.kind === 'permission' && !message.decision) message.decision = 'expired';
  } catch (error) { toast(`停止失败：${error.message}`); }
  finally { conversation.stopping = false; persist(); render(); }
}

// 模型与权限通过会话命令应用，选择结果用于下一轮真实执行。
async function configureSession(patch, conversation = active()) {
  if (['running', 'waiting', 'sending'].includes(conversation.status)) throw new Error('请先停止当前任务，再修改会话设置。');
  if (conversation.sessionId) await command('session.configure', { session_id: conversation.sessionId, ...patch });
  if (patch.model !== undefined) { conversation.selectedModel = patch.model; preferences.model = patch.model; }
  if (patch.permission_mode !== undefined) conversation.permissionMode = patch.permission_mode;
  persist(); render();
}
async function showModel() {
  if (!hasProject()) return;
  const conversation = active();
  const version = modal('选择模型', '<p class="loading-state">正在读取可用模型…</p>');
  try {
    const result = await command('config.models');
    if (!isCurrentModal(version)) return;
    modal('选择模型', `<form id="model-form"><p>用于这个会话的下一轮对话。</p><div class="choice-list">${(result.models || []).map(model => `<label class="choice"><input type="radio" name="model-choice" value="${escapeHtml(model.id)}" ${model.id === (conversation.selectedModel || info.model) ? 'checked' : ''}><span><strong>${escapeHtml(model.label || model.id)}</strong><small>${escapeHtml(model.id)}</small></span></label>`).join('')}</div><label for="custom-model">自定义模型名称</label><input id="custom-model" value="${escapeHtml(conversation.selectedModel || info.model || '')}" required maxlength="200" placeholder="模型服务提供的模型 ID"><p class="form-error" role="alert" hidden></p><div class="modal-actions"><button type="submit" class="primary">使用此模型</button></div></form>`);
    for (const radio of document.querySelectorAll('[name="model-choice"]')) radio.addEventListener('change', () => { $('#custom-model').value = radio.value; });
    $('#model-form').addEventListener('submit', async event => {
      event.preventDefault(); const submit = event.target.querySelector('[type="submit"]'); submit.disabled = true;
      try { const model = $('#custom-model').value.trim(); if (!model) throw new Error('请输入模型名称。'); await configureSession({ model }, conversation); if (event.target.isConnected) $('#modal').close(); if (currentPage === 'profile') views.show('profile'); }
      catch (error) { const el = event.target.querySelector('.form-error'); el.textContent = error.message; el.hidden = false; }
      finally { submit.disabled = false; }
    });
  } catch (error) { if (isCurrentModal(version)) $('#modal-content').innerHTML = `<h2>选择模型</h2><p class="form-error">${escapeHtml(error.message)}</p>`; }
}
function showPermissions() {
  if (!hasProject()) return;
  const conversation = active();
  const options = [['read_only', '只读', '允许读取项目；禁止写文件、执行命令及外部工具。'], ['ask', '按需审批', '根据已有工具策略，在需要时向你申请许可。'], ['full_access', '完全访问', '允许这个会话直接执行工具，包括修改文件和运行命令。']];
  modal('工具权限', `<form id="permission-form"><div class="choice-list">${options.map(([value, title, description]) => `<label class="choice"><input type="radio" name="permission" value="${value}" ${(conversation.permissionMode || 'ask') === value ? 'checked' : ''}><span><strong>${title}</strong><small>${description}</small></span></label>`).join('')}</div><p>设置仅作用于当前会话，也适用于它启动的子代理。</p><p class="form-error" role="alert" hidden></p><div class="modal-actions"><button class="primary" type="submit">应用设置</button></div></form>`);
  $('#permission-form').addEventListener('submit', async event => {
    event.preventDefault(); const submit = event.target.querySelector('[type="submit"]'); submit.disabled = true;
    try { await configureSession({ permission_mode: new FormData(event.target).get('permission') }, conversation); if (event.target.isConnected) $('#modal').close(); }
    catch (error) { const el = event.target.querySelector('.form-error'); el.textContent = error.message; el.hidden = false; }
    finally { submit.disabled = false; }
  });
}

// 会话管理明确区分本地草稿与已经持久化的远端记录。
function conversationMenu(id = state.activeId) {
  const conversation = state.conversations.find(c => c.id === id);
  if (!conversation) return;
  modal('对话操作', `<form id="rename-form"><label for="conversation-name">对话名称</label><input id="conversation-name" value="${escapeHtml(conversation.title)}" required maxlength="120"><button class="primary" type="submit">保存名称</button><p class="form-error" role="alert" hidden></p></form><div class="conversation-actions"><button class="secondary" id="pin-conversation">${icon('pin')}${conversation.pinned ? '取消置顶' : '置顶对话'}</button><button class="secondary" id="export-conversation">${icon('paperclip')}导出对话</button><button class="danger" id="delete-conversation">删除对话</button></div>`);
  $('#rename-form').addEventListener('submit', async event => {
    event.preventDefault();
    const title = $('#conversation-name').value.trim();
    const submit = event.target.querySelector('[type="submit"]'); submit.disabled = true;
    try { if (!title) throw new Error('请输入对话名称。'); if (conversation.sessionId) await command('session.rename', { session_id: conversation.sessionId, title }); conversation.title = title; persist(); render(); if (event.target.isConnected) $('#modal').close(); }
    catch (error) { const el = event.target.querySelector('.form-error'); el.textContent = error.message; el.hidden = false; }
    finally { submit.disabled = false; }
  });
  $('#pin-conversation').addEventListener('click', () => { conversation.pinned = !conversation.pinned; persist(); render(); $('#modal').close(); });
  $('#export-conversation').addEventListener('click', () => {
    const text = ['# ' + conversation.title, ...conversation.messages.map(m => m.kind === 'text' ? `## ${m.role === 'user' ? '你' : 'MiniClaude'}\n\n${m.text}` : m.kind === 'tool' ? `### ${m.name}\n\n${m.output || ''}` : '')].join('\n\n');
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
    const link = document.createElement('a'); link.href = url; link.download = conversation.title.replace(/[/\\:*?"<>|]/g, '_').slice(0, 60) + '.md'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $('#delete-conversation').addEventListener('click', () => {
    modal('删除对话', `<p>将从本机删除“${escapeHtml(conversation.title)}”及其记录。此操作无法撤销。</p><p class="form-error" role="alert" hidden></p><div class="modal-actions"><button class="danger" id="confirm-delete-conversation">删除对话</button></div>`);
    $('#confirm-delete-conversation').addEventListener('click', async event => {
      event.target.disabled = true;
      try {
        if (conversation.sessionId) await command('session.delete', { session_id: conversation.sessionId });
        state.conversations = state.conversations.filter(c => c.id !== conversation.id);
        if (state.activeId === conversation.id) { state.activeId = null; newConversation(); }
        navigation = navigation.filter(item => item.conversation !== conversation.id); navigationIndex = navigation.length - 1; updateHistoryButtons();
        delete state.events[conversation.id]; persist(); render(); if (event.target.isConnected) $('#modal').close();
      } catch (error) { if (event.target.isConnected) { const el = $('#modal-content .form-error'); el.textContent = error.message; el.hidden = false; event.target.disabled = false; } else toast(error.message); }
    });
  });
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast('已复制'); }
  catch { toast('系统未允许剪贴板访问，请选中文本后按 ⌘C。'); }
}

const views = new WorkspaceViews({
  command, modal, toast,
  getInfo: () => info, active, onProject: changeProject, isCurrentModal,
  onSession: (id, draft) => openSession(id, draft).catch(error => toast(error.message)),
  configure: action => action === 'model' ? showModel() : showPermissions(),
});
function handleAction(action, target) {
  if (['pull-requests', 'scheduled', 'plugins', 'profile', 'changes'].includes(action)) showPage(action);
  else if (action === 'project') projectPicker.open(target);
  else if (action === 'open-project') views.perform('pick-project', target).catch(error => toast(error.message));
  else if (action === 'new-chat') newConversation();
  else if (action === 'sync-history') syncHistory();
  else if (action === 'copy-code') copyText(target.closest('.code-block').querySelector('code').textContent);
  else if (action === 'recover-draft' && active().failedDraft) {
    $('#prompt').value = active().failedDraft.text; active().draft = $('#prompt').value; active().attachments = active().failedDraft.files; delete active().failedDraft; render(); persist(); $('#prompt').focus();
  }
}

// 主输入框原生支持文件选择、图片粘贴、拖放和输入法组合输入。
$('#composer-form').addEventListener('submit', sendMessage);
$('#prompt').addEventListener('input', () => { active().draft = $('#prompt').value; render(); persist(); });
$('#prompt').addEventListener('keydown', event => {
  // WebKit 会先结束组字再派发确认键，此时只能通过 229 识别输入法回车。
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    if (!['running', 'waiting', 'sending'].includes(active().status)) $('#composer-form').requestSubmit();
  }
});
$('#file-picker').addEventListener('change', event => addFiles([...event.target.files]));
$('#attach-button').addEventListener('click', () => $('#file-picker').click());
$('#prompt').addEventListener('paste', event => {
  const files = [...(event.clipboardData?.files || [])];
  if (files.length) { event.preventDefault(); addFiles(files); }
});
let dragDepth = 0;
document.addEventListener('dragover', event => { if ([...(event.dataTransfer?.types || [])].includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; } });
document.addEventListener('dragenter', event => { if ([...(event.dataTransfer?.types || [])].includes('Files')) { event.preventDefault(); dragDepth++; $('.composer-form').classList.add('drag-over'); } });
document.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; $('.composer-form').classList.remove('drag-over'); } });
document.addEventListener('drop', event => {
  if (!event.dataTransfer?.files.length) return;
  event.preventDefault(); dragDepth = 0; $('.composer-form').classList.remove('drag-over'); showChat(); render(); addFiles([...event.dataTransfer.files]);
});
$('#new-chat').addEventListener('click', newConversation);
$('#search-button').addEventListener('click', () => {
  $('#search-input').hidden = !$('#search-input').hidden;
  $('#search-button').setAttribute('aria-expanded', String(!$('#search-input').hidden));
  if (!$('#search-input').hidden) $('#search-input').focus();
  else { $('#search-input').value = ''; renderSidebar(); }
});
$('#search-input').addEventListener('input', renderSidebar);
$('#sidebar-toggle').addEventListener('click', () => {
  if (matchMedia('(max-width: 760px)').matches) document.body.classList.remove('sidebar-open');
  else document.body.classList.add('sidebar-collapsed');
  syncSidebar(); $('#sidebar-open').focus();
});
$('#sidebar-open').addEventListener('click', () => { document.body.classList.remove('sidebar-collapsed'); document.body.classList.add('sidebar-open'); syncSidebar(); $('#sidebar-toggle').focus(); });
$('#sidebar-backdrop').addEventListener('click', () => { document.body.classList.remove('sidebar-open'); syncSidebar(); });
window.addEventListener('resize', syncSidebar);
$('#project-button').addEventListener('click', () => projectPicker.open());
$('#permission-mode').addEventListener('click', showPermissions);
$('#model-button').addEventListener('click', showModel);
$('#conversation-menu').addEventListener('click', () => conversationMenu());
$('#history-back').addEventListener('click', () => traverseHistory(-1));
$('#history-forward').addEventListener('click', () => traverseHistory(1));
$('#event-toggle').addEventListener('click', () => { $('#event-panel').hidden = !$('#event-panel').hidden; $('#event-toggle').setAttribute('aria-expanded', String(!$('#event-panel').hidden)); renderEvents(); });
$('#event-close').addEventListener('click', () => { $('#event-panel').hidden = true; $('#event-toggle').setAttribute('aria-expanded', 'false'); $('#event-toggle').focus(); });
document.addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.conversation) selectConversation(button.dataset.conversation);
  else if (button.dataset.menu) conversationMenu(button.dataset.menu);
  else if (button.dataset.removeFile) { active().attachments = active().attachments.filter(file => file.id !== button.dataset.removeFile); render(); }
  else if (button.dataset.copyMessage != null) copyText(active().messages[Number(button.dataset.copyMessage)].text);
  else if (button.dataset.approval != null) approve(button.dataset.approval, button.dataset.decision);
  else if (button.dataset.action) handleAction(button.dataset.action, button);
});
document.addEventListener('keydown', event => {
  const modifier = event.metaKey || event.ctrlKey;
  if (modifier && event.shiftKey && event.key.toLowerCase() === 'o') { event.preventDefault(); $('#modal').close(); newConversation(); }
  else if (modifier && event.key.toLowerCase() === 'k') { event.preventDefault(); $('#search-input').hidden = false; $('#search-button').setAttribute('aria-expanded', 'true'); document.body.classList.remove('sidebar-collapsed'); document.body.classList.add('sidebar-open'); syncSidebar(); $('#search-input').focus(); }
  else if (modifier && event.shiftKey && event.key.toLowerCase() === 'a') { event.preventDefault(); $('#file-picker').click(); }
  else if (modifier && event.key.toLowerCase() === 'o') { event.preventDefault(); views.perform('pick-project', $('#project-button')).catch(error => toast(error.message)); }
  else if (modifier && event.key === ',') { event.preventDefault(); showPage('profile'); }
  if (event.key === 'Escape' && document.body.classList.contains('sidebar-open')) { document.body.classList.remove('sidebar-open'); syncSidebar(); $('#sidebar-open').focus(); }
});
window.addEventListener('pagehide', () => { saveNow(); connection.close(); });
$('#modal').addEventListener('cancel', () => { modalVersion++; });
$('#modal').addEventListener('close', () => { if (!$('#modal').open) modalVersion++; });
$('#prompt').disabled = true;
try {
  const response = await fetch('/api/info');
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  info = await response.json();
  storageKey += `:${info.project_path || 'no-project'}`;
  $('#project-name').textContent = info.project_name || '选择项目';
  $('#sidebar-project-name').textContent = info.project_name || '打开项目';
} catch (error) { toast(`项目信息加载失败：${error.message}`); }
if (hasProject()) restore();
if (!active()) newConversation();
else { $('#prompt').value = active().draft; recordNavigation({ conversation: state.activeId }); render(); }
$('#prompt').disabled = !hasProject();
syncSidebar();
connection.connect();
