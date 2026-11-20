# B · 网络与负载均衡

覆盖 SLB（七层/四层）、安全组、VPC 内网调用、TLS 证书。核心原则：
**入口问题先分层——DNS → 证书 → 监听 → 安全组 → 后端**。

---

## B1 · SLB 后端健康检查失败，接口 502

**业务现象**
App 与 OpenAPI 大量 502；SLB 控制台后端服务器健康状态 `abnormal`；同一接口直连 ECS 私网
有时正常；错误集中在某一台或全部后端。

**影响与定级**
全量 502 → P0；单后端异常 → P1。

**常见根因**
1. 应用确实不健康（依赖 DB/Redis 超时、线程池满）；
2. 健康检查路径/端口/协议配错（HTTPS 探 HTTP、路径 404）；
3. 应用只监听 `127.0.0.1`，SLB 从私网 IP 探测失败；
4. 后端响应慢于健康检查超时（默认 5s 内需返回）；
5. 安全组未放行 SLB 网段。

**首查命令**

```bash
curl -v http://127.0.0.1:8080/healthz          # 本机探活
curl -v http://10.10.1.11:8080/healthz         # 私网探活（从另一台机器）
ss -lntp | grep 8080                           # 监听地址是 0.0.0.0 还是 127.0.0.1
tail -n 100 /var/log/xhs/note-api.log | grep -E " 5[0-9]{2} "
```

云侧取证：

```bash
aliyun slb DescribeHealthStatus --LoadBalancerId lb-xxx
aliyun slb DescribeLoadBalancerAttribute --LoadBalancerId lb-xxx
```

**判断依据**
- 直连私网正常 + SLB 异常 → 探活配置/安全组；
- 直连也异常 → 应用或依赖故障；
- 所有后端同时异常 → 共同依赖（DB/Redis/证书）而非单机。

**处置要点**
1. 止血：摘除异常后端（保留容量评估），避免 502 扩大；
2. 修复后端（重启/修配置/扩容依赖）；
3. 健康检查至少连续 2 次通过再挂回；
4. 若是探活配置问题，改配置前先确认变更审批。

**预防与自动化**
- 健康检查使用独立 `/healthz`，不查 DB，超时 ≤ 2s；
- SLB 5xx 比率 + 后端健康状态双指标告警；
- 自动化：`automation/slb_recover.py`（取证 → 摘除 → 重启 → 等待健康 → 挂回）。

**关联案例**：[case-03](../02-cases/case-03-slb-502.md) · [Notebook](../../labs/case-03-slb-502/lab.ipynb)

---

## B2 · 安全组误删，VPC 内调用超时

**业务现象**
A 服务调用 B 服务突然超时（不是 connection refused）；同 VPC 内 `ping` 通、端口不通；
`tcpdump` 只见 SYN 重传、无 RST。

**影响与定级**
P1。超时比拒绝更隐蔽，容易误判为"对方服务卡死"。

**常见根因**
1. 安全组入方向规则被误删/收紧（常见于"安全加固"变更）；
2. 规则只放行了错误的源网段（如放了公网网段，实际走内网）；
3. 网络 ACL 与安全组同时限制；
4. 端口范围写错（8080/8081 写成 8080/80）。

**首查命令**

```bash
telnet 10.10.1.11 8080        # 超时 vs 拒绝，是两类问题
nc -zv -w 3 10.10.1.11 8080
tcpdump -i eth0 host 10.10.1.11 and port 8080 -nn
ss -lntp | grep 8080          # 对端是否在监听
```

云侧取证：

```bash
aliyun ecs DescribeSecurityGroups --SecurityGroupId sg-xxx
```

**判断依据**
- SYN 有去无回 → 大概率被安全组/ACL 丢弃；
- 收到 RST → 对端未监听（应用问题）；
- 变更时间与故障时间吻合 → 优先怀疑规则变更。

**处置要点**
1. 对比变更记录与当前规则，找缺失项；
2. 按**最小权限**补规则：只放行业务端口 + 调用方网段；
3. 补完立即复测，并观察 5 分钟；
4. 禁止用 `0.0.0.0/0` 全端口"先通了再说"。

**预防与自动化**
- 安全组纳入 IaC/基线巡检，变更需二次确认；
- 关键调用链配置黑盒探测（每 30s）；
- 自动化：`automation/sg_audit.py`（基线比对 → 生成最小权限变更 → 复测）。

**关联案例**：[case-04](../02-cases/case-04-sg-timeout.md) · [Notebook](../../labs/case-04-sg-timeout/lab.ipynb)

---

## B3 · HTTPS 证书过期，App 全量请求失败

**业务现象**
某一时刻起 App 全部网络请求失败，提示"网络异常"；服务端 CPU/内存正常、端口正常；
客户端日志出现 `certificate has expired`；浏览器/`curl` 报证书错误。

**影响与定级**
P0（全量不可用，且用户侧无法绕过）。

**常见根因**
1. 证书到期未续签（最常见）；
2. 证书链不完整（中间证书缺失）；
3. 域名与证书 SAN 不匹配（换域名未换证）；
4. 多张证书只更新了部分节点/部分 SLB 监听。

**首查命令**

```bash
openssl s_client -connect api.example.com:443 -servername api.example.com </dev/null \
  | openssl x509 -noout -subject -issuer -dates
curl -vI https://api.example.com/healthz
echo | openssl s_client -connect api.example.com:443 -showcerts 2>/dev/null | grep -c "BEGIN CERT"
```

云侧取证：

```bash
aliyun cas ListUserCertificateOrder
```

**判断依据**
- `notAfter` 已过 → 过期；
- 证书链只有 1 张 → 缺中间证书；
- 部分节点正常、部分异常 → 更新未覆盖全量。

**处置要点**
1. 确认影响面（全站/部分域名/部分节点）；
2. 紧急签发/上传新证书，先灰度一个监听，验证后再全量；
3. 验证必须用真实客户端校验（`curl --cacert` 或 `openssl verify`），不能只看 `-k`；
4. 证书更新后清理旧证书，避免回滚到过期证书。

**预防与自动化**
- 到期前 30/15/7 天多级告警，绑定 owner；
- 证书统一托管（阿里云 CAS/证书中心），禁止散落节点；
- 自动化：`automation/cert_renew.py`（读取有效期 → 续签 → 热加载 → 客户端校验）。

**关联案例**：[case-05](../02-cases/case-05-tls-cert.md) · [Notebook](../../labs/case-05-tls-cert/lab.ipynb)
