import { Alert, Button, Input, Modal, Space, message } from 'antd';
import { CopyOutlined } from '@ant-design/icons';
import copy from 'copy-to-clipboard';

export interface ShowOnceModalProps {
  /** 弹窗标题(如「Agent Key」「Pull Token」)。 */
  title: string;
  /** 待展示的明文值(密钥 / token);为空串时复制按钮禁用。 */
  value: string;
  open: boolean;
  onClose: () => void;
}

/**
 * Show-once 明文弹窗:展示后端一次性返回的敏感明文(agentKey / pullToken 等)+「复制」。
 * - 复制必须用 `copy-to-clipboard` 包:系统经 HTTP + 内网 IP 访问,`navigator.clipboard`
 *   为 undefined 会静默失效(见根 CLAUDE.md);按该包返回的布尔值给 message 成功/失败提示。
 * - 关闭即不可再得:文案明确提醒用户先妥善保存,关闭后无法再次查看。
 */
export default function ShowOnceModal({ title, value, open, onClose }: ShowOnceModalProps) {
  const [messageApi, contextHolder] = message.useMessage();

  const handleCopy = () => {
    // copy() 返回是否成功;失败(如浏览器禁止)时给出明确失败提示,不假装成功。
    const ok = copy(value);
    if (ok) messageApi.success('已复制到剪贴板');
    else messageApi.error('复制失败,请手动选中复制');
  };

  return (
    <Modal
      title={title}
      open={open}
      onCancel={onClose}
      maskClosable={false}
      footer={[
        <Button key="close" onClick={onClose}>
          关闭
        </Button>,
      ]}
    >
      {contextHolder}
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Alert
          type="warning"
          showIcon
          message="请立即复制并妥善保存,此明文仅展示这一次,关闭后无法再次查看。"
        />
        <Space.Compact style={{ width: '100%' }}>
          <Input readOnly value={value} />
          <Button icon={<CopyOutlined />} onClick={handleCopy} disabled={!value}>
            复制
          </Button>
        </Space.Compact>
      </Space>
    </Modal>
  );
}
