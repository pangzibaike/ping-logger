#!/usr/bin/env bash
set -Eeuo pipefail
umask 027
REPO=pangzibaike/ping-logger
PING_LOGGER_RELEASE_VERSION=${PING_LOGGER_VERSION:-v1.1.1}
PUBLIC_IP=${PUBLIC_IP:-}
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
fail() { echo "错误：$*" >&2; exit 1; }
[[ $EUID == 0 ]] || fail '请使用 sudo bash install.sh'
[[ -d /run/systemd/system ]] || fail '需要 systemd'
[[ $PING_LOGGER_RELEASE_VERSION =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail '版本号格式不正确'
source /etc/os-release
[[ $ID == debian || $ID == ubuntu ]] || fail '目前仅支持 Debian 12/13、Ubuntu 22.04/24.04（systemd）'
command -v apt-get >/dev/null || fail '需要 apt-get'
if [[ -e /etc/ping-logger/Caddyfile ]]; then
  fail '检测到已有安装。请保留配置和数据库，按 README 的更新流程操作。'
fi
if command -v caddy >/dev/null; then fail '已有 Caddy，请按 README 手动集成，安装器不会覆盖'; fi
if command -v ss >/dev/null && ss -H -ltn | awk '{print $4}' | grep -Eq ':(80|443|18080)$'; then
  fail '80、443 或 18080 已被占用，请先解决端口冲突'
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 iputils-ping curl ca-certificates iproute2 openssl
if [[ -z $PUBLIC_IP ]]; then
  PUBLIC_IP=$(curl -4fsS --connect-timeout 5 --max-time 15 https://api.ipify.org || true)
  if [[ -z $PUBLIC_IP ]]; then PUBLIC_IP=$(curl -6fsS --connect-timeout 5 --max-time 15 https://api64.ipify.org || true); fi
fi
DOMAIN=$(python3 - "$PUBLIC_IP" <<'PY'
import ipaddress,sys
try:
    ip=ipaddress.ip_address(sys.argv[1])
    if not ip.is_global: raise ValueError('需要公网 IP')
except ValueError:
    sys.exit('无法获取公网地址，请设置 PUBLIC_IP=你的服务器公网IP 后重试')
print('ping-'+str(ip).replace('.', '-').replace(':', '-')+'.sslip.io')
PY
) || fail '公网 IP 无效'
python3 - "$DOMAIN" "$PUBLIC_IP" <<'PY'
import socket,ipaddress,sys
addresses={ipaddress.ip_address(r[4][0]) for r in socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)}
if ipaddress.ip_address(sys.argv[2]) not in addresses: sys.exit('sslip.io DNS 未解析到公网 IP，请检查 DNS 后重试')
PY
if [[ -n ${PING_LOGGER_SOURCE_DIR:-} ]]; then
  SOURCE=$(realpath -- "$PING_LOGGER_SOURCE_DIR")
  for item in app.py monitor.py storage.py tests web ping-logger.service ping-logger-web.service; do
    [[ -e $SOURCE/$item ]] || fail "本地源码缺少 $item"
    cp -r -- "$SOURCE/$item" "$TMP/"
  done
else
  curl -fLsS --retry 3 "https://github.com/$REPO/archive/refs/tags/$PING_LOGGER_RELEASE_VERSION.tar.gz" -o "$TMP/source.tar.gz"
  tar -xzf "$TMP/source.tar.gz" --strip-components=1 -C "$TMP"
fi
python3 -m py_compile "$TMP/app.py"
python3 -m unittest discover -s "$TMP/tests" -v
# The distribution's Caddy package supplies the binary and caddy user.
# Its default site is stopped; our dedicated unit uses a separate configuration.
apt-get install -y caddy
systemctl disable --now caddy
id ping-logger >/dev/null 2>&1 || useradd --system --home-dir /var/lib/ping-logger --shell /usr/sbin/nologin ping-logger
install -d -m 755 /opt/ping-logger /opt/ping-logger/web
install -d -m 750 -o ping-logger -g ping-logger /var/lib/ping-logger
install -d -m 750 -o root -g caddy /etc/ping-logger
install -m 644 "$TMP/app.py" /opt/ping-logger/app.py
install -m 644 "$TMP/monitor.py" "$TMP/storage.py" /opt/ping-logger/
install -m 644 "$TMP"/web/* /opt/ping-logger/web/
install -m 644 "$TMP/ping-logger.service" /etc/systemd/system/
PASSWORD=$(openssl rand -hex 16)
HASH=$(printf '%s\n' "$PASSWORD" | caddy hash-password)
cat > /etc/ping-logger/Caddyfile <<EOF
{
    admin off
}
$DOMAIN {
    basicauth {
        admin $HASH
    }
    encode gzip
    reverse_proxy 127.0.0.1:18080
}
EOF
chown root:caddy /etc/ping-logger/Caddyfile
chmod 640 /etc/ping-logger/Caddyfile
install -m 644 "$TMP/ping-logger-web.service" /etc/systemd/system/
caddy validate --config /etc/ping-logger/Caddyfile --adapter caddyfile
printf 'URL=https://%s\nUSER=admin\nPASSWORD=%s\nVERSION=%s\n' "$DOMAIN" "$PASSWORD" "$PING_LOGGER_RELEASE_VERSION" > /etc/ping-logger/credentials
chmod 600 /etc/ping-logger/credentials
systemctl daemon-reload
systemctl enable --now ping-logger ping-logger-web
for i in {1..10}; do
  if curl -fsS http://127.0.0.1:18080/api/health >/dev/null; then break; fi
  sleep 1
done
curl -fsS http://127.0.0.1:18080/api/health >/dev/null || fail '后端未启动，查看 journalctl -u ping-logger'
echo "后端就绪，正在检查公网 HTTPS（请确保云安全组及防火墙允许 TCP 80/443）……"
if curl -fsS --retry 8 --retry-delay 5 --retry-all-errors --connect-timeout 5 --max-time 10 \
    --user "admin:$PASSWORD" "https://$DOMAIN/api/health" >/dev/null; then
  echo 'HTTPS 验证成功。'
else
  echo 'HTTPS 尚未就绪。服务已保留，Caddy 会继续尝试签发；检查 DNS、80/443、CA 限流及 journalctl -u ping-logger-web。' >&2
  echo '修复后无需重装；用浏览器访问下面的地址验证。' >&2
fi
printf '\n访问：https://%s\n用户名：admin\n密码：%s\n凭据：/etc/ping-logger/credentials（仅 root 可读）\n' "$DOMAIN" "$PASSWORD"
