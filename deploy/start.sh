#!/bin/sh
set -eu
umask 077

# 显式加入公网访问地址，避免自签证书遗漏云服务器的 NAT 公网 IP。
python - <<'PY'
import os
from pathlib import Path
from vat_automation.tls import ensure_certificate

host = os.environ.get("VAT_PUBLIC_HOST", "").strip()
if not host:
    raise SystemExit("请设置 VAT_PUBLIC_HOST 为本项目的访问 IP 或域名")
ensure_certificate(Path("certs"), hosts=[host])
PY

exec xvfb-run -a -s '-screen 0 1440x1000x24 -nolisten tcp' \
    vat-web --config vat-config.flow.json --eori-config vat-config.eori.flow.json \
    --host 0.0.0.0 --port 8765 --no-open \
    --ssl-certfile certs/server.crt --ssl-keyfile certs/server.key
