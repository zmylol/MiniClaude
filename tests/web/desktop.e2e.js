// 启动 fixture_core.py 后，通过 Playwright browser_run_code_unsafe 的 filename 参数运行。
async (page) => {
  const checks = [];
  const verify = (condition, label) => { if (!condition) throw new Error(label); checks.push(label); };
  await page.goto('http://127.0.0.1:7440/?desktop=1');
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.locator('#connection-status[data-state="connected"]').waitFor();
  await page.locator('#new-chat').click();
  await page.locator('#prompt').fill('');
  verify(await page.locator('#conversation-title').innerText() === '新对话', '空对话标题明确显示新对话');

  const conversationCount = await page.locator('[data-conversation]').count();
  const starters = page.locator('[data-starter]');
  verify(await starters.count() === 3, '首页提供三个开始建议');
  for (let index = 0; index < 3; index++) {
    await page.locator('#prompt').fill('');
    const draft = await starters.nth(index).getAttribute('data-starter');
    await starters.nth(index).click();
    verify(await page.locator('#prompt').inputValue() === draft
      && await page.locator('#prompt').evaluate(node => node === document.activeElement)
      && await page.locator('#welcome-starters').isHidden()
      && await page.locator('#messages').isHidden()
      && await page.locator('[data-conversation]').count() === conversationCount,
    `开始建议 ${index + 1} 仅填充草稿并聚焦，已有草稿时收起建议`);
  }

  await page.locator('#prompt').fill('短提示词');
  const shortHeight = await page.locator('#prompt').evaluate(node => node.clientHeight);
  const longDraft = Array.from({ length: 24 }, (_, index) => `第 ${index + 1} 行：请检查这个项目的桌面交互。`).join('\n');
  await page.locator('#prompt').fill(longDraft);
  const inputSize = await page.locator('#prompt').evaluate(node => ({ height: node.clientHeight, scroll: node.scrollHeight }));
  verify(inputSize.height > shortHeight + 60 && inputSize.height <= 241 && inputSize.scroll > inputSize.height,
    '长草稿自动增高到上限，更多内容在输入框内滚动');
  await page.keyboard.press('Control+,');
  await page.locator('#workspace-page').waitFor();
  await page.locator('#history-back').click();
  verify(await page.locator('#prompt').inputValue() === longDraft
    && await page.locator('#prompt').evaluate(node => node.clientHeight) > shortHeight + 60,
  '切换管理页面再返回，长草稿与输入高度保留');
  await page.locator('#prompt').fill('');
  verify(await page.locator('#prompt').evaluate(node => node.clientHeight) <= shortHeight + 1,
    '清空草稿后输入框恢复紧凑高度');

  await page.keyboard.press('Control+b');
  verify(await page.locator('#sidebar').evaluate(node => node.inert)
    && await page.locator('#sidebar-open').getAttribute('aria-expanded') === 'false',
  '侧栏快捷键收起内容并移除隐藏内容的键盘焦点');
  await page.keyboard.press('Control+b');
  verify(!await page.locator('#sidebar').evaluate(node => node.inert)
    && await page.locator('#sidebar-toggle').getAttribute('aria-expanded') === 'true',
  '再次按侧栏快捷键展开侧栏');
  await page.keyboard.press('Control+k');
  verify(await page.locator('#search-input').evaluate(node => node === document.activeElement), '搜索快捷键仍聚焦搜索框');
  await page.keyboard.press('Escape');
  verify(await page.locator('#search-input').isHidden(), 'Escape 关闭搜索并清除过滤');

  await page.locator('#event-toggle').click();
  verify(await page.locator('#event-close').evaluate(node => node === document.activeElement)
    && await page.locator('.workspace').evaluate(node => node.classList.contains('events-open'))
    && await page.locator('#event-toggle').getAttribute('aria-expanded') === 'true',
  '打开实时事件同步布局状态并聚焦关闭按钮');
  await page.locator('#model-button').click();
  await page.locator('#custom-model').waitFor();
  await page.keyboard.press('Control+k');
  verify(await page.locator('#search-input').isHidden()
    && await page.locator('#modal').evaluate(node => node.contains(document.activeElement)),
  '弹窗内快捷键不会改变背景搜索或移走焦点');
  await page.keyboard.press('Escape');
  verify(await page.locator('#modal').isHidden() && await page.locator('#event-panel').isVisible(),
    'Escape 优先关闭模型弹窗，保留底层实时事件');
  await page.locator('#project-button').click();
  await page.locator('#project-popover').waitFor();
  await page.keyboard.press('Escape');
  verify(await page.locator('#project-popover').isHidden() && await page.locator('#event-panel').isVisible(),
    'Escape 优先关闭项目浮层，保留底层实时事件');
  await page.keyboard.press('Escape');
  verify(await page.locator('#event-panel').isHidden()
    && !await page.locator('.workspace').evaluate(node => node.classList.contains('events-open'))
    && await page.locator('#event-toggle').getAttribute('aria-expanded') === 'false'
    && await page.locator('#event-toggle').evaluate(node => node === document.activeElement),
  'Escape 关闭实时事件，收回布局空间并恢复触发按钮焦点');

  for (const size of [{ width: 900, height: 650 }, { width: 1280, height: 800 }, { width: 1920, height: 1080 }]) {
    await page.setViewportSize(size);
    await page.locator('#event-toggle').click();
    const bounds = await page.locator('#event-panel').boundingBox();
    const close = await page.locator('#event-close').boundingBox();
    verify(bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= size.width + 1
      && bounds.y + bounds.height <= size.height + 1 && close.x >= bounds.x && close.y >= bounds.y,
    `${size.width} × ${size.height} 实时事件与关闭入口位于窗口内`);
    const composer = await page.locator('.composer-dock').boundingBox();
    verify(composer.x >= bounds.x + bounds.width - 1 || bounds.x >= composer.x + composer.width - 1
      || composer.y >= bounds.y + bounds.height - 1 || bounds.y >= composer.y + composer.height - 1,
    `${size.width} × ${size.height} 打开事件时输入框与面板不交叠`);
    verify(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth
      && document.body.scrollWidth <= window.innerWidth),
    `${size.width} × ${size.height} 打开事件时视口没有横向溢出`);
    await page.keyboard.press('Escape');
    const send = await page.locator('#send-button').boundingBox();
    verify(send.x >= 0 && send.y >= 0 && send.x + send.width <= size.width
      && send.y + send.height <= size.height,
    `${size.width} × ${size.height} 关闭事件后输入操作保持可见`);
  }

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.locator('#prompt').fill(longDraft.repeat(3));
  await page.locator('#send-button').click();
  await page.getByRole('button', { name: '允许一次', exact: true }).waitFor();
  await page.locator('#messages').evaluate(node => { node.scrollTop = 0; });
  await page.locator('#scroll-to-bottom').waitFor();
  verify(await page.locator('#scroll-to-bottom').isVisible(), '阅读较早消息时显示返回最新入口');
  // 先回复审批，再立即向上阅读；随后仍通过真实 WebSocket 接收模拟模型的流式回答。
  await page.evaluate(() => {
    document.querySelector('[data-decision="deny_once"]').click();
    document.querySelector('#messages').scrollTop = 0;
  });
  await page.getByRole('button', { name: '发送消息', exact: true }).waitFor();
  verify(await page.locator('#messages').evaluate(node => node.scrollTop) < 10
    && (await page.locator('#messages').innerText()).includes('已跳过被拒绝'),
  '后续流式回复不会打断向上阅读');
  await page.locator('#prompt').fill('继续阅读时准备下一条消息');
  verify(await page.locator('#messages').evaluate(node => node.scrollTop) < 10,
    '输入下一条草稿不重置消息阅读位置');
  await page.setViewportSize({ width: 900, height: 650 });
  const readingPosition = await page.locator('#messages').evaluate(node => { node.scrollTop = 320; return node.scrollTop; });
  await page.locator('#event-toggle').click();
  await page.keyboard.press('Escape');
  verify(readingPosition > 100 && Math.abs(await page.locator('#messages').evaluate(node => node.scrollTop) - readingPosition) < 2,
    '窄窗口打开再关闭实时事件，恢复之前的消息阅读位置');
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.locator('#scroll-to-bottom').click();
  await page.locator('#scroll-to-bottom').waitFor({ state: 'hidden' });
  verify(await page.locator('#messages').evaluate(node => node.scrollHeight - node.scrollTop - node.clientHeight) < 2,
    '返回最新按钮滚动到底部并自动隐藏');
  await page.locator('#prompt').fill('');
  await page.setViewportSize({ width: 900, height: 650 });
  await page.locator('#event-toggle').click();
  await page.locator('#prompt').fill(longDraft.repeat(3));
  await page.locator('#send-button').click();
  await page.waitForFunction(() => document.querySelector('[data-decision="deny_once"]'));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  verify(await page.locator('#event-panel').isVisible()
    && await page.locator('#messages').evaluate(node => node.scrollHeight - node.scrollTop - node.clientHeight) < 2,
  '窄屏事件面板打开期间发送长消息，拉宽后恢复显示最新回复');
  await page.getByRole('button', { name: '拒绝', exact: true }).click();
  await page.getByRole('button', { name: '发送消息', exact: true }).waitFor();
  await page.locator('#event-close').click();
  return { passed: checks.length, checks };
}
