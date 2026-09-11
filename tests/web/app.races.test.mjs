import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { createConversation } from '../../src/mini_claude/web/static/state.js';
import { hasConversationDraft } from '../../src/mini_claude/web/static/sidebar.js';
import { readAttachments } from '../../src/mini_claude/web/static/content.js';

const source = await readFile(new URL('../../src/mini_claude/web/static/app.js', import.meta.url), 'utf8');

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
