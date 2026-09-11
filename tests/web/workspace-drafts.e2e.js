// 使用独立浏览器上下文验证未发送草稿、项目导航和失效历史缓存。
async (page) => {
  const context = await page.context().browser().newContext();
  const testPage = await context.newPage();
  const commands = [];
  testPage.on('websocket', socket => socket.on('framesent', frame => commands.push(JSON.parse(frame.payload))));
  const checks = [];
  const verify = (condition, label) => { if (!condition) throw new Error(label); checks.push(label); };
  const ready = async name => {
    await testPage.waitForFunction(value => document.querySelector('#project-name').textContent === value, name);
    await testPage.locator('#connection-status[data-state="connected"]').waitFor();
  };
  try {
    await testPage.goto('http://127.0.0.1:7440/?desktop=1');
    await testPage.locator('#connection-status[data-state="connected"]').waitFor();
    const project = await testPage.locator('.workspace-group:not(.default-workspace) .project-nav-item span').first().innerText();
    const group = () => testPage.getByRole('region', { name: project, exact: true });
    await group().locator('.project-nav-item').click();
    await ready(project);
    await testPage.locator('#prompt').fill('草稿甲：需要保留的输入');
    await testPage.locator('#new-chat').click();
    verify(await testPage.locator('#prompt').inputValue() === '', '新建对话提供独立空输入，不复用已有草稿');
    await testPage.locator('#prompt').fill('草稿乙：另一项任务');
    await group().locator('.conversation-select').filter({ hasText: '草稿甲' }).click();
    verify(await testPage.locator('#prompt').inputValue() === '草稿甲：需要保留的输入', '组内点击恢复第一条未发送草稿');
    verify(await group().locator('.conversation-select').filter({ hasText: '草稿乙' }).count() === 1, '另一条草稿仍可发现');
    await testPage.getByRole('button', { name: `收起 ${project}`, exact: true }).click();
    await testPage.getByRole('button', { name: `在 ${project} 新建对话`, exact: true }).click();
    verify(await group().locator('.workspace-conversations').isVisible(), '折叠项目中新建对话会展开所属组');
    await testPage.locator('[data-action="plugins"]').click();
    await group().locator('.project-nav-item').click();
    verify(await testPage.locator('.composer-dock').isVisible(), '管理页点击当前项目返回对话');
    await testPage.locator('#file-picker').evaluate(input => {
      const transfer = new DataTransfer();
      transfer.items.add(new File(['A file kept with its draft'], 'draft-note.txt', { type: 'text/plain' }));
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await testPage.locator('.attachment-chip').filter({ hasText: 'draft-note.txt' }).waitFor();
    await testPage.locator('#new-chat').click();
    await group().locator('.conversation-select').filter({ hasText: 'draft-note.txt' }).click();
    verify(await testPage.locator('.attachment-chip').filter({ hasText: 'draft-note.txt' }).isVisible(), '只含附件的草稿也独立保存并可恢复');
    await testPage.getByRole('button', { name: '在 默认工作区 新建对话', exact: true }).click();
    await ready('默认工作区');
    await group().locator('.conversation-select').filter({ hasText: 'draft-note.txt' }).click();
    await ready(project);
    await testPage.locator('#attachment-list').getByText(/请重新添加附件：draft-note.txt/).waitFor();
    verify(await testPage.locator('#attachment-list').getByText(/请重新添加附件：draft-note.txt/).isVisible(), '切换项目后明确告知需重新添加的附件');
    await testPage.getByRole('button', { name: '在 默认工作区 新建对话', exact: true }).click();
    await ready('默认工作区');
    await group().locator('.conversation-select').filter({ hasText: '草稿乙' }).click();
    await ready(project);
    await testPage.waitForFunction(() => document.querySelector('#prompt').value === '草稿乙：另一项任务');
    verify(await testPage.locator('#prompt').inputValue() === '草稿乙：另一项任务', '跨项目直接打开指定未发送草稿');
    const storageKey = `miniclaude.web.v1:${await testPage.locator('#project-button').getAttribute('title')}`;
    await testPage.addInitScript(key => {
      const cached = JSON.parse(localStorage.getItem(key));
      if (cached) {
        cached.conversations.push({ id: 'deleted-local', sessionId: 'deleted-server-session', title: '已删除的幽灵记录', messages: [] });
        cached.conversations.push({ id: 'orphan-draft', sessionId: 'deleted-with-draft', title: '新对话', draft: '远端已删除但必须保留的草稿', messages: [{ kind: 'text', role: 'user', text: '旧会话消息不再发送' }] });
        cached.activeId = 'deleted-local';
        localStorage.setItem(key, JSON.stringify(cached));
      }
    }, storageKey);
    await testPage.reload();
    await ready(project);
    await testPage.waitForFunction(() => !document.querySelector('#project-list').textContent.includes('已删除的幽灵记录'));
    verify(await group().locator('.conversation-select').filter({ hasText: '草稿甲' }).count() === 1, '远端目录清理失效会话时保留本地草稿');
    await group().getByRole('button', { name: '草稿 · 远端已删除但必须保留的草稿', exact: true }).click();
    verify(await testPage.locator('#prompt').inputValue() === '远端已删除但必须保留的草稿', '已删除远端会话的未发送输入转为可恢复本地草稿');
    await testPage.getByRole('button', { name: '发送消息', exact: true }).click();
    await testPage.getByRole('button', { name: '拒绝', exact: true }).click();
    await testPage.getByRole('button', { name: '发送消息', exact: true }).waitFor();
    const send = commands.findLast(command => command.method === 'session.send_message');
    verify(commands.some(command => command.method === 'session.create') && send.params.session_id !== 'deleted-with-draft', '恢复的草稿创建有效新会话并完成发送');
    const beforeSwitch = commands.filter(command => command.method === 'workspace.select').length;
    const navigation = testPage.waitForEvent('framenavigated');
    await testPage.evaluate(() => {
      const current = document.querySelector('#project-button').title;
      const other = [...document.querySelectorAll('.workspace-group:not(.default-workspace) .project-nav-item')].find(button => button.dataset.path !== current);
      document.querySelector('.default-workspace .workspace-new').click();
      other.click();
    });
    await navigation;
    verify(commands.filter(command => command.method === 'workspace.select').length === beforeSwitch + 1, '快速交替点击新建和项目名仅提交一次工作区切换');
    return { passed: checks.length, checks };
  } finally { await context.close(); }
}
