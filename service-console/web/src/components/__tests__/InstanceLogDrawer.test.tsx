import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import InstanceLogDrawer from '../InstanceLogDrawer';
import { parseSseFrames } from '../sseParser';

// jsdom 不实现以下 API,antd Drawer(滚动锁/响应式)会调用,补最小 stub 保证可交互、用例稳定。
if (!HTMLElement.prototype.scrollIntoView) {
  HTMLElement.prototype.scrollIntoView = () => {};
}
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// 把若干 SSE 帧文本编码成一个 ReadableStream(模拟 fetch response.body)。
//   frames:已是完整 SSE 文本数组(每个含 `event:`/`data:` + 结尾 \n\n);
//   逐帧入队,模拟分块到达(贴合真实流式:解析需跨 chunk 拼接残帧)。
const sseStream = (frames: string[]): ReadableStream<Uint8Array> => {
  const enc = new TextEncoder();
  let i = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < frames.length) {
        controller.enqueue(enc.encode(frames[i]));
        i += 1;
      } else {
        controller.close();
      }
    },
  });
};

// 构造一个 SSE 事件帧文本(event + JSON data + 空行分隔)。
const frame = (event: string, data: Record<string, unknown>): string =>
  `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

// antd 会在两个汉字之间插空格(关闭→「关 闭」),按钮文字被拆成多节点。
// 抹掉空白后用子串匹配,定位包含目标文案的按钮(贴现有用例的 byNormalizedName 思路)。
const clickButtonByText = async (
  user: ReturnType<typeof userEvent.setup>,
  text: string,
): Promise<void> => {
  const btn = await screen.findByRole('button', {
    name: (name: string) => name.replace(/\s/g, '').includes(text),
  });
  await user.click(btn);
};

// 安装一个 fetch mock:返回给定 ok/status/body。返回捕获到的入参以便断言(URL/headers/body/signal)。
interface FetchCall {
  url: string;
  init: RequestInit;
}
const installFetch = (opts: {
  ok?: boolean;
  status?: number;
  body?: ReadableStream<Uint8Array> | null;
  reject?: unknown;
}): { calls: FetchCall[] } => {
  const calls: FetchCall[] = [];
  const fn = vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    if (opts.reject !== undefined) throw opts.reject;
    return {
      ok: opts.ok ?? true,
      status: opts.status ?? 200,
      body: opts.body === undefined ? sseStream([]) : opts.body,
    } as unknown as Response;
  });
  vi.stubGlobal('fetch', fn);
  return { calls };
};

describe('parseSseFrames(纯函数:SSE 帧增量解析)', () => {
  it('切出完整帧、保留未收齐残帧到 rest', () => {
    const buf = frame('started', { sessionId: 's1' }) + 'event: chunk\ndata: {"chunk":"hal';
    const { events, rest } = parseSseFrames(buf);
    expect(events).toHaveLength(1);
    expect(events[0].event).toBe('started');
    expect(events[0].data.sessionId).toBe('s1');
    // 残帧(未遇到 \n\n)整体留在 rest,等下次拼接。
    expect(rest).toContain('event: chunk');
  });

  it('兼容 \\r\\n 换行并按 JSON 解析 data', () => {
    const buf = 'event: chunk\r\ndata: {"chunk":"line-1"}\r\n\r\n';
    const { events } = parseSseFrames(buf);
    expect(events[0].event).toBe('chunk');
    expect(events[0].data.chunk).toBe('line-1');
  });

  it('data 非 JSON 时兜底当纯文本(text 字段),不丢内容', () => {
    const buf = 'data: plain-text-not-json\n\n';
    const { events } = parseSseFrames(buf);
    expect(events[0].data.text).toBe('plain-text-not-json');
  });
});

describe('InstanceLogDrawer', () => {
  beforeEach(() => {
    sessionStorage.setItem('platform_token', 'tok-log');
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('打开抽屉 → fetch POST /api/agents/{id}/logs/stream(带 Bearer + dir/tail/timestamps)', async () => {
    const { calls } = installFetch({ body: sseStream([frame('started', { sessionId: 's1' })]) });
    render(
      <InstanceLogDrawer open onClose={() => {}} agentId="agent-1" dir="/data/app" title="container-x" />,
    );

    await waitFor(() => expect(calls.length).toBe(1));
    const call = calls[0];
    // URL 命中 console SSE 端点,agentId 经 encodeURIComponent。
    expect(call.url).toBe('/api/agents/agent-1/logs/stream');
    expect(call.init.method).toBe('POST');
    // token 取法与 client.ts 一致:Authorization: Bearer <sessionStorage token>。
    const headers = call.init.headers as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer tok-log');
    expect(headers['Content-Type']).toBe('application/json');
    // body 含 dir + tail(默认 200)+ timestamps。
    const body = JSON.parse(call.init.body as string);
    expect(body.dir).toBe('/data/app');
    expect(body.tail).toBe(200);
    expect(body.timestamps).toBe(true);
    // 标题副标识显示容器名。
    expect(await screen.findByText('· container-x')).toBeInTheDocument();
  });

  it('chunk 事件:日志文本追加渲染到等宽终端区', async () => {
    // 用持续不关闭的流,保证收到 chunk 后仍处「实时」(流自然结束会切到 finished)。
    const enc = new TextEncoder();
    const liveStream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(enc.encode(frame('started', { sessionId: 's1' })));
        controller.enqueue(enc.encode(frame('chunk', { chunk: 'hello-world-log-line' })));
        controller.enqueue(enc.encode(frame('chunk', { chunk: 'second-line\nthird-line' })));
        // 不 close:模拟持续 tail,状态维持「● 实时」。
      },
    });
    installFetch({ body: liveStream });
    render(<InstanceLogDrawer open onClose={() => {}} agentId="agent-1" dir="/data/app" />);

    // 三行(第二个 chunk 含两行,按 \n 拆)都渲染到终端。
    expect(await screen.findByText('hello-world-log-line')).toBeInTheDocument();
    expect(await screen.findByText('second-line')).toBeInTheDocument();
    expect(await screen.findByText('third-line')).toBeInTheDocument();
    // 状态条:收到 started/chunk 且流未结束 → 实时。
    expect(await screen.findByText('● 实时')).toBeInTheDocument();
  });

  it('流自然结束(无 finished 帧):状态切到「日志流结束」', async () => {
    // 只发 started + chunk 后即 close(不发 finished):组件应据流关闭标记结束(不当错误)。
    installFetch({
      body: sseStream([frame('started', { sessionId: 's1' }), frame('chunk', { chunk: 'tail-line' })]),
    });
    render(<InstanceLogDrawer open onClose={() => {}} agentId="agent-1" dir="/data/app" />);

    expect(await screen.findByText('tail-line')).toBeInTheDocument();
    expect(await screen.findByText('日志流结束')).toBeInTheDocument();
  });

  it('finished 事件:显示「日志流结束」状态', async () => {
    installFetch({
      body: sseStream([frame('started', { sessionId: 's1' }), frame('finished', { reason: 'eof' })]),
    });
    render(<InstanceLogDrawer open onClose={() => {}} agentId="agent-1" dir="/data/app" />);

    expect(await screen.findByText('日志流结束')).toBeInTheDocument();
  });

  it('error 事件:显示错误条(取后端 message)', async () => {
    installFetch({
      body: sseStream([frame('error', { message: 'agent 流读取失败' })]),
    });
    render(<InstanceLogDrawer open onClose={() => {}} agentId="agent-1" dir="/data/app" />);

    expect(await screen.findByText('agent 流读取失败')).toBeInTheDocument();
    expect(await screen.findByText('出错')).toBeInTheDocument();
  });

  it('鉴权失败(403):显式报错(不静默)', async () => {
    installFetch({ ok: false, status: 403, body: null });
    render(<InstanceLogDrawer open onClose={() => {}} agentId="agent-1" dir="/data/app" />);

    // 403 → 明确「鉴权失败(403)」文案 + 出错状态(用子串匹配,避开正则括号转义)。
    expect(
      await screen.findByText((t) => t.includes('鉴权失败') && t.includes('403')),
    ).toBeInTheDocument();
    expect(await screen.findByText('出错')).toBeInTheDocument();
  });

  it('网络异常:fetch reject → 显示连接失败错误(非 abort)', async () => {
    installFetch({ reject: new Error('网络断了') });
    render(<InstanceLogDrawer open onClose={() => {}} agentId="agent-1" dir="/data/app" />);

    expect(await screen.findByText('网络断了')).toBeInTheDocument();
    expect(await screen.findByText('出错')).toBeInTheDocument();
  });

  it('关闭抽屉 → abort 当前流(fetch signal 进入 aborted)+ 调 onClose', async () => {
    // 用一个永不关闭的流,确保关闭时连接仍在(可被 abort 观察)。
    const neverEnding = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(frame('started', { sessionId: 's1' })));
        // 不 close,不再 enqueue:模拟持续 tail。
      },
    });
    const { calls } = installFetch({ body: neverEnding });
    const onClose = vi.fn();
    render(<InstanceLogDrawer open onClose={onClose} agentId="agent-1" dir="/data/app" />);

    await waitFor(() => expect(calls.length).toBe(1));
    const signal = (calls[0].init.signal as AbortSignal)!;
    expect(signal.aborted).toBe(false);

    // 点「关闭」按钮(extra 区):抹空白子串匹配定位(antd 在汉字间插空格)。
    const user = userEvent.setup();
    await clickButtonByText(user, '关闭');

    // 关闭即 abort 该流(后端断连即停会话);并回调父级。
    await waitFor(() => expect(signal.aborted).toBe(true));
    expect(onClose).toHaveBeenCalled();
  });

  it('卸载组件 → abort 流(清理副作用,不留孤儿连接)', async () => {
    const neverEnding = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(frame('started', { sessionId: 's1' })));
      },
    });
    const { calls } = installFetch({ body: neverEnding });
    const { unmount } = render(
      <InstanceLogDrawer open onClose={() => {}} agentId="agent-1" dir="/data/app" />,
    );

    await waitFor(() => expect(calls.length).toBe(1));
    const signal = (calls[0].init.signal as AbortSignal)!;
    unmount();
    await waitFor(() => expect(signal.aborted).toBe(true));
  });

  it('清屏:清空已渲染日志行(不影响错误/状态)', async () => {
    installFetch({
      body: sseStream([frame('started', { sessionId: 's1' }), frame('chunk', { chunk: 'to-be-cleared' })]),
    });
    render(<InstanceLogDrawer open onClose={() => {}} agentId="agent-1" dir="/data/app" />);

    expect(await screen.findByText('to-be-cleared')).toBeInTheDocument();
    const user = userEvent.setup();
    // 「清屏」按钮含图标,抹空白子串匹配定位。
    await clickButtonByText(user, '清屏');
    await waitFor(() => expect(screen.queryByText('to-be-cleared')).not.toBeInTheDocument());
  });

  it('无 dir:不发起 fetch,显式提示缺目录', async () => {
    const { calls } = installFetch({ body: sseStream([]) });
    render(<InstanceLogDrawer open onClose={() => {}} agentId="agent-1" dir={null} />);

    // 兜底防御:无 dir 不应发 fetch。
    await waitFor(() =>
      expect(screen.getByText(/缺少目录(dir)|无法拉取实时日志/)).toBeInTheDocument(),
    );
    expect(calls.length).toBe(0);
  });

  it('未打开(open=false):不发起 fetch', async () => {
    const { calls } = installFetch({ body: sseStream([]) });
    render(<InstanceLogDrawer open={false} onClose={() => {}} agentId="agent-1" dir="/data/app" />);
    // 给一拍时间确认确实没发起。
    await Promise.resolve();
    expect(calls.length).toBe(0);
  });

  it('无 token(未登录态):请求不带 Authorization 头', async () => {
    sessionStorage.removeItem('platform_token');
    const { calls } = installFetch({ body: sseStream([frame('started', { sessionId: 's1' })]) });
    render(<InstanceLogDrawer open onClose={() => {}} agentId="agent-1" dir="/data/app" />);

    await waitFor(() => expect(calls.length).toBe(1));
    const headers = calls[0].init.headers as Record<string, string>;
    // 无 token 时不应注入 Authorization(交由后端按白名单/admin-token 处理)。
    expect(headers.Authorization).toBeUndefined();
  });

  it('自动滚动开关:取消勾选可切换跟随态(不报错)', async () => {
    installFetch({
      body: sseStream([frame('started', { sessionId: 's1' }), frame('chunk', { chunk: 'a-line' })]),
    });
    render(<InstanceLogDrawer open onClose={() => {}} agentId="agent-1" dir="/data/app" />);
    await screen.findByText('a-line');

    // 「自动滚动」复选框默认勾选;点一下取消(覆盖 onChange 跟随切换分支)。
    const checkbox = screen.getByRole('checkbox', { name: /自动滚动/ }) as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
    const user = userEvent.setup();
    await user.click(checkbox);
    await waitFor(() => expect(checkbox.checked).toBe(false));
  });

  it('终端滚动:上滚离底关跟随、滚回贴底恢复跟随', async () => {
    installFetch({
      body: sseStream([frame('started', { sessionId: 's1' }), frame('chunk', { chunk: 'scroll-line' })]),
    });
    render(<InstanceLogDrawer open onClose={() => {}} agentId="agent-1" dir="/data/app" />);
    await screen.findByText('scroll-line');

    const term = screen.getByTestId('log-terminal');
    const checkbox = screen.getByRole('checkbox', { name: /自动滚动/ }) as HTMLInputElement;

    // jsdom 不真实布局:手动设置滚动几何模拟「离底」(scrollHeight 远大于 scrollTop+clientHeight)。
    Object.defineProperty(term, 'scrollHeight', { value: 1000, configurable: true });
    Object.defineProperty(term, 'clientHeight', { value: 200, configurable: true });
    Object.defineProperty(term, 'scrollTop', { value: 0, writable: true, configurable: true });
    term.dispatchEvent(new Event('scroll'));
    // 离底 → 关闭跟随。
    await waitFor(() => expect(checkbox.checked).toBe(false));

    // 再滚到贴底(scrollTop 使 scrollHeight-scrollTop-clientHeight < 阈值)→ 恢复跟随。
    (term as unknown as { scrollTop: number }).scrollTop = 800;
    term.dispatchEvent(new Event('scroll'));
    await waitFor(() => expect(checkbox.checked).toBe(true));
  });
});
