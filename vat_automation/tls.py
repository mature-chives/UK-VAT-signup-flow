from __future__ import annotations

import ipaddress
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path


# Safari / iOS 会拒绝有效期超过 398 天的 TLS 证书，取 397 天保证各平台都接受。
CERT_VALID_DAYS = 397
CERT_NAME = "server.crt"
KEY_NAME = "server.key"
MANUAL_HINT = (
    "未找到 openssl 命令，无法自动生成自签证书。请安装 openssl 后重试，"
    "或自行准备证书并用 --ssl-certfile / --ssl-keyfile 指定。"
)


def detect_lan_ip() -> str | None:
    """探测本机在局域网中的出口地址。UDP connect 只做路由选择，不发包。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 53))
        return str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()


def local_candidates() -> tuple[set[str], set[str]]:
    """收集本机可能被同事访问的 (IP 集合, 主机名集合)。

    默认路由地址在开 VPN 时会是隧道地址，因此同时并入主机名解析出的
    全部本机 IPv4，避免证书 SAN 缺少真实局域网 IP 导致 Chrome 拒绝。
    """
    addresses: set[str] = set()
    names: set[str] = set()
    lan = detect_lan_ip()
    if lan:
        addresses.add(lan)
    hostname = socket.gethostname().strip().rstrip(".")
    if hostname:
        names.add(hostname.casefold())
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
        except OSError:
            infos = []
        for info in infos:
            addresses.add(str(info[4][0]))
    return addresses, names


def preferred_lan_ip() -> str | None:
    """返回最适合展示给同事的访问地址，优先私网网段而不是 VPN 隧道地址。"""
    addresses, _ = local_candidates()
    private = sorted(
        address
        for address in addresses
        if address != "127.0.0.1" and ipaddress.ip_address(address).is_private
    )
    if private:
        return private[0]
    others = sorted(address for address in addresses if address != "127.0.0.1")
    return others[0] if others else None


def subject_alt_names(hosts: Iterable[str] = ()) -> tuple[list[str], list[str]]:
    """返回写入证书 SAN 的 (IP 列表, 域名列表)。

    Chrome 对不含 SAN 的证书直接拒绝且不提供“继续访问”，因此按 IP 访问时
    必须把局域网 IP 写进去。
    """
    addresses = {"127.0.0.1"}
    names = {"localhost"}
    detected_addresses, detected_names = local_candidates()
    addresses |= detected_addresses
    names |= detected_names
    for host in hosts:
        candidate = (host or "").strip()
        # 0.0.0.0 是监听通配符，不是可访问地址，写进 SAN 没有意义。
        if not candidate or candidate == "0.0.0.0":
            continue
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            names.add(candidate)
        else:
            addresses.add(candidate)
    return sorted(addresses), sorted(names)


def _openssl_config(addresses: list[str], names: list[str]) -> str:
    """生成 openssl 配置。

    用配置文件而不是 -addext：macOS 自带的是 LibreSSL，对 -addext 的支持
    不可靠，而 -config 在 OpenSSL 和 LibreSSL 上行为一致。
    """
    alt = [f"IP.{index} = {value}" for index, value in enumerate(addresses, 1)]
    alt += [f"DNS.{index} = {value}" for index, value in enumerate(names, 1)]
    return "\n".join(
        [
            "[req]",
            "distinguished_name = dn",
            "x509_extensions = ext",
            "prompt = no",
            "",
            "[dn]",
            "CN = UK VAT Automation",
            "",
            "[ext]",
            "basicConstraints = critical, CA:FALSE",
            "keyUsage = critical, digitalSignature, keyEncipherment",
            "extendedKeyUsage = serverAuth",
            "subjectAltName = @alt",
            "",
            "[alt]",
            *alt,
            "",
        ]
    )


def ensure_certificate(
    directory: Path, *, hosts: Iterable[str] = ()
) -> tuple[Path, Path]:
    """返回 (证书, 私钥) 路径，缺失时生成自签证书。"""
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    certificate = directory / CERT_NAME
    key = directory / KEY_NAME
    if certificate.is_file() and key.is_file():
        return certificate, key

    openssl = shutil.which("openssl")
    if openssl is None:
        raise RuntimeError(MANUAL_HINT)

    addresses, names = subject_alt_names(hosts)
    with tempfile.TemporaryDirectory(prefix="uk-vat-tls-") as workspace:
        config = Path(workspace) / "openssl.cnf"
        config.write_text(_openssl_config(addresses, names), encoding="utf-8")
        completed = subprocess.run(
            [
                openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(certificate),
                "-days", str(CERT_VALID_DAYS), "-config", str(config),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    if completed.returncode != 0 or not certificate.is_file() or not key.is_file():
        raise RuntimeError(
            "自签证书生成失败："
            + (completed.stderr.strip() or f"openssl 返回 {completed.returncode}")
        )
    key.chmod(0o600)
    certificate.chmod(0o644)
    return certificate, key


def describe(certificate: Path) -> str:
    """返回证书的 SAN 摘要，用于启动时打印给用户确认。"""
    openssl = shutil.which("openssl")
    if openssl is None:
        return ""
    completed = subprocess.run(
        [openssl, "x509", "-in", str(certificate), "-noout", "-text"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    lines = completed.stdout.splitlines()
    for index, line in enumerate(lines):
        if "Subject Alternative Name" in line and index + 1 < len(lines):
            return lines[index + 1].strip()
    return ""
