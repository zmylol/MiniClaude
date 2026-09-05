import { escapeHtml as esc, safeUrl } from './content.js';

const $ = selector => document.querySelector(selector);
const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const button = (action, label, extra = '') => `<button class="secondary" data-view-action="${action}" ${extra}>${label}</button>`;
const dateLabel = value => value ? new Date(value).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '尚未运行';

// 页面共用同一事件连接；切换页面时丢弃旧请求的显示结果。
export class WorkspaceViews {
  constructor({ command, modal, toast, getInfo, onProject, onSession, configure, active, isCurrentModal }) {
    Object.assign(this, { command, modal, toast, getInfo, onProject, onSession, configure, active, isCurrentModal });
    this.page = null;
    this.generation = 0;
    this.schedules = [];
    this.plugins = [];
    document.addEventListener('click', event => {
      const target = event.target.closest('[data-view-action]');
      if (target) this.perform(target.dataset.viewAction, target).catch(error => this.toast(error.message));
    });
    document.addEventListener('submit', event => {
      if (!event.target.dataset.viewForm) return;
      event.preventDefault();
      this.submit(event.target).catch(error => {
        const field = event.target.querySelector('.form-error');
        if (field) { field.textContent = error.message; field.hidden = false; }
        else this.toast(error.message);
      });
    });
  }

  // 获取真实页面数据，并显示可重试的连接或服务错误。
  async show(page) {
    this.page = page;
    const generation = ++this.generation;
    const names = { 'pull-requests': 'Pull Request', changes: '项目改动', scheduled: '已安排', plugins: '插件', profile: '设置' };
    const title = names[page] || page;
    $('#workspace-page').innerHTML = `<div class="page-heading"><div><p class="eyebrow">${esc(this.getInfo().project_name || '工作区')}</p><h2>${title}</h2></div>${button('refresh', '刷新')}</div><div class="page-content"><p class="loading-state" role="status">正在读取…</p></div>`;
    if (this.getInfo().project_selected === false) {
      $('#workspace-page .page-content').innerHTML = `<div class="page-empty">${icon('folder')}<h3>先打开一个项目</h3><p>选择项目文件夹后，即可使用${esc(title)}。</p>${button('pick-project', '打开项目')}</div>`;
      return;
    }
    try {
      let content;
      if (page === 'pull-requests') content = this.prPage(await this.command('workspace.pull_requests'));
      else if (page === 'changes') content = this.changesPage(await this.command('workspace.git_status'));
      else if (page === 'scheduled') content = this.schedulePage(await this.command('schedules.list'));
      else if (page === 'plugins') content = this.pluginsPage(await this.command('plugins.list'));
      else content = this.settingsPage();
      if (generation === this.generation && this.page === page) $('#workspace-page .page-content').innerHTML = content;
    } catch (error) {
      if (generation === this.generation) $('#workspace-page .page-content').innerHTML = `<div class="page-empty">${icon('terminal')}<h3>暂时无法读取</h3><p>${esc(error.message)}</p>${button('refresh', '重新加载')}</div>`;
    }
  }

  // 显示当前仓库的真实 PR，并将链接交给系统浏览器。
  prPage(result) {
    if (!result.available) return `<div class="page-empty">${icon('git-branch')}<h3>连接这个项目的 Pull Request</h3><p>${esc(result.reason || '当前项目没有可用的 GitHub 仓库。')}</p><p>这里读取项目 Git remote 对应的 GitHub PR。连接好本机 GitHub CLI 后点击刷新。</p>${button('refresh', '刷新列表')}${button('github-help', '打开 GitHub CLI 指南')}</div>`;
    if (!result.items?.length) return `<div class="page-empty">${icon('git-branch')}<h3>没有待处理的 Pull Request</h3><p>当前仓库的 PR 会显示在这里。</p>${button('refresh', '刷新')}</div>`;
    return `<div class="item-list">${result.items.map(pr => `<article class="management-row"><div class="row-symbol green">${icon('git-branch')}</div><div class="row-main"><h3>${esc(pr.title)}</h3><p>#${esc(pr.number)} · ${esc(pr.headRefName || '')} · ${esc(pr.state || 'OPEN')}</p></div>${safeUrl(pr.url) ? `<a class="secondary" href="${esc(safeUrl(pr.url))}" target="_blank" rel="noopener noreferrer">打开 PR</a>` : ''}</article>`).join('')}</div>`;
  }

  // 列出 Git 改动，逐文件查看 diff 或直接发起代码审查。
  changesPage(result) {
    if (!result.available) return `<div class="page-empty">${icon('git-branch')}<h3>当前文件夹没有 Git 仓库</h3><p>${esc(result.reason || '打开已有 Git 项目后，可以在这里检查改动。')}</p>${button('pick-project', '打开项目')}</div>`;
    return `<div class="page-intro"><div><span class="tag">${icon('git-branch')}${esc(result.branch || 'HEAD')}</span><p>${result.files?.length || 0} 个文件有改动</p></div>${button('review-changes', '让 MiniClaude 审查')}</div>${!result.files?.length ? '<div class="page-empty"><h3>工作区是干净的</h3><p>文件编辑后，改动会出现在这里。</p></div>' : `<div class="change-layout"><div class="change-files">${result.files.map(file => `<button class="file-row" data-view-action="diff" data-path="${esc(file.path)}"><span class="file-status">${esc(file.status)}</span><span>${esc(file.path)}</span>${icon('chevron-right')}</button>`).join('')}</div><div id="diff-content" class="diff-content"><p class="empty-state">选择一个文件，查看具体改动。</p></div></div>`}`;
  }

  // 已安排任务显示执行时间、开关及最近一次会话。
  schedulePage(result) {
    this.schedules = result.schedules || [];
    return `<div class="page-intro"><div><h3>把重复的工作交给 MiniClaude</h3><p>应用打开时按计划执行，使用当前项目。任务执行仍遵循工具审批。</p></div><button class="primary" data-view-action="new-schedule">${icon('plus')}新建任务</button></div>${!this.schedules.length ? `<div class="page-empty">${icon('clock')}<h3>安排下一次任务</h3><p>每天检查项目改动，或在指定时间整理待办。</p>${button('new-schedule', '创建第一个任务')}</div>` : `<div class="item-list">${this.schedules.map(job => `<article class="management-row"><div class="row-symbol">${icon('clock')}</div><div class="row-main"><h3>${esc(job.title)}<span class="tag">${job.enabled ? job.repeat === 'daily' ? '每天' : '单次' : '已暂停'}</span></h3><p>${job.enabled ? `下次 ${esc(dateLabel(job.next_run))}` : '自动执行已关闭'} · ${{ running: '正在运行', completed: '已完成', success: '已完成', failed: '执行失败', error: '执行失败', idle: '等待执行', pending: '等待执行' }[job.status] || esc(job.status || '等待执行')}</p>${job.last_error ? `<p class="inline-error">${esc(job.last_error)}</p>` : ''}<div class="row-actions">${button('edit-schedule', '编辑', `data-id="${esc(job.id)}"`)}${button('toggle-schedule', job.enabled ? '暂停' : '启用', `data-id="${esc(job.id)}" ${job.status === 'running' ? 'disabled' : ''}`)}${button('run-schedule', '立即运行', `data-id="${esc(job.id)}" ${job.status === 'running' ? 'disabled' : ''}`)}${job.last_session_id ? button('schedule-session', '查看运行', `data-id="${esc(job.id)}"`) : ''}</div></div><button class="icon-button" data-view-action="delete-schedule" data-id="${esc(job.id)}" aria-label="删除 ${esc(job.title)}" ${job.status === 'running' ? 'disabled' : ''}>${icon('x')}</button></article>`).join('')}</div>`}`;
  }

  // MCP 服务展示真实连接和工具清单，不输出服务环境变量。
  pluginsPage(result) {
    this.plugins = result.servers || [];
    return `<div class="page-intro"><div><h3>连接你的工具</h3><p>通过 MCP 为助手添加工具，连接后在下一轮对话中使用。</p></div><button class="primary" data-view-action="new-plugin">${icon('plus')}添加 MCP 服务</button></div>${!this.plugins.length ? `<div class="page-empty">${icon('plugin')}<h3>你的工具，从这里连接</h3><p>添加本地 MCP 服务，或连接已有的 TCP 服务。</p>${button('new-plugin', '添加服务')}</div>` : `<div class="item-list">${this.plugins.map(server => `<article class="management-row"><div class="row-symbol">${icon('plugin')}</div><div class="row-main"><h3>${esc(server.name)}<span class="tag ${server.status === 'connected' ? 'green' : ''}">${{ connected: '已连接', disabled: '已停用', error: '连接失败' }[server.status] || esc(server.status)}</span></h3><p>${esc(server.transport)} · ${server.tools?.length || 0} 个工具${!server.managed ? ' · 来自项目配置' : ''}</p>${server.error ? `<p class="inline-error">${esc(server.error)}</p>` : ''}${server.tools?.length ? `<details class="plugin-tools"><summary>查看工具</summary><div>${server.tools.map(tool => `<code>${esc(typeof tool === 'string' ? tool : tool.name)}</code>`).join('')}</div></details>` : ''}<div class="row-actions">${button('toggle-plugin', server.status === 'connected' ? '断开' : '连接', `data-name="${esc(server.name)}"`)}${server.managed ? button('remove-plugin', '移除', `data-name="${esc(server.name)}"`) : ''}</div></div></article>`).join('')}</div>`}`;
  }

  // 设置使用实际操作入口，所有偏好作用于当前桌面工作区。
  settingsPage() {
    const info = this.getInfo();
    return `<div class="settings-section"><h3>工作区</h3><div class="setting-row"><div><strong>${esc(info.project_name)}</strong><p>${esc(info.project_path)}</p></div>${button('pick-project', '打开其他项目')}</div><div class="setting-row"><div><strong>模型</strong><p>${esc(this.active()?.selectedModel || info.model)}</p></div>${button('model', '选择模型')}</div><div class="setting-row"><div><strong>工具权限</strong><p>只读、按需审批或完全访问；每个会话单独设置。</p></div>${button('permissions', '设置权限')}</div></div><div class="settings-section"><h3>快捷键</h3><div class="setting-row"><span>新对话</span><kbd>⌘ ⇧ O</kbd></div><div class="setting-row"><span>搜索所有对话</span><kbd>⌘ K</kbd></div><div class="setting-row"><span>添加文件</span><kbd>⌘ ⇧ A</kbd></div><div class="setting-row"><span>打开项目文件夹</span><kbd>⌘ O</kbd></div><div class="setting-row"><span>设置</span><kbd>⌘ ,</kbd></div></div><div class="settings-section"><h3>本机数据</h3><p>对话保存在本机 Core 中，重启应用后可继续。草稿和界面偏好保存在此桌面工作区。</p></div>`;
  }

  // 新建与编辑任务共用明确的时间和重复规则表单。
  scheduleForm(job = {}) {
    const date = job.next_run ? new Date(job.next_run) : new Date(Date.now() + 3600000);
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    this.modal(job.id ? '编辑任务' : '新建任务', `<form data-view-form="schedule" data-id="${esc(job.id || '')}"><label>任务名称<input name="title" required maxlength="120" placeholder="例如：每日代码检查" value="${esc(job.title || '')}"></label><label>让 MiniClaude 做什么<textarea name="prompt" required rows="4" placeholder="检查今天的代码改动，找出可能的问题并给出建议。">${esc(job.prompt || '')}</textarea></label><div class="form-columns"><label>执行时间<input name="next_run" type="datetime-local" required value="${local}"></label><label>重复<select name="repeat"><option value="once" ${job.repeat !== 'daily' ? 'selected' : ''}>仅一次</option><option value="daily" ${job.repeat === 'daily' ? 'selected' : ''}>每天</option></select></label></div><p>应用需要保持打开。首次执行工具时可能需要你的审批。</p><p class="form-error" role="alert" hidden></p><div class="modal-actions"><button class="primary" type="submit">${job.id ? '保存修改' : '创建任务'}</button></div></form>`);
  }

  // MCP 配置采用结构化表单，不要求用户手写配置文件。
  pluginForm() {
    this.modal('添加 MCP 服务', `<form data-view-form="plugin"><label>名称<input name="name" required pattern="[a-zA-Z0-9_-]+" placeholder="my-tools" maxlength="80"></label><label>连接方式<select name="transport" id="plugin-transport"><option value="stdio">本地进程 · stdio</option><option value="tcp">网络服务 · TCP</option></select></label><div id="plugin-stdio"><label>启动程序<input name="command" placeholder="例如 npx、uvx 或程序名称"></label><label>参数（每行一个）<textarea name="args" rows="3" placeholder="-y&#10;@your-package/mcp-server"></textarea></label></div><div id="plugin-tcp" hidden><label>主机<input name="host" value="127.0.0.1"></label><label>端口<input name="port" type="number" min="1" max="65535" value="3000"></label></div><p>连接后会启动服务程序，并让助手使用该服务提供的工具。</p><p class="form-error" role="alert" hidden></p><div class="modal-actions"><button type="submit" class="primary">添加并连接</button></div></form>`);
    $('#plugin-transport').addEventListener('change', event => {
      $('#plugin-stdio').hidden = event.target.value !== 'stdio';
      $('#plugin-tcp').hidden = event.target.value !== 'tcp';
    });
  }

  // 表单提交期间禁用重复提交，失败保留填写内容。
  async submit(form) {
    const page = this.page;
    const submit = form.querySelector('[type="submit"]');
    if (submit.disabled) return;
    const fields = new FormData(form);
    submit.disabled = true;
    try {
      if (form.dataset.viewForm === 'schedule') {
        const payload = { title: fields.get('title').trim(), prompt: fields.get('prompt').trim(), next_run: new Date(fields.get('next_run')).toISOString(), repeat: fields.get('repeat') };
        if (form.dataset.id) payload.id = form.dataset.id;
        await this.command(form.dataset.id ? 'schedules.update' : 'schedules.create', payload);
      } else {
        const payload = { name: fields.get('name').trim(), transport: fields.get('transport') };
        if (payload.transport === 'stdio') { payload.command = fields.get('command').trim(); payload.args = fields.get('args').split('\n').map(x => x.trim()).filter(Boolean); if (!payload.command) throw new Error('请输入启动程序。'); }
        else { payload.host = fields.get('host').trim(); payload.port = Number(fields.get('port')); }
        await this.command('plugins.add', payload);
      }
      if (form.isConnected) $('#modal').close();
      await this.refreshCurrent(page);
    } finally { submit.disabled = false; }
  }

  // 后台操作完成时仅刷新仍在浏览的原页面。
  async refreshCurrent(page) {
    if (page && this.page === page) await this.show(page);
  }

  // 所有按钮对应明确的命令；耗时操作显示忙碌状态。
  async perform(action, target) {
    if (target.disabled) return;
    const job = this.schedules.find(item => item.id === target.dataset.id);
    const plugin = this.plugins.find(item => item.name === target.dataset.name);
    if (action === 'new-schedule') return this.scheduleForm();
    if (action === 'edit-schedule') return this.scheduleForm(job);
    if (action === 'new-plugin') return this.pluginForm();
    if (action === 'model' || action === 'permissions') return this.configure(action);
    if (action === 'github-help') { window.open('https://cli.github.com/manual/gh_auth_login', '_blank', 'noopener,noreferrer'); return; }
    target.disabled = true;
    try {
      if (action === 'pick-project') {
        const result = await this.command('workspace.pick');
        if (!result.cancelled) { $('#modal').close(); this.onProject(result); }
      } else if (action === 'refresh') await this.show(this.page);
      else if (action === 'diff') {
        const result = await this.command('workspace.git_diff', { path: target.dataset.path });
        const node = $('#diff-content');
        if (node) node.innerHTML = `<div class="diff-heading">${esc(target.dataset.path)}</div><pre class="diff-code">${(result.diff || '没有可显示的文本差异。').split('\n').map(line => `<span class="${line.startsWith('+') && !line.startsWith('+++') ? 'diff-add' : line.startsWith('-') && !line.startsWith('---') ? 'diff-remove' : line.startsWith('@@') ? 'diff-hunk' : ''}">${esc(line)}\n</span>`).join('')}</pre>`;
      } else if (action === 'review-changes') this.onSession(null, '请审查当前项目的未提交改动，指出潜在问题并给出修改建议。');
      else if (action === 'schedule-session') this.onSession(job.last_session_id);
      else if (action === 'toggle-schedule') { await this.command('schedules.update', { id: job.id, enabled: !job.enabled }); await this.refreshCurrent('scheduled'); }
      else if (action === 'run-schedule') { await this.command('schedules.run_now', { id: job.id }); await this.refreshCurrent('scheduled'); this.toast('任务已开始，可在任务卡片中查看运行对话。'); }
      else if (action === 'delete-schedule') {
        this.modal('删除任务', `<p>删除“${esc(job.title)}”后将不再自动执行，已产生的对话仍会保留。</p><div class="modal-actions"><button class="danger" data-view-action="confirm-delete-schedule" data-id="${esc(job.id)}">删除任务</button></div>`);
      } else if (action === 'confirm-delete-schedule') { await this.command('schedules.delete', { id: job.id }); if (target.isConnected) $('#modal').close(); await this.refreshCurrent('scheduled'); }
      else if (action === 'toggle-plugin') { await this.command('plugins.set_enabled', { name: plugin.name, enabled: plugin.status !== 'connected' }); await this.refreshCurrent('plugins'); }
      else if (action === 'remove-plugin') this.modal('移除 MCP 服务', `<p>停止并移除“${esc(plugin.name)}”的桌面配置。</p><div class="modal-actions"><button class="danger" data-view-action="confirm-remove-plugin" data-name="${esc(plugin.name)}">移除服务</button></div>`);
      else if (action === 'confirm-remove-plugin') { await this.command('plugins.remove', { name: plugin.name }); if (target.isConnected) $('#modal').close(); await this.refreshCurrent('plugins'); }
    } finally { target.disabled = false; }
  }
}
