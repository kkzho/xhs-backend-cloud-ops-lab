# 案例 05 · HTTPS 证书过期，App 全量请求失败

> 分类：[B 网络与负载均衡](../01-knowledge-base/02-network.md) ｜ 等级：P0 ｜ 角色：一线值班

## 一、运维前

### 1.1 业务背景

`api.xiaohongshu-lab.com` 使用自签名/第三方证书，部署在 SLB 的 443 监听上。
证书由某位已离职同学申请，未纳入证书台账，也没有到期提醒。证书于 11 月 1 日到期。

### 1.2 触发诱因

- 证书到期自动失效（无续签、无提醒）；
- 客户端（App/curl）严格校验证书，握手直接失败；
- 服务端进程与端口完全正常，监控只看 CPU/端口，未监控证书有效期。

### 1.3 告警信息

| 时间 | 来源 | 内容 |
|------|------|------|
| 11-01 00:00 | 客户端 | TLS 握手失败：`certificate has expired` |
| 00:02 | App 监控 | 网络错误率 100%，P0 |
| 00:05 | 服务端 | 端口正常、CPU 正常（无业务告警） |
| 00:10 | 用户反馈 | App 完全无法使用 |

## 二、运维中

### 2.1 处理逻辑线

```text
入口全挂但服务端资源正常 → 优先怀疑 TLS/DNS
   ↓
openssl s_client 取证书有效期 → notAfter 已过期
   ↓
客户端视角复现（ssl 校验）→ certificate has expired
   ↓
紧急签发新证书 → 替换监听证书 → 重启/热加载
   ↓
用客户端校验证书链与域名（不能只用 -k）
   ↓
观察错误率恢复，补证书台账与到期告警
```

### 2.2 逐步拆解

**Step 1 · 服务端证书取证（1 分钟）**

```bash
openssl s_client -connect api.xiaohongshu-lab.com:443 -servername api.xiaohongshu-lab.com </dev/null \
  | openssl x509 -noout -subject -issuer -dates
# subject=CN = api.xiaohongshu-lab.com
# notBefore=Oct 16 2025
# notAfter=Oct 30 2026        ← 已过期（当前 2026-11-20）
```

**Step 2 · 客户端复现（1 分钟）**

```bash
curl -vI https://api.xiaohongshu-lab.com/healthz 2>&1 | grep -i -E "SSL|certificate|expire"
# SSL certificate problem: certificate has expired

python3 - <<'PY'
import ssl, socket
ctx = ssl.create_default_context()
with socket.create_connection(("127.0.0.1", 443), timeout=3) as s:
    ctx.wrap_socket(s, server_hostname="localhost")
PY
# ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired
```

> `curl -k` 能通不代表没问题，一定要做**真实校验**。

**Step 3 · 签发与替换（5 分钟）**

```bash
# 生产：在 CAS 申请/上传新证书，绑定到 SLB 443 监听
# 沙箱：cryptography 生成 90 天有效证书
openssl x509 -in /etc/nginx/ssl/server.crt -noout -dates
systemctl reload nginx        # 或 SLB 控制台更新证书
```

**Step 4 · 验证（2 分钟）**

```bash
echo | openssl s_client -connect 127.0.0.1:443 -CAfile /etc/nginx/ssl/server.crt 2>/dev/null \
  | openssl x509 -noout -dates
curl --cacert /etc/nginx/ssl/server.crt -s -o /dev/null -w "%{http_code}\n" https://localhost/healthz
# 200
```

同时用**多地域/多客户端**验证（部分节点可能未更新）。

### 2.3 关键取舍

| 选择 | 原因 | 副作用 |
|------|------|--------|
| 直接换新证书而非改客户端 | 客户端不可控（尤其 App 旧版本） | 需要重新签发，几分钟 |
| 保留旧证书文件备份 | 便于对比/回滚 | 注意不要回滚到过期证书 |
| 不采用 `-k`/关闭校验 | 只是掩盖问题，用户仍然不可用 | — |

## 三、运维后

### 3.1 复盘

- **直接原因**：证书到期；
- **根本原因**：无证书台账、无到期提醒、无续签流程；
- **做得好**：通过"资源正常但全站失败"快速定位到 TLS 层；
- **待改进**：监控缺失；证书分散在个人手里。

### 3.2 预防措施

| 措施 | 类型 | 验收标准 |
|------|------|---------|
| 证书统一托管（CAS/证书中心） | 架构 | 无节点私存证书 |
| 到期前 30/15/7 天告警 + owner | 监控 | 至少提前 7 天 |
| 续签自动化（ACME/云托管） | 自动化 | 到期前自动更新 |
| 证书巡检纳入日常 | 流程 | 每周报告 |
| 客户端校验纳入发布门禁 | 工程 | 冒烟含 `--cacert` 校验 |

### 3.3 自动化沉淀

脚本：[automation/cert_renew.py](../../automation/cert_renew.py)

```bash
python automation/cert_renew.py --case case-05-tls-cert --apply
```

逻辑：`openssl s_client` 读取有效期 + 客户端校验 → 临期/过期则签发 → 重启/热加载 → 用 CA 校验 → 更新台账状态。

## 四、演练

Notebook：[labs/case-05-tls-cert/lab.ipynb](../../labs/case-05-tls-cert/lab.ipynb)

沙箱生成**真实的过期证书**并由 HTTPS 服务加载，客户端会真实报 `certificate has expired`；
续签后同一客户端校验通过。

## 五、思考题

1. 证书链不完整会报什么错？如何用 `openssl` 判断？
2. 为什么"部分用户正常、部分用户失败"常见于证书更新？
3. 自签名证书在内部服务中如何做信任管理？
