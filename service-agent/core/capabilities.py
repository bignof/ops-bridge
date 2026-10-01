"""
capabilities.py — 本 Agent 支持的协议能力清单，随 agent_report.runtime.capabilities 上报。

Hub 按能力决定下发方式和是否提前拒绝（例如滚动重启用一条命令完成 drain+restart，
旧 Agent 不支持日志协议时直接提示升级而不是等超时）。只增不改：已发布的名字含义固定，
新能力加新名字；对照表见仓库 docs/PROTOCOL.md。
"""

CAPABILITIES = (
    'drain_restart',    # restart/update 接受 drain=true：在同一条命令、同一把目录锁里先下线再重启
    'status_all',       # 巡检包含已退出的容器（compose ps --all），不再整个缺席
    'header_auth',      # 连接鉴权的 key 走 X-Agent-Key 请求头，不再放在 URL 里
    'logfile',          # logfile_list / logfile_fetch / logfile_follow / logfile_unfollow
    'compose_inspect',  # compose_discover / compose_inspect
    'plugin_ops',       # plugin_scan / plugin_remove / plugin_restore
)
