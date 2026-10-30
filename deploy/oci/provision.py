#!/usr/bin/env python3
"""Provision OmniStream on Oracle Cloud Infrastructure (OCI).

Creates, or reuses by display name, a dedicated VCN with a public subnet,
internet gateway, default route and security list, then launches an Ampere A1
VM whose cloud-init (deploy/oci/cloud-init.sh) installs Docker, clones this
repository and runs `docker compose up -d --build`. Nothing outside the new
VCN is touched.

Requirements: `pip install oci argon2-cffi` (or run it from the project venv
after `uv pip install oci`).

Credentials are read from environment variables:
  OCI_TENANCY_ID, OCI_USER_ID, OCI_REGION_ID
  OCI_FINGERPRINT (or OCI_SSH_FINGERPRINT)   API signing key fingerprint
  OCI_KEY_FILE, or the PEM in OCI_PRIVATE_KEY (or OCI_SSH_PRIVATE_KEY)
  OCI_COMPARTMENT_ID (optional; full OCID or bare suffix; default: tenancy root)
The VM gets the public half of the API key as its SSH key unless
--ssh-public-key is given.

Usage:
  python deploy/oci/provision.py up --branch main      # prints the admin password once
  python deploy/oci/provision.py status --follow       # bootstrap progress from the serial console
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CLOUD_INIT = HERE / "cloud-init.sh"
DEFAULT_REPO = "https://github.com/lehelkovach/multi-live-stream.git"
SHAPE = "VM.Standard.A1.Flex"
VCN_CIDR = "10.60.0.0/16"
SUBNET_CIDR = "10.60.0.0/24"
TAGS = {"project": "omnistream", "provisioned-by": "deploy/oci/provision.py"}


# --------------------------------------------------------------- pure helpers ----


def render_cloud_init(
    *, repo_url: str, branch: str, admin_password_hash: str, domain: str = ""
) -> str:
    """Fill the placeholders of deploy/oci/cloud-init.sh."""
    if "'" in admin_password_hash:
        msg = "admin password hash must not contain single quotes"
        raise ValueError(msg)
    text = CLOUD_INIT.read_text()
    values = {
        "__REPO_URL__": repo_url,
        "__BRANCH__": branch,
        "__ADMIN_PASSWORD_HASH__": admin_password_hash,
        "__DOMAIN__": domain,
    }
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    return text


def current_branch() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(HERE), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip() or "main"
    except (OSError, subprocess.CalledProcessError):
        return "main"


def omni_lines(console_text: str) -> list[str]:
    """Progress lines written by cloud-init.sh ("OMNI: ..."), without console noise."""
    lines = []
    for raw in console_text.splitlines():
        idx = raw.find("OMNI: ")
        if idx >= 0:
            lines.append(raw[idx + 6 :].rstrip())
    return lines


# ------------------------------------------------------------------- OCI glue ----


def oci_config() -> tuple[dict[str, str], str, str]:
    """(config, compartment ocid, ssh public key) from the environment."""
    from cryptography.hazmat.primitives import serialization

    env = os.environ
    key_file = env.get("OCI_KEY_FILE", "")
    pem = env.get("OCI_PRIVATE_KEY") or env.get("OCI_SSH_PRIVATE_KEY") or ""
    if not key_file:
        if not pem.strip():
            sys.exit("set OCI_KEY_FILE or OCI_PRIVATE_KEY / OCI_SSH_PRIVATE_KEY (PEM)")
        fd, key_file = tempfile.mkstemp(prefix="oci-key-", suffix=".pem")
        with os.fdopen(fd, "w") as fh:
            fh.write(pem if pem.endswith("\n") else pem + "\n")
        os.chmod(key_file, 0o600)
    config = {
        "user": env["OCI_USER_ID"],
        "tenancy": env["OCI_TENANCY_ID"],
        "fingerprint": env.get("OCI_FINGERPRINT") or env["OCI_SSH_FINGERPRINT"],
        "key_file": key_file,
        "region": env["OCI_REGION_ID"],
    }
    raw = env.get("OCI_COMPARTMENT_ID", "")
    if not raw or config["tenancy"].endswith("." + raw):
        compartment = config["tenancy"]
    elif raw.startswith("ocid1."):
        compartment = raw
    else:
        compartment = "ocid1.compartment.oc1.." + raw
    private = serialization.load_pem_private_key(Path(key_file).read_bytes(), password=None)
    public = private.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    return config, compartment, public.decode() + " omnistream"


def wait_state(get: Any, ocid: str, states: tuple[str, ...], timeout: float = 600) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        obj = get(ocid).data
        if obj.lifecycle_state in states:
            return obj
        if time.monotonic() > deadline:
            sys.exit(f"timed out waiting for {ocid} to reach {states} (is {obj.lifecycle_state})")
        time.sleep(5)


def ensure_network(oci: Any, vnet: Any, compartment: str, name: str, ingest_cidr: str) -> Any:
    models = oci.core.models
    vcn = next(
        (
            v
            for v in vnet.list_vcns(compartment, display_name=f"{name}-vcn").data
            if v.lifecycle_state == "AVAILABLE"
        ),
        None,
    )
    if vcn is None:
        vcn = vnet.create_vcn(
            models.CreateVcnDetails(
                compartment_id=compartment,
                display_name=f"{name}-vcn",
                cidr_blocks=[VCN_CIDR],
                dns_label=name.replace("-", "")[:15],
                freeform_tags=TAGS,
            )
        ).data
        vcn = wait_state(vnet.get_vcn, vcn.id, ("AVAILABLE",))
    else:
        pass

    igw = next(
        (
            g
            for g in vnet.list_internet_gateways(compartment, vcn_id=vcn.id).data
            if g.lifecycle_state == "AVAILABLE"
        ),
        None,
    )
    if igw is None:
        igw = vnet.create_internet_gateway(
            models.CreateInternetGatewayDetails(
                compartment_id=compartment,
                vcn_id=vcn.id,
                is_enabled=True,
                display_name=f"{name}-igw",
                freeform_tags=TAGS,
            )
        ).data
        igw = wait_state(vnet.get_internet_gateway, igw.id, ("AVAILABLE",))
    vnet.update_route_table(
        vcn.default_route_table_id,
        models.UpdateRouteTableDetails(
            route_rules=[
                models.RouteRule(
                    destination="0.0.0.0/0", destination_type="CIDR_BLOCK", network_entity_id=igw.id
                ),
            ]
        ),
    )

    def tcp(port: int, source: str = "0.0.0.0/0", desc: str = "") -> Any:
        return models.IngressSecurityRule(
            protocol="6",
            source=source,
            description=desc,
            tcp_options=models.TcpOptions(
                destination_port_range=models.PortRange(min=port, max=port)
            ),
        )

    def udp(port: int, source: str = "0.0.0.0/0", desc: str = "") -> Any:
        return models.IngressSecurityRule(
            protocol="17",
            source=source,
            description=desc,
            udp_options=models.UdpOptions(
                destination_port_range=models.PortRange(min=port, max=port)
            ),
        )

    ingress = [
        tcp(22, desc="ssh (key only)"),
        tcp(80, desc="http -> https redirect, ACME"),
        tcp(443, desc="dashboard / api"),
        udp(443, desc="http/3"),
        tcp(1935, ingest_cidr, "rtmp ingest (stream key required)"),
        udp(8890, ingest_cidr, "srt ingest (stream key required)"),
        models.IngressSecurityRule(
            protocol="1",
            source="0.0.0.0/0",
            description="path mtu",
            icmp_options=models.IcmpOptions(type=3, code=4),
        ),
        models.IngressSecurityRule(
            protocol="1", source=VCN_CIDR, icmp_options=models.IcmpOptions(type=3)
        ),
    ]
    egress = [models.EgressSecurityRule(protocol="all", destination="0.0.0.0/0")]
    sl = next(
        (
            s
            for s in vnet.list_security_lists(
                compartment, vcn_id=vcn.id, display_name=f"{name}-sl"
            ).data
            if s.lifecycle_state == "AVAILABLE"
        ),
        None,
    )
    if sl is None:
        sl = vnet.create_security_list(
            models.CreateSecurityListDetails(
                compartment_id=compartment,
                vcn_id=vcn.id,
                display_name=f"{name}-sl",
                ingress_security_rules=ingress,
                egress_security_rules=egress,
                freeform_tags=TAGS,
            )
        ).data
        sl = wait_state(vnet.get_security_list, sl.id, ("AVAILABLE",))
    else:
        vnet.update_security_list(
            sl.id,
            models.UpdateSecurityListDetails(
                ingress_security_rules=ingress, egress_security_rules=egress
            ),
        )

    subnet = next(
        (
            s
            for s in vnet.list_subnets(
                compartment, vcn_id=vcn.id, display_name=f"{name}-public"
            ).data
            if s.lifecycle_state == "AVAILABLE"
        ),
        None,
    )
    if subnet is None:
        subnet = vnet.create_subnet(
            models.CreateSubnetDetails(
                compartment_id=compartment,
                vcn_id=vcn.id,
                display_name=f"{name}-public",
                cidr_block=SUBNET_CIDR,
                dns_label="pub",
                route_table_id=vcn.default_route_table_id,
                security_list_ids=[sl.id],
                prohibit_public_ip_on_vnic=False,
                freeform_tags=TAGS,
            )
        ).data
        subnet = wait_state(vnet.get_subnet, subnet.id, ("AVAILABLE",))
    return subnet


def find_instance(compute: Any, compartment: str, name: str) -> Any:
    live = ("PROVISIONING", "STARTING", "RUNNING", "STOPPING", "STOPPED")
    return next(
        (
            i
            for i in compute.list_instances(compartment, display_name=name).data
            if i.lifecycle_state in live
        ),
        None,
    )


def public_ip(compute: Any, vnet: Any, compartment: str, instance_id: str) -> str:
    for att in compute.list_vnic_attachments(compartment, instance_id=instance_id).data:
        if att.lifecycle_state == "ATTACHED":
            vnic = vnet.get_vnic(att.vnic_id).data
            if vnic.public_ip:
                return str(vnic.public_ip)
    return ""


def cmd_up(args: argparse.Namespace) -> int:
    import oci

    config, compartment, ssh_key = oci_config()
    if args.ssh_public_key:
        ssh_key = Path(args.ssh_public_key).read_text().strip()
    ident = oci.identity.IdentityClient(config)
    compute = oci.core.ComputeClient(config)
    vnet = oci.core.VirtualNetworkClient(config)
    limits = oci.limits.LimitsClient(config)
    models = oci.core.models

    instance = find_instance(compute, compartment, args.name)
    if instance is not None:
        public_ip(compute, vnet, compartment, instance.id)
        return 0

    subnet = ensure_network(oci, vnet, compartment, args.name, args.ingest_cidr)

    password = ""
    pw_hash = args.admin_password_hash
    if not pw_hash:
        from argon2 import PasswordHasher

        password = secrets.token_urlsafe(18)
        pw_hash = PasswordHasher().hash(password)
    user_data = render_cloud_init(
        repo_url=args.repo, branch=args.branch, admin_password_hash=pw_hash, domain=args.domain
    )

    image = compute.list_images(
        compartment,
        operating_system="Canonical Ubuntu",
        operating_system_version="24.04",
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data[0]

    ads = [a.name for a in ident.list_availability_domains(config["tenancy"]).data]

    def a1_used(ad: str) -> float:
        try:
            return float(
                limits.get_resource_availability(
                    "compute", "standard-a1-core-count", config["tenancy"], availability_domain=ad
                ).data.used
                or 0
            )
        except oci.exceptions.ServiceError:
            return 0.0

    ads.sort(key=a1_used)
    if args.ad:
        ads = [a for a in ads if a.endswith(args.ad)] or ads

    for ad in ads:
        try:
            instance = compute.launch_instance(
                models.LaunchInstanceDetails(
                    availability_domain=ad,
                    compartment_id=compartment,
                    display_name=args.name,
                    shape=SHAPE,
                    shape_config=models.LaunchInstanceShapeConfigDetails(
                        ocpus=args.ocpus, memory_in_gbs=args.memory
                    ),
                    source_details=models.InstanceSourceViaImageDetails(
                        image_id=image.id, boot_volume_size_in_gbs=args.boot_gb
                    ),
                    create_vnic_details=models.CreateVnicDetails(
                        subnet_id=subnet.id,
                        assign_public_ip=True,
                        hostname_label=args.name.replace("-", "")[:15],
                    ),
                    metadata={
                        "ssh_authorized_keys": ssh_key,
                        "user_data": base64.b64encode(user_data.encode()).decode(),
                    },
                    freeform_tags=TAGS,
                )
            ).data
            break
        except oci.exceptions.ServiceError as exc:
            if "capacity" in str(exc.message).lower() and ad != ads[-1]:
                continue
            raise
    assert instance is not None
    instance = wait_state(compute.get_instance, instance.id, ("RUNNING", "TERMINATED"), timeout=900)
    if instance.lifecycle_state == "TERMINATED":
        sys.exit(f"instance {instance.id} terminated during launch")
    public_ip(compute, vnet, compartment, instance.id)
    if password:
        pass
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    import oci

    config, compartment, _ = oci_config()
    compute = oci.core.ComputeClient(config)
    instance = find_instance(compute, compartment, args.name)
    if instance is None:
        sys.exit(f"no instance named {args.name}")
    seen: list[str] = []
    deadline = time.monotonic() + args.timeout
    while True:
        cap = compute.capture_console_history(
            oci.core.models.CaptureConsoleHistoryDetails(instance_id=instance.id)
        ).data
        cap = wait_state(compute.get_console_history, cap.id, ("SUCCEEDED", "FAILED"), timeout=120)
        text = compute.get_console_history_content(cap.id, length=1_048_576).data
        text = text.decode(errors="replace") if isinstance(text, bytes) else str(text)
        lines = omni_lines(text)
        for _line in lines[len(seen) :]:
            pass
        seen = lines
        with contextlib.suppress(oci.exceptions.ServiceError):
            compute.delete_console_history(cap.id)
        finished = any(line.startswith(("DONE", "FAILED")) for line in lines)
        if finished or not args.follow or time.monotonic() > deadline:
            return 1 if any(line.startswith("FAILED") for line in lines) else 0
        time.sleep(args.interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up", help="create the network and the VM (idempotent)")
    up.add_argument("--name", default="omnistream")
    up.add_argument("--ocpus", type=float, default=1.0)
    up.add_argument("--memory", type=float, default=6.0, help="GB")
    up.add_argument("--boot-gb", type=int, default=50)
    up.add_argument("--ad", default="", help="availability domain suffix, e.g. AD-3")
    up.add_argument("--repo", default=DEFAULT_REPO)
    up.add_argument("--branch", default=current_branch())
    up.add_argument(
        "--domain", default="", help="DNS name pointing at the VM (default: <ip>.sslip.io)"
    )
    up.add_argument("--ingest-cidr", default="0.0.0.0/0", help="who may reach RTMP/SRT ingest")
    up.add_argument(
        "--admin-password-hash", default="", help="argon2 hash; default: generate a password"
    )
    up.add_argument(
        "--ssh-public-key", default="", help="file; default: public half of the API key"
    )
    up.set_defaults(func=cmd_up)
    st = sub.add_parser("status", help="print bootstrap progress from the serial console")
    st.add_argument("--name", default="omnistream")
    st.add_argument("--follow", action="store_true")
    st.add_argument("--interval", type=float, default=30.0)
    st.add_argument("--timeout", type=float, default=3600.0)
    st.set_defaults(func=cmd_status)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
