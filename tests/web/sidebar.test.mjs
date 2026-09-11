import test from 'node:test';
import assert from 'node:assert/strict';
import { sidebarConversations } from '../../src/mini_claude/web/static/sidebar.js';

// 功能：会话始终取自所属项目，远端删除不会被旧缓存复活，置顶留在组内。
// 设计：两个同名会话使用不同 ID，保留本地偏好并以远端标题和更新时间排序。
test('workspace summaries retain pins without leaking stale or foreign sessions', () => {
  const result = sidebarConversations({
    sessions: [
      { session_id: 'sess-new', title: '同名聊天', updated_at: '2026-09-11' },
      { session_id: 'sess-pin', title: '已重命名', updated_at: '2026-09-01' },
    ],
    conversations: [
      { id: 'local-pin', sessionId: 'sess-pin', title: '旧标题', pinned: true },
      { id: 'foreign', sessionId: 'sess-foreign', title: '同名聊天', pinned: true },
    ],
  });
  assert.deepEqual(result.map(item => item.sessionId), ['sess-pin', 'sess-new']);
  assert.equal(result[0].title, '已重命名');
  assert.equal(result[0].id, 'local-pin');
});

// 功能：当前项目支持正文搜索、最近排序，仅隐藏没有内容的非活动空会话。
// 设计：直接使用会话数据模拟恢复历史后的搜索，避免依赖具体 DOM 布局。
test('active workspace searches content and hides empty drafts', () => {
  const conversations = [
    { id: 'draft', title: '草稿', messages: [] },
    { id: 'old', sessionId: 'sess-old', title: '讨论', updatedAt: '2026-09-01', messages: [{ kind: 'text', text: '查找这里' }] },
    { id: 'new', sessionId: 'sess-new', title: '新讨论', updatedAt: '2026-09-11', messages: [] },
  ];
  assert.deepEqual(sidebarConversations({ conversations }).map(item => item.id), ['new', 'old']);
  assert.deepEqual(sidebarConversations({ conversations, query: '查找' }).map(item => item.id), ['old']);
  assert.deepEqual(sidebarConversations({ conversations, query: '不存在' }), []);
});

test('workspace drafts remain discoverable and searchable before the first send', () => {
  const conversations = [
    { id: 'draft', title: '新对话', draft: '尚未发送的计划', messages: [] },
    { id: 'file', title: '新对话', attachments: [{ name: 'notes.txt' }], messages: [] },
    { id: 'active', title: '新对话', messages: [] },
    { id: 'unused', title: '新对话', messages: [] },
  ];
  assert.deepEqual(sidebarConversations({ conversations, activeId: 'active' }).map(item => item.id), ['draft', 'file', 'active']);
  assert.deepEqual(sidebarConversations({ sessions: [], conversations, query: '计划' }).map(item => item.id), ['draft']);
  assert.match(sidebarConversations({ conversations })[0].title, /尚未发送的计划/);
});
