// 启动全新的 fixture_core.py 后运行；两个浏览器上下文共用真实网关。
async (page) => {
  const checks = [];
  const verify = (condition, label) => { if (!condition) throw new Error(label); checks.push(label); };
  const otherContext = await page.context().browser().newContext();
  const other = await otherContext.newPage();
  const sockets = [];
  page.on('websocket', socket => {
    const methods = [];
    sockets.push(methods);
    socket.on('framesent', frame => methods.push(JSON.parse(frame.payload).method));
  });
  try {
    await page.addInitScript(() => {
      window.__projectTestSockets = [];
      const Socket = window.WebSocket;
      window.WebSocket = class extends Socket {
        constructor(...args) { super(...args); window.__projectTestSockets.push(this); }
      };
    });
    await page.goto('http://127.0.0.1:7440/?desktop=1');
    await page.locator('#connection-status[data-state="connected"]').waitFor();
    const original = await page.locator('#project-name').innerText();
    for (const [removed, expected] of [[original, 'another-project'], ['another-project', '选择项目']]) {
      await page.context().setOffline(true);
      // Chromium 离线模式不会关闭已有 WS，显式断开以模拟真实链路中断。
      await page.evaluate(() => window.__projectTestSockets.forEach(socket => socket.close()));
      await page.locator('#connection-status:not([data-state="connected"])').waitFor();
      await other.goto('http://127.0.0.1:7440/?desktop=1');
      await other.locator('#connection-status[data-state="connected"]').waitFor();
      await other.getByRole('button', { name: `管理项目 ${removed}`, exact: true }).click();
      await other.getByRole('button', { name: '从列表移除', exact: true }).click();
      await other.waitForFunction(value => document.querySelector('#project-name').textContent === value, expected);
      const firstReconnect = sockets.length;
      await page.context().setOffline(false);
      await page.waitForFunction(value => document.querySelector('#project-name').textContent === value, expected);
      await page.locator('#connection-status[data-state="connected"]').waitFor();
      const verified = sockets.slice(firstReconnect).find(methods => methods.includes('workspace.list'));
      verify(verified && !verified.includes('session.list') && !verified.includes('session.create'), `断线期间移除 ${removed}：旧界面先核对项目，不向替代 Core 发送会话命令`);
      verify(await page.locator('#project-name').innerText() === expected, `重连后恢复正确工作区：${expected}`);
    }
    verify(await page.locator('#prompt').isDisabled() && await page.locator('#empty-workspace').isVisible(), '断线期间移除最后项目后，输入区进入空状态');
    return { passed: checks.length, checks };
  } finally {
    await page.context().setOffline(false);
    await otherContext.close();
  }
}
