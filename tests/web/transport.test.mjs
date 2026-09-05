import test from 'node:test';
import assert from 'node:assert/strict';
import { EventConnection } from '../../src/mini_claude/web/static/transport.js';

class FakeSocket {
  // 保存发出的帧以验证并发请求配对。
  constructor() { this.readyState = 1; this.frames = []; }
  // 记录序列化请求。
  send(frame) { this.frames.push(JSON.parse(frame)); }
  // 模拟关闭，使用真实连接的清理路径。
  close() { if (this.readyState === 3) return; this.readyState = 3; this.onclose?.(); }
  // 模拟服务器推送或响应。
  receive(envelope) { this.onmessage({ data: JSON.stringify(envelope) }); }
}

// 功能：发送命令等待时事件继续分发，审批响应不必等待主命令。
// 设计：倒序返回两个请求的响应，验证 ID 配对与实时事件分发互不阻塞。
test('events and approval replies continue during a pending run', async () => {
  const events = [];
  const connection = new EventConnection('ws://test', { onEvent: event => events.push(event), onStatus() {} }, FakeSocket);
  connection.connect();
  const run = connection.command('session.send_message', { session_id: 's', content: 'hello' });
  const approval = connection.command('permission.respond', { tool_use_id: 't', decision: 'allow_once' });
  connection.socket.receive({ kind: 'event', event: { type: 'llm.token', token: '你好' } });
  assert.equal(events[0].token, '你好');
  const [runFrame, approvalFrame] = connection.socket.frames;
  connection.socket.receive({ id: approvalFrame.id, result: { ok: true } });
  assert.deepEqual(await approval, { ok: true });
  connection.socket.receive({ id: runFrame.id, result: { run_id: 'r' } });
  assert.deepEqual(await run, { run_id: 'r' });
  connection.close();
});

// 功能：订阅确认前连接不显示为可发送。
// 设计：显式控制订阅响应时机，防止启动时丢失第一批事件。
test('becomes ready only after subscription is acknowledged', async () => {
  const states = [];
  const connection = new EventConnection('ws://test', { onEvent() {}, onStatus: status => states.push(status) }, FakeSocket);
  connection.connect();
  const open = connection.socket.onopen();
  assert.equal(connection.ready, false);
  const subscribe = connection.socket.frames[0];
  assert.equal(subscribe.method, 'event.subscribe');
  connection.socket.receive({ id: subscribe.id, result: { subscription_id: 'sub' } });
  await open;
  assert.equal(connection.ready, true);
  assert.equal(states.at(-1), 'connected');
  connection.close();
});

// 功能：连接丢失会拒绝所有等待请求且清空队列，不能误显示成功。
// 设计：在未收到运行响应时关闭连接，验证错误信息并禁用自动重连计时器。
test('disconnect rejects pending work without replaying commands', async () => {
  const connection = new EventConnection('ws://test', { onEvent() {}, onStatus() {} }, FakeSocket);
  connection.connect();
  const run = connection.command('session.send_message');
  const assertion = assert.rejects(run, /完成状态尚未确认/);
  connection.close();
  await assertion;
  assert.equal(connection.pending.size, 0);
  assert.equal(connection.socket.frames.length, 1);
  assert.equal(connection.ready, false);
  await assert.rejects(connection.command('core.ping'), /尚未连接/);
});

// 功能：服务器业务错误保留错误码，非法 JSON 会关闭连接。
// 设计：先模拟不存在会话错误，再给损坏帧，确认错误不会变成空成功结果。
test('propagates RPC errors and closes malformed streams', async () => {
  const connection = new EventConnection('ws://test', { onEvent() {}, onStatus() {} }, FakeSocket);
  connection.connect();
  const result = connection.command('session.get_history');
  connection.socket.receive({ id: connection.socket.frames[0].id, error: { code: -32010, message: 'session not found' } });
  await assert.rejects(result, error => error.code === -32010);
  connection.stopped = true;
  connection.socket.onmessage({ data: 'invalid' });
  assert.equal(connection.socket.readyState, 3);
});
