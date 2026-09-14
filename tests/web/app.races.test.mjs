import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { applyEvent, createConversation, restorePermissions, interruptConversations } from '../../src/mini_claude/web/static/state.js';
import { hasConversationDraft } from '../../src/mini_claude/web/static/sidebar.js';
import { readAttachments, historyMessages, escapeHtml, markdown, serverToolHtml } from '../../src/mini_claude/web/static/content.js';

const source = await readFile(new URL('../../src/mini_claude/web/static/app.js', import.meta.url), 'utf8');

// 功能：离线刷新仍保留从服务端历史恢复的搜索卡片及结果。
// 设计：执行真实 restore 白名单过滤路径，缓存含顶层搜索卡而非实时嵌套内容。
test('offline restore retains server search history cards', () => {
  const messages = historyMessages([{ role: 'assistant', content: [
    { type: 'server_tool_use', id: 'search', name: 'web_search', input: { query: 'Saved query' } },
    { type: 'web_search_tool_result', tool_use_id: 'search', content: [{ type: 'web_search_result', title: 'Saved result', url: 'https://example.com' }] },
  ] }]);
  const state = { conversations: [], runs: {} };
  const sandbox = vm.createContext({
    state, storageKey: 'test', preferences: {}, createConversation, interruptConversations,
    localStorage: { getItem: () => JSON.stringify({ conversations: [{ ...createConversation('a'), messages }], activeId: 'a' }) },
    toast(message) { assert.fail(message); },
  });
  vm.runInContext(source.slice(source.indexOf('function restore('), source.indexOf('function persist(')), sandbox);
  sandbox.restore();
  assert.equal(state.conversations[0].messages.length, 1);
  assert.equal(state.conversations[0].messages[0].kind, 'server_tool');
  assert.equal(state.conversations[0].messages[0].results[0].title, 'Saved result');
});

// 功能：真实消息渲染同时支持实时响应和历史搜索，正文与查询按原顺序出现且没有重复。
// 设计：复用实际 messageHtml 与内容投影，仅替换图标函数，验证 HTML 最终输出链路。
test('message renderer displays live and historical server searches once', () => {
  const blocks = historyMessages([{ role: 'assistant', content: [
    { type: 'text', text: 'Before search' },
    { type: 'server_tool_use', id: 'search', name: 'web_search', input: { query: 'Exact query' } },
    { type: 'web_search_tool_result', tool_use_id: 'search', content: [{ type: 'web_search_result', title: 'Result title', url: 'https://example.com' }] },
    { type: 'text', text: 'After search' },
  ] }]);
  const sandbox = vm.createContext({ escapeHtml, markdown, serverToolHtml, icon: () => '' });
  vm.runInContext(source.slice(source.indexOf('function messageHtml('), source.indexOf('function renderMessages(')), sandbox);
  const live = sandbox.messageHtml({ kind: 'text', text: 'Before searchAfter search', blocks }, 0);
  const history = blocks.map((block, index) => sandbox.messageHtml(block, index)).join('');
  for (const html of [live, history]) {
    assert.equal(html.split('Before search').length, 2);
    assert.equal(html.split('Exact query').length, 2);
    assert.ok(html.indexOf('Before search') < html.indexOf('Exact query'));
    assert.ok(html.indexOf('Exact query') < html.indexOf('After search'));
    assert.ok(html.includes('Result title'));
  }
});

// 功能：漏收开始事件后可从目录快照恢复当前运行，完成后正确解锁输入。
// 设计：调用真实 reconcileSessions，分别模拟断线后新运行与快照等待期间更晚到达的开始事件。
for (const newerEvent of [false, true]) test(`reconciliation restores run identity without overwriting newer events (${newerEvent})`, async () => {
  const conversation = { ...createConversation('local'), sessionId: 'session', runId: 'old', eventVersion: 0 };
  const state = { conversations: [conversation], activeId: 'local', runs: {} };
  let finishList;
  const sandbox = vm.createContext({
    state, reconciling: null, info: { model: 'fixture' },
    hasProject: () => true, active: () => conversation,
    command: () => new Promise(resolve => { finishList = resolve; }),
    createConversation, hasConversationDraft, restorePermissions,
    render() {}, persist() {}, loadHistory() {},
  });
  vm.runInContext(source.slice(source.indexOf('async function reconcileSessions()'), source.indexOf('async function loadHistory(')), sandbox);
  const reconciliation = sandbox.reconcileSessions();
  if (newerEvent) {
    applyEvent(state, { type: 'run.started', session_id: 'session', run_id: 'newest' });
    conversation.eventVersion += 1;
  }
  finishList({ sessions: [{ session_id: 'session', running: true, active_run_id: 'new', pending_permissions: [] }] });
  await reconciliation;
  const current = newerEvent ? 'newest' : 'new';
  assert.equal(conversation.runId, current);
  assert.equal(conversation.status, 'running');
  applyEvent(state, { type: 'llm.response.completed', session_id: 'session', run_id: current, step: 1, text: 'done' });
  applyEvent(state, { type: 'run.finished', session_id: 'session', run_id: current, status: 'success' });
  applyEvent(state, { type: 'session.waiting_for_input', session_id: 'session', last_run_id: current });
  assert.equal(conversation.mainStatus, 'idle');
  assert.equal(conversation.status, 'idle');
  assert.equal(conversation.messages.at(-1).text, 'done');
});

// 功能：审批空成功只表示请求已处理，最终决定必须来自服务端事件。
// 设计：执行真实 approve 函数后再投递相反决定，验证 UI 不会提前显示本地允许。
test('approval RPC acknowledgment waits for authoritative denial', async () => {
  const conversation = { sessionId: 's', messages: [{ kind: 'permission', id: 't', runId: 'r', decision: null }] };
  const sandbox = vm.createContext({
    state: {}, active: () => conversation, command: async () => ({}),
    render() {}, persist() {}, toast() {},
    applyEvent(_state, event) { conversation.messages[0].decision ||= event.decision; },
  });
  vm.runInContext(source.slice(source.indexOf('async function approve('), source.indexOf('async function syncHistory(')), sandbox);
  await sandbox.approve(0, 'allow_once');
  assert.equal(conversation.messages[0].decision, null);
  assert.equal(conversation.messages[0].submitting, true);
  sandbox.applyEvent(sandbox.state, { decision: 'deny_once' });
  assert.equal(conversation.messages[0].decision, 'deny_once');
});

// 调用真实应用函数和文件读取逻辑，仅替换 DOM 与远端目录，控制两个异步操作的先后。
test('attachment reading survives server reconciliation replacing its conversation with a draft', async () => {
  const original = { ...createConversation('local'), sessionId: 'deleted', draft: 'keep text' };
  const state = { conversations: [original], activeId: 'local' };
  let finishRead;
  const file = { name: 'notes.txt', size: 8, type: 'text/plain', arrayBuffer: () => new Promise(resolve => { finishRead = resolve; }) };
  const sandbox = vm.createContext({
    state, attachmentLoading: false, reconciling: null,
    hasProject: () => true, active: () => state.conversations.find(c => c.id === state.activeId),
    command: async () => ({ sessions: [] }), createConversation, hasConversationDraft, readAttachments,
    render() {}, persist() {}, toast(message) { assert.fail(message); }, $: () => ({ value: '' }),
  });
  vm.runInContext(source.slice(source.indexOf('async function reconcileSessions()'), source.indexOf('async function loadHistory(')), sandbox);
  vm.runInContext(source.slice(source.indexOf('async function addFiles('), source.indexOf('async function stopConversation(')), sandbox);
  const adding = sandbox.addFiles([file]);
  await sandbox.reconcileSessions();
  finishRead(new TextEncoder().encode('contents').buffer);
  await adding;
  const current = state.conversations[0];
  assert.equal(current.sessionId, null);
  assert.equal(current.draft, 'keep text');
  assert.equal(current.attachments.length, 1);
  assert.equal(current.attachments[0].name, 'notes.txt');
  assert.equal(current.attachments[0].text, 'contents');
  assert.equal(sandbox.attachmentLoading, false);
});
