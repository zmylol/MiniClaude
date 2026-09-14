import test from 'node:test';
import assert from 'node:assert/strict';
import { markdown, safeUrl, historyMessages, readAttachments, composeContent } from '../../src/mini_claude/web/static/content.js';
import * as contentView from '../../src/mini_claude/web/static/content.js';

// 功能：历史恢复按调用 ID 配对服务端搜索，保留正文顺序与失败状态，不暴露思考签名。
// 设计：交错两个搜索结果并混入本地工具，验证配对依赖 ID 而非最后一张卡片。
test('restores server search results without exposing opaque thinking fields', () => {
  const messages = historyMessages([{ role: 'assistant', content: [
    { type: 'thinking', thinking: 'internal', signature: 'SECRET-SIGNATURE' },
    { type: 'redacted_thinking', data: 'SECRET-REDACTED' },
    { type: 'text', text: 'Searching' },
    { type: 'server_tool_use', id: 'ok', name: 'web_search_prime', input: { query: 'Claude API' } },
    { type: 'server_tool_use', id: 'bad', name: 'web_search', input: { query: 'Other' } },
    { type: 'web_search_tool_result', tool_use_id: 'bad', content: { type: 'web_search_tool_result_error', error_code: 'max_uses_exceeded' } },
    { type: 'web_search_tool_result', tool_use_id: 'ok', content: [{ type: 'web_search_result', title: 'Docs', url: 'https://example.com/docs', encrypted_content: 'SECRET-RESULT' }] },
    { type: 'text', text: 'Found it' },
    { type: 'tool_use', id: 'local', name: 'read_file', input: { path: 'a.py' } },
  ] }]);
  assert.deepEqual(messages.map(message => message.kind), ['text', 'server_tool', 'server_tool', 'text', 'tool']);
  const searches = messages.filter(message => message.kind === 'server_tool');
  assert.equal(searches[0].params.query, 'Claude API');
  assert.deepEqual(searches[0].results, [{ title: 'Docs', url: 'https://example.com/docs' }]);
  assert.equal(searches[0].status, 'success');
  assert.equal(searches[1].status, 'failed');
  assert.equal(searches[1].error, 'max_uses_exceeded');
  assert.ok(!JSON.stringify(messages).includes('SECRET'));
});

// 功能：搜索卡片转义第三方标题和查询，结果只允许安全网页链接。
// 设计：混入脚本 URL、HTML 标题和错误文本，直接检查交给 DOM 的最终 HTML。
test('renders safe server search cards with explicit errors', () => {
  assert.equal(typeof contentView.serverToolHtml, 'function');
  const html = contentView.serverToolHtml({ kind: 'server_tool', name: 'web_search', params: { query: '<query>' }, status: 'success', results: [
    { title: '<img src=x onerror=alert(1)>', url: 'javascript:alert(1)' },
    { title: 'Docs', url: 'https://example.com/docs?a=1&b=2' },
  ] });
  assert.ok(html.includes('&lt;query&gt;'));
  assert.ok(html.includes('&lt;img'));
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('href="javascript:'));
  assert.ok(html.includes('href="https://example.com/docs?a=1&amp;b=2"'));
  const failed = contentView.serverToolHtml({ name: 'web_search', params: {}, status: 'failed', results: [], error: '<error>' });
  assert.ok(failed.includes('失败'));
  assert.ok(failed.includes('&lt;error&gt;'));
});

// 功能：实时事件面板隐藏不透明签名和密文，原始事件对象保持可供持久化的完整内容。
// 设计：把敏感字段嵌入实际内容块并检查展示 JSON，同时断言源对象没有被删除字段。
test('event preview omits opaque fields without mutating original content', () => {
  assert.equal(typeof contentView.eventJson, 'function');
  const event = { type: 'llm.response.completed', content: [
    { type: 'thinking', thinking: 'summary', signature: 'SECRET-SIGNATURE' },
    { type: 'redacted_thinking', data: 'SECRET-REDACTED' },
    { type: 'web_search_tool_result', content: [{ type: 'web_search_result', title: 'Docs', encrypted_content: 'SECRET-RESULT' }] },
  ] };
  const shown = contentView.eventJson(event);
  assert.ok(!shown.includes('SECRET'));
  assert.ok(shown.includes('Docs'));
  assert.equal(event.content[0].signature, 'SECRET-SIGNATURE');
  assert.equal(event.content[1].data, 'SECRET-REDACTED');
});

test('renders code and links without allowing executable HTML or protocols', () => {
  const html = markdown('<img src=x onerror=alert(1)>\n[site](https://example.com/?a=1&b=2)\n```js\nconst html = "<script>";\n```');
  assert.ok(html.includes('&lt;img'));
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('target="_blank" rel="noopener noreferrer"'));
  assert.ok(html.includes('const html = &quot;&lt;script&gt;&quot;;'));
  assert.equal(safeUrl('javascript:alert(1)'), '');
  assert.equal(safeUrl('file:///tmp/secret'), '');
});

test('restores paired tool results and real image history', () => {
  const messages = historyMessages([
    { role: 'user', content: [{ type: 'text', text: 'Read this' }, { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'AAAA' } }] },
    { role: 'assistant', content: [{ type: 'tool_use', id: '1', name: 'read_file', input: { path: 'a.py' } }] },
    { role: 'user', content: [{ type: 'tool_result', tool_use_id: '1', content: 'Permission denied', is_error: true }] },
  ]);
  assert.deepEqual(messages.map(m => m.kind), ['text', 'image', 'tool']);
  assert.equal(messages[2].output, 'Permission denied');
  assert.equal(messages[2].status, 'failed');
});

test('file picker sends file contents and rejects unsupported binary files', async () => {
  const files = await readAttachments([new File(['print("hello")'], 'example.py', { type: 'text/plain' })]);
  assert.ok(composeContent('Review', files).includes('print("hello")'));
  assert.ok(composeContent('Review', files).includes('example.py'));
  await assert.rejects(readAttachments([new File([new Uint8Array([0, 12, 0])], 'data.bin')]), /二进制/);
  await assert.rejects(readAttachments([new File([new Uint8Array([255, 255])], 'bad.txt')]), /UTF-8/);
  assert.deepEqual(files.map(f => f.name), ['example.py']);
});

test('rejects oversized files before reading and preserves existing selection', async () => {
  const existing = await readAttachments([new File(['one'], 'one.txt')]);
  let read = false;
  await assert.rejects(readAttachments([{ name: 'big.txt', type: 'text/plain', size: 300000, arrayBuffer() { read = true; } }], existing), /256 KB/);
  assert.equal(read, false);
  assert.equal(existing.length, 1);
  const merged = await readAttachments([new File(['one'], 'one.txt')], existing);
  assert.equal(merged.length, 1);
});

test('encodes selected images as real base64 payloads', async () => {
  const bytes = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);
  const files = await readAttachments([new File([bytes], 'picture.png', { type: 'image/png' })]);
  assert.equal(files[0].mediaType, 'image/png');
  assert.equal(files[0].data, Buffer.from(bytes).toString('base64'));
  assert.equal(composeContent('look', files), 'look');
});
