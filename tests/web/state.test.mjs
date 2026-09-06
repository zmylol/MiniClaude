import test from 'node:test';
import assert from 'node:assert/strict';
import { createConversation, applyEvent, interruptConversations, restorePermissions } from '../../src/mini_claude/web/static/state.js';

test('reconnection restores only still-pending approvals', () => {
  const conversation = createConversation('restore');
  conversation.mainStatus = 'running';
  conversation.messages.push({ kind: 'permission', id: 'live', decision: 'disconnected' }, { kind: 'permission', id: 'expired', decision: 'disconnected' });
  restorePermissions(conversation, [{ tool_use_id: 'live', tool_name: 'bash', run_id: 'run-live', param_preview: 'echo hello' }]);
  assert.equal(conversation.status, 'waiting');
  assert.equal(conversation.messages[0].decision, null);
  assert.equal(conversation.messages[1].decision, 'expired');
  assert.equal(conversation.messages[0].runId, 'run-live');
  restorePermissions(conversation, []);
  assert.equal(conversation.messages[0].decision, 'expired');
  assert.equal(conversation.status, 'running');
});

// 功能：多个会话交错输出时各自接收自己的文本，未知运行不进入当前对话。
// 设计：交错发送两个运行和外部 CLI 事件，检查独立消息而非内部映射实现。
test('routes interleaved streams and ignores foreign runs', () => {
  const a = createConversation('a'); a.sessionId = 'session-a';
  const b = createConversation('b'); b.sessionId = 'session-b';
  const state = { conversations: [a, b], runs: {} };
  applyEvent(state, { type: 'run.started', run_id: 'r1', session_id: a.sessionId });
  applyEvent(state, { type: 'run.started', run_id: 'r2', session_id: b.sessionId });
  applyEvent(state, { type: 'llm.token', run_id: 'r1', token: '你好' });
  applyEvent(state, { type: 'llm.token', run_id: 'foreign', token: '秘密' });
  applyEvent(state, { type: 'llm.token', run_id: 'r2', token: '另一个' });
  applyEvent(state, { type: 'llm.token', run_id: 'r1', token: '世界' });
  assert.equal(a.messages[0].text, '你好世界');
  assert.equal(b.messages[0].text, '另一个');
  assert.equal(a.messages.length, 1);
});

// 功能：工具执行把回复分成前后两段，失败原因完整可见。
// 设计：模拟先解释、调用失败、再解释的事件顺序，避免文字与工具顺序错乱。
test('preserves text and tool order and reports failures', () => {
  const c = createConversation('a'); c.sessionId = 's';
  const state = { conversations: [c], runs: {} };
  for (const e of [
    { type: 'run.started', session_id: 's' },
    { type: 'llm.token', token: '检查文件' },
    { type: 'tool.call_started', tool_use_id: 't', tool_name: 'read_file', params: { path: 'x' } },
    { type: 'tool.call_failed', tool_use_id: 't', error_message: '不存在', elapsed_ms: 2 },
    { type: 'llm.token', token: '文件不存在' },
    { type: 'run.finished', status: 'failed', reason: 'llm_error' },
  ]) applyEvent(state, { ...e, run_id: 'r' });
  assert.deepEqual(c.messages.slice(0, 3).map(m => m.kind), ['text', 'tool', 'text']);
  assert.equal(c.messages[1].output, '不存在');
  assert.equal(c.messages[1].status, 'failed');
  assert.equal(c.status, 'error');
});

// 功能：审批结束或连接丢失后旧审批不可继续提交。
// 设计：覆盖拒绝与断线两种实际路径，确保按钮状态依事件变化。
test('settles approvals and interrupts pending work on disconnect', () => {
  const c = createConversation('a'); c.sessionId = 's';
  const state = { conversations: [c], runs: {} };
  applyEvent(state, { type: 'run.started', run_id: 'r', session_id: 's' });
  applyEvent(state, { type: 'permission.requested', run_id: 'r', session_id: 's', tool_use_id: 't', tool_name: 'bash' });
  assert.equal(c.status, 'waiting');
  applyEvent(state, { type: 'permission.denied', run_id: 'r', tool_use_id: 't', decision: 'deny_once' });
  assert.equal(c.messages[0].decision, 'deny_once');
  applyEvent(state, { type: 'permission.requested', run_id: 'r', session_id: 's', tool_use_id: 't2', tool_name: 'bash' });
  interruptConversations(state);
  assert.equal(c.status, 'interrupted');
  assert.equal(c.messages[1].decision, 'disconnected');
});

// 功能：子代理输出不混入主代理回复，子代理结束也不会结束主运行。
// 设计：在主运行中插入子运行与 token，检查主会话仍处于执行状态。
test('keeps subagent output separate from the main response', () => {
  const c = createConversation('a'); c.sessionId = 's';
  const state = { conversations: [c], runs: {} };
  for (const e of [
    { type: 'run.started', run_id: 'r', session_id: 's' },
    { type: 'subagent.started', run_id: 'child', parent_run_id: 'r', description: '分析代码' },
    { type: 'run.started', run_id: 'child', session_id: null },
    { type: 'llm.token', run_id: 'child', token: '内部分析' },
    { type: 'run.finished', run_id: 'child', status: 'success' },
    { type: 'subagent.finished', run_id: 'child', parent_run_id: 'r', status: 'success' },
  ]) applyEvent(state, e);
  assert.equal(c.status, 'running');
  assert.equal(c.messages.length, 1);
  assert.equal(c.messages[0].kind, 'agent');
  assert.equal(c.messages[0].status, 'success');
});

// 功能：后台子代理晚于主运行审批时，不锁死会话且不被主运行错误过期。
// 设计：覆盖主运行结束时仍在等待的子审批，再模拟另一次延后审批。
test('background approvals restore the finished main run state', () => {
  const c = createConversation('a'); c.sessionId = 's';
  const state = { conversations: [c], runs: {} };
  applyEvent(state, { type: 'run.started', run_id: 'r', session_id: 's' });
  applyEvent(state, { type: 'subagent.started', run_id: 'child', parent_run_id: 'r' });
  applyEvent(state, { type: 'permission.requested', run_id: 'child', session_id: 's', tool_use_id: 't' });
  applyEvent(state, { type: 'run.finished', run_id: 'r', status: 'success' });
  assert.equal(c.messages[1].decision, null);
  assert.equal(c.status, 'waiting');
  applyEvent(state, { type: 'permission.granted', run_id: 'child', tool_use_id: 't', decision: 'allow_once' });
  assert.equal(c.status, 'idle');
  applyEvent(state, { type: 'permission.requested', run_id: 'child', session_id: 's', tool_use_id: 't2' });
  applyEvent(state, { type: 'permission.granted', run_id: 'child', tool_use_id: 't2', decision: 'allow_once' });
  applyEvent(state, { type: 'subagent.finished', run_id: 'child', parent_run_id: 'r', status: 'success' });
  assert.equal(c.status, 'idle');
});

// 功能：运行结束也保留断线期间可能漏收文本的提示。
// 设计：模拟断线后重新收到完成事件，检查缺失标记独立于运行状态。
test('completion after reconnect preserves the incomplete transcript marker', () => {
  const c = createConversation('a'); c.sessionId = 's';
  const state = { conversations: [c], runs: {} };
  applyEvent(state, { type: 'run.started', run_id: 'r', session_id: 's' });
  interruptConversations(state);
  applyEvent(state, { type: 'run.finished', run_id: 'r', status: 'success' });
  assert.equal(c.status, 'idle');
  assert.equal(c.needsHistorySync, true);
});
