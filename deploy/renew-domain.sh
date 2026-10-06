#!/bin/sh
set -eu
if [ "${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/uk.amberaccount.cn ]; then
    /usr/sbin/nginx -t
    /usr/bin/systemctl reload nginx
fi
