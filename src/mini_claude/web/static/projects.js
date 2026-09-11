import { escapeHtml as esc } from './content.js';

const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;

// 项目切换和列表管理使用轻量浮层，保留工作区可见与原有键盘焦点。
export class ProjectPicker {
  constructor({ command, onChange, onList, toast }) {
    Object.assign(this, { command, onChange, onList, toast });
    this.panel = document.querySelector('#project-popover');
    this.sidebar = document.querySelector('#project-list');
    this.projects = [];
    this.currentPath = null;
    this.version = 0;
    this.busy = false;
    this.panel.addEventListener('click', event => this.handleClick(event));
    this.sidebar.addEventListener('click', event => this.handleClick(event));
    document.addEventListener('pointerdown', event => {
      if (!this.panel.hidden && !this.panel.contains(event.target) && !this.anchor?.contains(event.target)) this.close(false);
    });
    this.panel.addEventListener('focusout', event => {
      if (event.relatedTarget && !this.panel.contains(event.relatedTarget) && !this.anchor?.contains(event.relatedTarget)) this.close(false);
    });
    this.panel.addEventListener('keydown', event => this.onKey(event));
    window.addEventListener('resize', () => this.position());
    document.addEventListener('scroll', event => {
      if (!this.panel.contains(event.target)) this.position();
    }, true);
  }

  // 复用服务端列表更新侧栏，移除项目后不改动任何对话内容。
  update(result) {
    this.projects = result.projects || [];
    this.currentPath = result.current_path;
    this.onList(result);
    if (!this.panel.hidden && this.mode === 'picker') this.renderList();
  }

  // 重连时从后端取最新列表，不将启动目录擅自补回已清空的列表。
  async refresh() {
    const result = await this.command('workspace.list');
    this.update(result);
    return result;
  }

  // 在点击的位置打开浮层，窗口底部的项目按钮向上展开。
  begin(anchor, mode, label) {
    if (!this.panel.hidden && this.anchor === anchor && this.mode === mode) { this.close(); return false; }
    this.close(false);
    this.anchor = anchor;
    this.mode = mode;
    this.panel.setAttribute('aria-label', label);
    this.panel.classList.toggle('project-context', mode === 'menu');
    this.panel.hidden = false;
    anchor.setAttribute('aria-expanded', 'true');
    return true;
  }

  // 搜索框默认获得焦点，列表就地加载，关闭后的响应不再打开浮层。
  async open(anchor = document.querySelector('#project-button')) {
    if (!this.begin(anchor, 'picker', '选择工作区')) return;
    const version = this.version;
    this.panel.innerHTML = `<div class="project-popover-heading">切换工作区</div><label class="project-search">${icon('search')}<input type="search" placeholder="搜索工作区…" aria-label="搜索工作区" autocomplete="off"></label><div class="project-options"><p class="project-picker-empty" role="status">正在读取…</p></div><p class="project-action-error" role="alert" hidden></p><div class="project-popover-footer"><button data-project-action="open">${icon('plus')}打开项目文件夹…</button></div>`;
    this.panel.querySelector('input').addEventListener('input', () => this.renderList());
    this.position();
    this.panel.querySelector('input').focus();
    try {
      const result = await this.command('workspace.list');
      if (version !== this.version || this.panel.hidden) return;
      this.update(result);
    } catch (error) { if (version === this.version && !this.panel.hidden) this.error(error.message); }
  }

  // 项目名是主信息，路径缩略显示并可悬停查看完整内容。
  renderList() {
    const input = this.panel.querySelector('input');
    if (!input) return;
    const query = input.value.trim().toLocaleLowerCase();
    const matches = this.projects.filter(project => `${project.name}\n${project.path}`.toLocaleLowerCase().includes(query));
    this.panel.querySelector('.project-options').innerHTML = matches.map(project => `<div class="project-option-row"><button class="project-option" data-project-action="select" data-path="${esc(project.path)}" title="${esc(project.path)}" ${project.path === this.currentPath ? 'aria-current="true"' : ''}>${icon(project.is_default ? 'workspace' : 'folder')}<span><strong>${esc(project.name)}</strong><small>${esc(project.path)}</small></span>${project.path === this.currentPath ? icon('check') : ''}</button>${project.is_default ? '' : `<button class="icon-button project-remove" data-project-action="remove" data-path="${esc(project.path)}" aria-label="从列表移除 ${esc(project.name)}" title="从列表移除，保留文件和会话">${icon('x')}</button>`}</div>`).join('') || `<p class="project-picker-empty">${query ? '没有匹配的项目' : '尚未添加项目，打开一个文件夹开始。'}</p>`;
    this.position();
  }

  // 侧栏省略号直接展示项目管理操作，不再打开遮挡工作区的模态窗口。
  menu(anchor, path) {
    const project = this.projects.find(item => item.path === path);
    if (!project || project.is_default || !this.begin(anchor, 'menu', `管理项目 ${project.name}`)) return;
    this.panel.innerHTML = `<div class="project-context-heading"><strong>${esc(project.name)}</strong><small title="${esc(project.path)}">${esc(project.path)}</small></div><button class="project-remove-action" data-project-action="remove" data-path="${esc(path)}">${icon('x')}从列表移除</button><p class="project-context-note">保留文件和会话记录</p><p class="project-action-error" role="alert" hidden></p>`;
    this.position();
    this.panel.querySelector('button').focus();
  }

  // 将浮层限制在视口内，列表过长时只在浮层内滚动。
  position() {
    if (this.panel.hidden || !this.anchor?.isConnected) return;
    const anchor = this.anchor.getBoundingClientRect();
    const width = Math.min(this.mode === 'menu' ? 260 : 336, window.innerWidth - 24);
    this.panel.style.width = `${width}px`;
    this.panel.style.maxHeight = `${window.innerHeight - 24}px`;
    const height = this.panel.getBoundingClientRect().height;
    let left = this.mode === 'menu' ? anchor.right - width : anchor.left;
    left = Math.max(12, Math.min(left, window.innerWidth - width - 12));
    let top = anchor.bottom + 8;
    if (top + height > window.innerHeight - 12) top = anchor.top - height - 8;
    this.panel.style.left = `${left}px`;
    this.panel.style.top = `${Math.max(12, Math.min(top, window.innerHeight - height - 12))}px`;
  }

  // 关闭时恢复触发按钮焦点；外部点击保持目标按钮的正常焦点行为。
  close(restoreFocus = true) {
    this.version++;
    this.panel.hidden = true;
    this.anchor?.setAttribute('aria-expanded', 'false');
    if (restoreFocus) (this.anchor?.isConnected ? this.anchor : document.querySelector('#project-button'))?.focus();
  }

  // 错误留在操作旁边，运行中项目被拒绝移除时仍能继续选择。
  error(message) {
    const output = this.panel.querySelector('.project-action-error');
    if (output && !this.panel.hidden) { output.textContent = message; output.hidden = false; this.position(); }
    else this.toast(message);
  }

  // 列表使用方向键快速选项，Tab 仍可访问每个项目的移除按钮。
  onKey(event) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); this.close(); return; }
    if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    const options = [...this.panel.querySelectorAll('[data-project-action="select"], .project-popover-footer button, .project-remove-action')];
    if (!options.length) return;
    event.preventDefault();
    const index = options.indexOf(document.activeElement);
    const next = index < 0 ? event.key === 'ArrowDown' ? 0 : options.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
    options[next].focus();
  }

  // 原生打开、切换和移除共用同一条事件连接，并抑制重复请求。
  async handleClick(event) {
    const target = event.target.closest('[data-project-action]');
    if (!target || this.busy) return;
    const action = target.dataset.projectAction;
    if (action === 'menu') { this.menu(target, target.dataset.path); return; }
    if (action === 'select' && target.dataset.path === this.currentPath) { this.close(); return; }
    const version = this.version;
    this.busy = true;
    target.disabled = true;
    try {
      const method = { select: 'workspace.select', remove: 'workspace.remove', open: 'workspace.pick' }[action];
      if (!method) return;
      const result = await this.command(method, action === 'open' ? {} : { path: target.dataset.path });
      if (result.cancelled) return;
      if (result.projects) this.update(result);
      this.onChange(result);
      if (action === 'remove') {
        this.toast('已从列表移除，文件和会话记录已保留。');
        if (version === this.version && this.mode === 'picker' && !this.panel.hidden) this.panel.querySelector('input')?.focus();
        else if (version === this.version) this.close();
      } else if (version === this.version) this.close();
    } catch (error) { this.error(error.message); }
    finally { this.busy = false; target.disabled = false; }
  }
}
