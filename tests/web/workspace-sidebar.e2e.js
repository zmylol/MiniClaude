// 使用三个独立端口的 fixture_core.py 验证工作区归属与界面导航。
async (page) => {
  const checks = [];
  const verify = (condition, label) => { if (!condition) throw new Error(label); checks.push(label); };
  const ready = async name => {
    await page.waitForFunction(value => document.querySelector('#project-name').textContent === value, name);
    await page.locator('#connection-status[data-state="connected"]').waitFor();
  };
  const group = name => page.getByRole('region', { name, exact: true });
  const send = async text => {
    await page.locator('#prompt').fill(text);
    await page.getByRole('button', { name: '发送消息', exact: true }).click();
    await page.getByRole('button', { name: '拒绝', exact: true }).click();
    await page.getByRole('button', { name: '发送消息', exact: true }).waitFor();
  };
  await page.goto('http://127.0.0.1:7440/?desktop=1');
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.locator('#connection-status[data-state="connected"]').waitFor();
  const original = await page.locator('#project-name').innerText();
  const originalPath = await group(original).getAttribute('data-workspace-path');
  verify(await page.locator('.workspace-group').count() === 3
    && await page.locator('.workspace-group').first().getAttribute('aria-label') === '默认工作区',
  '默认工作区固定在所有项目之前');
  await send('项目专属：检查侧栏结构');
  await page.getByRole('button', { name: '管理对话 项目专属：检查侧栏结构', exact: true }).click();
  await page.getByRole('button', { name: '置顶对话', exact: true }).click();
  await page.locator('#prompt').fill('项目中的未发送草稿');
  await page.getByRole('button', { name: '在 默认工作区 新建对话', exact: true }).click();
  await ready('默认工作区');
  await send('无项目：整理想法');
  const defaultPath = await page.locator('#project-button').getAttribute('title');
  verify(defaultPath.endsWith('/desktop-state/workspace') && defaultPath !== originalPath,
    '无项目对话使用独立的真实工作目录');
  verify(await group('默认工作区').getByText('无项目：整理想法', { exact: true }).count() === 1
    && await group('默认工作区').getByText('项目专属：检查侧栏结构', { exact: true }).count() === 0,
  '默认工作区只展示自己的会话');
  if (await group(original).getByRole('button', { name: `展开 ${original}`, exact: true }).count()) {
    await group(original).getByRole('button', { name: `展开 ${original}`, exact: true }).click();
  }
  await group(original).getByRole('button', { name: '项目专属：检查侧栏结构', exact: true }).waitFor();
  verify(await group(original).locator('.conversation-select use').getAttribute('href') === '#i-pin',
    '置顶保留在所属项目内，切换后仍显示置顶标记');
  await group(original).getByRole('button', { name: '项目专属：检查侧栏结构', exact: true }).click();
  await ready(original);
  await page.waitForFunction(() => document.querySelector('#conversation-title').textContent === '项目专属：检查侧栏结构');
  verify(await page.locator('#prompt').inputValue() === '项目中的未发送草稿'
    && await page.locator('#project-button').getAttribute('title') === originalPath,
  '跨项目历史点击同时恢复正确对话、目录和草稿');
  await page.getByRole('button', { name: `收起 ${original}`, exact: true }).click();
  verify(await group(original).locator('.workspace-conversations').isHidden()
    && await page.locator('#project-name').innerText() === original, '折叠只影响列表，不切换执行目录');
  await page.reload();
  await ready(original);
  verify(await group(original).locator('.workspace-conversations').isHidden(), '折叠状态刷新后保留');
  await page.getByRole('button', { name: '搜索对话', exact: true }).click();
  await page.getByRole('searchbox', { name: '搜索对话', exact: true }).fill('无项目');
  await group('默认工作区').getByRole('button', { name: '无项目：整理想法', exact: true }).waitFor();
  verify(await group(original).locator('.conversation-select').count() === 0,
    '跨工作区搜索仍保留归属，且能搜到非当前工作区的记录');
  await group('默认工作区').getByRole('button', { name: '无项目：整理想法', exact: true }).click();
  await ready('默认工作区');
  await page.waitForFunction(() => document.querySelector('#conversation-title').textContent === '无项目：整理想法');
  verify(await page.locator('#messages').innerText().then(text => text.includes('无项目：整理想法') && !text.includes('项目专属：检查侧栏结构')),
    '搜索结果打开目标历史，消息不会跨项目混入');
  await page.locator('#project-button').click();
  verify(await page.getByRole('button', { name: '从列表移除 默认工作区', exact: true }).count() === 0,
    '默认工作区没有移除入口');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: `管理项目 ${original}`, exact: true }).click();
  await page.locator('.project-remove-action').click();
  await group(original).waitFor({ state: 'detached' });
  verify(await page.locator('#project-name').innerText() === '默认工作区'
    && await page.locator('#prompt').isEnabled(), '移除其他项目保留可用的默认工作区');
  await page.getByRole('button', { name: '打开项目文件夹', exact: true }).click();
  await ready(original);
  await page.getByRole('button', { name: `管理项目 ${original}`, exact: true }).click();
  await page.locator('.project-remove-action').click();
  await ready('默认工作区');
  verify(await page.locator('#prompt').isEnabled(), '移除当前项目自动回到默认工作区');
  await page.setViewportSize({ width: 720, height: 720 });
  await page.getByRole('button', { name: '展开侧栏', exact: true }).click();
  verify(await page.locator('#sidebar').evaluate(node => node.scrollWidth <= node.clientWidth),
    '窄窗口侧栏没有横向溢出');
  return { passed: checks.length, checks };
}
