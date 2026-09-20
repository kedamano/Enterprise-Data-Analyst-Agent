"""自签证书生成脚本（trustme-based local PKI）。

生成：
- CA 证书（``certs/ca.crt``）
- Server 证书（SAN: localhost / 127.0.0. 1 / ::1） → ``certs/server.crt`` + ``certs/server.key``
- 可选 client 证书（``--client``） → ``certs/client.crt`` + ``certs/client.key`` + ``certs/client-ca.crt``

用法::

    python scripts/generate_certs.py            # 只生成 server 证书
    python scripts/generate_certs.py --client   # 同时生成 client 证书（mTLS）
    python scripts/generate_certs.py --force    # 覆盖已存在文件

依赖（可选）：``trustme``。未安装时自动退回 ``cryptography`` 内置方案，
保证在 ``trustme`` 也装不了的极简环境里能跑测试用例。
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

DEFAULT_DIR = Path("certs")


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------ trustme 首选
def _use_trustme(client: bool, out_dir: Path) -> bool:
    try:
        import trustme
    except ImportError:
        print("[generate_certs] trustme 不可用，退回 cryptography 方案。", file=sys.stderr)
        return False

    ca = trustme.CA()

    # Server 证书（含 SAN）
    server_cert = ca.issue_cert("localhost", "127.0.0.1", "::1")

    # 写 CA
    _ensure_dir(out_dir)
    (out_dir / "ca.crt").write_bytes(
        ca.cert_pem.bytes()
    )

    # 写 server（私钥 + 证书链）
    # trustme 0.9+ 用 private_key_pem / cert_chain_pems；兼容老版
    pk = server_cert.private_key_pem
    if hasattr(pk, "bytes"):
        (out_dir / "server.key").write_bytes(pk.bytes())
    else:
        (out_dir / "server.key").write_bytes(pk)

    # cert_chain_pems 可能是一或多个叶子 +  CA；server.crt 只放叶子
    chain = server_cert.cert_chain_pems
    if hasattr(chain[0], "bytes"):
        leaf_bytes = chain[0].bytes()
        ca_bytes = chain[-1].bytes() if len(chain) > 1 else ca.cert_pem.bytes()
    else:
        leaf_bytes = chain[0]
        ca_bytes = chain[-1] if len(chain) > 1 else ca.cert_pem.bytes()
    (out_dir / "server.crt").write_bytes(leaf_bytes + ca_bytes)

    # 可选 client 证书
    if client:
        client_ca = trustme.CA()
        c_cert = client_ca.issue_cert("client@local")
        (out_dir / "client-ca.crt").write_bytes(client_ca.cert_pem.bytes())
        if hasattr(c_cert.private_key_pem, "bytes"):
            (out_dir / "client.key").write_bytes(c_cert.private_key_pem.bytes())
        else:
            (out_dir / "client.key").write_bytes(c_cert.private_key_pem)
        if hasattr(c_cert.cert_chain_pems[0], "bytes"):
            (out_dir / "client.crt").write_bytes(
                c_cert.cert_chain_pems[0].bytes()
                + (c_cert.cert_chain_pems[-1].bytes() if len(c_cert.cert_chain_pems) > 1 else b"")
            )
        else:
            (out_dir / "client.crt").write_bytes(
                c_cert.cert_chain_pems[0]
                + (c_cert.cert_chain_pems[-1] if len(c_cert.cert_chain_pems) > 1 else b"")
            )

    print(
        f"[generate_certs] trustme OK → ca.crt / server.crt / server.key"
        f"{' / client.crt / client.key / client-ca.crt' if client else ''} @ {out_dir}"
    )
    return True


# ------------------------------------------------------------------ cryptography 兜底
def _use_cryptography(client: bool, out_dir: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    def _new_key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def _self_signed(cn: str, key, ca: bool = False, issuer=None, issuer_key=None):
        subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        builder = (
            x509.CertificateBuilder()
            .subject_name(subj)
            .issuer_name(issuer if issuer else subj)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
            .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
        )
        if ca:
            builder = builder.add_extension(
                x509.BasicConstraints(ca=True, path_length=0), critical=True
            )
        else:
            # SAN server / client
            if cn == "localhost":
                san = x509.SubjectAlternativeName([
                    x509.DNSName("localhost"),
                    x509.IPAddress(_ip("127.0.0.1")),
                    x509.IPAddress(_ip("::1")),
                ])
            else:
                san = x509.SubjectAlternativeName([x509.DNSName("client@local")])
            builder = builder.add_extension(san, critical=False)
            builder = builder.add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            builder = builder.add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_encipherment=True,
                    content_commitment=False, data_encipherment=False,
                    key_agreement=False, encipher_only=False, decipher_only=False,
                    crl_sign=False, key_cert_sign=False,
                ),
                critical=True,
            )
        sign_key = issuer_key if issuer_key else key
        return builder.sign(sign_key, hashes.SHA256())

    def _ip(addr):
        import ipaddress
        return ipaddress.ip_address(addr)

    _ensure_dir(out_dir)

    # CA
    ca_key = _new_key()
    ca_cert = _self_signed("Local Dev CA", ca_key, ca=True)
    (out_dir / "ca.crt").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))

    # Server
    s_key = _new_key()
    s_cert = _self_signed("localhost", s_key, issuer=ca_cert.subject, issuer_key=ca_key)
    (out_dir / "server.key").write_bytes(
        s_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    (out_dir / "server.crt").write_bytes(s_cert.public_bytes(serialization.Encoding.PEM))
    # 把 CA 也附加进去（方便客户端一条 chain 用）
    (out_dir / "server.crt").write_bytes(
        s_cert.public_bytes(serialization.Encoding.PEM)
        + ca_cert.public_bytes(serialization.Encoding.PEM)
    )

    if client:
        cca_key = _new_key()
        cca_cert = _self_signed("Local Dev Client CA", cca_key, ca=True)
        (out_dir / "client-ca.crt").write_bytes(cca_cert.public_bytes(serialization.Encoding.PEM))
        c_key = _new_key()
        c_cert = _self_signed("client@local", c_key, issuer=cca_cert.subject, issuer_key=cca_key)
        (out_dir / "client.key").write_bytes(
            c_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        (out_dir / "client.crt").write_bytes(
            c_cert.public_bytes(serialization.Encoding.PEM)
            + cca_cert.public_bytes(serialization.Encoding.PEM)
        )

    print(
        f"[generate_certs] cryptography OK → ca.crt / server.crt / server.key"
        f"{' / client.crt / client.key / client-ca.crt' if client else ''} @ {out_dir}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate self-signed certificates for local HTTPS/mTLS")
    parser.add_argument("--client", action="store_true", help="Also generate a client certificate (for mTLS)")
    parser.add_argument("--out-dir", default=str(DEFAULT_DIR), help="Output directory (default: certs/)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing certificates")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir != str(DEFAULT_DIR) else DEFAULT_DIR

    # 已存在则跳过（除非 --force）
    if not args.force and (out_dir / "ca.crt").exists() and (out_dir / "server.crt").exists():
        print(f"[generate_certs] {out_dir}/ 已存在，跳过（加 --force 重写）。")
        return

    if _use_trustme(args.client, out_dir):
        return
    _use_cryptography(args.client, out_dir)


if __name__ == "__main__":
    main()
