import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import PluginUploadPage from '../PluginUploadPage';

// mock 资源层:插件上传页的数据访问都走 ../../api/resources。
//  - listPluginVersions → 下方 ProTable 列表(列用后端可读名 pluginCode/version/filename)
//  - uploadPluginVersion → 拖拽/选 .tgz 后的上传(成功回 { version },失败按状态码区分)
const listPluginVersions = vi.fn();
const uploadPluginVersion = vi.fn();
vi.mock('../../api/resources', () => ({
  listPluginVersions: (...a: unknown[]) => listPluginVersions(...a),
  uploadPluginVersion: (...a: unknown[]) => uploadPluginVersion(...a),
}));

// jsdom 不实现 ResizeObserver,ProTable / antd 内部会调用,补最小 stub 保证渲染稳定。
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// 下方列表信封:mock 字段集对齐后端真实 PluginVersionOut(根治假绿,勿注入后端不回的字段)。
// PluginVersionOut = { id, pluginId, version, name, pluginCode, filename }
//   —— G1 后端已让 GET /api/plugin-versions LEFT JOIN 回 pluginCode(=plugin.code)/filename(=attachment.filename),
//   故 mock 保留这两字段是「真回」而非伪造;列渲染 pluginCode/version/filename。
const versionsEnvelope = {
  count: 1,
  rows: [
    {
      id: 21,
      pluginId: 3,
      version: '1.0.0',
      name: '演示插件',
      pluginCode: 'plugin-demo',
      filename: 'plugin-demo-1.0.0.tgz',
    },
  ],
  page: 1,
  pageSize: 20,
  totalPage: 1,
};

// 取 Upload.Dragger 渲染的隐藏 file input(无 accessible label,按 type 选)。
const fileInput = (container: HTMLElement) =>
  container.querySelector('input[type="file"]') as HTMLInputElement;

// 构造一个 .tgz 假文件用于上传交互。
const makeTgz = () => new File([new Uint8Array([1, 2, 3])], 'plugin-demo-1.2.3.tgz');

describe('PluginUploadPage', () => {
  beforeEach(() => {
    listPluginVersions.mockReset();
    uploadPluginVersion.mockReset();
    listPluginVersions.mockResolvedValue(versionsEnvelope);
  });

  it('列表渲染:走 listPluginVersions(服务端分页),列用后端可读名 pluginCode/version/filename', async () => {
    render(<PluginUploadPage />);
    expect(await screen.findByText('plugin-demo')).toBeInTheDocument();
    expect(screen.getByText('1.0.0')).toBeInTheDocument();
    expect(screen.getByText('plugin-demo-1.0.0.tgz')).toBeInTheDocument();
    // 服务端分页:request 透传 page/pageSize 给 listPluginVersions。
    expect(listPluginVersions).toHaveBeenCalledWith(
      expect.objectContaining({ page: 1, pageSize: 20 }),
    );
  });

  it('上传成功 → 成功提示含解析出的 version,并刷新列表', async () => {
    uploadPluginVersion.mockResolvedValue({
      pluginVersionId: 99,
      attachmentId: 88,
      version: '1.2.3',
    });
    const user = userEvent.setup();
    const { container } = render(<PluginUploadPage />);

    // 列表先加载(确认 ProTable request 已走 listPluginVersions)。
    expect(await screen.findByText('plugin-demo')).toBeInTheDocument();
    listPluginVersions.mockClear();

    // 选 .tgz 文件 → 触发 customRequest → 调 uploadPluginVersion。
    await user.upload(fileInput(container), makeTgz());

    await waitFor(() => {
      expect(uploadPluginVersion).toHaveBeenCalledTimes(1);
    });
    // 成功 message 必须回显后端解析出的 version。
    expect(await screen.findByText((t) => t.includes('1.2.3'))).toBeInTheDocument();
    // 上传成功后刷新列表(再次调 listPluginVersions)。
    await waitFor(() => {
      expect(listPluginVersions).toHaveBeenCalled();
    });
  });

  it('上传 409 → 明确提示「该版本已存在」', async () => {
    uploadPluginVersion.mockRejectedValue({ response: { status: 409 } });
    const user = userEvent.setup();
    const { container } = render(<PluginUploadPage />);

    expect(await screen.findByText('plugin-demo')).toBeInTheDocument();

    await user.upload(fileInput(container), makeTgz());

    await waitFor(() => {
      expect(uploadPluginVersion).toHaveBeenCalledTimes(1);
    });
    expect(await screen.findByText((t) => t.includes('该版本已存在'))).toBeInTheDocument();
  });

  it('上传 400 → 明确提示「未匹配到插件」', async () => {
    uploadPluginVersion.mockRejectedValue({ response: { status: 400 } });
    const user = userEvent.setup();
    const { container } = render(<PluginUploadPage />);

    expect(await screen.findByText('plugin-demo')).toBeInTheDocument();

    await user.upload(fileInput(container), makeTgz());

    await waitFor(() => {
      expect(uploadPluginVersion).toHaveBeenCalledTimes(1);
    });
    expect(await screen.findByText((t) => t.includes('未匹配到插件'))).toBeInTheDocument();
  });

  it('上传 413 → 明确提示「文件超出大小限制」', async () => {
    uploadPluginVersion.mockRejectedValue({ response: { status: 413 } });
    const user = userEvent.setup();
    const { container } = render(<PluginUploadPage />);

    expect(await screen.findByText('plugin-demo')).toBeInTheDocument();

    await user.upload(fileInput(container), makeTgz());

    await waitFor(() => {
      expect(uploadPluginVersion).toHaveBeenCalledTimes(1);
    });
    expect(await screen.findByText((t) => t.includes('文件超出大小限制'))).toBeInTheDocument();
  });

  it('上传非特定错误码(500)→ 通用兜底提示「上传失败,请重试」(opt-out 后页面自管,不静默吞)', async () => {
    uploadPluginVersion.mockRejectedValue({ response: { status: 500 } });
    const user = userEvent.setup();
    const { container } = render(<PluginUploadPage />);

    expect(await screen.findByText('plugin-demo')).toBeInTheDocument();

    await user.upload(fileInput(container), makeTgz());

    await waitFor(() => {
      expect(uploadPluginVersion).toHaveBeenCalledTimes(1);
    });
    // A2:上传请求在资源层 opt-out 全局兜底,页面对非 400/409/413 必须有通用可见提示,不可静默吞。
    expect(await screen.findByText((t) => t.includes('上传失败'))).toBeInTheDocument();
  });
});
