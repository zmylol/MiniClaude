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
  const ids = new Set(pending.map(item => item.tool_use_id));
  for (const message of conversation.messages) if (message.kind === 'permission' && !ids.has(message.id) && (!message.decision || message.decision === 'disconnected')) message.decision = 'expired';
  for (const item of pending) {
    let message = conversation.messages.find(m => m.kind === 'permission' && m.id === item.tool_use_id);
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
  if (event.type === 'run.started' && event.session_id) {
    const owner = state.conversations.find(c => c.sessionId === event.session_id);
    if (owner) state.runs[event.run_id] = { conversationId: owner.id, parent: null };
  }
  if (event.type === 'subagent.started') {
    const parent = state.runs[event.parent_run_id];
    if (parent) state.runs[event.run_id] = { ...parent, parent: event.parent_run_id };
  }
  const run = state.runs[event.run_id];
  const conversation = event.session_id
    ? state.conversations.find(c => c.sessionId === event.session_id)
    : state.conversations.find(c => c.id === run?.conversationId);
  if (!conversation) return null;
  const messages = conversation.messages;
  if (['run.finished', 'subagent.finished'].includes(event.type)) {
    for (const message of messages) {
      if (message.kind === 'permission' && message.runId === event.run_id && !message.decision) message.decision = 'expired';
    }
  }
  if (event.type === 'subagent.started') {
    messages.push({ kind: 'agent', runId: event.run_id, text: event.description, status: 'running' });
  } else if (event.type === 'subagent.finished') {
    const agent = messages.find(m => m.kind === 'agent' && m.runId === event.run_id);
    if (agent) agent.status = event.status;
    refreshStatus(conversation);
  } else if (run?.parent && event.type !== 'permission.requested' && !event.type.startsWith('permission.')) {
    refreshStatus(conversation);
    return conversation;
  } else if (event.type === 'run.started') {
    conversation.mainStatus = 'running';
    refreshStatus(conversation);
    conversation.runId = event.run_id;
  } else if (event.type === 'llm.token') {
    const last = messages.at(-1);
    if (last?.kind === 'text' && last.role === 'assistant' && last.runId === event.run_id) last.text += event.token;
    else messages.push({ kind: 'text', role: 'assistant', text: event.token, runId: event.run_id });
  } else if (event.type === 'tool.call_started') {
    messages.push({ kind: 'tool', id: event.tool_use_id, runId: event.run_id, name: event.tool_name, params: event.params, output: '', status: 'running' });
  } else if (['tool.call_finished', 'tool.call_failed'].includes(event.type)) {
    const tool = messages.findLast(m => m.kind === 'tool' && m.id === event.tool_use_id);
    if (tool) {
      tool.output = event.output ?? event.error_message ?? '';
      tool.elapsed = event.elapsed_ms;
      tool.status = event.type === 'tool.call_failed' ? 'failed' : 'success';
    }
  } else if (event.type === 'permission.requested') {
    if (!messages.some(m => m.kind === 'permission' && m.id === event.tool_use_id)) {
      messages.push({ kind: 'permission', id: event.tool_use_id, runId: event.run_id, name: event.tool_name, preview: event.param_preview || JSON.stringify(event.params, null, 2), decision: null });
    }
    conversation.status = 'waiting';
  } else if (['permission.granted', 'permission.denied'].includes(event.type)) {
    const permission = messages.findLast(m => m.kind === 'permission' && m.id === event.tool_use_id);
    if (permission) permission.decision = event.decision;
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
    conversation.mainStatus = event.status === 'success' || event.reason === 'cancelled' ? 'idle' : 'error';
    refreshStatus(conversation);
    if (event.reason === 'cancelled') messages.push({ kind: 'notice', text: '已停止本次任务。可以修改输入后继续。' });
    else if (event.status !== 'success') messages.push({ kind: 'notice', text: `运行未完成：${event.reason || event.status}` });
  } else if (event.type === 'session.waiting_for_input' && conversation.status !== 'error') {
    conversation.mainStatus = 'idle';
    refreshStatus(conversation);
  }
  return conversation;
}
