# Ping Logger

轻量 VPS / WSL ICMP 往返延迟监测。Python 标准库、SQLite WAL、HTTP Server、Chart.js 和 Linux iputils ping；不引入新的运行依赖，保留原有界面、Caddy HTTPS 和 Basic Auth。

**v1.1.0：可配置采样、逐目标健康检查和更完整的网络质量统计。** 从 v1.0.0 升级时，新版首次启动会备份并迁移数据库；请先阅读迁移和升级说明。

## 运行与配置

```bash
python3 app.py --db ./development.db --port 18080
python3 app.py --db ./development.db --port 18080 --interval 10 --packets 3 --workers 4 --retention-days 30
```

仅绑定 127.0.0.1，本地访问 http://localhost:18080。后端自身没有登录认证，不要公开监听；公网访问必须经过带认证的 Caddy。WSL 开发模式无需域名和证书。

```bash
cp config.example.json config.json
python3 app.py --db ./development.db --config ./config.json
```

| JSON 字段 / CLI | 可选值 | 默认 |
|---|---|---|
| interval / --interval | 10、30、60 秒 | 60 |
| packets / --packets | 1、3、5 包 | 1 |
| workers / --workers | 整数 1–20 | 4 |
| retention_days / --retention-days | 7、30 天 | 7 |
| enable_cleanup / --enable-cleanup | JSON 布尔 / CLI 启用标志 | false |

CLI 覆盖 JSON；非法参数或未知字段拒绝启动。配置启动时加载，修改后需操作者安排重启生效。网页管理目标：最多 20 个，支持 IPv4/IPv6 字面地址，不接受域名、回环、组播、未指定和链路本地地址。

## 采样和统计

- 每个目标每周期只写一条汇总记录，保存发送/成功包数、平均/最小/最大 RTT、成功包 RTT 列表。失败包数为 sent-received；单包时 min=avg=max。
- ping 使用 `-n -c N -i 0.2 -W 2`；发包间隔 0.2 秒，等待响应上限 2 秒，外层进程保护超时 6 秒。不同时设置 `-w`，避免 iputils 在高延迟路径上为等待足够回复而继续发送额外包，造成包数超限和错误分类。
- 线程池最多 workers 个探测进程。周期不重叠；过慢时记录 collection_overrun，不额外启动第二轮。退出时跳过尚未开始的任务，活动命令受 6 秒保护期限限制，已完成结果仍写入。
- 图中延迟为每周期成功包平均值；部分丢包保留成功 RTT。丢包事件在图底部显示红点，悬停显示该周期丢包率与收包数；不再使用右侧百分比纵轴，整体丢包率仍显示在上方统计中。本机错误为灰点；无响应或停采断线，不将延迟补零。左右方向键可切换当前目标，编辑输入框、选择控件或可编辑文本时不触发，切换保留时间范围和延迟上限。
- 历史 min/avg/max、P50/P95/P99 基于所选范围内**所有成功包 RTT**，不再次平均周期均值。分位数按排序后位置 `(n-1)*p` 线性插值；只有一个样本时等于该实测值，无样本返回 null，界面显示“—”。iputils 的 time<1 按上界 1ms 记录，并非精确值。
- 丢包率 = 失败包数 / 有效发送包数；本地权限、缺路由、命令缺失、进程异常和系统错误不计网络丢包。没有有效包返回 null。
- 连续丢包是**连续全丢采样周期数**，不是单包连续失败数；部分成功、本机错误或时间断档会重置。最近成功是有成功包的周期，最近失败是有网络丢包的周期，本机错误不算网络失败。
- summary 属于所选时间范围；current 是最新一个周期，可能早于所选范围；state 是目标当前状态。最近成功/失败字段限于各自统计范围，不推断已清理历史之外的信息。

Ping 是 RTT，不能独立测回程或显示路由。目标禁用/限制 ICMP 会影响结果，本机必须具备目标 IP 协议的网络连通性。

## API 与健康检查

保留 /api/targets、/api/data?target=ID&hours=24、/api/delete、/api/health。samples 仍为 `[ts,ms,status]` 数组，status 保持 ok/loss/error。新增 sample_details、summary、current、state 和 retention_days。部分丢包保留 status=ok，具体由 sent/received 和 reason 区分。

时间范围仍是 1/6/24/72/168 小时；30 天保留不等于新增 30 天网页视图。删除接口改为**停用**，保留 ID 和历史，再次添加相同 IP 恢复监测。启用保留策略后，停用目标的过期历史也会被清理，不保证永久归档。

健康接口保留 ok、targets（数量）、latest（全局最新时间），新增 sampling_ok 和逐目标 details。每个目标通过主键索引单独查询最新记录，不会被其他目标掩盖。

| state | 含义 |
|---|---|
| pending | 尚无采样 |
| success | 有响应，无丢包 |
| partial_loss | 部分成功、部分丢包 |
| loss | 全丢，采集器仍在工作 |
| probe_error | 本地探测错误 |
| interrupted | 采样超过宽限期 |

宽限期为 `max(3×interval, 20×6/workers+10)` 秒，兼顾最大目标数量排队。任一目标 pending/interrupted/probe_error 返回 503；仅网络丢包返回 200，但明确显示 loss。无目标返回 200。ok 比旧版更严格，健康不代表网络无丢包。

## 数据库与迁移

Schema version 2（PRAGMA user_version）：

- targets 保留 id/address/name，新增 active。
- samples 保留 target_id/ts/ms/status，新增 reason、sent、received、min_ms、max_ms、rtts（JSON）、interval。
- 保留 `(target_id,ts)` 主键，新增 samples_expiry(ts) 索引供过期清理。
- 旧样本按单包语义回填：成功 1/1，丢包 1/0，本机错误 0/0；reason=legacy，不猜测过去无法确认的失败原因。

新版下次启动时自动迁移。迁移前通过 SQLite Backup API 自动创建 `数据库路径.pre-v2-时间戳.sqlite` 一致性备份。迁移事务不删除目标和历史，重复启动不重复迁移。新版库不要交回旧版采集器；回退应保留新库，恢复迁移前备份，备份后的新样本不会自动合并。

连接启用 WAL、foreign_keys、5 秒 busy_timeout、synchronous=FULL；探测在事务外执行，写入为短事务。busy/locked 最多三次尝试并记录日志，耗尽后本轮报错、下轮继续。外部写锁一直不释放仍需人为排查，无法保证持锁期间写入成功。

## 保留与空间管理

**防止未经确认删除已有数据，默认关闭清理。** 启用前先备份并确认天数：

```bash
python3 app.py --db ./development.db --retention-days 30 --enable-cleanup
```

启用后每轮按 ts 索引最多删除 5000 条过期记录，积压分多轮处理；随后执行 PASSIVE checkpoint 和增量空闲页回收。不删除 WAL/SHM，不执行全库 VACUUM。已有库未启用 incremental auto_vacuum 时不强制重建，空闲页可复用，但文件不保证马上缩小。

不启用清理则数据库持续增长，生产使用应由操作者确认后启用。20 目标、10 秒周期、30 天约 518 万条汇总记录，空间取决于包数，需要监控磁盘。长期读事务可能延迟 WAL 回收，不要用外部工具长期持有事务。

## 安装和升级

全新 Debian/Ubuntu systemd 主机需放行公网 TCP 80/443。仍使用 sslip.io 域名、Caddy 自动 HTTPS 和全站 Basic Auth；已有 Caddy 或端口占用会停止，不覆盖现有网关。

在全新服务器上一键安装 v1.1.0：

```bash
curl -fsSL https://raw.githubusercontent.com/pangzibaike/ping-logger/v1.1.0/install.sh -o /tmp/ping-logger-install.sh && sudo bash /tmp/ping-logger-install.sh
```

安装完成会显示 sslip.io HTTPS 地址、admin 用户名和随机密码。公网 IP 自动检测错误时，可用 `sudo PUBLIC_IP=你的公网IP bash /tmp/ping-logger-install.sh` 指定。

也支持在全新主机从当前源码安装（会安装依赖、创建并启动服务，请自行确认环境）：

```bash
sudo PING_LOGGER_SOURCE_DIR="$PWD" bash install.sh
```

v1.0.0 安装命令不包含这些改进。安装器用于首次安装，不能用它覆盖已有安装。

已有安装应先备份源码和数据库，由操作者安排维护窗口停止旧进程，替换 app.py、monitor.py、storage.py 和 web 文件，再启动。不要让两个采集器写同一库。先在数据库副本上验证迁移，开启清理前确认保留范围。在线备份使用 SQLite Backup API，不直接只复制正在写入的 .db 文件。

systemd 参数可用 `systemctl edit ping-logger` 的 ExecStart 覆盖。unit 改动需操作者执行 daemon-reload 和重启；本次未执行。两个 unit 保留非 root 用户、最小 capability、异常自动重启、Caddy 安全配置，新增 journal 输出和 45 秒停止期限。SIGTERM/SIGINT 后完成当前写入并退出。新参数无须改 Caddy 配置。新前端兼容旧后端只读展示；为保护历史，检测到旧后端时拒绝删除操作。

## 故障排查

```bash
sudo journalctl -u ping-logger -u ping-logger-web -n 100 --no-pager
curl -sS http://127.0.0.1:18080/api/health
```

| 日志 / reason | 排查方向 |
|---|---|
| http_connection_timeout | HTTP 读写超时，例如浏览器空闲预连接，与 Ping 无响应无关 |
| http_client_disconnected | 浏览器断开 |
| timeout / unreachable | 目标无响应 / 远端不可达 |
| permission / no_route | 本地权限 / 路由 |
| command_missing | 缺少 iputils-ping |
| process_timeout / system_error | 进程超过期限 / 系统执行故障，不计网络丢包 |
| database_busy | 写锁或存储繁忙 |
| collection_cycle_failed | 保留 traceback，检查磁盘、库和异常原因 |
| collection_overrun | 周期、目标数和并发不匹配 |

旧日志 `INFO:root:Request timed out: TimeoutError('timed out')` 源于标准库 http.server.BaseHTTPRequestHandler.handle_one_request() 捕获 HTTP TimeoutError 后调用 log_error，旧项目将日志统一转为 INFO。新版独立分类并保留原因，不吞异常。普通 ICMP 丢包只记录 DEBUG 并写库，本机探测错误为 WARNING/ERROR。

## 测试

```bash
python3 -m unittest discover -s tests -v
node tests/test_frontend.js
bash -n install.sh
```

Node 只用于开发测试，不是运行依赖。测试涵盖 IPv4/IPv6 命令、分类、多包、并发、迁移、事务、清理、健康、HTTP 超时、图表复用、切换乱序和数据缺口。真实公网 ICMP 和证书签发仍受网络影响。

## 致谢与许可

感谢 [sslip.io](https://sslip.io/) 的 IP 域名 DNS、Caddy 自动 HTTPS 和 Chart.js。sslip.io 提供 DNS，不代理流量或签发证书。许可见 LICENSE / THIRD_PARTY_LICENSES.md。
