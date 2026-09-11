// 使用独立浏览器上下文验证未发送草稿、项目导航和失效历史缓存。
async (page) => {
  const context = await page.context().browser().newContext();
  const testPage = await context.newPage();
  const checks = [];
  const verify = (condition, label) => { if (!condition) throw new Error(label); checks.push(label); };
  const ready = async name => {
    await testPage.waitForFunction(value => document.querySelector('#project-name').textContent === value, name);
    await testPage.locator('#connection-status[data-state="connected"]').waitFor();
  };
  try {
    await testPage.goto('http://127.0.0.1:7440/?desktop=1');
    await testPage.locator('#connection-status[data-state="connected"]').waitFor();
    const project = await testPage.locator('#project-name').innerText();
    const group = () => testPage.getByRole('region', { name: project, exact: true });
    await testPage.locator('#prompt').fill('草稿甲：需要保留的输入');
    await testPage.locator('#new-chat').click();
    verify(await testPage.locator('#prompt').inputValue() === '', '新建对话提供独立空输入，不复用已有草稿');
    await testPage.locator('#prompt').fill('草稿乙：另一项任务');
    await group().getByRole('button', { name: /草稿甲/, exact: false }).filter({ has: testPage.locator('span') }).first().click();
    verify(await testPage.locator('#prompt').inputValue() === '草稿甲：需要保留的输入', '组内点击恢复第一条未发送草稿');
    verify(await group().locator('.conversation-select').filter({ hasText: '草稿乙' }).count() === 1, '另一条草稿仍可发现');
    await testPage.getByRole('button', { name: `收起 ${project}`, exact: true }).click();
    await testPage.getByRole('button', { name: `在 ${project} 新建对话`, exact: true }).click();
    verify(await group().locator('.workspace-conversations').isVisible(), '折叠项目中新建对话会展开所属组');
    await testPage.locator('[data-action="plugins"]').click();
    await group().locator('.project-nav-item').click();
    verify(await testPage.locator('.composer-dock').isVisible(), '管理页点击当前项目返回对话');
    await testPage.getByRole('button', { name: '在 默认工作区 新建对话', exact: true }).click();
    await ready('默认工作区');
    await group().locator('.conversation-select').filter({ hasText: '草稿乙' }).click();
    await ready(project);
    await testPage.waitForFunction(() => document.querySelector('#prompt').value === '草稿乙：另一项任务');
    verify(await testPage.locator('#prompt').inputValue() === '草稿乙：另一项任务', '跨项目直接打开指定未发送草稿');
    await testPage.evaluate(() => {
      const key = `miniclaude.web.v1:${document.querySelector('#project-button').title}`;
      const cached = JSON.parse(localStorage.getItem(key));
      cached.conversations.push({ id: 'deleted-local', sessionId: 'deleted-server-session', title: '已删除的幽灵记录', messages: [] });
      localStorage.setItem(key, JSON.stringify(cached));
    });
    await testPage.reload();
    await ready(project);
    await testPage.waitForFunction(() => !document.querySelector('#project-list').textContent.includes('已删除的幽灵记录'));
    verify(await group().locator('.conversation-select').filter({ hasText: '草稿甲' }).count() === 1, '远端目录清理失效会话时保留本地草稿');
    return { passed: checks.length, checks };
  } finally { await context.close(); }
}
