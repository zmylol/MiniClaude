import test from 'node:test';
import assert from 'node:assert/strict';
import {
  applyEvent, createConversation, interruptConversations, restorePermissions,
} from '../../src/mini_claude/web/static/state.js';

// 创建带独立草稿的会话，便于检查事件归属之外的用户输入也未被覆盖。
function fixture(count) {
  const conversations = Array.from({ length: count }, (_, index) => ({
    ...createConversation(`local-${index}`),
    sessionId: `session-${index}`, draft: `尚未发送-${index}`,
  }));
  return { conversations, runs: {} };
}

// 为事件添加实际协议中的会话、根运行和父运行归属。
function scope(index, runId = `root-${index}`, rootRunId = runId, parentRunId = null) {
  return { session_id: `session-${index}`, run_id: runId, root_run_id: rootRunId, parent_run_id: parentRunId };
}

// 固定种子只决定队列间穿插顺序，各条事件队列始终保持原始顺序。
function* interleave(queues, seed) {
  const pending = queues.map(queue => [...queue]);
  while (pending.some(queue => queue.length)) {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    const available = pending.filter(queue => queue.length);
    yield available[seed % available.length].shift();
  }
}

// 每次投递都验证其他会话的完整可见状态未变化，而非只检查最终文本。
function deliver(state, queues, seed) {
  for (const event of interleave(queues, seed)) {
    const before = state.conversations.map(conversation => structuredClone(conversation));
    applyEvent(state, event);
    state.conversations.forEach((conversation, index) => {
      if (conversation.sessionId !== event.session_id) {
        assert.deepEqual(conversation, before[index], `${event.type} from ${event.run_id} changed ${conversation.sessionId}`);
      }
      assert.equal(conversation.draft, `尚未发送-${index}`);
    });
  }
}

// 将运行内的顺序事件绑定到同一归属，便于从场景直接阅读实际事件顺序。
function events(owner, sequence) {
  return sequence.map(event => ({ ...owner, ...event }));
}

// 功能：十二会话交错流式输出、同 ID 工具及审批时，空响应和失败都不会留下残句或串会话。
// 设计：三个固定种子穿插真实的两步骤顺序，每次事件检查旁路会话，最终核对完整可见结果。
test('twelve sessions retain independent two-step responses across seeded interleavings', () => {
  for (const seed of [0xc0ffee, 0xabc123, 0xdecafbad]) {
    const state = fixture(12);
    const queues = state.conversations.map((_, index) => {
      const failedResponse = index % 3 === 2;
      const allowed = index % 2 === 0;
      return events(scope(index), [
        { type: 'run.started' },
        { type: 'step.started', step: 1 },
        { type: 'llm.token', step: 1, token: `暂存-${index}\n` },
        { type: 'llm.token', step: 1, token: '断流前残句' },
        { type: 'llm.response.completed', step: 1, text: `完整解释-${index}` },
        { type: 'tool.call_started', tool_use_id: 'shared-tool', tool_name: 'bash', params: { command: `echo ${index}` } },
        { type: 'permission.requested', tool_use_id: 'shared-tool', tool_name: 'bash', param_preview: `echo ${index}` },
        { type: allowed ? 'permission.granted' : 'permission.denied', tool_use_id: 'shared-tool', decision: allowed ? 'allow_once' : 'deny_once' },
        allowed
          ? { type: 'tool.call_finished', tool_use_id: 'shared-tool', output: `执行结果-${index}` }
          : { type: 'tool.call_failed', tool_use_id: 'shared-tool', error_message: `拒绝-${index}` },
        { type: 'step.started', step: 2 },
        { type: 'llm.token', step: 2, token: `第二次残句-${index}` },
        failedResponse
          ? { type: 'llm.response.failed', step: 2, reason: 'llm_error' }
          : { type: 'llm.response.completed', step: 2, text: index % 3 === 0 ? `最终答案-${index}` : '' },
        { type: 'run.finished', status: failedResponse ? 'failed' : 'success', reason: failedResponse ? 'llm_error' : null },
      ]);
    });
    deliver(state, queues, seed);
    state.conversations.forEach((conversation, index) => {
      assert.deepEqual(conversation.messages.filter(message => message.kind === 'text').map(message => message.text), [
        `完整解释-${index}`, index % 3 === 0 ? `最终答案-${index}` : '',
      ]);
      const [tool] = conversation.messages.filter(message => message.kind === 'tool');
      assert.equal(tool.output, index % 2 === 0 ? `执行结果-${index}` : `拒绝-${index}`);
      assert.equal(tool.status, index % 2 === 0 ? 'success' : 'failed');
      assert.deepEqual(conversation.messages.filter(message => message.kind === 'permission').map(message => message.decision), [index % 2 === 0 ? 'allow_once' : 'deny_once']);
      assert.equal(conversation.status, index % 3 === 2 ? 'error' : 'idle');
    });
  }
});

// 功能：八会话的后台审批可晚于主运行，旧子运行随后结束也不会解锁正在执行的新一轮。
// 设计：以阶段屏障建立合法跨轮顺序，再交错子运行事件，检查等待、空闲和执行中的实际输入状态。
test('background approvals after completion and old children during the next turn stay isolated', () => {
  const state = fixture(8);
  const children = state.conversations.map((_, index) => scope(index, `child-${index}`, `root-${index}`, `root-${index}`));
  deliver(state, state.conversations.map((_, index) => [
    ...events(scope(index), [{ type: 'run.started' }]),
    ...events(children[index], [{ type: 'subagent.started', description: `后台任务-${index}` }, { type: 'run.started' }]),
    ...events(scope(index), [{ type: 'run.finished', status: 'success' }]),
  ]), 0x123456);
  assert.ok(state.conversations.every(conversation => conversation.status === 'idle'));

  deliver(state, children.map(owner => events(owner, [
    { type: 'tool.call_started', tool_use_id: 'shared-tool', tool_name: 'bash', params: {} },
    { type: 'permission.requested', tool_use_id: 'shared-tool', tool_name: 'bash' },
  ])), 0x345678);
  assert.ok(state.conversations.every(conversation => conversation.status === 'waiting'));
  deliver(state, children.map(owner => events(owner, [
    { type: 'permission.granted', tool_use_id: 'shared-tool', decision: 'allow_once' },
    { type: 'tool.call_finished', tool_use_id: 'shared-tool', output: '后台结果' },
  ])), 0x56789a);
  assert.ok(state.conversations.every(conversation => conversation.status === 'idle'));

  deliver(state, state.conversations.map((_, index) => events(scope(index, `next-${index}`), [
    { type: 'run.started' }, { type: 'llm.token', step: 1, token: `新一轮-${index}` },
  ])), 0x789abc);
  deliver(state, children.map((owner, index) => events(owner, [
    { type: 'llm.token', step: 2, token: `后台残句-${index}` },
    { type: 'llm.response.completed', step: 2, text: `后台完成-${index}` },
    { type: 'run.finished', status: 'success' },
    { type: 'subagent.finished', status: 'success' },
  ])), 0x9abcde);
  state.conversations.forEach((conversation, index) => {
    assert.equal(conversation.status, 'running');
    assert.equal(conversation.runId, `next-${index}`);
    assert.deepEqual(conversation.messages.filter(message => message.kind === 'text').map(message => message.text), [`新一轮-${index}`]);
    assert.deepEqual(conversation.messages.filter(message => message.kind === 'agent').map(message => message.status), ['success']);
    assert.equal(conversation.messages.find(message => message.kind === 'permission').decision, 'allow_once');
  });
});

// 功能：同会话主子运行及不同会话共用工具 ID 时，一次审批只解除对应请求，子运行结束不影响主运行。
// 设计：十会话各挂两份同 ID 审批，分别拒绝子运行、批准主运行并核对可见卡片和工具结果。
test('twenty simultaneous approvals sharing one tool ID settle only their own requests', () => {
  const state = fixture(10);
  const children = state.conversations.map((_, index) => scope(index, `child-${index}`, `root-${index}`, `root-${index}`));
  deliver(state, state.conversations.map((_, index) => [
    ...events(scope(index), [{ type: 'run.started' }]),
    ...events(children[index], [{ type: 'subagent.started', description: '并行子任务' }, { type: 'run.started' }]),
    ...events(scope(index), [
      { type: 'tool.call_started', tool_use_id: 'same', tool_name: 'bash', params: {} },
      { type: 'permission.requested', tool_use_id: 'same', tool_name: 'bash' },
    ]),
    ...events(children[index], [
      { type: 'tool.call_started', tool_use_id: 'same', tool_name: 'bash', params: {} },
      { type: 'permission.requested', tool_use_id: 'same', tool_name: 'bash' },
    ]),
  ]), 0x1337);
  deliver(state, children.map(owner => events(owner, [
    { type: 'permission.denied', tool_use_id: 'same', decision: 'deny_once' },
    { type: 'tool.call_failed', tool_use_id: 'same', error_message: '已拒绝子任务' },
    { type: 'run.finished', status: 'success' },
    { type: 'subagent.finished', status: 'success' },
  ])), 0x2468);
  state.conversations.forEach(conversation => {
    assert.equal(conversation.status, 'waiting');
    assert.deepEqual(conversation.messages.filter(message => message.kind === 'permission').map(message => message.decision), [null, 'deny_once']);
    assert.equal(conversation.messages.find(message => message.kind === 'tool').status, 'running');
  });
  deliver(state, state.conversations.map((_, index) => events(scope(index), [
    { type: 'permission.granted', tool_use_id: 'same', decision: 'allow_once' },
    { type: 'tool.call_finished', tool_use_id: 'same', output: `主任务-${index}` },
  ])), 0x3579);
  state.conversations.forEach((conversation, index) => {
    assert.equal(conversation.status, 'running');
    assert.deepEqual(conversation.messages.filter(message => message.kind === 'permission').map(message => message.decision), ['allow_once', 'deny_once']);
    assert.equal(conversation.messages.find(message => message.kind === 'tool').output, `主任务-${index}`);
  });
});

// 功能：十会话重复断连及恢复审批快照不会复制卡片或复活已决定的请求。
// 设计：每次都使用当前有效快照，穿插真实决定及空快照，避免制造服务端不可能存在的过时审批。
test('repeated reconnect snapshots preserve exactly the current approvals in ten sessions', () => {
  const state = fixture(10);
  deliver(state, state.conversations.map((_, index) => [
    ...events(scope(index), [{ type: 'run.started' }]),
    ...events(scope(index, `child-${index}`, `root-${index}`, `root-${index}`), [{ type: 'subagent.started' }]),
    ...['root', 'child'].flatMap(prefix => events(
      scope(index, `${prefix}-${index}`, `root-${index}`, prefix === 'child' ? `root-${index}` : null),
      [{ type: 'permission.requested', tool_use_id: 'same', tool_name: 'bash' }],
    )),
  ]), 0x4321);
  for (let reconnect = 0; reconnect < 4; reconnect++) {
    interruptConversations(state);
    state.conversations.forEach((conversation, index) => {
      const pending = ['root', 'child'].map(prefix => ({ run_id: `${prefix}-${index}`, tool_use_id: 'same', tool_name: 'bash' }));
      restorePermissions(conversation, pending);
      const restored = structuredClone(conversation);
      restorePermissions(conversation, pending);
      assert.deepEqual(conversation, restored);
      assert.equal(conversation.status, 'waiting');
      assert.equal(conversation.messages.filter(message => message.kind === 'permission').length, 2);
    });
  }
  deliver(state, state.conversations.map((_, index) => events(scope(index), [
    { type: 'permission.denied', tool_use_id: 'same', decision: 'deny_once' },
  ])), 0x6543);
  state.conversations.forEach((conversation, index) => {
    const pending = [{ run_id: `child-${index}`, tool_use_id: 'same', tool_name: 'bash' }];
    restorePermissions(conversation, pending);
    restorePermissions(conversation, pending);
    assert.deepEqual(conversation.messages.filter(message => message.kind === 'permission').map(message => message.decision), ['deny_once', null]);
    assert.equal(conversation.status, 'waiting');
  });
  deliver(state, state.conversations.map((_, index) => events(scope(index, `child-${index}`, `root-${index}`, `root-${index}`), [
    { type: 'permission.granted', tool_use_id: 'same', decision: 'allow_once' },
  ])), 0x8765);
  state.conversations.forEach(conversation => {
    restorePermissions(conversation, []);
    restorePermissions(conversation, []);
    assert.deepEqual(conversation.messages.filter(message => message.kind === 'permission').map(message => message.decision), ['deny_once', 'allow_once']);
    assert.equal(conversation.status, 'interrupted');
    assert.equal(conversation.needsHistorySync, true);
  });
});

// 功能：重连漏收子运行开始事件时，显式归属仍隔离多层子任务、审批和主运行输入状态。
// 设计：八会话只收到主运行开始，随后交错子孙事件，验证没有子任务文本混进主回复。
test('explicit lineage restores nested children without their start events in eight sessions', () => {
  const state = fixture(8);
  deliver(state, state.conversations.map((_, index) => events(scope(index), [
    { type: 'run.started' }, { type: 'llm.token', step: 1, token: `主回复-${index}` },
  ])), 0xaabbcc);
  deliver(state, state.conversations.map((_, index) => [
    ...events(scope(index, `child-${index}`, `root-${index}`, `root-${index}`), [
      { type: 'llm.response.completed', step: 1, text: `子回复-${index}` },
    ]),
    ...events(scope(index, `grandchild-${index}`, `root-${index}`, `child-${index}`), [
      { type: 'permission.requested', tool_use_id: 'same', tool_name: 'bash' },
      { type: 'permission.granted', tool_use_id: 'same', decision: 'allow_once' },
      { type: 'llm.response.completed', step: 1, text: `孙回复-${index}` },
      { type: 'run.finished', status: 'success' },
      { type: 'subagent.finished', status: 'success' },
    ]),
    ...events(scope(index, `child-${index}`, `root-${index}`, `root-${index}`), [
      { type: 'run.finished', status: 'success' },
      { type: 'subagent.finished', status: 'success' },
    ]),
  ]), 0xddeeff);
  state.conversations.forEach((conversation, index) => {
    assert.equal(conversation.status, 'running');
    assert.equal(conversation.runId, `root-${index}`);
    assert.deepEqual(conversation.messages.filter(message => message.kind === 'text').map(message => message.text), [`主回复-${index}`]);
    assert.deepEqual(conversation.messages.filter(message => message.kind === 'agent').map(message => message.status), ['success', 'success']);
    assert.equal(conversation.messages.find(message => message.kind === 'permission').decision, 'allow_once');
  });
});

// 功能：成功、失败及取消的完成事件重复回放时，文本、错误提示和输入状态均保持幂等。
// 设计：九会话各完成一次后重放相同终态序列五次，比对完整可见状态，捕捉仅错误路径会重复的提示。
test('replaying terminal responses and run completion does not duplicate visible messages', () => {
  const state = fixture(9);
  const terminal = state.conversations.map((_, index) => events(scope(index), [
    index % 3 === 0
      ? { type: 'llm.response.completed', step: 1, text: `完成-${index}` }
      : { type: 'llm.response.failed', step: 1, reason: index % 3 === 1 ? 'llm_error' : 'cancelled' },
    { type: 'run.finished', status: index % 3 === 0 ? 'success' : 'failed', reason: [null, 'llm_error', 'cancelled'][index % 3] },
  ]));
  deliver(state, state.conversations.map((_, index) => [
    ...events(scope(index), [{ type: 'run.started' }, { type: 'llm.token', step: 1, token: `临时文本-${index}` }]),
    ...terminal[index],
  ]), 0xbada55);
  const completed = structuredClone(state.conversations);
  for (let replay = 0; replay < 5; replay++) {
    deliver(state, terminal, 0xbada55 + replay);
    assert.deepEqual(state.conversations, completed);
  }
  deliver(state, terminal.map((ending, index) => [
    ...events(scope(index), [{ type: 'run.started' }]), ...ending,
  ]), 0xbada56);
  assert.deepEqual(state.conversations, completed);
});
