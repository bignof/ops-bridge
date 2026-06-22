import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthContext';
import AppShell from '../AppShell';

// mock api client:AuthContext 在有 token 时会 best-effort 调 client.get('/auth/me'),
// 这里 mock 掉防真网络;返回用户名供顶栏展示。
const get = vi.fn();
vi.mock('../../api/client', () => ({
  default: { get: (...a: unknown[]) => get(...a) },
}));

// jsdom 不实现以下 API,antd Menu/Dropdown 渲染会用到,补最小 stub。
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

function renderShell() {
  return render(
    <AuthProvider>
      <MemoryRouter initialEntries={['/namespaces']}>
        <Routes>
          <Route path="/" element={<AppShell />}>
            <Route path="namespaces" element={<div>命名空间页</div>} />
          </Route>
          <Route path="/login" element={<div>登录页</div>} />
        </Routes>
      </MemoryRouter>
    </AuthProvider>,
  );
}

describe('AppShell 退出登录(B8)', () => {
  beforeEach(() => {
    get.mockReset();
    get.mockResolvedValue({ data: { user: 'admin' } });
    sessionStorage.clear();
  });

  it('点「退出登录」→ 清 sessionStorage token', async () => {
    sessionStorage.setItem('platform_token', 'tok-live');
    const user = userEvent.setup();
    renderShell();

    // 顶栏用户区渲染出来(/auth/me 回来的用户名)。
    expect(await screen.findByText('admin')).toBeInTheDocument();

    // 打开右上角用户下拉(点用户名触发),再点「退出登录」(菜单项渲染在 portal)。
    await user.click(screen.getByText('admin'));
    const logoutItem = await screen.findByText('退出登录');
    await user.click(logoutItem);

    // B8 主闸:logout 清掉 sessionStorage 里的会话 token。
    await waitFor(() => {
      expect(sessionStorage.getItem('platform_token')).toBeNull();
    });
  });
});
