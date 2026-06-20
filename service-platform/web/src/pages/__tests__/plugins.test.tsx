import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import PluginsPage from '../PluginsPage';

// mock 资源层:插件页所有数据访问都走 ../../api/resources。
const list = vi.fn();
const create = vi.fn();
const update = vi.fn();
const remove = vi.fn();
vi.mock('../../api/resources', () => ({
  list: (...a: unknown[]) => list(...a),
  create: (...a: unknown[]) => create(...a),
  update: (...a: unknown[]) => update(...a),
  remove: (...a: unknown[]) => remove(...a),
}));

// antd 在两个汉字按钮间插空格,带图标按钮的 accessible name 还含图标名;抹空白后用 includes 匹配。
const byNormalizedName = (text: string) => (name: string) => name.replace(/\s/g, '').includes(text);

const oneRowEnvelope = {
  count: 1,
  rows: [{ id: 3, code: 'plugin-demo', name: '演示插件' }],
  page: 1,
  pageSize: 20,
  totalPage: 1,
};

describe('PluginsPage', () => {
  beforeEach(() => {
    list.mockReset();
    create.mockReset();
    update.mockReset();
    remove.mockReset();
    list.mockResolvedValue(oneRowEnvelope);
  });

  it('列表渲染(走 resources.list,列用后端 code/name)', async () => {
    render(<PluginsPage />);
    expect(await screen.findByText('plugin-demo')).toBeInTheDocument();
    expect(screen.getByText('演示插件')).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith('plugins', expect.anything());
  });

  it('点「添加」→ 填 code → 提交 → 调 create', async () => {
    create.mockResolvedValue({ id: 4, code: 'plugin-new' });
    const user = userEvent.setup();
    render(<PluginsPage />);

    expect(await screen.findByText('plugin-demo')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: byNormalizedName('添加') }));
    await user.type(await screen.findByLabelText('插件编码'), 'plugin-new');
    await user.click(screen.getByRole('button', { name: byNormalizedName('确认') }));

    await waitFor(() => {
      expect(create).toHaveBeenCalledWith('plugins', expect.objectContaining({ code: 'plugin-new' }));
    });
  });
});
