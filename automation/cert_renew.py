#!/usr/bin/env python3
"""TLS 证书有效期巡检与续签。

处置逻辑：
1. openssl 读取证书 notAfter，计算剩余天数；
2. 到期/临期时签发新证书（沙箱自签），重启 HTTPS 服务完成热加载；
3. 校验证书链与域名（curl --cacert 200）。
"""
from __future__ import annotations

import re
import ssl
import socket
import time

from _base import cli, done, log

from labs.common.sims.certs import make_cert


def _dates(lab) -> tuple[str, str]:
    _, out = lab.run(f"openssl s_client -connect 127.0.0.1:{lab.ports['api']} </dev/null 2>/dev/null "
                     "| openssl x509 -noout -dates")
    m = re.findall(r"notAfter=(.+)", out)
    return (out.strip(), m[0].strip() if m else "")


def _verify(lab) -> tuple[bool, str]:
    try:
        ctx = ssl.create_default_context(cafile=str(lab.dir / "tls/server.crt"))
        with socket.create_connection(("127.0.0.1", lab.ports["api"]), timeout=3) as s:
            with ctx.wrap_socket(s, server_hostname="localhost"):
                return True, "证书校验通过"
    except ssl.SSLCertVerificationError as e:
        return False, f"{e.verify_message or e.reason}"
    except OSError as e:
        return False, str(e)


def main() -> None:
    lab, apply = cli("TLS 证书巡检")
    dates, not_after = _dates(lab)
    ok, msg = _verify(lab)
    log("openssl s_client → " + dates.replace("\n", " | "))
    log(f"客户端校验：{msg}")

    if ok:
        log("证书有效，无需处置")
        done(True, "NOOP")

    if not apply:
        log("[dry-run] 计划：签发新证书 → 更新接入层证书 → 重启 HTTPS 服务 → 校验")
        done(True, "dry-run")

    cert, key = make_cert(lab.dir / "tls/server.crt", lab.dir / "tls/server.key", days=90)
    log(f"新证书已签发: {cert} notAfter={time.strftime('%Y-%m-%d')} + 90d")
    lab.restart_api(port=lab.ports["api"], tls=True)
    lab.state["certs"][0].update({"NotAfter": time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400 * 90)),
                                  "Status": "valid"})
    lab._save()

    ok2, msg2 = _verify(lab)
    log(f"续签后校验：{msg2}")
    done(ok2, "证书已续签并生效")


if __name__ == "__main__":
    main()
