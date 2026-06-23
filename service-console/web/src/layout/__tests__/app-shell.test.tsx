import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthContext';
import { NamespaceContext, type NamespaceContextValue } from '../../context/NamespaceContext';
import type { NamespaceRow } from '../../api/resources';
import AppShell from '../AppShell';

// mock api client:AuthContext 在有 token 时会 best-effort 调 client.get('/auth/me'),
// 这里 mock 掉防真网络;返回用户名供顶栏展示。
const get = vi.fn();
vi.mock('../../api/client', () => ({
  default: { get: (...a: unknown[]) => get(...a) },
}));

// 命名空间切换器(P3-10)用受控 NamespaceContext 喂定值,免触发真实 listNamespaces 拉取;
// setNamespace 记录调用以断言切换行为。
const nsOptions: NamespaceRow[] = [
  { id: 1, code: 'ns-admin', name: '管理' },
  { id: 2, code: 'ns-prod', name: '生产' },
];

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

function renderShell(nsValue?: Partial<NamespaceContextValue>) {
  const ctx: NamespaceContextValue = {
    namespace: null,
    setNamespace: () => {},
    options: nsOptions,
    optionsLoading: false,
    ...nsValue,
  };
  return render(
    <AuthProvider>
      <NamespaceContext.Provider value={ctx}>
        <MemoryRouter initialEntries={['/namespaces']}>
          <Routes>
            <Route path="/" element={<AppShell />}>
              <Route path="namespaces" element={<div>命名空间页</div>} />
            </Route>
            <Route path="/login" element={<div>登录页</div>} />
          </Routes>
        </MemoryRouter>
      </NamespaceContext.Provider>
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

describe('AppShell 命名空间切换器(P3-10)', () => {
  beforeEach(() => {
    get.mockReset();
    get.mockResolvedValue({ data: { user: 'admin' } });
    sessionStorage.clear();
  });

  it('顶栏渲染切换器:含「全部命名空间」+ 各命名空间;默认显「全部命名空间」', async () => {
    const user = userEvent.setup();
    renderShell();

    // 切换器以 aria-label 定位(combobox 角色)。默认值「全部命名空间」(namespace=null)。
    const combo = await screen.findByRole('combobox', { name: '命名空间切换器' });
    expect(combo).toBeInTheDocument();
    // antd Select 选中项文本渲染在切换器容器内。
    expect(screen.getByText('全部命名空间')).toBeInTheDocument();

    // 展开下拉:选项含「全部命名空间」+ 各命名空间 code。
    await user.click(combo);
    await waitFor(() => {
      const opts = document.querySelectorAll('.ant-select-item-option-content');
      const texts = Array.from(opts).map((o) => o.textContent);
      expect(texts).toContain('全部命名空间');
      expect(texts).toContain('ns-admin');
      expect(texts).toContain('ns-prod');
    });
  });

  it('选某具体命名空间 → setNamespace({id, code}) 带该行 id 与 code', async () => {
    const setNamespace = vi.fn();
    const user = userEvent.setup();
    renderShell({ setNamespace });

    const combo = await screen.findByRole('combobox', { name: '命名空间切换器' });
    await user.click(combo);
    const option = await screen.findByText(
      (_t, node) =>
        node?.classList.contains('ant-select-item-option-content') === true &&
        node.textContent === 'ns-prod',
    );
    await user.click(option);

    // 关键:切到 ns-prod → setNamespace({ id: 2, code: 'ns-prod' })(id 给台账过滤、code 给发现过滤)。
    await waitFor(() => {
      expect(setNamespace).toHaveBeenCalledWith({ id: 2, code: 'ns-prod' });
    });
  });

  it('已选具体命名空间时,切回「全部命名空间」→ setNamespace(null)', async () => {
    const setNamespace = vi.fn();
    const user = userEvent.setup();
    // 初始选中 ns-prod。
    renderShell({ namespace: { id: 2, code: 'ns-prod' }, setNamespace });

    const combo = await screen.findByRole('combobox', { name: '命名空间切换器' });
    // 当前选中项显示为 ns-prod 的 code。
    const selector = combo.closest('.ant-select')!;
    expect(within(selector as HTMLElement).getByText('ns-prod')).toBeInTheDocument();

    await user.click(combo);
    const allOption = await screen.findByText(
      (_t, node) =>
        node?.classList.contains('ant-select-item-option-content') === true &&
        node.textContent === '全部命名空间',
    );
    await user.click(allOption);

    // 关键:选「全部命名空间」→ setNamespace(null)(回到跨 ns 全集)。
    await waitFor(() => {
      expect(setNamespace).toHaveBeenCalledWith(null);
    });
  });
});
