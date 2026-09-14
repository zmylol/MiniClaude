import test from 'node:test';
import assert from 'node:assert/strict';
import { createConversation, applyEvent, interruptConversations, restorePermissions } from '../../src/mini_claude/web/static/state.js';

// 功能：服务端搜索完成事件校正残句且可幂等重放，混合本地工具只生成一次执行卡片。
// 设计：重复完成事件和迟到 token，再发送下一步回复，检查搜索与本地工具的独立归属。
test('reconciles mixed server search content without duplicate text or local tools', () => {
  const conversation = createConversation('a'); conversation.sessionId = 's';
  const state = { conversations: [conversation], runs: {} };
  const completed = { type: 'llm.response.completed', session_id: 's', run_id: 'r', step: 1, text: 'BeforeAfter', stop_reason: 'tool_use', content: [
    { type: 'text', text: 'Before' },
    { type: 'server_tool_use', id: 'search', name: 'web_search', input: { query: 'docs' } },
    { type: 'web_search_tool_result', tool_use_id: 'search', content: [{ type: 'web_search_result', title: 'Docs', url: 'https://example.com' }] },
    { type: 'text', text: 'After' },
    { type: 'tool_use', id: 'local', name: 'read_file', input: { path: 'a.py' } },
  ] };
  for (const event of [
    { type: 'llm.token', session_id: 's', run_id: 'r', step: 1, token: 'partial' },
    completed,
    { type: 'tool.call_started', session_id: 's', run_id: 'r', tool_use_id: 'local', tool_name: 'read_file', params: { path: 'a.py' } },
    completed,
    { type: 'llm.token', session_id: 's', run_id: 'r', step: 1, token: 'late' },
    { type: 'llm.response.completed', session_id: 's', run_id: 'r', step: 2, text: 'Done' },
  ]) applyEvent(state, event);
  assert.deepEqual(conversation.messages.map(message => message.kind), ['text', 'tool', 'text']);
  assert.deepEqual(conversation.messages[0].blocks?.map(message => message.kind), ['text', 'server_tool', 'text']);
  assert.equal(conversation.messages[0].stopReason, 'tool_use');
  assert.equal(conversation.messages[0].blocks[1].results[0].title, 'Docs');
  assert.equal(conversation.messages[2].text, 'Done');
});

// 功能：暂停续写的搜索结果更新先前步骤卡片，相同调用 ID 在不同运行之间不串线。
// 设计：交错两个运行的同 ID 搜索，再分别返回成功与失败并重放旧事件，检查原卡片的最终状态。
test('pairs server search results across steps and isolates identical ids across runs', () => {
  const conversation = createConversation('a'); conversation.sessionId = 's';
  const state = { conversations: [conversation], runs: {} };
  const started = run => ({ type: 'llm.response.completed', session_id: 's', run_id: run, step: 1, text: `Before ${run}`, stop_reason: 'pause_turn', content: [
    { type: 'text', text: `Before ${run}` },
    { type: 'server_tool_use', id: 'shared', name: 'web_search', input: { query: `${run} query` } },
  ] });
  applyEvent(state, started('a'));
  applyEvent(state, started('b'));
  const searches = () => conversation.messages.flatMap(message => message.blocks || []).filter(message => message.kind === 'server_tool');
  const completed = { type: 'llm.response.completed', session_id: 's', run_id: 'a', step: 2, text: 'After a', content: [
    { type: 'web_search_tool_result', tool_use_id: 'shared', content: [{ type: 'web_search_result', title: 'A result', url: 'https://example.com/a' }] },
    { type: 'text', text: 'After a' },
  ] };
  applyEvent(state, completed);
  assert.equal(searches()[0].status, 'success');
  assert.equal(searches()[0].results[0].title, 'A result');
  assert.equal(searches()[1].status, 'running');
  applyEvent(state, { ...completed, run_id: 'b', text: '', content: [
    { type: 'web_search_tool_result', tool_use_id: 'shared', content: { type: 'web_search_tool_result_error', error_code: 'unavailable' } },
  ] });
  applyEvent(state, completed);
  applyEvent(state, started('a'));
  assert.equal(searches().length, 2);
  assert.equal(searches()[0].status, 'success');
  assert.equal(searches()[1].status, 'failed');
  assert.equal(searches()[1].error, 'unavailable');
  assert.equal(conversation.messages.filter(message => message.text === 'After a').length, 1);
});

// 功能：响应停止原因以可理解的中文呈现，截断正文仍保留。
// 设计：覆盖模型停止与服务端工具不可用，并检查最终状态和对应提示。
test('explains model stop reasons without removing returned text', () => {
  for (const [reason, label] of [['max_tokens', '输出达到上限'], ['model_context_window_exceeded', '上下文已满'], ['refusal', '模型拒绝'], ['unexpected_stop_reason', '响应不完整'], ['incomplete_response', '响应不完整'], ['server_tool_unavailable', '服务器搜索不可用']]) {
    const conversation = createConversation(reason); conversation.sessionId = 's';
    const state = { conversations: [conversation], runs: {} };
    applyEvent(state, { type: 'llm.response.completed', session_id: 's', run_id: 'r', step: 1, text: 'Partial answer', stop_reason: reason });
    applyEvent(state, { type: 'run.finished', session_id: 's', run_id: 'r', status: 'failed', reason });
    assert.equal(conversation.messages[0].text, 'Partial answer');
    assert.ok(conversation.messages.at(-1).text.includes(label));
  }
});

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

// 功能：完整响应校正临时文本，空文本与失败清除残句，旧响应不会覆盖下一轮。
// 设计：重放完成、迟到 token 和多步骤交错，要求按 run 与 step 关联而不是覆盖最后一条文本。
test('reconciles authoritative responses with replay and failed attempts', () => {
  const c = createConversation('a'); c.sessionId = 's';
  const state = { conversations: [c], runs: {} };
  for (const event of [
    { type: 'llm.token', run_id: 'r', step: 1, token: 'half' },
    { type: 'llm.response.completed', run_id: 'r', step: 1, text: 'complete' },
    { type: 'llm.token', run_id: 'r', step: 1, token: 'late' },
    { type: 'llm.token', run_id: 'r', step: 2, token: 'empty half' },
    { type: 'llm.response.completed', run_id: 'r', step: 2, text: '' },
    { type: 'llm.token', run_id: 'r', step: 3, token: 'failed half' },
    { type: 'llm.response.failed', run_id: 'r', step: 3, reason: 'cancelled' },
    { type: 'llm.response.completed', run_id: 'next', step: 1, text: 'next answer' },
    { type: 'llm.response.completed', run_id: 'r', step: 1, text: 'complete' },
  ]) applyEvent(state, { ...event, session_id: 's' });
  assert.deepEqual(c.messages.filter(m => m.kind === 'text').map(m => m.text), ['complete', '', '', 'next answer']);
});

// 功能：重连错过子运行开始事件时仍用显式归属隔离其回复和完成状态。
// 设计：直接投递带 session/root/parent 的子运行事件，并在下一轮后回放前轮完成。
test('uses explicit child ownership without prior start and ignores old completion status', () => {
  const c = createConversation('a'); c.sessionId = 's';
  const state = { conversations: [c], runs: {} };
  applyEvent(state, { type: 'run.started', run_id: 'root', session_id: 's' });
  for (const event of [
    { type: 'llm.response.completed', text: 'child analysis', step: 1 },
    { type: 'run.finished', status: 'success' },
  ]) applyEvent(state, { ...event, run_id: 'child', root_run_id: 'root', parent_run_id: 'root', session_id: 's' });
  assert.equal(c.status, 'running');
  assert.equal(c.messages.filter(m => m.kind === 'text').length, 0);
  applyEvent(state, { type: 'run.started', run_id: 'next', session_id: 's' });
  applyEvent(state, { type: 'run.finished', run_id: 'root', session_id: 's', status: 'success' });
  assert.equal(c.status, 'running');
});

// 功能：同一会话中主子运行共用工具 ID 时，审批与工具结果分别归属对应运行。
// 设计：创建两个同 ID 审批并只批准子运行，再恢复两份快照，避免按 ID 单独匹配造成覆盖。
test('separates duplicate tool identifiers across runs and restores both approvals', () => {
  const c = createConversation('a'); c.sessionId = 's';
  const state = { conversations: [c], runs: {} };
  applyEvent(state, { type: 'run.started', run_id: 'root', session_id: 's' });
  for (const runId of ['root', 'child']) applyEvent(state, { type: 'permission.requested', session_id: 's', run_id: runId, tool_use_id: 'same', tool_name: 'bash' });
  applyEvent(state, { type: 'permission.granted', session_id: 's', run_id: 'child', tool_use_id: 'same', decision: 'allow_once' });
  assert.equal(c.messages[0].decision, null);
  assert.equal(c.messages[1].decision, 'allow_once');
  assert.equal(c.status, 'waiting');
  restorePermissions(c, ['root', 'child'].map(runId => ({ tool_use_id: 'same', run_id: runId, tool_name: 'bash' })));
  assert.equal(c.messages.filter(m => m.kind === 'permission' && !m.decision).length, 2);
  applyEvent(state, { type: 'permission.denied', session_id: 's', run_id: 'child', tool_use_id: 'same', decision: 'deny_once' });
  assert.equal(c.messages[0].decision, null);
});

// 功能：恢复时的断连或过期占位状态不能遮挡随后到达的服务端真实审批结果。
// 设计：对两种临时状态分别投递拒绝及重复通知，检查最终决定被校正且提交中标记消失。
test('authoritative permission events replace provisional reconnect decisions', () => {
  for (const decision of ['expired', 'disconnected']) {
    const c = createConversation('a'); c.sessionId = 's';
    c.messages.push({ kind: 'permission', id: 't', runId: 'r', decision, submitting: true });
    const state = { conversations: [c], runs: {} };
    for (let repeat = 0; repeat < 2; repeat++) applyEvent(state, { type: 'permission.denied', session_id: 's', run_id: 'r', tool_use_id: 't', decision: 'deny_once' });
    assert.equal(c.messages[0].decision, 'deny_once');
    assert.equal(c.messages[0].submitting, false);
  }
});
