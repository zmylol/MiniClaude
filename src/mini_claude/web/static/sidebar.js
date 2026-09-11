import { escapeHtml as esc } from './content.js';

const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;

export const hasConversationDraft = item => Boolean(item.draft?.trim() || item.attachments?.length || item.missingAttachments?.length || item.failedDraft?.text?.trim() || item.failedDraft?.files?.length);

// 以项目内远端目录为准合并本地置顶偏好，当前项目仍可搜索已加载的正文。
export function sidebarConversations({ sessions, conversations = [], query = '', activeId }) {
  const entries = sessions == null ? conversations : [...sessions.map(remote => ({
    ...conversations.find(item => item.sessionId === remote.session_id),
    sessionId: remote.session_id, title: remote.title || '新对话', updatedAt: remote.updated_at,
    status: remote.running ? 'running' : 'idle',
  })), ...conversations.filter(item => !item.sessionId && hasConversationDraft(item))];
  const search = query.trim().toLocaleLowerCase();
  return entries.filter(item => item.sessionId || item.messages?.length || hasConversationDraft(item) || item.id === activeId)
    .map(item => ({ ...item, title: !item.sessionId && hasConversationDraft(item)
      ? `草稿 · ${(item.title && item.title !== '新对话' ? item.title : item.draft?.trim() || item.attachments?.[0]?.name || item.missingAttachments?.[0] || item.failedDraft?.text?.trim() || item.failedDraft?.files?.[0]?.name || '新对话').slice(0, 40)}` : item.title }))
    .filter(item => `${item.title}\n${item.draft || ''}\n${item.failedDraft?.text || ''}\n${(item.messages || []).filter(message => message.kind === 'text').map(message => message.text).join('\n')}`.toLocaleLowerCase().includes(search))
    .sort((a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned))
      || (b.updatedAt || '').localeCompare(a.updatedAt || ''));
}

// 每个工作区单独展开和读取历史，折叠与搜索不会改变当前执行目录。
export class WorkspaceSidebar {
  constructor({ command, getCurrent, getCached, onNavigate, toast }) {
    Object.assign(this, { command, getCurrent, getCached, onNavigate, toast });
    this.container = document.querySelector('#project-list');
    this.projects = [];
    this.histories = new Map();
    this.expanded = {};
    try { this.expanded = JSON.parse(localStorage.getItem('miniclaude.sidebar.v1') || '{}') || {}; }
    catch { this.toast('侧栏展开状态未能恢复。'); }
    this.container.addEventListener('click', event => this.handleClick(event));
  }

  // 项目列表来自服务端，保留各组自己的展开状态。
  update(result) {
    this.projects = result.projects || [];
    for (const project of this.projects) {
      if (!Object.hasOwn(this.expanded, project.path)) this.expanded[project.path] = project.is_default || project.path === result.current_path;
    }
    this.render();
  }

  setExpanded(path, expanded) {
    this.expanded[path] = expanded;
    try { localStorage.setItem('miniclaude.sidebar.v1', JSON.stringify(this.expanded)); }
    catch { this.toast('侧栏展开状态未能保存。'); }
  }

  // 只读取会话摘要；失败在所属组内展示，并允许单独重试。
  async load(path) {
    if (this.histories.get(path)?.loading) return;
    this.histories.set(path, { ...this.histories.get(path), loading: true });
    this.render();
    try {
      const result = await this.command('workspace.sessions', { path });
      this.histories.set(path, { sessions: result.sessions || [], conversations: this.getCached(path) });
    } catch (error) { this.histories.set(path, { error: error.message }); }
    this.render();
  }

  // 在组内呈现置顶与最近对话，标题和路径帮助区分同名文件夹。
  render() {
    const current = this.getCurrent();
    const query = document.querySelector('#search-input').value.trim();
    const load = [];
    const html = this.projects.map((project, index) => {
      const selected = project.path === current.path;
      const expanded = Boolean(query || this.expanded[project.path]);
      const history = this.histories.get(project.path);
      if (expanded && !selected && !history) load.push(project.path);
      const entries = expanded ? sidebarConversations({
        sessions: selected ? null : history?.sessions || [],
        conversations: selected ? current.conversations : history?.conversations || [], query,
        activeId: selected ? current.activeId : null,
      }) : [];
      const path = esc(project.path);
      const name = esc(project.name);
      const listId = `workspace-conversations-${index}`;
      const rows = entries.map(item => {
        const active = selected && item.id === current.activeId && current.chatVisible;
        return `<div class="conversation-item ${active ? 'active' : ''}"><button class="conversation-select" ${selected ? `data-conversation="${esc(item.id)}"` : `${item.sessionId ? `data-workspace-session="${esc(item.sessionId)}"` : `data-workspace-conversation="${esc(item.id)}"`} data-path="${path}"`} ${active ? 'aria-current="page"' : ''} title="${esc(item.title)}">${icon(item.pinned ? 'pin' : 'message')}<span>${esc(item.title)}</span>${['running', 'waiting', 'sending'].includes(item.status) ? '<span class="conversation-running" aria-label="运行中">·</span>' : ''}</button>${selected ? `<button class="icon-button pin-button" data-menu="${esc(item.id)}" aria-label="管理对话 ${esc(item.title)}" title="对话操作">${icon('more')}</button>` : ''}</div>`;
      }).join('');
      const empty = !selected && history?.error
        ? `<p class="sidebar-empty" role="status">${esc(history.error)}</p><button class="workspace-retry" data-workspace-retry data-path="${path}">重新加载</button>`
        : `<p class="sidebar-empty">${!selected && (!history || history.loading) ? '正在读取对话…' : query ? '没有匹配的对话' : project.is_default ? '随手聊聊，或处理零散文件' : '还没有对话'}</p>`;
      return `<section class="workspace-group ${project.is_default ? 'default-workspace' : ''}" data-workspace-path="${path}" aria-label="${name}"><div class="sidebar-project-row ${selected ? 'selected' : ''}"><button class="icon-button workspace-toggle" data-workspace-toggle data-path="${path}" aria-expanded="${expanded}" aria-controls="${listId}" aria-label="${expanded ? '收起' : '展开'} ${name}">${icon('chevron-down')}</button><button class="project-nav-item" data-project-action="select" data-path="${path}" title="${path}" ${selected ? 'aria-current="true"' : ''}>${icon(project.is_default ? 'workspace' : 'folder')}<span>${name}</span></button><button class="icon-button workspace-new" data-workspace-new data-path="${path}" aria-label="在 ${name} 新建对话" title="新建对话">${icon('plus')}</button>${project.is_default ? '' : `<button class="icon-button project-more" data-project-action="menu" data-path="${path}" aria-label="管理项目 ${name}" aria-haspopup="dialog" aria-expanded="false" title="项目操作">${icon('more')}</button>`}</div><div id="${listId}" class="workspace-conversations conversation-list" ${expanded ? '' : 'hidden'}>${rows || empty}${!rows && !query && (selected || history?.sessions) ? `<button class="workspace-start" data-workspace-new data-path="${path}">新建对话</button>` : ''}</div></section>`;
    }).join('');
    if (this.container.innerHTML !== html) {
      const focused = this.container.contains(document.activeElement) ? document.activeElement : null;
      const dataset = focused ? { ...focused.dataset } : null;
      this.container.innerHTML = html;
      if (dataset) [...this.container.querySelectorAll('button')].find(button => Object.entries(dataset).every(([key, value]) => button.dataset[key] === value))?.focus({ preventScroll: true });
    }
    for (const path of load) this.load(path);
  }

  // 折叠仅作用于侧栏，新建和打开历史都携带明确的工作区路径。
  handleClick(event) {
    const target = event.target.closest('button');
    if (!target) return;
    const path = target.dataset.path;
    if (Object.hasOwn(target.dataset, 'workspaceToggle')) {
      this.setExpanded(path, !this.expanded[path]);
      if (this.expanded[path] && path !== this.getCurrent().path) this.load(path);
      else this.render();
    } else if (Object.hasOwn(target.dataset, 'workspaceRetry')) this.load(path);
    else if (Object.hasOwn(target.dataset, 'workspaceNew') || target.dataset.workspaceSession || target.dataset.workspaceConversation) {
      this.setExpanded(path, true);
      this.onNavigate(path, target.dataset.workspaceSession || null, target.dataset.workspaceConversation || null);
    }
  }
}
