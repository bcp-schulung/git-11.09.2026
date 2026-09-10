#!/usr/bin/env python3
"""
Container Seminar — Hetzner + Cloudflare Provisioner
=====================================================

Provisions 1 VM per student on Hetzner Cloud, then on each VM:
  - creates a 'student' user with passwordless sudo
  - installs code-server (browser IDE) behind Caddy with Let's Encrypt TLS
  - installs Docker Engine + Docker Compose v2 plugin
  - installs kubectl (latest stable)
  - installs Helm
  - adds 'student' to the docker group

Prerequisites:
    pip install hcloud cloudflare fabric python-dotenv paramiko
    Copy .env.example -> .env and fill in credentials.

Usage:
    python provision.py
"""

from __future__ import annotations

import io
import json
import os
import secrets
import string
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cloudflare as cf_module
from dotenv import load_dotenv
from fabric import Connection
from hcloud import Client as HCloudClient
from hcloud.firewalls.domain import FirewallResource, FirewallRule
from hcloud.images.domain import Image
from hcloud.locations.domain import Location
from hcloud.server_types.domain import ServerType
from hcloud.servers.domain import ServerCreatePublicNetwork
import paramiko

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

SERVER_TYPE      = "cx23"                        # 2 vCPU / 4 GB RAM (Intel)
IMAGE_NAME       = "ubuntu-24.04"
LOCATION_NAME    = "hel1"                        # Helsinki, Finland
DOMAIN_SUFFIX    = "cicd.it-scholar.com"
HETZNER_KEY_NAME = "container-seminar-provisioner"
FW_NAME          = "container-seminar-fw"
PASSWORDS_FILE   = Path(__file__).parent / ".passwords.json"

# ── Harbor registry — one shared instance for the instructor + all students ──
HARBOR_SLUG         = "harbor"
HARBOR_SERVER_TYPE  = "cx33"                      # 4 vCPU / 8 GB RAM (Intel) — Harbor's recommended minimum
HARBOR_HOSTNAME     = f"{HARBOR_SLUG}.{DOMAIN_SUFFIX}"
HARBOR_HTTP_PORT    = 8081                        # Harbor's internal proxy port; Caddy terminates TLS in front of it
HARBOR_PROJECT      = "seminar"
HARBOR_ROBOT_NAME   = "students"                  # full robot account name becomes robot$students
HARBOR_SECRETS_FILE = Path(__file__).parent / ".harbor-passwords.json"

# slug   : used for the Hetzner VM name and DNS subdomain (ASCII, no umlauts)
# display: shown in the summary / credentials file
STUDENTS = [
    {"slug": "ben-coeppicus",          "display": "Ben Cöppicus"},
    {"slug": "thorsten-breug",        "display": "Thorsten Breug"},
    {"slug": "tanja-grimm",           "display": "Tanja Grimm"},
    {"slug": "reinhold-guenter",      "display": "Reinhold Günter"},
    {"slug": "akin-ilhan",            "display": "Akin Ilhan"},
    {"slug": "stephan-kuhr",          "display": "Stephan Kuhr"},
    {"slug": "reiner-maurer",         "display": "Reiner Maurer"},
    {"slug": "ronnie-saliger",        "display": "Ronnie Saliger"},
    {"slug": "roland-strohschneider", "display": "Roland Strohschneider"},
]

# ─────────────────────────────────────────────────────────────────────────────
# Systemd service for code-server (runs as the student user)
# ─────────────────────────────────────────────────────────────────────────────

CODESERVER_SERVICE = """\
[Unit]
Description=code-server IDE (student)
After=network.target

[Service]
Type=simple
User=student
Group=student
WorkingDirectory=/home/student
ExecStart=/usr/bin/code-server --config /home/student/.config/code-server/config.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

# ─────────────────────────────────────────────────────────────────────────────
# Credentials — loaded from .env
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        sys.exit(f"ERROR: Missing required environment variable '{key}'. Check .env.")
    return val


HCLOUD_TOKEN    = _require("HCLOUD_TOKEN")
CF_API_TOKEN    = _require("CF_API_TOKEN")
CF_ZONE_ID      = _require("CF_ZONE_ID")
SSH_PRIVATE_KEY = os.path.expanduser(_require("SSH_PRIVATE_KEY_PATH"))
SSH_PUBLIC_KEY  = os.path.expanduser(_require("SSH_PUBLIC_KEY_PATH"))

hc = HCloudClient(token=HCLOUD_TOKEN)
cf = cf_module.Cloudflare(api_token=CF_API_TOKEN)

# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def gen_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def load_or_generate_passwords() -> dict[str, str]:
    """Load persisted passwords from disk; generate and save if absent."""
    slugs = [s["slug"] for s in STUDENTS]
    if PASSWORDS_FILE.exists():
        data = json.loads(PASSWORDS_FILE.read_text())
        # Generate only for new students not already in the file
        updated = False
        for slug in slugs:
            if slug not in data:
                data[slug] = gen_password()
                updated = True
        if updated:
            PASSWORDS_FILE.write_text(json.dumps(data, indent=2))
        log(f"Loaded passwords from {PASSWORDS_FILE}")
        return {slug: data[slug] for slug in slugs}
    passwords = {slug: gen_password() for slug in slugs}
    PASSWORDS_FILE.write_text(json.dumps(passwords, indent=2))
    log(f"Generated and saved passwords to {PASSWORDS_FILE}")
    return passwords


def load_or_generate_harbor_secrets() -> dict[str, str]:
    """Load persisted Harbor admin/DB passwords from disk; generate once and reuse.

    Re-running the provisioner must NOT regenerate these, otherwise the admin
    login used in harbor.yml on first boot would no longer match what's on disk.
    """
    if HARBOR_SECRETS_FILE.exists():
        log(f"Loaded Harbor secrets from {HARBOR_SECRETS_FILE}")
        return json.loads(HARBOR_SECRETS_FILE.read_text())
    secrets_data = {
        "admin_password": gen_password(),
        "db_password": gen_password(),
    }
    HARBOR_SECRETS_FILE.write_text(json.dumps(secrets_data, indent=2))
    log(f"Generated and saved Harbor secrets to {HARBOR_SECRETS_FILE}")
    return secrets_data


def connection(ip: str, user: str = "root") -> Connection:
    """Return a Fabric Connection for the given IP, forced over IPv4."""
    import socket as _socket
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.settimeout(90)
    sock.connect((ip, 22))
    conn = Connection(
        host=ip,
        user=user,
        connect_kwargs={
            "key_filename": SSH_PRIVATE_KEY,
            "timeout": 90,
            "look_for_keys": False,
            "allow_agent": False,
            "sock": sock,
        },
    )
    conn.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    return conn


def wait_for_ssh(ip: str, timeout: int = 600) -> None:
    """Block until port 22 on *ip* accepts TCP connections."""
    import subprocess
    log(f"  Waiting for SSH on {ip} ...")
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        try:
            r = subprocess.run(["nc", "-zw3", ip, "22"], capture_output=True, timeout=10)
            if r.returncode == 0:
                log(f"  SSH ready: {ip}")
                return
            last_err = f"nc exited {r.returncode}: {r.stderr.decode().strip()}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(3)
    raise TimeoutError(
        f"SSH never became available on {ip} after {timeout}s (last: {last_err})"
    )


def put_text(c: Connection, content: str, remote_path: str) -> None:
    """Upload a string as a remote file via SFTP (avoids shell-quoting issues)."""
    c.put(io.BytesIO(content.encode("utf-8")), remote_path)


def upsert_dns_record(hostname: str, ip: str) -> None:
    """Create/update a single non-proxied (grey-cloud) Cloudflare A record."""
    all_records = list(cf.dns.records.list(zone_id=CF_ZONE_ID))
    existing = [
        r for r in all_records
        if getattr(r, "name", "") == hostname and getattr(r, "type", "") == "A"
    ]
    if existing and getattr(existing[0], "content", "") == ip:
        log(f"  DNS {hostname} -> {ip} already correct, skipping.")
        return
    if existing:
        log(f"  Updating DNS {hostname} -> {ip} ...")
        cf.dns.records.update(
            dns_record_id=existing[0].id, zone_id=CF_ZONE_ID,
            name=hostname, type="A", content=ip, proxied=False, ttl=300,
        )
    else:
        log(f"  Creating DNS {hostname} -> {ip} ...")
        cf.dns.records.create(
            zone_id=CF_ZONE_ID, name=hostname, type="A",
            content=ip, proxied=False, ttl=300,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — Hetzner Infrastructure
# ─────────────────────────────────────────────────────────────────────────────


def ensure_hetzner_ssh_key() -> object:
    """Upload provisioner public key to Hetzner; reuse if already present."""
    existing = hc.ssh_keys.get_by_name(HETZNER_KEY_NAME)
    if existing:
        log(f"Hetzner SSH key '{HETZNER_KEY_NAME}' already exists, reusing.")
        return existing
    pub = Path(SSH_PUBLIC_KEY).read_text().strip()
    log(f"Uploading SSH key '{HETZNER_KEY_NAME}' to Hetzner ...")
    return hc.ssh_keys.create(name=HETZNER_KEY_NAME, public_key=pub)


def ensure_firewall() -> object:
    """Create a firewall allowing SSH + HTTP + HTTPS; reuse if present."""
    existing = hc.firewalls.get_by_name(FW_NAME)
    if existing:
        log(f"Firewall '{FW_NAME}' already exists, reusing.")
        return existing
    log(f"Creating firewall '{FW_NAME}' ...")
    rules = [
        FirewallRule(
            direction="in",
            protocol="tcp",
            port=p,
            source_ips=["0.0.0.0/0", "::/0"],
            description=f"Allow inbound TCP {p}",
        )
        for p in ["22", "80", "443"]
    ]
    result = hc.firewalls.create(name=FW_NAME, rules=rules)
    return result.firewall


def create_vm(name: str, ssh_key: object, server_type: str = SERVER_TYPE) -> tuple[str, str]:
    """Create a single VM; return (name, public_ipv4)."""
    existing = hc.servers.get_by_name(name)
    if existing:
        ip = existing.public_net.ipv4.ip
        log(f"  VM '{name}' already exists (IP: {ip}), skipping.")
        return name, ip
    log(f"  Creating VM '{name}' ({server_type}, {IMAGE_NAME}, {LOCATION_NAME}) ...")
    response = hc.servers.create(
        name=name,
        server_type=ServerType(name=server_type),
        image=Image(name=IMAGE_NAME),
        location=Location(name=LOCATION_NAME),
        ssh_keys=[ssh_key],
        public_net=ServerCreatePublicNetwork(enable_ipv4=True, enable_ipv6=True),
    )
    # Poll until the server exists and is running (Hetzner action API times out early)
    deadline = time.time() + 600
    server = None
    while time.time() < deadline:
        server = hc.servers.get_by_name(name)
        if server and server.status == "running":
            break
        time.sleep(5)
    if not server:
        raise RuntimeError(f"Server '{name}' never appeared in Hetzner API after 600s")
    ip = server.public_net.ipv4.ip
    log(f"  VM '{name}' created (IP: {ip})")
    return name, ip


def provision_hetzner() -> dict[str, str]:
    """Phase 1: SSH key, firewall, and all VMs. Returns {slug: ip}."""
    log("=== Phase 1: Hetzner Infrastructure ===")
    ssh_key  = ensure_hetzner_ssh_key()
    ensure_firewall()

    vm_ips: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(STUDENTS)) as pool:
        futures = {
            pool.submit(create_vm, s["slug"], ssh_key): s["slug"]
            for s in STUDENTS
        }
        for f in as_completed(futures):
            name, ip = f.result()
            vm_ips[name] = ip

    # Apply firewall to all servers
    fw = hc.firewalls.get_by_name(FW_NAME)
    for slug in vm_ips:
        server = hc.servers.get_by_name(slug)
        if server:
            try:
                hc.firewalls.apply_to_resources(
                    firewall=fw,
                    resources=[
                        FirewallResource(
                            type=FirewallResource.TYPE_SERVER,
                            server=server,
                        )
                    ],
                )
                log(f"  Firewall applied to '{slug}'.")
            except Exception as e:
                if "firewall_already_applied" in str(e):
                    log(f"  Firewall already applied to '{slug}', skipping.")
                else:
                    log(f"  Warning: could not apply firewall to '{slug}': {e}")

    log("Phase 1 complete.\n")
    return vm_ips


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Cloudflare DNS
# ─────────────────────────────────────────────────────────────────────────────


def setup_dns(vm_ips: dict[str, str]) -> None:
    """Phase 2: create/update A records for every student VM."""
    log("=== Phase 2: Cloudflare DNS ===")
    for student in STUDENTS:
        upsert_dns_record(f"{student['slug']}.{DOMAIN_SUFFIX}", vm_ips[student["slug"]])
    log("Phase 2 complete.\n")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — Base OS Configuration (all VMs)
# ─────────────────────────────────────────────────────────────────────────────


def configure_base_vm(name: str, ip: str) -> None:
    """
    On a single VM:
      - upgrade packages, install essentials
      - create 'student' user with passwordless sudo
      - place provisioner's public key in student's authorized_keys
    """
    pub_key      = Path(SSH_PUBLIC_KEY).read_text().strip()
    sudoers_line = "student ALL=(ALL) NOPASSWD:ALL\n"

    log(f"  [{name}] Configuring base OS ...")
    for attempt in range(1, 6):
        try:
            with connection(ip) as c:
                # Wait for any cloud-init / unattended-upgrades apt lock to clear
                c.run(
                    "systemctl stop unattended-upgrades 2>/dev/null || true && "
                    "flock --timeout 120 /var/lib/dpkg/lock-frontend true 2>/dev/null || true",
                    hide=True,
                )
                # Remove any stale/broken third-party apt sources from a previous
                # partial run so that apt-get update doesn't fail on them.
                c.run(
                    "rm -f /etc/apt/sources.list.d/helm-stable-debian.list "
                    "/usr/share/keyrings/helm.gpg",
                    hide=True,
                )
                c.run(
                    "apt-get update -qq && "
                    "NEEDRESTART_SUSPEND=1 DEBIAN_FRONTEND=noninteractive "
                    "apt-get upgrade -y -qq",
                    hide=True,
                )
                c.run(
                    "NEEDRESTART_SUSPEND=1 DEBIAN_FRONTEND=noninteractive "
                    "apt-get install -y -qq "
                    "curl git vim net-tools gnupg lsb-release "
                    "apt-transport-https ca-certificates",
                    hide=True,
                )
                # Create student user (idempotent)
                c.run("id student &>/dev/null || useradd -m -s /bin/bash student", hide=True)

                # Passwordless sudo
                put_text(c, sudoers_line, "/etc/sudoers.d/student")
                c.run("chmod 440 /etc/sudoers.d/student", hide=True)

                # SSH authorised key for student
                c.run(
                    "install -d -m 700 -o student -g student /home/student/.ssh",
                    hide=True,
                )
                put_text(c, pub_key + "\n", "/home/student/.ssh/authorized_keys")
                c.run(
                    "chmod 600 /home/student/.ssh/authorized_keys && "
                    "chown student:student /home/student/.ssh/authorized_keys",
                    hide=True,
                )
            log(f"  [{name}] Base configuration done.")
            return
        except Exception as e:
            log(f"  [{name}] Attempt {attempt}/5 failed: {type(e).__name__}: {e}")
            if attempt < 5:
                time.sleep(15)
    raise RuntimeError(f"[{name}] All 5 base-configuration attempts failed")


def configure_all_base(vm_ips: dict[str, str]) -> None:
    """Phase 3: wait for SSH, then configure base OS in parallel."""
    log("=== Phase 3: Base OS Configuration ===")
    for ip in vm_ips.values():
        wait_for_ssh(ip)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(configure_base_vm, name, ip): name
            for name, ip in vm_ips.items()
        }
        for f in as_completed(futures):
            f.result()
    log("Phase 3 complete.\n")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 — code-server + Caddy + Docker + kubectl + Helm
# ─────────────────────────────────────────────────────────────────────────────

_CADDY_INSTALL = """\
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy
"""

_DOCKER_INSTALL = """\
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    | tee /etc/apt/sources.list.d/docker.list >/dev/null
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
systemctl enable docker
usermod -aG docker student
"""

_KUBECTL_INSTALL = """\
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.31/deb/Release.key \
    | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
chmod a+r /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] \
https://pkgs.k8s.io/core:/stable:/v1.31/deb/ /' \
    | tee /etc/apt/sources.list.d/kubernetes.list >/dev/null
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq kubectl
"""

_HELM_INSTALL = """\
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
"""

_TRIVY_INSTALL = """\
curl -fsSL https://aquasecurity.github.io/trivy-repo/deb/public.key \
    | gpg --dearmor -o /usr/share/keyrings/trivy.gpg
chmod a+r /usr/share/keyrings/trivy.gpg
echo 'deb [signed-by=/usr/share/keyrings/trivy.gpg] \
https://aquasecurity.github.io/trivy-repo/deb generic main' \
    | tee /etc/apt/sources.list.d/trivy.list >/dev/null
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq trivy
"""


def configure_vm(ip: str, slug: str, password: str) -> None:
    """
    Install and configure the full toolstack on one VM:
      code-server, Caddy, Docker Engine, Docker Compose v2, kubectl, Helm.
    """
    fqdn = f"{slug}.{DOMAIN_SUFFIX}"

    cs_config = (
        "bind-addr: 127.0.0.1:8080\n"
        "auth: password\n"
        f"password: {password}\n"
        "cert: false\n"
    )

    caddyfile = (
        f"{fqdn} {{\n"
        f"    reverse_proxy localhost:8080\n"
        f"    log {{\n"
        f"        output file /var/log/caddy/access.log\n"
        f"    }}\n"
        f"}}\n"
    )

    with connection(ip) as c:
        log(f"  [{slug}] Installing code-server ...")
        c.run("curl -fsSL https://code-server.dev/install.sh | sh", hide=True)

        log(f"  [{slug}] Configuring code-server ...")
        # Pass both dirs explicitly: install -d only chowns dirs named as
        # arguments, not implicitly-created parents (leaves ~/.config
        # root-owned, blocking student-run tools like Helm from writing
        # their own config dirs, e.g. ~/.config/helm, under it).
        c.run(
            "install -d -m 755 -o student -g student "
            "/home/student/.config /home/student/.config/code-server",
            hide=True,
        )
        put_text(c, cs_config, "/home/student/.config/code-server/config.yaml")
        c.run(
            "chown student:student /home/student/.config/code-server/config.yaml",
            hide=True,
        )
        put_text(c, CODESERVER_SERVICE, "/etc/systemd/system/code-server.service")
        c.run(
            "systemctl daemon-reload && systemctl enable code-server && "
            "systemctl restart code-server",
            hide=True,
        )

        log(f"  [{slug}] Installing Caddy ...")
        c.run(_CADDY_INSTALL, hide=True)
        c.run("mkdir -p /var/log/caddy", hide=True)
        put_text(c, caddyfile, "/etc/caddy/Caddyfile")
        c.run("systemctl enable caddy && systemctl restart caddy", hide=True)

        log(f"  [{slug}] Installing Docker Engine + Compose v2 ...")
        c.run(_DOCKER_INSTALL, hide=True)

        log(f"  [{slug}] Installing kubectl ...")
        c.run(_KUBECTL_INSTALL, hide=True)

        log(f"  [{slug}] Installing Helm ...")
        c.run(_HELM_INSTALL, hide=True)

        log(f"  [{slug}] Installing Trivy ...")
        c.run(_TRIVY_INSTALL, hide=True)

    log(f"  [{slug}] VM setup complete.")


def configure_all_vms(vm_ips: dict[str, str]) -> dict[str, str]:
    """Phase 4: configure all VMs in parallel. Returns {slug: password}."""
    log("=== Phase 4: code-server + Caddy + Docker + kubectl + Helm ===")

    passwords = load_or_generate_passwords()

    with ThreadPoolExecutor(max_workers=len(STUDENTS)) as pool:
        futures = {
            pool.submit(
                configure_vm,
                vm_ips[s["slug"]],
                s["slug"],
                passwords[s["slug"]],
            ): s["slug"]
            for s in STUDENTS
        }
        for f in as_completed(futures):
            f.result()

    log("Phase 4 complete.\n")
    return passwords


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5 — Summary
# ─────────────────────────────────────────────────────────────────────────────


def print_summary(vm_ips: dict[str, str], passwords: dict[str, str]) -> None:
    sep = "=" * 72
    print(f"\n{sep}")
    print("  PROVISIONING COMPLETE — Container Seminar")
    print(sep)
    for s in STUDENTS:
        slug = s["slug"]
        ip   = vm_ips[slug]
        url  = f"https://{slug}.{DOMAIN_SUFFIX}"
        print(f"\n  Student  : {s['display']}")
        print(f"  VM       : {slug}  ({ip})")
        print(f"  URL      : {url}")
        print(f"  Password : {passwords[slug]}")
        print(f"  SSH      : ssh student@{ip}")
    print()
    print("  NOTES:")
    print("  • Caddy obtains a Let's Encrypt cert on the first HTTPS request.")
    print("    Allow ~60 s after DNS propagates before opening the URLs.")
    print("  • DNS is set grey-cloud (proxy OFF) — required for HTTP-01 challenge.")
    print("  • Docker is installed from the official Docker apt repository.")
    print("    'student' is a member of the docker group (no sudo needed for docker).")
    print("  • Docker Compose v2 is available as: docker compose")
    print("  • kubectl 1.31 and Helm are pre-installed system-wide.")
    print(sep)

    # Write a machine-readable credentials markdown file alongside this script
    md_path = Path(__file__).parent / "container-seminar-credentials.md"
    rows = "\n".join(
        f"| {s['display']} "
        f"| https://{s['slug']}.{DOMAIN_SUFFIX} "
        f"| `{passwords[s['slug']]}` "
        f"| {vm_ips[s['slug']]} |"
        for s in STUDENTS
    )
    md_path.write_text(
        "# Container Seminar — Credentials\n\n"
        "| Student | URL | Password | IP |\n"
        "|---|---|---|---|\n"
        f"{rows}\n\n"
        "SSH login: `ssh student@<ip>`  \n"
        "Docker, Docker Compose v2, kubectl, and Helm are pre-installed on each VM.\n"
    )
    print(f"\n  Credentials written to: {md_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6/7 — Harbor Registry (dedicated VM, Docker Compose install)
#
# Harbor is installed with its official Docker Compose installer (the
# supported, simplest path — no Kubernetes cluster required). TLS is
# terminated by Caddy exactly like the code-server VMs, so harbor.yml's own
# https block is disabled and Harbor's internal proxy just listens on
# HARBOR_HTTP_PORT (loopback only, hardened the same way code-server binds
# to 127.0.0.1). Placeholders (@@...@@) are substituted via str.replace()
# rather than str.format()/f-strings because these scripts are full of
# literal '{' '}' (curl -w, JSON bodies) that would otherwise need escaping.
# ─────────────────────────────────────────────────────────────────────────────

_HARBOR_INSTALL_TEMPLATE = r"""
set -e
if [ ! -d /opt/harbor ]; then
  TAG=$(curl -fsSL https://api.github.com/repos/goharbor/harbor/releases/latest | grep -m1 '"tag_name"' | cut -d'"' -f4)
  echo "Installing Harbor ${TAG} ..."
  curl -fsSL "https://github.com/goharbor/harbor/releases/download/${TAG}/harbor-online-installer-${TAG}.tgz" -o /tmp/harbor.tgz
  tar xzf /tmp/harbor.tgz -C /opt
fi
cd /opt/harbor
cp -n harbor.yml.tmpl harbor.yml
sed -i \
  -e 's/^hostname: .*/hostname: @@HOSTNAME@@/' \
  -e 's/^  port: 80$/  port: @@HTTP_PORT@@/' \
  -e '/^https:$/s/^/#/' \
  -e '/^  port: 443$/s/^/#/' \
  -e '/^  certificate: /s/^/#/' \
  -e '/^  private_key: /s/^/#/' \
  -e 's/^harbor_admin_password: .*/harbor_admin_password: @@ADMIN_PASSWORD@@/' \
  -e 's/^  password: root123$/  password: @@DB_PASSWORD@@/' \
  harbor.yml
./install.sh --with-trivy
# Defense in depth: bind Harbor's internal proxy to loopback only, same as
# code-server's 127.0.0.1 bind. The Hetzner firewall already blocks this
# port publicly, so failures here (format drift between Harbor releases)
# are non-fatal.
sed -i "s/'@@HTTP_PORT@@:8080'/'127.0.0.1:@@HTTP_PORT@@:8080'/" docker-compose.yml || true
sed -i "s/- @@HTTP_PORT@@:8080$/- 127.0.0.1:@@HTTP_PORT@@:8080/" docker-compose.yml || true
docker compose up -d
"""

_HARBOR_CONFIGURE_TEMPLATE = r"""
set -e
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq jq >/dev/null
BASE="http://127.0.0.1:@@HTTP_PORT@@/api/v2.0"
AUTH="admin:@@ADMIN_PASSWORD@@"

echo "Waiting for Harbor API to become healthy ..."
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health") || code=000
  [ "$code" = "200" ] && break
  sleep 5
done

# Create the shared project (409 if it already exists — that's fine)
curl -s -o /dev/null -u "$AUTH" -X POST "$BASE/projects" \
  -H 'Content-Type: application/json' \
  -d '{"project_name":"@@PROJECT@@","metadata":{"public":"false"}}'

# Recreate the robot account so we always know its secret (Harbor only
# reveals a robot secret once, at creation time).
# Harbor stores project-level robot names as "robot$<project>+<name>"
ROBOT_ID=$(curl -s -u "$AUTH" "$BASE/robots?page_size=100" | jq -r --arg n "@@ROBOT_NAME@@" '.[] | select(.name | contains($n)) | .id' | head -1)
if [ -n "$ROBOT_ID" ]; then
  curl -s -o /dev/null -X DELETE -u "$AUTH" "$BASE/robots/$ROBOT_ID"
fi

curl -s -u "$AUTH" -X POST "$BASE/robots" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "@@ROBOT_NAME@@",
        "duration": -1,
        "level": "project",
        "permissions": [
          {
            "kind": "project",
            "namespace": "@@PROJECT@@",
            "access": [
              {"resource": "repository", "action": "push"},
              {"resource": "repository", "action": "pull"},
              {"resource": "tag", "action": "create"},
              {"resource": "artifact", "action": "read"},
              {"resource": "artifact", "action": "list"}
            ]
          }
        ]
      }' > /root/harbor-robot.json
"""


def install_harbor(ip: str, secrets_data: dict[str, str]) -> None:
    """Install Docker, Caddy, and Harbor itself on the Harbor VM."""
    caddyfile = (
        f"{HARBOR_HOSTNAME} {{\n"
        f"    reverse_proxy 127.0.0.1:{HARBOR_HTTP_PORT}\n"
        f"    log {{\n"
        f"        output file /var/log/caddy/access.log\n"
        f"    }}\n"
        f"}}\n"
    )
    install_script = (
        _HARBOR_INSTALL_TEMPLATE
        .replace("@@HOSTNAME@@", HARBOR_HOSTNAME)
        .replace("@@HTTP_PORT@@", str(HARBOR_HTTP_PORT))
        .replace("@@ADMIN_PASSWORD@@", secrets_data["admin_password"])
        .replace("@@DB_PASSWORD@@", secrets_data["db_password"])
    )

    with connection(ip) as c:
        log(f"  [{HARBOR_SLUG}] Installing Docker Engine + Compose v2 ...")
        c.run(_DOCKER_INSTALL, hide=True)

        log(f"  [{HARBOR_SLUG}] Installing Caddy ...")
        c.run(_CADDY_INSTALL, hide=True)
        c.run("mkdir -p /var/log/caddy", hide=True)
        put_text(c, caddyfile, "/etc/caddy/Caddyfile")
        c.run("systemctl enable caddy && systemctl restart caddy", hide=True)

        log(f"  [{HARBOR_SLUG}] Installing Harbor (this can take a few minutes) ...")
        c.run(install_script, hide=True)

    log(f"  [{HARBOR_SLUG}] Harbor installed.")


def configure_harbor(ip: str, secrets_data: dict[str, str]) -> dict[str, str]:
    """Create the shared project + a push/pull robot account for students."""
    log(f"  [{HARBOR_SLUG}] Creating project '{HARBOR_PROJECT}' and robot account ...")
    configure_script = (
        _HARBOR_CONFIGURE_TEMPLATE
        .replace("@@HTTP_PORT@@", str(HARBOR_HTTP_PORT))
        .replace("@@ADMIN_PASSWORD@@", secrets_data["admin_password"])
        .replace("@@PROJECT@@", HARBOR_PROJECT)
        .replace("@@ROBOT_NAME@@", HARBOR_ROBOT_NAME)
    )
    local_path = Path(__file__).parent / ".harbor-robot.json"
    with connection(ip) as c:
        c.run(configure_script, hide=True)
        c.get("/root/harbor-robot.json", str(local_path))
    robot = json.loads(local_path.read_text())
    local_path.unlink()
    if "name" not in robot or "secret" not in robot:
        raise RuntimeError(f"Harbor robot creation failed — API response: {robot}")
    return {"name": robot["name"], "secret": robot["secret"]}


def print_harbor_summary(ip: str, secrets_data: dict[str, str], robot: dict[str, str]) -> None:
    sep = "=" * 72
    print(f"\n{sep}")
    print("  HARBOR REGISTRY READY")
    print(sep)
    print(f"\n  URL           : https://{HARBOR_HOSTNAME}")
    print(f"  VM            : {HARBOR_SLUG}  ({ip})")
    print(f"  Admin login   : admin / {secrets_data['admin_password']}")
    print(f"  Project       : {HARBOR_PROJECT}")
    print(f"  Robot account : {robot['name']}")
    print(f"  Robot secret  : {robot['secret']}")
    print(sep)

    md_path = Path(__file__).parent / "harbor-credentials.md"
    md_path.write_text(
        "# Harbor Registry — Credentials\n\n"
        f"URL: https://{HARBOR_HOSTNAME}\n\n"
        "## Instructor (admin)\n\n"
        "- Username: `admin`\n"
        f"- Password: `{secrets_data['admin_password']}`\n\n"
        "## Students (shared push/pull robot account)\n\n"
        f"- Project: `{HARBOR_PROJECT}`\n"
        f"- Username: `{robot['name']}`\n"
        f"- Password: `{robot['secret']}`\n\n"
        "### Usage on any student VM\n\n"
        "```bash\n"
        f"docker login {HARBOR_HOSTNAME}\n"
        f"# username: {robot['name']}\n"
        "# password: <the robot secret above>\n\n"
        f"docker tag exercise-2 {HARBOR_HOSTNAME}/{HARBOR_PROJECT}/exercise-2:latest\n"
        f"docker push {HARBOR_HOSTNAME}/{HARBOR_PROJECT}/exercise-2:latest\n"
        "```\n"
    )
    print(f"\n  Credentials written to: {md_path}")


def provision_harbor() -> None:
    """Phase 6/7: dedicated VM + Docker/Caddy/Harbor + shared project/robot account."""
    log("=== Phase 6: Harbor Infrastructure ===")
    ssh_key = ensure_hetzner_ssh_key()
    name, ip = create_vm(HARBOR_SLUG, ssh_key, server_type=HARBOR_SERVER_TYPE)

    fw = hc.firewalls.get_by_name(FW_NAME)
    server = hc.servers.get_by_name(name)
    try:
        hc.firewalls.apply_to_resources(
            firewall=fw,
            resources=[FirewallResource(type=FirewallResource.TYPE_SERVER, server=server)],
        )
        log(f"  Firewall applied to '{name}'.")
    except Exception as e:
        if "firewall_already_applied" in str(e):
            log(f"  Firewall already applied to '{name}', skipping.")
        else:
            log(f"  Warning: could not apply firewall to '{name}': {e}")

    upsert_dns_record(HARBOR_HOSTNAME, ip)

    wait_for_ssh(ip)
    configure_base_vm(HARBOR_SLUG, ip)
    log("Phase 6 complete.\n")

    log("=== Phase 7: Harbor Install + Configuration ===")
    secrets_data = load_or_generate_harbor_secrets()
    install_harbor(ip, secrets_data)
    robot = configure_harbor(ip, secrets_data)
    print_harbor_summary(ip, secrets_data, robot)
    log("Phase 7 complete.\n")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    vm_ips = provision_hetzner()

    # DNS setup and base OS config are independent — run them concurrently
    with ThreadPoolExecutor(max_workers=2) as pool:
        dns_f  = pool.submit(setup_dns, vm_ips)
        base_f = pool.submit(configure_all_base, vm_ips)
        dns_f.result()
        base_f.result()

    passwords = configure_all_vms(vm_ips)
    print_summary(vm_ips, passwords)

    provision_harbor()


if __name__ == "__main__":
    main()
