// 转义文件名、模型输出和所有用户提供的文本。
export function escapeHtml(value = '') {
  return String(value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

// 仅允许普通网页链接，阻止脚本与本地文件协议。
export function safeUrl(value) {
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : ''; }
  catch { return ''; }
}

// 保留安全的 Markdown 结构；代码和原始 HTML 不参与格式替换。
export function markdown(text) {
  return String(text).split(/(```[^\n]*\n[\s\S]*?(?:```|$))/g).map(part => {
    if (part.startsWith('```')) {
      const line = part.indexOf('\n');
      const language = part.slice(3, line).trim();
      return `<div class="code-block"><div class="code-heading"><span>${escapeHtml(language || '代码')}</span><button data-action="copy-code">复制</button></div><pre><code>${escapeHtml(part.slice(line + 1).replace(/```$/, '').replace(/\n$/, ''))}</code></pre></div>`;
    }
    return part.split(/(`[^`\n]+`)/g).map(chunk => {
      if (chunk.startsWith('`') && chunk.endsWith('`')) return `<code>${escapeHtml(chunk.slice(1, -1))}</code>`;
      return escapeHtml(chunk)
        .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) => {
          const decoded = url.replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'");
          const href = safeUrl(decoded);
          return href ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
        })
        .replace(/^### (.+)$/gm, '<h3>$1</h3>').replace(/^## (.+)$/gm, '<h2>$1</h2>').replace(/^# (.+)$/gm, '<h1>$1</h1>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/^[-*] (.+)$/gm, '<div class="markdown-bullet">$1</div>')
        .replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
    }).join('');
  }).join('');
}

// 将持久化的 API 消息恢复成文本、图像和配对工具卡片。
export function historyMessages(messages) {
  const output = [];
  for (const message of messages) {
    if (typeof message.content === 'string') {
      if (message.content) output.push({ kind: 'text', role: message.role, text: message.content });
      continue;
    }
    if (!Array.isArray(message.content)) continue;
    for (const block of message.content) {
      if (block.type === 'text' && block.text) output.push({ kind: 'text', role: message.role, text: block.text });
      else if (block.type === 'image' && block.source?.type === 'base64' && /^image\/(png|jpeg|webp|gif)$/.test(block.source.media_type)) {
        output.push({ kind: 'image', mediaType: block.source.media_type, data: block.source.data });
      } else if (block.type === 'tool_use') output.push({ kind: 'tool', id: block.id, name: block.name, params: block.input, status: 'success', output: '' });
      else if (block.type === 'tool_result') {
        const tool = output.findLast(item => item.kind === 'tool' && item.id === block.tool_use_id);
        if (tool) {
          tool.output = typeof block.content === 'string' ? block.content : JSON.stringify(block.content, null, 2);
          tool.status = block.is_error ? 'failed' : 'success';
        }
      }
    }
  }
  return output;
}

// 文本附件携带真实内容，文件名不参与提示结构或路径解析。
export function composeContent(text, attachments) {
  const files = attachments.filter(file => file.kind === 'text');
  return [text, ...files.map(file => `\n附件 ${JSON.stringify(file.name)}（以下为文件内容）：\n${file.text}`)].filter(Boolean).join('\n');
}

// 读取拖入或选择的文件并限制总体内存和协议负载。
export async function readAttachments(files, existing = []) {
  const result = [...existing];
  for (const file of files) {
    if (result.length >= 10) throw new Error('一次最多添加 10 个文件。');
    if (result.some(item => item.name === file.name && item.size === file.size)) continue;
    const mime = file.type || ({ png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp', gif: 'image/gif' })[file.name.split('.').at(-1).toLowerCase()];
    if (/^image\/(png|jpeg|webp|gif)$/.test(mime || '')) {
      const images = result.filter(item => item.kind === 'image');
      if (file.size > 5 * 1024 * 1024 || images.reduce((n, item) => n + item.size, file.size) > 10 * 1024 * 1024 || images.length >= 5) throw new Error('图片最多 5 张，每张不超过 5 MB，合计不超过 10 MB。');
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = '';
      for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
      result.push({ id: crypto.randomUUID(), name: file.name, size: file.size, kind: 'image', mediaType: mime, data: btoa(binary) });
    } else {
      if (file.size > 256 * 1024 || result.filter(item => item.kind === 'text').reduce((n, item) => n + item.size, file.size) > 512 * 1024) throw new Error('文本附件每个不超过 256 KB，合计不超过 512 KB；大文件可在对话中让助手读取。');
      let text;
      try { text = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer()); }
      catch { throw new Error(`${file.name} 不是 UTF-8 文本。支持代码、文本和 PNG/JPEG/WebP/GIF 图片。`); }
      if (text.includes('\0')) throw new Error(`${file.name} 是二进制文件，请选择代码、文本或图片。`);
      result.push({ id: crypto.randomUUID(), name: file.name, size: file.size, kind: 'text', text });
    }
  }
  return result;
}
