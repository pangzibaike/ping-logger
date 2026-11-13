# Ping Logger

在新买的 VPS 上一键部署，通过网页添加目标 IP，持续记录 **ICMP 往返延迟（RTT）**，用折线图查看线路表现与丢包率。

Python 标准库 + SQLite + 本地 Chart.js，无 pip、Node.js 或 Docker 运行依赖。Caddy 提供自动 HTTPS、证书续期及全站登录保护。

## 一键安装

支持全新的 **Debian 12/13、Ubuntu 22.04/24.04** 服务器（systemd）。需要 root、可直达的公网 IPv4 或 IPv6，以及入站 TCP **80、443**；云安全组和系统防火墙均需放行，安装器不会改动防火墙规则。出站需要 DNS、HTTPS 和目标 ICMP。

```bash
curl -fsSL https://raw.githubusercontent.com/pangzibaike/ping-logger/v1.0.0/install.sh -o /tmp/ping-logger-install.sh && sudo bash /tmp/ping-logger-install.sh
```

脚本从固定版本标签下载源码并先运行测试。安装完成会显示访问地址、用户名 `admin` 和随机密码。用浏览器打开地址，登录后添加目标，通常 60 秒内出现首次采样。关闭网页不影响后台采集。

默认自动检测出口公网 IP，生成类似 `https://ping-203-0-113-10.sslip.io` 的地址（示例 IP 不可实际部署）。多网卡、NAT 或自动检测错误时请手动指定可入站的公网地址：

```bash
sudo PUBLIC_IP=你的公网IP bash /tmp/ping-logger-install.sh
```

IPv6 会以短横线替换冒号生成 sslip.io 域名；客户端也需要 IPv6 连通性。公网 IP 改变后需要同步修改 `/etc/ping-logger/Caddyfile` 中的域名并重启网页服务。

**sslip.io 只提供 DNS，不提供证书或流量代理。** Caddy 向公共证书机构申请并续期证书。签发依赖公网连通性、DNS 与证书机构限额；脚本会验证 HTTPS，失败时输出诊断说明并保留服务继续重试，不会宣称签发成功。

## 功能与统计口径

- 网页添加、切换、删除目标，支持 IPv4 / IPv6，最多 20 个。
- 每个目标每 60 秒一个 ICMP 包，超时 2 秒；最多 4 个并发探测。
- 保留 7 天历史，可查看 1/6/24/72/168 小时。
- 平均延迟只计算成功样本。丢包率 = 丢包 /（成功 + 丢包）；本机执行错误另计。
- 本机没有路由、权限不足或 ping 无法执行等属于探测错误；超时及远端返回不可达属于失败探测。
- 采样中断不伪造为丢包，折线断开；时间轴按实际时间绘制。时间使用浏览器本地时区。
- 删除目标会同时删除该目标历史，网页操作前会提示确认。

Ping 测的是两个方向合计的往返耗时，**不能独立测量回程单向延迟，也不能识别具体回程路由**。填入家庭公网 IP 时，家庭路由器必须响应 ICMP；防火墙禁 Ping、ICMP 限速也可能表现为丢包。纯 IPv6 VPS 无法直接探测 IPv4 目标，反之亦然。

## 管理与排错

```bash
sudo systemctl status ping-logger ping-logger-web
sudo journalctl -u ping-logger -u ping-logger-web -n 100 --no-pager
sudo cat /etc/ping-logger/credentials
curl -fsS http://127.0.0.1:18080/api/health
```

健康检查：没有目标时健康；有目标而尚无采样或最新样本超过 180 秒时返回 503。探测错误表示采集器仍在运行，需要检查服务器的 ICMP 权限和网络；健康检查不代表目标可达。

路径：程序 `/opt/ping-logger`；数据库 `/var/lib/ping-logger/samples.db`；网关配置 `/etc/ping-logger/Caddyfile`；登录凭据 `/etc/ping-logger/credentials`（仅 root 可读）。Python 只监听 `127.0.0.1:18080`；公网请求必须经过 Caddy 的 HTTPS 和 Basic 登录。网页写入只接受 JSON，并检查浏览器 Origin。

忘记密码可用 `caddy hash-password` 交互生成新哈希，替换 Caddyfile 中 `admin` 后的哈希，再执行 `sudo systemctl restart ping-logger-web`。同步更新或删除本地 credentials 中旧密码；网页认证使用的是哈希。Basic 登录由浏览器记忆，退出请关闭相应浏览器会话。

安装器拒绝重复安装、已有 Caddy 和 80/443/18080 端口冲突，避免覆盖现有网站。已有反向代理时手动运行后端并将本项目 Caddyfile 的站点块合并进现有配置；**必须保留认证，不要将后端端口直接暴露公网**。

## 更新与回滚

首版安装器只负责首次安装；不自动更新或修改已有配置。后续版本手动更新：下载对应版本源码，运行测试，停止 `ping-logger`，备份 `/opt/ping-logger` 和 `/var/lib/ping-logger`（包含 SQLite 的 WAL/SHM 文件），替换程序与 web 文件，再启动并验证健康检查及网页。失败则停服务、恢复备份后重启。更新网关或 unit 前另备份配置并验证。不要在服务写入时仅复制 `samples.db`。

## 卸载

```bash
sudo systemctl disable --now ping-logger ping-logger-web
sudo rm -f /etc/systemd/system/ping-logger.service /etc/systemd/system/ping-logger-web.service
sudo systemctl daemon-reload
sudo rm -rf /opt/ping-logger
```

上述命令保留数据库、凭据和证书。确认不再需要后，可自行删除 `/var/lib/ping-logger` 和 `/etc/ping-logger`。Caddy 软件包及其证书存储默认保留。

## 开发与验证

```bash
python3 -m unittest discover -s tests -v
python3 app.py --db ./development.db --port 18080
```

本地开发访问 `http://127.0.0.1:18080`，开发后端自身不处理认证。探测命令使用 Linux iputils 参数，不支持 Windows 自带 ping。

## 致谢与许可

感谢 [sslip.io](https://sslip.io/) 提供便捷的 IP 域名 DNS 服务，让没有自有域名的服务器也能快速获得可解析的域名。感谢 [Caddy](https://caddyserver.com/docs/automatic-https) 的自动 HTTPS，以及 [Chart.js](https://www.chartjs.org/) 的图表组件。

本项目采用 MIT License。`web/chart.js` 为 Chart.js 4.5.1，采用 MIT License，版权归 Chart.js Contributors；完整许可见 `THIRD_PARTY_LICENSES.md`。
