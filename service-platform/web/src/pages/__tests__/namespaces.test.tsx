import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import NamespacesPage from '../NamespacesPage';

// mock 资源层:命名空间页所有数据访问都走 ../../api/resources。
// list 必须回一行,使行操作「轮换 pull token」可见;create/rotatePullToken 为断言目标。
const list = vi.fn();
const create = vi.fn();
const update = vi.fn();
const remove = vi.fn();
const rotateKey = vi.fn();
const rotatePullToken = vi.fn();
vi.mock('../../api/resources', () => ({
  list: (...a: unknown[]) => list(...a),
  create: (...a: unknown[]) => create(...a),
  update: (...a: unknown[]) => update(...a),
  remove: (...a: unknown[]) => remove(...a),
  rotateKey: (...a: unknown[]) => rotateKey(...a),
  rotatePullToken: (...a: unknown[]) => rotatePullToken(...a),
}));

// antd 在两个汉字按钮间插空格,且带图标的按钮 accessible name 会含图标名(如「plus 添加」);
// 抹掉空白后用 includes 匹配按钮文案。
const byNormalizedName = (text: string) => (name: string) => name.replace(/\s/g, '').includes(text);

const oneRowEnvelope = {
  count: 1,
  rows: [{ id: 7, code: 'ns-demo', name: '演示命名空间' }],
  page: 1,
  pageSize: 20,
  totalPage: 1,
};

describe('NamespacesPage', () => {
  beforeEach(() => {
    list.mockReset();
    create.mockReset();
    update.mockReset();
    remove.mockReset();
    rotateKey.mockReset();
    rotatePullToken.mockReset();
    list.mockResolvedValue(oneRowEnvelope);
  });

  it('点「添加」→ 填 code → 提交 → 调 create', async () => {
    create.mockResolvedValue({ id: 8, code: 'ns-new' }); // 无 agentKey,不弹 show-once
    const user = userEvent.setup();
    render(<NamespacesPage />);

    // 列表先加载出来(确认 request 走了 resources.list)。
    expect(await screen.findByText('ns-demo')).toBeInTheDocument();

    // 打开新建 Drawer。
    await user.click(screen.getByRole('button', { name: byNormalizedName('添加') }));

    // Drawer 里填命名空间编码(label「命名空间编码」)。
    const codeInput = await screen.findByLabelText('命名空间编码');
    await user.type(codeInput, 'ns-new');

    // 提交(DrawerForm 默认提交按钮文案为「确 认」)。
    await user.click(screen.getByRole('button', { name: byNormalizedName('确认') }));

    await waitFor(() => {
      expect(create).toHaveBeenCalledWith('namespaces', expect.objectContaining({ code: 'ns-new' }));
    });
  });

  it('创建响应含 agentKey → 弹 ShowOnceModal 展示明文', async () => {
    create.mockResolvedValue({ id: 9, code: 'ns-key', agentKey: 'AK-SECRET-123' });
    const user = userEvent.setup();
    render(<NamespacesPage />);
    await screen.findByText('ns-demo');

    await user.click(screen.getByRole('button', { name: byNormalizedName('添加') }));
    await user.type(await screen.findByLabelText('命名空间编码'), 'ns-key');
    await user.click(screen.getByRole('button', { name: byNormalizedName('确认') }));

    // show-once 弹窗里出现返回的明文(input value)。
    await waitFor(() => {
      expect(screen.getByDisplayValue('AK-SECRET-123')).toBeInTheDocument();
    });
  });

  it('点「轮换 pull token」→ 调 API → 弹 ShowOnceModal 含返回明文', async () => {
    rotatePullToken.mockResolvedValue({ pullToken: 'PT-PLAINTEXT-456' });
    const user = userEvent.setup();
    render(<NamespacesPage />);

    // 等行渲染出来,再点该行的「轮换 pull token」。
    const cell = await screen.findByText('ns-demo');
    const row = cell.closest('tr')!;
    await user.click(within(row).getByText('轮换 pull token'));

    // 调到了 rotatePullToken(id=7)。
    await waitFor(() => {
      expect(rotatePullToken).toHaveBeenCalledWith(7);
    });
    // 弹窗展示返回的 pull token 明文。
    expect(await screen.findByDisplayValue('PT-PLAINTEXT-456')).toBeInTheDocument();
  });
});
