export class EventConnection {
  // 保存连接状态和待响应请求，同一 WebSocket 同时处理命令与事件。
  constructor(url, { onEvent, onStatus }, Socket = WebSocket) {
    this.url = url;
    this.onEvent = onEvent;
    this.onStatus = onStatus;
    this.Socket = Socket;
    this.pending = new Map();
    this.ready = false;
    this.stopped = false;
    this.retry = 0;
  }

  // 建立连接后先订阅事件，订阅成功才允许用户提交消息。
  connect() {
    if (this.stopped) return;
    this.onStatus('connecting');
    const socket = this.socket = new this.Socket(this.url);
    socket.onopen = async () => {
      try {
        await this.command('event.subscribe', { topics: ['*'], scope: 'global' });
        if (this.socket !== socket || socket.readyState !== 1) return;
        this.ready = true;
        this.retry = 0;
        this.onStatus('connected');
      } catch { socket.close(); }
    };
    socket.onmessage = ({ data }) => {
      let envelope;
      try { envelope = JSON.parse(data); }
      catch { socket.close(1007, 'Invalid JSON'); return; }
      if (envelope.kind === 'event') this.onEvent(envelope.event);
      else if (this.pending.has(envelope.id)) {
        const request = this.pending.get(envelope.id);
        this.pending.delete(envelope.id);
        clearTimeout(request.timer);
        if (envelope.error) request.reject(Object.assign(new Error(envelope.error.message), { code: envelope.error.code }));
        else request.resolve(envelope.result);
      }
    };
    socket.onclose = () => {
      this.ready = false;
      for (const request of this.pending.values()) {
        clearTimeout(request.timer);
        request.reject(new Error('连接已断开，本次命令的完成状态尚未确认。'));
      }
      this.pending.clear();
      this.onStatus('disconnected');
      if (!this.stopped) this.timer = setTimeout(() => this.connect(), Math.min(1000 * 2 ** this.retry++, 10000));
    };
    socket.onerror = () => socket.close();
  }

  // 命令响应按 ID 配对，长运行不设短超时，绝不因重连自动重发。
  command(method, params = {}) {
    if (this.socket?.readyState !== 1) return Promise.reject(new Error('尚未连接 mini-core'));
    const id = crypto.randomUUID();
    return new Promise((resolve, reject) => {
      const timer = ['session.send_message', 'session.compact', 'workspace.pick'].includes(method) ? null : setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('命令响应超时，请检查 mini-core。'));
      }, ['workspace.select', 'workspace.remove', 'session.cancel', 'plugins.add', 'plugins.set_enabled', 'workspace.pull_requests'].includes(method) ? 30000 : 10000);
      this.pending.set(id, { resolve, reject, timer });
      try { this.socket.send(JSON.stringify({ jsonrpc: '2.0', id, method, params: { ...params, type: method } })); }
      catch (error) { clearTimeout(timer); this.pending.delete(id); reject(error); }
    });
  }

  // 页面退出时取消重连并关闭连接。
  close() {
    this.stopped = true;
    clearTimeout(this.timer);
    this.socket?.close();
  }
}
