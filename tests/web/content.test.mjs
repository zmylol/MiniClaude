import test from 'node:test';
import assert from 'node:assert/strict';
import { markdown, safeUrl, historyMessages, readAttachments, composeContent } from '../../src/mini_claude/web/static/content.js';

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
