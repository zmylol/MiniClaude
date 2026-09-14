import { historyMessages, applyServerToolResult } from './content.js';

// 创建尚未分配 core session 的本地会话。
export function createConversation(id) {
  return { id, sessionId: null, title: '新对话', pinned: false, messages: [], status: 'idle', mainStatus: 'idle', needsHistorySync: false, draft: '', model: '', selectedModel: '', permissionMode: 'ask', attachments: [], usage: null };
}

// 连接中断时保留文本，把运行和审批标为未确认，避免重复提交过期审批。
export function interruptConversations(state) {
  for (const conversation of state.conversations) {
    if (['running', 'waiting', 'sending'].includes(conversation.status)) {
      conversation.status = 'interrupted';
      conversation.needsHistorySync = true;
      if (['running', 'sending'].includes(conversation.mainStatus)) conversation.mainStatus = 'interrupted';
    }
    for (const message of conversation.messages) {
      if (message.kind === 'permission' && !message.decision) message.decision = 'disconnected';
    }
  }
}

// 只用 Core 当前有效的审批快照恢复按钮，过期卡片不能重新授权。
export function restorePermissions(conversation, pending) {
  const ids = new Set(pending.map(item => JSON.stringify([item.run_id, item.tool_use_id])));
  for (const message of conversation.messages) if (message.kind === 'permission' && !ids.has(JSON.stringify([message.runId, message.id])) && (!message.decision || message.decision === 'disconnected')) message.decision = 'expired';
  for (const item of pending) {
    let message = conversation.messages.find(m => m.kind === 'permission' && m.id === item.tool_use_id && (!m.runId || m.runId === item.run_id));
    if (!message) { message = { kind: 'permission', id: item.tool_use_id }; conversation.messages.push(message); }
    Object.assign(message, { name: item.tool_name, preview: item.param_preview, runId: item.run_id, decision: null, submitting: false });
  }
  refreshStatus(conversation);
}

// 审批展示状态独立于主运行状态，后台子代理结束后不会锁住输入。
function refreshStatus(conversation) {
  conversation.status = conversation.messages.some(m => m.kind === 'permission' && !m.decision)
    ? 'waiting' : conversation.mainStatus;
}

// 用明确的 session/run 归属处理事件，未知运行永远不会落入当前打开的会话。
export function applyEvent(state, event) {
  const known = state.runs[event.run_id];
  const parent = state.runs[event.parent_run_id] || state.runs[event.root_run_id];
  const conversation = event.session_id
    ? state.conversations.find(c => c.sessionId === event.session_id)
    : state.conversations.find(c => c.id === (known?.conversationId || parent?.conversationId));
  if (!conversation) return null;
  if (known && known.conversationId !== conversation.id) return null;
  if (event.run_id) state.runs[event.run_id] = {
    ...known, conversationId: conversation.id,
    parent: event.parent_run_id || (event.root_run_id !== event.run_id && event.root_run_id) || known?.parent || null,
  };
  const run = state.runs[event.run_id];
  const messages = conversation.messages;
  if (['run.finished', 'subagent.finished'].includes(event.type)) {
    for (const message of messages) {
      if (message.kind === 'permission' && message.runId === event.run_id && !message.decision) message.decision = 'expired';
    }
  }
  if (event.type === 'subagent.started') {
    if (!messages.some(m => m.kind === 'agent' && m.runId === event.run_id)) messages.push({ kind: 'agent', runId: event.run_id, text: event.description, status: 'running' });
  } else if (event.type === 'subagent.finished') {
    const agent = messages.find(m => m.kind === 'agent' && m.runId === event.run_id);
    if (agent) agent.status = event.status;
    refreshStatus(conversation);
  } else if (run?.parent && event.type !== 'permission.requested' && !event.type.startsWith('permission.')) {
    if (!messages.some(m => m.kind === 'agent' && m.runId === event.run_id)) messages.push({ kind: 'agent', runId: event.run_id, text: '子代理', status: 'running' });
    refreshStatus(conversation);
    return conversation;
  } else if (event.type === 'run.started') {
    conversation.mainStatus = 'running';
    refreshStatus(conversation);
    conversation.runId = event.run_id;
  } else if (event.type === 'step.started') {
    if (run) run.step = event.step;
  } else if (['llm.token', 'llm.response.completed', 'llm.response.failed'].includes(event.type)) {
    const step = event.step || run?.step || 0;
    const matches = m => m.kind === 'text' && m.role === 'assistant' && m.runId === event.run_id && (m.step || 0) === step;
    let response = step || event.type !== 'llm.token' ? messages.find(matches) : (matches(messages.at(-1) || {}) ? messages.at(-1) : null);
    if (!response) { response = { kind: 'text', role: 'assistant', text: '', runId: event.run_id, step }; messages.push(response); }
    if (!response.responseStatus) {
      if (event.type === 'llm.token') response.text += event.token;
      else {
        response.text = event.text || '';
        response.responseStatus = event.type === 'llm.response.completed' ? 'completed' : 'failed';
        response.stopReason = event.stop_reason || 'end_turn';
        if (event.type === 'llm.response.completed' && event.content?.length) {
          response.blocks = historyMessages([{ role: 'assistant', content: event.content }]).filter(message => message.kind !== 'tool');
          const searches = messages.filter(message => message.runId === event.run_id).flatMap(message => message.blocks || []).filter(message => message.kind === 'server_tool');
          for (const result of event.content.filter(block => block.type === 'web_search_tool_result')) {
            const search = searches.findLast(message => message.id === result.tool_use_id);
            if (search) applyServerToolResult(search, result.content);
          }
        }
      }
    }
  } else if (event.type === 'tool.call_started') {
    messages.push({ kind: 'tool', id: event.tool_use_id, runId: event.run_id, name: event.tool_name, params: event.params, output: '', status: 'running' });
  } else if (['tool.call_finished', 'tool.call_failed'].includes(event.type)) {
    const tool = messages.findLast(m => m.kind === 'tool' && m.id === event.tool_use_id && m.runId === event.run_id);
    if (tool) {
      tool.output = event.output ?? event.error_message ?? '';
      tool.elapsed = event.elapsed_ms;
      tool.status = event.type === 'tool.call_failed' ? 'failed' : 'success';
    }
  } else if (event.type === 'permission.requested') {
    if (!messages.some(m => m.kind === 'permission' && m.id === event.tool_use_id && m.runId === event.run_id)) {
      messages.push({ kind: 'permission', id: event.tool_use_id, runId: event.run_id, name: event.tool_name, preview: event.param_preview || JSON.stringify(event.params, null, 2), decision: null });
    }
    refreshStatus(conversation);
  } else if (['permission.granted', 'permission.denied'].includes(event.type)) {
    const permission = messages.findLast(m => m.kind === 'permission' && m.id === event.tool_use_id && m.runId === event.run_id);
    if (permission) {
      if (!permission.decision || ['expired', 'disconnected'].includes(permission.decision)) permission.decision = event.decision;
      permission.submitting = false;
    }
    refreshStatus(conversation);
  } else if (event.type === 'llm.model_selected') {
    conversation.model = event.model;
  } else if (event.type === 'llm.usage') {
    conversation.usage = event;
  } else if (event.type === 'context.compacted') {
    messages.push({ kind: 'notice', text: `上下文已压缩 · ${event.original_tokens} → ${event.summary_tokens} tokens` });
  } else if (event.type === 'run.finished') {
    for (const message of messages) if (message.kind === 'tool' && message.runId === event.run_id && message.status === 'running') {
      message.status = 'failed'; message.output ||= event.reason === 'cancelled' ? '执行已停止。' : '本次运行已结束。';
    }
    if (conversation.runId && conversation.runId !== event.run_id) { refreshStatus(conversation); return conversation; }
    conversation.mainStatus = event.status === 'success' || event.reason === 'cancelled' ? 'idle' : 'error';
    refreshStatus(conversation);
    const reasons = { max_tokens: '输出达到上限', model_context_window_exceeded: '上下文已满', refusal: '模型拒绝', unexpected_stop_reason: '响应不完整', unsupported_stop_reason: '响应不完整', incomplete_response: '响应不完整', invalid_tool_response: '工具响应不完整', server_tool_unavailable: '服务器搜索不可用或权限已变更' };
    const notice = event.reason === 'cancelled' ? '已停止本次任务。可以修改输入后继续。'
      : event.status !== 'success' ? `运行未完成：${reasons[event.reason] || event.reason || event.status}` : null;
    if (notice && !messages.some(m => m.kind === 'notice' && m.runId === event.run_id && m.source === 'run.finished')) {
      messages.push({ kind: 'notice', runId: event.run_id, source: 'run.finished', text: notice });
    }
  } else if (event.type === 'session.waiting_for_input' && conversation.status !== 'error') {
    if (event.last_run_id && conversation.runId && event.last_run_id !== conversation.runId) return conversation;
    conversation.mainStatus = 'idle';
    refreshStatus(conversation);
  } else if (event.type === 'session.closed') {
    for (const message of messages) if (message.kind === 'permission' && !message.decision) message.decision = 'expired';
    conversation.mainStatus = 'idle';
    refreshStatus(conversation);
  }
  return conversation;
}
