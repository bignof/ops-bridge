import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { AxiosAdapter, AxiosResponse } from 'axios';

// mock antd 的静态 message:拦截器在非 React 上下文里用静态 message.error 兜底全局错误,
// 这里把它换成 spy 以断言「是否弹了/弹了什么」。其余 antd 导出按需透传(本测试只用 message)。
const errorSpy = vi.fn();
vi.mock('antd', () => ({
  message: { error: (...a: unknown[]) => errorSpy(...a) },
}));

// 被测:真实 client(含请求/响应拦截器)。用 import() 确保上面的 antd mock 已就位。
import client from '../client';

// 用自定义 adapter 把每个请求「短路」成我们指定的 HTTP 结果(成功或带状态码失败),
// 从而真实驱动响应拦截器的错误分支(而非靠真网络)。
// - 成功:resolve 一个 AxiosResponse;
// - 失败:reject 一个带 `response.status` / `response.data` + `config` 的 AxiosError 形状对象
//         (axios 内部会把它交给响应拦截器的 onRejected)。
type MockOutcome =
  | { ok: true; status?: number; data?: unknown }
  | { ok: false; status: number; data?: unknown };

const installAdapter = (outcome: MockOutcome) => {
  const adapter: AxiosAdapter = (config) => {
    const response: AxiosResponse = {
      data: outcome.data,
      status: outcome.ok ? (outcome.status ?? 200) : outcome.status,
      statusText: '',
      headers: {},
      config,
    };
    if (outcome.ok) return Promise.resolve(response);
    return Promise.reject(
      Object.assign(new Error(`HTTP ${outcome.status}`), {
        isAxiosError: true,
        config,
        response,
      }),
    );
  };
  client.defaults.adapter = adapter;
};

describe('api/client 响应拦截器(A2 全局兜底 + B8 会话失效)', () => {
  beforeEach(() => {
    errorSpy.mockReset();
    sessionStorage.clear();
    // 每个用例后被 cleanup 复位 hash;先归零到非 login,便于断言 401 跳转。
    location.hash = '#/namespaces';
  });

  afterEach(() => {
    // 还原默认 adapter,避免污染其它用例。
    delete (client.defaults as { adapter?: unknown }).adapter;
  });

  // ── B8:401 会话失效主闸(清 token + 跳登录) ───────────────────────────────
  it('401 → 清 sessionStorage token 且跳 #/login', async () => {
    sessionStorage.setItem('platform_token', 'tok-xyz');
    installAdapter({ ok: false, status: 401, data: { detail: '未认证' } });

    await expect(client.get('/api/namespaces')).rejects.toBeTruthy();

    // 主闸不变式:token 被清、hash 跳到登录。
    expect(sessionStorage.getItem('platform_token')).toBeNull();
    expect(location.hash).toBe('#/login');
    // 401 不走通用兜底 toast(交登录页/跳转处理),不应弹全局错误。
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it('401 时即使请求带 suppressGlobalError 仍清 token + 跳登录(会话失效优先级最高)', async () => {
    sessionStorage.setItem('platform_token', 'tok-xyz');
    installAdapter({ ok: false, status: 401 });

    await expect(
      client.post('/api/namespaces', {}, { suppressGlobalError: true }),
    ).rejects.toBeTruthy();

    expect(sessionStorage.getItem('platform_token')).toBeNull();
    expect(location.hash).toBe('#/login');
  });

  // ── A2:非 401 写失败全局兜底 toast(杜绝静默吞) ─────────────────────────────
  it('非 401(500)且未 opt-out → 兜底 toast(取后端 detail);不跳登录、不清 token', async () => {
    sessionStorage.setItem('platform_token', 'keep-me');
    installAdapter({ ok: false, status: 500, data: { detail: '上游 hub 不可用' } });

    await expect(client.delete('/api/namespaces/1')).rejects.toBeTruthy();

    // A2 主张:非 401 失败必须有可见提示,且优先用后端 detail。
    expect(errorSpy).toHaveBeenCalledTimes(1);
    expect(errorSpy).toHaveBeenCalledWith('上游 hub 不可用');
    // 非 401 不应误伤会话:token 保留、不跳登录。
    expect(sessionStorage.getItem('platform_token')).toBe('keep-me');
    expect(location.hash).toBe('#/namespaces');
  });

  it('非 401 且无 detail → 兜底通用文案「操作失败,请稍后重试」', async () => {
    installAdapter({ ok: false, status: 502, data: {} });

    await expect(client.post('/api/namespaces/1/rotate-key')).rejects.toBeTruthy();

    expect(errorSpy).toHaveBeenCalledTimes(1);
    expect(errorSpy).toHaveBeenCalledWith('操作失败,请稍后重试');
  });

  // ── 防双 toast:opt-out 的请求拦截器不兜底(交页面精确提示) ──────────────────
  it('非 401 且 suppressGlobalError=true → 拦截器不兜底 toast(交页面处理,防双弹)', async () => {
    installAdapter({ ok: false, status: 409, data: { detail: '编码已存在' } });

    await expect(
      client.post('/api/namespaces', {}, { suppressGlobalError: true }),
    ).rejects.toBeTruthy();

    // 关键:opt-out 后拦截器静默,由调用页(CrudTable/上传页/发布页)自弹精确文案 → 不双弹。
    expect(errorSpy).not.toHaveBeenCalled();
  });

  // 成功响应原样透传(回归:拦截器不误伤正常链路)。
  it('成功响应原样返回,不弹错误', async () => {
    installAdapter({ ok: true, status: 200, data: { rows: [] } });

    const r = await client.get<{ rows: unknown[] }>('/api/namespaces');
    expect(r.data).toEqual({ rows: [] });
    expect(errorSpy).not.toHaveBeenCalled();
  });
});
