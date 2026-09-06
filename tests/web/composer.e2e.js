// 启动 fixture_core.py 后，通过 Playwright browser_run_code_unsafe 的 filename 参数运行。
async (page) => {
  const checks = [];
  const verify = (condition, label) => { if (!condition) throw new Error(label); checks.push(label); };
  await page.goto('http://127.0.0.1:7440/?desktop=1');
  await page.locator('#connection-status[data-state="connected"]').waitFor();
  await page.locator('#new-chat').click();
  await page.locator('#prompt').fill('hello');
  // 捕获真实表单提交入口，只记录发送意图，不创建会话或调用模型。
  await page.evaluate(() => {
    window.composerSubmissions = 0;
    document.querySelector('#composer-form').addEventListener('submit', event => {
      event.preventDefault();
      event.stopImmediatePropagation();
      window.composerSubmissions++;
    }, { capture: true });
  });
  const confirm = async ({ keyCode, isComposing, endFirst }) => page.evaluate(args => {
    const input = document.querySelector('#prompt');
    input.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
    if (args.endFirst) input.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: input.value }));
    const enter = new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: args.keyCode, isComposing: args.isComposing, bubbles: true, cancelable: true });
    input.dispatchEvent(enter);
    if (!args.endFirst) input.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: input.value }));
    input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }));
    return { submissions: window.composerSubmissions, prevented: enter.defaultPrevented, text: input.value };
  }, { keyCode, isComposing, endFirst });
  const webkit = await confirm({ keyCode: 229, isComposing: false, endFirst: true });
  verify(webkit.submissions === 0 && !webkit.prevented && webkit.text === 'hello', 'WebKit 先结束组字再回车确认字母时，不发送且不拦截输入法');
  await page.locator('#prompt').fill('你好');
  const composing = await confirm({ keyCode: 13, isComposing: true, endFirst: false });
  verify(composing.submissions === 0 && !composing.prevented && composing.text === '你好', '中文组字期间回车只确认输入');
  const webkitChinese = await confirm({ keyCode: 229, isComposing: false, endFirst: true });
  verify(webkitChinese.submissions === 0 && !webkitChinese.prevented && webkitChinese.text === '你好', 'WebKit 确认中文候选时保留文本');
  await page.locator('#prompt').press('Enter');
  verify(await page.evaluate(() => window.composerSubmissions) === 1, '确认输入后再按一次普通回车可以发送');
  await page.locator('#prompt').fill('第一行');
  await page.locator('#prompt').press('End');
  await page.locator('#prompt').press('Shift+Enter');
  verify(await page.locator('#prompt').inputValue() === '第一行\n' && await page.evaluate(() => window.composerSubmissions) === 1, 'Shift+Enter 继续换行而不发送');
  await page.locator('#send-button').click();
  verify(await page.evaluate(() => window.composerSubmissions) === 2, '发送按钮仍可正常提交');
  await page.locator('#prompt').fill('');
  return { passed: checks.length, checks };
}
