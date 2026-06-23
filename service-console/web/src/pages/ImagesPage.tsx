import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { PictureOutlined, PlusOutlined } from '@ant-design/icons';
import * as resources from '../api/resources';
import type { ServiceImageRow } from '../api/resources';
import { useNamespace } from '../context/NamespaceContext';

const { Text } = Typography;

// 服务下拉项(顶部「选服务」用):list('services')。namespaceId/namespaceCode 供全局 ns 收窄。
interface ServiceOption {
  id: string | number;
  serviceCode: string;
  namespaceId?: string | number;
  namespaceCode?: string;
}

// 各 list 端点硬卡 pageSize le=200(后端 Query le=200,前后端必须一致;>200 会 422)。
const OPTIONS_PAGE_SIZE = 200;

/**
 * 镜像配置(二级页,resource `services/{id}/images`):为某个服务管理镜像台账 +「设为当前」。
 *
 * 形态照 ServicePluginsPage「顶部选服务 → 该服务绑定配置」二级页范式:
 *  - 顶部选服务(下拉,showSearch;label=namespaceCode/serviceCode)。可接 useNamespace():选了具体 ns
 *    时把服务下拉收窄到该 ns(按 service.namespaceId 过滤);「全部」则全列。
 *  - 选定服务后展示其镜像台账(listServiceImages):列 = 镜像 / 是否当前 / 创建时间 / 操作。
 *  - 「设为当前」(非当前行):Popconfirm 确认 → setCurrentImage → 刷新 + toast。
 *  - 「新增镜像」(顶部):输入 image 字符串 → 也走 setCurrentImage(后端**只暴露 set-current**,无纯追加
 *    端点),故新增即「设为当前」(文案明示「新增并设为当前」)。
 *
 * ⚠️ 取舍闭环提示:本页只改台账「当前镜像」位,**不**触发实例拉新镜像。镜像应用(pull-redeploy)走
 *    「节点」页 redeploy(后端已改为读本台账当前行);服务级一键投放镜像待 P4-5 发布弹窗。页顶说明明示,
 *    不假装本页能一键滚镜像。
 */
export default function ImagesPage() {
  const [messageApi, contextHolder] = message.useMessage();

  // P3-10:全局命名空间选中态(null = 全部命名空间)。选具体 ns 时收窄服务下拉。
  const { namespace } = useNamespace();

  // ── 顶部选服务 ───────────────────────────────────────────────────────────────
  const [serviceOptions, setServiceOptions] = useState<ServiceOption[]>([]);
  const [serviceId, setServiceId] = useState<string | number | undefined>(undefined);
  const [svcLoading, setSvcLoading] = useState(true);
  const [svcErrored, setSvcErrored] = useState(false);

  // ── 选定服务后的镜像台账 ─────────────────────────────────────────────────────
  const [images, setImages] = useState<ServiceImageRow[]>([]);
  const [imagesLoading, setImagesLoading] = useState(false);
  const [imagesErrored, setImagesErrored] = useState(false);
  // 「设为当前」提交中的目标 image(用于该行按钮 loading;同时全表禁用避免并发切换)。
  const [settingImage, setSettingImage] = useState<string | null>(null);

  // 「新增镜像」弹窗:输入的 image、提交中。
  const [addOpen, setAddOpen] = useState(false);
  const [newImage, setNewImage] = useState('');
  const [adding, setAdding] = useState(false);

  // 全局 ns 过滤后的服务全集(选了具体 ns → 仅该 ns 的服务;「全部」→ 全列)。
  const visibleServices = useMemo(
    () =>
      namespace === null
        ? serviceOptions
        : serviceOptions.filter((s) => String(s.namespaceId) === String(namespace.id)),
    [serviceOptions, namespace],
  );

  // 服务下拉 options(label=namespaceCode/serviceCode,value=id;showSearch 本地过滤)。
  const serviceSelectOptions = useMemo(
    () =>
      visibleServices.map((s) => ({
        label: s.namespaceCode ? `${s.namespaceCode}/${s.serviceCode}` : s.serviceCode,
        value: s.id,
      })),
    [visibleServices],
  );

  // 选中服务的展示标签。
  const selectedService = useMemo(
    () => serviceOptions.find((s) => String(s.id) === String(serviceId)),
    [serviceOptions, serviceId],
  );

  // 拉服务列表(顶部选服务用);失败置错误态,给页面级重试。
  const fetchServices = useCallback(async () => {
    setSvcLoading(true);
    setSvcErrored(false);
    try {
      const env = await resources.list<ServiceOption>('services', { pageSize: OPTIONS_PAGE_SIZE });
      setServiceOptions(env.rows);
    } catch {
      setSvcErrored(true);
      setServiceOptions([]);
    } finally {
      setSvcLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchServices();
  }, [fetchServices]);

  // 全局 ns 切换后,若当前选中服务已不在收窄后的集合里,清空选中(避免展示与下拉不一致)。
  useEffect(() => {
    if (serviceId === undefined) return;
    if (!visibleServices.some((s) => String(s.id) === String(serviceId))) {
      setServiceId(undefined);
    }
  }, [visibleServices, serviceId]);

  // 拉选定服务的镜像台账(listServiceImages;后端不分页一次回全集)。失败置错误态。
  const fetchImages = useCallback(async (svcId: string | number) => {
    setImagesLoading(true);
    setImagesErrored(false);
    try {
      const env = await resources.listServiceImages<ServiceImageRow>(svcId);
      setImages(env.rows);
    } catch {
      setImagesErrored(true);
      setImages([]);
    } finally {
      setImagesLoading(false);
    }
  }, []);

  // 选定服务变化时(含切换)重拉镜像;未选则清空。
  useEffect(() => {
    if (serviceId === undefined || serviceId === null || serviceId === '') {
      setImages([]);
      return;
    }
    void fetchImages(serviceId);
  }, [serviceId, fetchImages]);

  // 设为当前:setCurrentImage(serviceId, image) → 成功刷新台账 + toast。失败由 client 拦截器统一兜底。
  const handleSetCurrent = async (image: string) => {
    if (serviceId === undefined) return;
    setSettingImage(image);
    try {
      await resources.setCurrentImage(serviceId, image);
      messageApi.success('已设为当前镜像');
      await fetchImages(serviceId);
    } catch {
      // 失败由 client 拦截器统一提示。
    } finally {
      setSettingImage(null);
    }
  };

  // 新增并设为当前:输入 image → setCurrentImage(后端无纯追加端点,新增即置当前)→ 成功关弹窗 + 刷新。
  const handleAdd = async () => {
    if (serviceId === undefined) return;
    const image = newImage.trim();
    if (!image) return;
    setAdding(true);
    try {
      await resources.setCurrentImage(serviceId, image);
      messageApi.success('已新增并设为当前镜像');
      setAddOpen(false);
      setNewImage('');
      await fetchImages(serviceId);
    } catch {
      // 失败由 client 拦截器统一提示;弹窗不关,便于改后重试。
    } finally {
      setAdding(false);
    }
  };

  // 台账列:镜像 / 是否当前 / 创建时间 / 操作(非当前行可「设为当前」)。
  const imageColumns: ColumnsType<ServiceImageRow> = [
    {
      title: '镜像',
      dataIndex: 'image',
      key: 'image',
      render: (_v, r) => (
        <Text style={{ maxWidth: 420 }} ellipsis={{ tooltip: r.image }} copyable={{ text: r.image }}>
          {r.image}
        </Text>
      ),
    },
    {
      title: '是否当前',
      dataIndex: 'isCurrent',
      key: 'isCurrent',
      width: 120,
      render: (_v, r) => (r.isCurrent ? <Tag color="green">当前</Tag> : <Tag>历史</Tag>),
    },
    { title: '创建时间', dataIndex: 'createdAt', key: 'createdAt', width: 200 },
    {
      title: '操作',
      key: 'option',
      width: 140,
      render: (_v, r) =>
        // 当前行无需切换,置灰显「当前」;非当前行可「设为当前」(Popconfirm 二次确认)。
        r.isCurrent ? (
          <span style={{ color: 'rgba(0,0,0,0.25)' }}>当前</span>
        ) : (
          <Popconfirm
            title="设为当前镜像?"
            description="设为当前后,需经「节点」页对该服务重部署(redeploy)才会实际拉取。"
            okText="设为当前"
            cancelText="取消"
            onConfirm={() => handleSetCurrent(r.image)}
          >
            <Button
              type="link"
              size="small"
              loading={settingImage === r.image}
              // 任一行设为当前提交中时禁用其余行,避免并发切换。
              disabled={settingImage !== null && settingImage !== r.image}
            >
              设为当前
            </Button>
          </Popconfirm>
        ),
    },
  ];

  // 服务下拉初次加载中:整页居中 Spin(对齐 lazyPage fallback 风格)。
  if (svcLoading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 64 }}>
        <Spin />
      </div>
    );
  }

  // 服务下拉加载失败:页面级重试(不静默卡空白)。
  if (svcErrored) {
    return (
      <>
        {contextHolder}
        <Alert
          type="error"
          showIcon
          message="加载服务列表失败"
          description="请检查网络或稍后重试。"
          action={
            <Button size="small" onClick={() => void fetchServices()}>
              重试
            </Button>
          }
        />
      </>
    );
  }

  return (
    <>
      {contextHolder}

      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        {/* 顶部:选服务 + 闭环说明。 */}
        <Card>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Space wrap>
              <Text strong>服务:</Text>
              <Select
                showSearch
                allowClear
                optionFilterProp="label"
                style={{ minWidth: 320 }}
                placeholder="请选择要配置镜像的服务"
                options={serviceSelectOptions}
                value={serviceId}
                onChange={(v) => setServiceId(v)}
                notFoundContent={
                  visibleServices.length ? undefined : '当前命名空间下暂无服务'
                }
              />
            </Space>
            <Text type="secondary">
              设为当前后,需经「节点」页对该服务重部署(redeploy)使实例拉取新镜像;统一发布弹窗将于后续接入。
            </Text>
          </Space>
        </Card>

        {/* 未选服务:占位引导。 */}
        {serviceId === undefined ? (
          <Card>
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="请选择一个服务以管理其镜像"
            />
          </Card>
        ) : (
          <Card
            title={
              <Space>
                <PictureOutlined />
                <span>镜像台账</span>
                {selectedService ? (
                  <Tag color="blue">
                    {selectedService.namespaceCode
                      ? `${selectedService.namespaceCode}/${selectedService.serviceCode}`
                      : selectedService.serviceCode}
                  </Tag>
                ) : null}
              </Space>
            }
            extra={
              <Button
                type="primary"
                icon={<PlusOutlined />}
                onClick={() => {
                  setNewImage('');
                  setAddOpen(true);
                }}
                disabled={imagesLoading || imagesErrored}
              >
                新增并设为当前
              </Button>
            }
          >
            {imagesLoading ? (
              <div style={{ display: 'flex', justifyContent: 'center', padding: 32 }}>
                <Spin />
              </div>
            ) : imagesErrored ? (
              <Alert
                type="error"
                showIcon
                message="加载该服务的镜像台账失败"
                description="请稍后重试。"
                action={
                  <Button size="small" onClick={() => void fetchImages(serviceId)}>
                    重试
                  </Button>
                }
              />
            ) : images.length ? (
              <Table<ServiceImageRow>
                rowKey={(r) => String(r.id)}
                columns={imageColumns}
                dataSource={images}
                pagination={false}
                size="middle"
              />
            ) : (
              <Empty description="该服务暂无镜像 —— 点右上「新增并设为当前」" />
            )}
          </Card>
        )}
      </Space>

      {/* 新增镜像弹窗:输入 image → set-current(后端无纯追加端点,新增即置当前)。 */}
      <Modal
        title="新增并设为当前镜像"
        open={addOpen}
        onCancel={() => setAddOpen(false)}
        onOk={() => void handleAdd()}
        okText="新增并设为当前"
        cancelText="取消"
        okButtonProps={{ loading: adding, disabled: newImage.trim() === '' }}
        destroyOnClose
      >
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            message="后端仅提供「设为当前」语义,故新增镜像即把它置为该服务的当前镜像;实际拉取仍需到「节点」页重部署。"
          />
          <Form layout="vertical">
            <Form.Item label="镜像地址" required style={{ marginBottom: 0 }}>
              <Input
                placeholder="如 oci.example.com/nocobase-pro:1.7.20"
                value={newImage}
                onChange={(e) => setNewImage(e.target.value)}
                onPressEnter={() => {
                  if (newImage.trim() && !adding) void handleAdd();
                }}
              />
            </Form.Item>
          </Form>
        </Space>
      </Modal>
    </>
  );
}
