#!/usr/bin/env python3
"""
Terraformer -> production-ready Terraform (community modules) MCP server.

INPUT:  a directory of raw terraformer output (``generated/aws/<service>/*.tf``
        plus the ``terraform.tfstate`` terraformer writes beside it).
OUTPUT: a production-shaped Terraform repo whose resources are expressed as
        calls to the terraform-aws-modules community modules, with the live
        objects adopted into the module addresses via ``import`` blocks, driven
        to a zero-diff plan.

Pipeline the tools are designed around
--------------------------------------
  1. ingest_terraformer      copy raw terraformer output into a workspace
  2. inventory               resource inventory *from tfstate* (real IDs)
  3. plan_conversion         inventory x catalog -> module grouping + gaps
  4. scaffold_project        production repo skeleton (versions/providers/...)
  5. emit_module_call        catalog + state values -> module "x" { ... }
  6. emit_import_blocks      real IDs -> import { to = module... id = "..." }
  7. tf_init / tf_validate / tf_fmt
  8. CONVERGE LOOP:
        tf_plan -> classify_plan -> patch_lifecycle / dereference_ids
                -> tf_plan ... until status == "converged"
  9. audit_production_readiness

Why state, not HCL, is the source of truth
------------------------------------------
Terraformer's ``terraform.tfstate`` holds every attribute *and the real
resource ID*, as plain JSON. The ``.tf`` files it emits do not contain ``id``
at all, so any tool that scrapes IDs out of the HCL produces "UNKNOWN_ID".
Every tool here reads state first and falls back to HCL only when state is
absent.

Safety model
------------
* Everything writable is confined to ``TF_MCP_WORKSPACE_ROOT``.
* ``ingest_terraformer`` may *read* a terraformer directory anywhere on disk
  (that is the whole point of the server) but can only *write* into the
  workspace root. Set ``TF_MCP_SOURCE_ROOTS`` to a ``:``-separated allowlist to
  restrict where it may read from.
* No tool applies anything to AWS except ``tf_apply_imports``, which refuses to
  run unless the saved plan is import-only (no create/update/delete/replace).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("terraformer-to-community-modules")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WORKSPACE_ROOT = Path(
    os.environ.get("TF_MCP_WORKSPACE_ROOT", os.path.expanduser("~/tf-import-workspaces"))
).resolve()
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

#: Optional ``:``-separated allowlist of directories ``ingest_terraformer`` may
#: read from. Empty means "anywhere readable" (read-only regardless).
SOURCE_ROOTS = [
    Path(p).expanduser().resolve()
    for p in os.environ.get("TF_MCP_SOURCE_ROOTS", "").split(os.pathsep)
    if p.strip()
]

TERRAFORMER_BIN = os.environ.get("TERRAFORMER_BIN", "terraformer")
TERRAFORM_BIN = os.environ.get("TERRAFORM_BIN", "terraform")

DEFAULT_TIMEOUT = int(os.environ.get("TF_MCP_TIMEOUT", "900"))

#: Command output is fed back into a model's context; cap it hard.
MAX_STREAM_CHARS = int(os.environ.get("TF_MCP_MAX_STREAM_CHARS", "24000"))
MAX_ITEMS = 200


# ---------------------------------------------------------------------------
# Result / path helpers
# ---------------------------------------------------------------------------


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


def _err(message: str, **extra: Any) -> str:
    return _dump({"ok": False, "error": message, **extra})


def _ok(**payload: Any) -> str:
    return _dump({"ok": True, **payload})


def _truncate(text: str, limit: int = MAX_STREAM_CHARS) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n... [{len(text) - limit} chars truncated] ...\n{tail}"


def _resolve_workspace(workspace_dir: str) -> Path:
    """Resolve a workspace argument, refusing anything outside WORKSPACE_ROOT."""
    raw = Path(workspace_dir).expanduser()
    candidate = (raw if raw.is_absolute() else WORKSPACE_ROOT / raw).resolve()
    try:
        candidate.relative_to(WORKSPACE_ROOT)
    except ValueError:
        raise ValueError(
            f"workspace_dir {workspace_dir!r} resolves to {candidate}, outside the "
            f"allowed root {WORKSPACE_ROOT}. Set TF_MCP_WORKSPACE_ROOT to change it."
        )
    return candidate


def _resolve_in(base: Path, relative: Optional[str], *, label: str = "path") -> Path:
    """Resolve ``relative`` under ``base``, refusing traversal outside it."""
    if not relative:
        return base
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise ValueError(f"{label} {relative!r} escapes {base}")
    return candidate


def _resolve_source(source_dir: str) -> Path:
    """Resolve a read-only terraformer source directory."""
    candidate = Path(source_dir).expanduser().resolve()
    if not candidate.is_dir():
        raise ValueError(f"source_dir {source_dir!r} is not a directory ({candidate})")
    if SOURCE_ROOTS and not any(
        candidate == root or root in candidate.parents for root in SOURCE_ROOTS
    ):
        raise ValueError(
            f"source_dir {candidate} is not under any TF_MCP_SOURCE_ROOTS entry "
            f"({', '.join(str(r) for r in SOURCE_ROOTS)})"
        )
    return candidate


def _run(
    cmd: list[str],
    cwd: Path,
    timeout: int = DEFAULT_TIMEOUT,
    env_extra: Optional[dict] = None,
) -> dict:
    """Run a subprocess. Never raises: a non-zero terraform exit is data."""
    env = os.environ.copy()
    env.setdefault("TF_IN_AUTOMATION", "1")
    env.setdefault("CHECKPOINT_DISABLE", "1")
    if env_extra:
        env.update(env_extra)

    base = {
        "command": " ".join(shlex.quote(c) for c in cmd),
        "cwd": str(cwd),
        "timed_out": False,
    }
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env
        )
        return {
            **base,
            "returncode": proc.returncode,
            "stdout": _truncate(proc.stdout),
            "stderr": _truncate(proc.stderr),
            # kept unstruncated for internal parsing, stripped before return
            "_stdout_full": proc.stdout,
        }
    except FileNotFoundError as exc:
        return {
            **base,
            "returncode": 127,
            "stdout": "",
            "stderr": f"Binary not found: {exc}. Is it installed and on PATH?",
            "_stdout_full": "",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            **base,
            "returncode": 124,
            "timed_out": True,
            "stdout": _truncate(exc.stdout or ""),
            "stderr": f"Command timed out after {timeout}s",
            "_stdout_full": exc.stdout or "",
        }


def _public(result: dict) -> dict:
    """Strip internal keys before a run result goes back over the wire."""
    return {k: v for k, v in result.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# HCL scanning
#
# Brace counting on raw text miscounts as soon as a `{` appears inside a
# string, a comment or a heredoc -- which happens constantly in terraformer
# output (IAM policy JSON, user_data scripts). _mask() blanks every
# non-code region while preserving length and newlines, so offsets computed on
# the mask are valid offsets into the original text.
# ---------------------------------------------------------------------------

_HEREDOC_RE = re.compile(r"<<[-~]?\s*([A-Za-z_][A-Za-z0-9_]*)")


def _mask(text: str) -> str:
    """Return a same-length copy of ``text`` with comments/strings/heredocs blanked."""
    out = list(text)
    n = len(text)
    i = 0
    # stack entries: "str" (inside "..."), "interp" (inside ${ } or %{ })
    stack: list[str] = []

    def blank(start: int, end: int) -> None:
        for k in range(start, min(end, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        ch = text[i]

        if stack and stack[-1] == "str":
            if ch == "\\" and i + 1 < n:
                blank(i, i + 2)
                i += 2
                continue
            if ch == '"':
                blank(i, i + 1)
                stack.pop()
                i += 1
                continue
            if ch in "$%" and i + 1 < n and text[i + 1] == "{":
                # interpolation: its contents are code again (may hold strings)
                blank(i, i + 2)
                stack.append("interp")
                i += 2
                continue
            blank(i, i + 1)
            i += 1
            continue

        # -- code (or inside an interpolation) --
        if ch == "#" or (ch == "/" and i + 1 < n and text[i + 1] == "/"):
            j = text.find("\n", i)
            j = n if j == -1 else j
            blank(i, j)
            i = j
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            blank(i, j)
            i = j
            continue

        if ch == "<" and text.startswith("<<", i):
            m = _HEREDOC_RE.match(text, i)
            if m:
                marker = m.group(1)
                line_end = text.find("\n", i)
                line_end = n if line_end == -1 else line_end
                # find the terminator line
                term = re.compile(rf"^\s*{re.escape(marker)}\s*$", re.M)
                tm = term.search(text, line_end)
                end = tm.end() if tm else n
                blank(i, end)
                i = end
                continue

        if ch == '"':
            blank(i, i + 1)
            stack.append("str")
            i += 1
            continue

        if ch == "}" and stack and stack[-1] == "interp":
            blank(i, i + 1)
            stack.pop()
            i += 1
            continue

        i += 1

    return "".join(out)


@dataclass
class HclBlock:
    kind: str  # "resource", "module", "data", ...
    labels: list[str]
    start: int  # line index of the header, 0-based
    end: int  # line index of the closing brace, 0-based
    text: str


def _iter_blocks(text: str) -> Iterable[HclBlock]:
    """Yield every top-level block in ``text``, brace-matched on masked source."""
    masked = _mask(text)
    lines = text.splitlines()
    mlines = masked.splitlines()
    header = re.compile(r'^\s*([a-zA-Z_][\w-]*)((?:\s+"[^"]*")*)\s*\{')

    i = 0
    while i < len(mlines):
        m = header.match(mlines[i])
        if not m:
            i += 1
            continue
        # labels must be read from the *original* line (mask blanked them)
        kind = m.group(1)
        labels = re.findall(r'"([^"]*)"', lines[i])
        depth = mlines[i].count("{") - mlines[i].count("}")
        start = i
        j = i
        while depth > 0 and j + 1 < len(mlines):
            j += 1
            depth += mlines[j].count("{") - mlines[j].count("}")
        yield HclBlock(kind, labels, start, j, "\n".join(lines[start : j + 1]))
        i = j + 1


def _parse_attributes(block_text: str) -> dict[str, Any]:
    """Best-effort ``key = value`` extraction from one block's body.

    Repeated nested blocks (``ebs_block_device { }`` twice) collect into a list.
    Scalars stay strings; the caller decides how to interpret them. This is a
    convenience view of the HCL -- prefer state values from ``inventory``.
    """
    lines = block_text.splitlines()
    if len(lines) < 2:
        return {}
    mlines = _mask(block_text).splitlines()
    attrs: dict[str, Any] = {}

    def add(key: str, value: Any, repeatable: bool) -> None:
        if key not in attrs:
            attrs[key] = [value] if repeatable else value
        elif isinstance(attrs[key], list):
            attrs[key].append(value)
        else:
            attrs[key] = [attrs[key], value]

    i = 1
    end = len(lines) - 1  # skip the block's own closing brace
    while i < end:
        mstripped = mlines[i].strip()
        if not mstripped:
            i += 1
            continue

        assign = re.match(r"^([\w-]+)\s*=\s*(.*)$", mstripped)
        nested = re.match(r"^([\w-]+)\s*\{\s*$", mstripped)

        if assign:
            key, rhs_masked = assign.group(1), assign.group(2)
            opens = rhs_masked.count("{") + rhs_masked.count("[") + rhs_masked.count("(")
            closes = rhs_masked.count("}") + rhs_masked.count("]") + rhs_masked.count(")")
            depth = opens - closes
            # a heredoc RHS was blanked by the mask; follow it to its terminator
            heredoc = _HEREDOC_RE.match(lines[i].split("=", 1)[1].strip())
            start = i
            i += 1
            if heredoc:
                term = re.compile(rf"^\s*{re.escape(heredoc.group(1))}\s*$")
                while i < end and not term.match(lines[i]):
                    i += 1
                i += 1
            else:
                while i < end and depth > 0:
                    ml = mlines[i]
                    depth += ml.count("{") + ml.count("[") + ml.count("(")
                    depth -= ml.count("}") + ml.count("]") + ml.count(")")
                    i += 1
            raw = "\n".join(lines[start:i])
            value = raw.split("=", 1)[1].strip() if "=" in raw.split("\n")[0] else raw
            add(key, value, repeatable=False)
            continue

        if nested:
            key = nested.group(1)
            depth = 1
            start = i
            i += 1
            while i < end and depth > 0:
                depth += mlines[i].count("{") - mlines[i].count("}")
                i += 1
            add(key, "\n".join(lines[start:i]), repeatable=True)
            continue

        i += 1

    return attrs


def _unquote(value: Any) -> Any:
    if isinstance(value, str):
        v = value.strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            return v[1:-1]
        return v
    return value


def _hcl_literal(value: Any, indent: int = 2) -> str:
    """Render a Python value as HCL. Strings that already look like HCL
    expressions (refs, functions, interpolations) pass through unquoted."""
    pad = " " * indent
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if _looks_like_expression(value):
            return value
        return json.dumps(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        items = ",\n".join(f"{pad}  {_hcl_literal(v, indent + 2)}" for v in value)
        return f"[\n{items},\n{pad}]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        width = max(len(str(k)) for k in value)
        items = "\n".join(
            f"{pad}  {k:<{width}} = {_hcl_literal(v, indent + 2)}" for k, v in value.items()
        )
        return f"{{\n{items}\n{pad}}}"
    return json.dumps(str(value))


_EXPR_RE = re.compile(
    r"^(var|local|module|data|each|count|aws_[a-z0-9_]+)\.|^\$\{|^\[|^\{|"
    r"^(true|false|null)$|^-?\d+(\.\d+)?$|^[a-z_]+\("
)


def _looks_like_expression(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if t.startswith('"') and t.endswith('"'):
        return False
    return bool(_EXPR_RE.match(t))


def _sanitize_name(name: str) -> str:
    """Turn a terraformer name (``tfer--my-002D-vpc``) into a clean HCL name."""
    # terraformer encodes non-alphanumerics as -00XX- hex escapes
    name = re.sub(r"-00([0-9A-Fa-f]{2})-", lambda m: chr(int(m.group(1), 16)), name)
    name = re.sub(r"^tfer--", "", name)
    name = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").lower()
    if not name:
        name = "resource"
    if name[0].isdigit():
        name = f"r_{name}"
    return name


# ---------------------------------------------------------------------------
# Community module catalog
#
# ``child_addresses`` is the part that actually matters: to adopt a live object
# into a module you must know the *exact* address the module gives that object
# internally. ``{m}`` is the module call name, ``{i}`` the index.
# Versions are pinned pessimistically; run ``registry_lookup`` to check for
# newer majors before committing to them.
# ---------------------------------------------------------------------------


@dataclass
class ModuleSpec:
    key: str
    source: str
    version: str
    #: raw resource types this module is the canonical home for
    covers: list[str]
    #: raw type -> address template inside the module
    child_addresses: dict[str, str]
    #: module input name -> state attribute name (or callable-ish marker)
    inputs: dict[str, str] = field(default_factory=dict)
    difficulty: str = "medium"
    notes: str = ""
    #: attributes that are provider-computed for resources of this module
    computed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "module": self.key,
            "source": self.source,
            "version": self.version,
            "covers": self.covers,
            "child_addresses": self.child_addresses,
            "inputs": self.inputs,
            "difficulty": self.difficulty,
            "notes": self.notes,
        }


CATALOG: dict[str, ModuleSpec] = {
    "vpc": ModuleSpec(
        key="vpc",
        source="terraform-aws-modules/vpc/aws",
        version="~> 5.13",
        covers=[
            "aws_vpc", "aws_subnet", "aws_internet_gateway", "aws_nat_gateway",
            "aws_eip", "aws_route_table", "aws_route", "aws_route_table_association",
            "aws_default_security_group", "aws_default_route_table",
            "aws_default_network_acl", "aws_vpc_dhcp_options", "aws_flow_log",
        ],
        child_addresses={
            "aws_vpc": "module.{m}.aws_vpc.this[0]",
            "aws_internet_gateway": "module.{m}.aws_internet_gateway.this[0]",
            # Tiers verified against module source; it also declares elasticache,
            # redshift and outpost subnet sets.
            "aws_subnet.public": "module.{m}.aws_subnet.public[{i}]",
            "aws_subnet.private": "module.{m}.aws_subnet.private[{i}]",
            "aws_subnet.database": "module.{m}.aws_subnet.database[{i}]",
            "aws_subnet.intra": "module.{m}.aws_subnet.intra[{i}]",
            "aws_subnet.elasticache": "module.{m}.aws_subnet.elasticache[{i}]",
            "aws_subnet.redshift": "module.{m}.aws_subnet.redshift[{i}]",
            "aws_subnet.outpost": "module.{m}.aws_subnet.outpost[{i}]",
            "aws_route_table.public": "module.{m}.aws_route_table.public[0]",
            "aws_route_table.private": "module.{m}.aws_route_table.private[{i}]",
            "aws_route_table.database": "module.{m}.aws_route_table.database[{i}]",
            "aws_route_table.intra": "module.{m}.aws_route_table.intra[0]",
            "aws_route_table.elasticache": "module.{m}.aws_route_table.elasticache[{i}]",
            "aws_route_table.redshift": "module.{m}.aws_route_table.redshift[{i}]",
            "aws_db_subnet_group": "module.{m}.aws_db_subnet_group.database[0]",
            "aws_elasticache_subnet_group": "module.{m}.aws_elasticache_subnet_group.elasticache[0]",
            "aws_egress_only_internet_gateway": "module.{m}.aws_egress_only_internet_gateway.this[0]",
            "aws_nat_gateway": "module.{m}.aws_nat_gateway.this[{i}]",
            "aws_eip": "module.{m}.aws_eip.nat[{i}]",
            "aws_default_security_group": "module.{m}.aws_default_security_group.this[0]",
            "aws_default_route_table": "module.{m}.aws_default_route_table.default[0]",
            "aws_default_network_acl": "module.{m}.aws_default_network_acl.this[0]",
        },
        inputs={
            "name": "tags.Name",
            "cidr": "cidr_block",
            "enable_dns_hostnames": "enable_dns_hostnames",
            "enable_dns_support": "enable_dns_support",
            "instance_tenancy": "instance_tenancy",
            "tags": "tags",
        },
        difficulty="hard",
        notes=(
            "Subnets must be ordered to match azs/*_subnets list order exactly, or "
            "imports land on the wrong index and the plan shows swapped CIDRs. Build "
            "the subnet lists sorted by availability_zone then cidr_block, and use the "
            "same ordering when generating import blocks."
        ),
        computed=["arn", "owner_id", "default_route_table_id", "default_network_acl_id",
                  "default_security_group_id", "main_route_table_id", "ipv6_association_id",
                  "ipv6_cidr_block", "dhcp_options_id"],
    ),
    "security-group": ModuleSpec(
        key="security-group",
        source="terraform-aws-modules/security-group/aws",
        version="~> 5.2",
        covers=["aws_security_group", "aws_security_group_rule",
                "aws_vpc_security_group_ingress_rule", "aws_vpc_security_group_egress_rule"],
        child_addresses={
            # Verified against module source: it declares BOTH aws_security_group.this
            # and aws_security_group.this_name_prefix, gated on use_name_prefix.
            # An imported SG has a fixed name, so this[0] (use_name_prefix = false)
            # is the correct default; the prefix variant is the "name_prefix" tier.
            "aws_security_group": "module.{m}.aws_security_group.this[0]",
            "aws_security_group.name_prefix": "module.{m}.aws_security_group.this_name_prefix[0]",
        },
        inputs={
            "name": "name",
            "description": "description",
            "vpc_id": "vpc_id",
            "tags": "tags",
        },
        difficulty="hard",
        notes=(
            "Set use_name_prefix = false. An imported SG has a fixed name, and the "
            "module only uses aws_security_group.this[0] in that mode; leave the "
            "default on and it creates this_name_prefix[0] instead, destroying and "
            "recreating the group. Inline ingress/egress on the live SG must be "
            "re-expressed as the module's ingress_with_cidr_blocks / "
            "ingress_with_source_security_group_id inputs — the module manages rules "
            "as separate aws_security_group_rule objects, so any rule left inline "
            "will be fought over on every apply. Convert the rules; do not "
            "lifecycle-ignore them."
        ),
        computed=["arn", "owner_id", "id"],
    ),
    "ec2-instance": ModuleSpec(
        key="ec2-instance",
        source="terraform-aws-modules/ec2-instance/aws",
        version="~> 5.7",
        covers=["aws_instance", "aws_ebs_volume", "aws_volume_attachment"],
        child_addresses={
            # Verified against module source: this / ignore_ami / spot are three
            # distinct declarations selected by ignore_ami_changes and create_spot_instance.
            "aws_instance": "module.{m}.aws_instance.this[0]",
            "aws_instance.ignore_ami": "module.{m}.aws_instance.ignore_ami[0]",
            "aws_instance.spot": "module.{m}.aws_spot_instance_request.this[0]",
            "aws_iam_instance_profile": "module.{m}.aws_iam_instance_profile.this[0]",
            "aws_iam_role": "module.{m}.aws_iam_role.this[0]",
            "aws_eip": "module.{m}.aws_eip.this[0]",
        },
        inputs={
            "name": "tags.Name",
            "ami": "ami",
            "instance_type": "instance_type",
            "subnet_id": "subnet_id",
            "vpc_security_group_ids": "vpc_security_group_ids",
            "key_name": "key_name",
            "iam_instance_profile": "iam_instance_profile",
            "monitoring": "monitoring",
            "user_data_base64": "user_data_base64",
            "availability_zone": "availability_zone",
            "tags": "tags",
        },
        difficulty="easy",
        notes=(
            "Pass user_data_base64 (not user_data) when the state holds an already "
            "base64 value, else every plan shows a replace. root_block_device and "
            "ebs_block_device map to the module's root_block_device / ebs_block_device "
            "list inputs. NOTE: ignore_ami_changes = true switches the instance to a "
            "different declaration (aws_instance.ignore_ami[0]) — decide before you "
            "import, because changing it later moves the address and forces a "
            "destroy/recreate."
        ),
        computed=["arn", "id", "private_ip", "private_dns", "public_ip", "public_dns",
                  "primary_network_interface_id", "instance_state", "outpost_arn",
                  "password_data", "spot_instance_request_id"],
    ),
    "rds": ModuleSpec(
        key="rds",
        source="terraform-aws-modules/rds/aws",
        version="~> 6.10",
        covers=["aws_db_instance", "aws_db_subnet_group", "aws_db_parameter_group",
                "aws_db_option_group"],
        child_addresses={
            "aws_db_instance": "module.{m}.module.db_instance.aws_db_instance.this[0]",
            "aws_db_subnet_group": "module.{m}.module.db_subnet_group.aws_db_subnet_group.this[0]",
            "aws_db_parameter_group": "module.{m}.module.db_parameter_group.aws_db_parameter_group.this[0]",
            "aws_db_option_group": "module.{m}.module.db_option_group.aws_db_option_group.this[0]",
        },
        inputs={
            "identifier": "identifier",
            "engine": "engine",
            "engine_version": "engine_version",
            "instance_class": "instance_class",
            "allocated_storage": "allocated_storage",
            "max_allocated_storage": "max_allocated_storage",
            "db_name": "db_name",
            "username": "username",
            "port": "port",
            "multi_az": "multi_az",
            "storage_encrypted": "storage_encrypted",
            "kms_key_id": "kms_key_id",
            "vpc_security_group_ids": "vpc_security_group_ids",
            "backup_retention_period": "backup_retention_period",
            "backup_window": "backup_window",
            "maintenance_window": "maintenance_window",
            "deletion_protection": "deletion_protection",
            "tags": "tags",
        },
        difficulty="hard",
        notes=(
            "The rds module nests sub-modules -- addresses go through "
            "module.<name>.module.db_instance.*, not module.<name>.aws_db_instance.*. "
            "Set create_db_subnet_group / create_db_parameter_group to match what "
            "actually exists, otherwise the module tries to create duplicates. Never "
            "commit the password: use manage_master_user_password = true (Secrets "
            "Manager) or a var marked sensitive."
        ),
        computed=["arn", "endpoint", "address", "hosted_zone_id", "resource_id",
                  "status", "latest_restorable_time", "ca_cert_identifier",
                  "master_user_secret", "replicas"],
    ),
    "s3-bucket": ModuleSpec(
        key="s3-bucket",
        source="terraform-aws-modules/s3-bucket/aws",
        version="~> 4.2",
        covers=[
            "aws_s3_bucket", "aws_s3_bucket_policy", "aws_s3_bucket_versioning",
            "aws_s3_bucket_acl", "aws_s3_bucket_public_access_block",
            "aws_s3_bucket_server_side_encryption_configuration",
            "aws_s3_bucket_lifecycle_configuration", "aws_s3_bucket_cors_configuration",
            "aws_s3_bucket_logging", "aws_s3_bucket_ownership_controls",
        ],
        child_addresses={
            "aws_s3_bucket": "module.{m}.aws_s3_bucket.this[0]",
            "aws_s3_bucket_policy": "module.{m}.aws_s3_bucket_policy.this[0]",
            "aws_s3_bucket_versioning": "module.{m}.aws_s3_bucket_versioning.this[0]",
            "aws_s3_bucket_public_access_block": "module.{m}.aws_s3_bucket_public_access_block.this[0]",
            "aws_s3_bucket_server_side_encryption_configuration": "module.{m}.aws_s3_bucket_server_side_encryption_configuration.this[0]",
            "aws_s3_bucket_lifecycle_configuration": "module.{m}.aws_s3_bucket_lifecycle_configuration.this[0]",
            "aws_s3_bucket_cors_configuration": "module.{m}.aws_s3_bucket_cors_configuration.this[0]",
            "aws_s3_bucket_logging": "module.{m}.aws_s3_bucket_logging.this[0]",
            "aws_s3_bucket_ownership_controls": "module.{m}.aws_s3_bucket_ownership_controls.this[0]",
            "aws_s3_bucket_acl": "module.{m}.aws_s3_bucket_acl.this[0]",
        },
        inputs={
            "bucket": "bucket",
            "force_destroy": "force_destroy",
            "tags": "tags",
        },
        difficulty="medium",
        notes=(
            "Each S3 sub-resource is a separate object in the module and each needs "
            "its own import block, keyed by bucket name. If the live bucket has no "
            "versioning/encryption config, leave the corresponding module input unset "
            "rather than importing a non-existent object."
        ),
        computed=["arn", "bucket_domain_name", "bucket_regional_domain_name",
                  "hosted_zone_id", "region", "website_endpoint", "website_domain"],
    ),
    "iam-assumable-role": ModuleSpec(
        key="iam-assumable-role",
        source="terraform-aws-modules/iam/aws//modules/iam-assumable-role",
        version="~> 5.44",
        covers=["aws_iam_role", "aws_iam_role_policy_attachment", "aws_iam_instance_profile"],
        child_addresses={
            "aws_iam_role": "module.{m}.aws_iam_role.this[0]",
            "aws_iam_instance_profile": "module.{m}.aws_iam_instance_profile.this[0]",
            "aws_iam_role_policy_attachment": "module.{m}.aws_iam_role_policy_attachment.custom[{i}]",
        },
        inputs={
            "role_name": "name",
            "role_path": "path",
            "role_description": "description",
            "max_session_duration": "max_session_duration",
            "role_permissions_boundary_arn": "permissions_boundary",
            "tags": "tags",
        },
        difficulty="medium",
        notes=(
            "The module builds assume_role_policy from trusted_role_arns / "
            "trusted_role_services -- it will not accept the raw JSON. Decode the live "
            "assume_role_policy and split principals into those two inputs, or use "
            "iam-role-for-service-accounts-eks / iam-assumable-role-with-oidc for OIDC "
            "trusts. Inline policies need the iam-policy module plus custom_role_policy_arns."
        ),
        computed=["arn", "unique_id", "create_date", "role_last_used"],
    ),
    "iam-policy": ModuleSpec(
        key="iam-policy",
        source="terraform-aws-modules/iam/aws//modules/iam-policy",
        version="~> 5.44",
        covers=["aws_iam_policy"],
        child_addresses={"aws_iam_policy": "module.{m}.aws_iam_policy.this[0]"},
        inputs={"name": "name", "path": "path", "description": "description",
                "policy": "policy", "tags": "tags"},
        difficulty="easy",
        notes="Feed `policy` from a data.aws_iam_policy_document rather than raw JSON.",
        computed=["arn", "policy_id", "attachment_count", "default_version_id"],
    ),
    "alb": ModuleSpec(
        key="alb",
        source="terraform-aws-modules/alb/aws",
        version="~> 9.11",
        covers=["aws_lb", "aws_lb_target_group", "aws_lb_listener", "aws_lb_listener_rule",
                "aws_alb", "aws_alb_target_group", "aws_alb_listener"],
        child_addresses={
            "aws_lb": "module.{m}.aws_lb.this[0]",
            "aws_lb_target_group": "module.{m}.aws_lb_target_group.this[\"{i}\"]",
            "aws_lb_listener": "module.{m}.aws_lb_listener.this[\"{i}\"]",
            "aws_lb_listener_rule": "module.{m}.aws_lb_listener_rule.this[\"{i}\"]",
        },
        inputs={
            "name": "name",
            "load_balancer_type": "load_balancer_type",
            "vpc_id": "vpc_id",
            "subnets": "subnets",
            "internal": "internal",
            "enable_deletion_protection": "enable_deletion_protection",
            "tags": "tags",
        },
        difficulty="hard",
        notes=(
            "v9 keys target_groups and listeners by map key, so import IDs must use "
            "the same keys you choose in HCL. The module creates its own security "
            "group unless create_security_group = false."
        ),
        computed=["arn", "arn_suffix", "dns_name", "zone_id", "id"],
    ),
    "eks": ModuleSpec(
        key="eks",
        source="terraform-aws-modules/eks/aws",
        version="~> 20.31",
        covers=["aws_eks_cluster", "aws_eks_node_group", "aws_eks_addon",
                "aws_eks_fargate_profile"],
        child_addresses={
            "aws_eks_cluster": "module.{m}.aws_eks_cluster.this[0]",
            "aws_eks_node_group": "module.{m}.module.eks_managed_node_group[\"{i}\"].aws_eks_node_group.this[0]",
            "aws_eks_addon": "module.{m}.aws_eks_addon.this[\"{i}\"]",
        },
        inputs={
            "cluster_name": "name",
            "cluster_version": "version",
            "vpc_id": "vpc_config.vpc_id",
            "subnet_ids": "vpc_config.subnet_ids",
            "cluster_endpoint_public_access": "vpc_config.endpoint_public_access",
            "tags": "tags",
        },
        difficulty="hard",
        notes=(
            "v20 replaced aws-auth ConfigMap with EKS access entries; importing a v19-era "
            "cluster usually also means importing aws_eks_access_entry objects. Node "
            "groups live in a nested module keyed by node group name."
        ),
        computed=["arn", "endpoint", "certificate_authority", "identity", "status",
                  "platform_version", "cluster_id"],
    ),
    "lambda": ModuleSpec(
        key="lambda",
        source="terraform-aws-modules/lambda/aws",
        version="~> 7.20",
        covers=["aws_lambda_function", "aws_lambda_permission", "aws_lambda_alias",
                "aws_lambda_event_source_mapping", "aws_cloudwatch_log_group"],
        child_addresses={
            "aws_lambda_function": "module.{m}.aws_lambda_function.this[0]",
            "aws_lambda_permission": "module.{m}.aws_lambda_permission.current_version_triggers[\"{i}\"]",
            "aws_cloudwatch_log_group": "module.{m}.aws_cloudwatch_log_group.lambda[0]",
            "aws_iam_role": "module.{m}.aws_iam_role.lambda[0]",
        },
        inputs={
            "function_name": "function_name",
            "handler": "handler",
            "runtime": "runtime",
            "memory_size": "memory_size",
            "timeout": "timeout",
            "architectures": "architectures",
            "tags": "tags",
        },
        difficulty="hard",
        notes=(
            "The module builds and uploads the deployment package. For an imported "
            "function set create_package = false and point at the existing S3 object "
            "or local artifact, otherwise every plan re-uploads and shows a diff on "
            "source_code_hash."
        ),
        computed=["arn", "invoke_arn", "qualified_arn", "version", "last_modified",
                  "source_code_hash", "source_code_size", "signing_job_arn",
                  "signing_profile_version_arn"],
    ),
    "dynamodb-table": ModuleSpec(
        key="dynamodb-table",
        source="terraform-aws-modules/dynamodb-table/aws",
        version="~> 4.2",
        covers=["aws_dynamodb_table"],
        child_addresses={"aws_dynamodb_table": "module.{m}.aws_dynamodb_table.this[0]"},
        inputs={"name": "name", "hash_key": "hash_key", "range_key": "range_key",
                "billing_mode": "billing_mode", "read_capacity": "read_capacity",
                "write_capacity": "write_capacity", "tags": "tags"},
        difficulty="easy",
        notes="attribute blocks must list exactly the key attributes, no more.",
        computed=["arn", "id", "stream_arn", "stream_label"],
    ),
    "sqs": ModuleSpec(
        key="sqs",
        source="terraform-aws-modules/sqs/aws",
        version="~> 4.2",
        covers=["aws_sqs_queue", "aws_sqs_queue_policy"],
        child_addresses={
            "aws_sqs_queue": "module.{m}.aws_sqs_queue.this[0]",
            "aws_sqs_queue_policy": "module.{m}.aws_sqs_queue_policy.this[0]",
        },
        inputs={"name": "name", "fifo_queue": "fifo_queue",
                "visibility_timeout_seconds": "visibility_timeout_seconds",
                "message_retention_seconds": "message_retention_seconds", "tags": "tags"},
        difficulty="easy",
        notes="The dead-letter queue is a second module call, not an input.",
        computed=["arn", "url", "id"],
    ),
    "sns": ModuleSpec(
        key="sns",
        source="terraform-aws-modules/sns/aws",
        version="~> 6.1",
        covers=["aws_sns_topic", "aws_sns_topic_policy", "aws_sns_topic_subscription"],
        child_addresses={
            "aws_sns_topic": "module.{m}.aws_sns_topic.this[0]",
            "aws_sns_topic_policy": "module.{m}.aws_sns_topic_policy.this[0]",
            "aws_sns_topic_subscription": "module.{m}.aws_sns_topic_subscription.this[\"{i}\"]",
        },
        inputs={"name": "name", "display_name": "display_name",
                "kms_master_key_id": "kms_master_key_id", "tags": "tags"},
        difficulty="easy",
        notes="",
        computed=["arn", "id", "owner"],
    ),
    "kms": ModuleSpec(
        key="kms",
        source="terraform-aws-modules/kms/aws",
        version="~> 3.1",
        covers=["aws_kms_key", "aws_kms_alias"],
        child_addresses={
            "aws_kms_key": "module.{m}.aws_kms_key.this[0]",
            "aws_kms_alias": "module.{m}.aws_kms_alias.this[\"{i}\"]",
        },
        inputs={"description": "description", "key_usage": "key_usage",
                "deletion_window_in_days": "deletion_window_in_days",
                "enable_key_rotation": "enable_key_rotation", "tags": "tags"},
        difficulty="medium",
        notes="Aliases are keyed by alias name without the 'alias/' prefix.",
        computed=["arn", "key_id", "id"],
    ),
    "acm": ModuleSpec(
        key="acm",
        source="terraform-aws-modules/acm/aws",
        version="~> 5.1",
        covers=["aws_acm_certificate", "aws_acm_certificate_validation"],
        child_addresses={
            "aws_acm_certificate": "module.{m}.aws_acm_certificate.this[0]",
            "aws_acm_certificate_validation": "module.{m}.aws_acm_certificate_validation.this[0]",
        },
        inputs={"domain_name": "domain_name", "validation_method": "validation_method",
                "tags": "tags"},
        difficulty="medium",
        notes="For an already-validated cert set create_route53_records = false and "
              "validate_certificate = false to avoid re-running validation.",
        computed=["arn", "id", "status", "domain_validation_options",
                  "validation_emails", "not_before", "not_after"],
    ),
    "route53": ModuleSpec(
        key="route53",
        source="terraform-aws-modules/route53/aws//modules/zones",
        version="~> 4.1",
        covers=["aws_route53_zone", "aws_route53_record"],
        child_addresses={
            "aws_route53_zone": "module.{m}.aws_route53_zone.this[\"{i}\"]",
            "aws_route53_record": "module.{m}.aws_route53_record.this[\"{i}\"]",
        },
        inputs={"zones": "name", "tags": "tags"},
        difficulty="medium",
        notes="Records live in the sibling //modules/records module, keyed by "
              "'<name> <type>'. Import IDs are 'ZONEID_name_TYPE'.",
        computed=["zone_id", "name_servers", "arn"],
    ),
    "ecs": ModuleSpec(
        key="ecs",
        source="terraform-aws-modules/ecs/aws",
        version="~> 5.11",
        covers=["aws_ecs_cluster", "aws_ecs_service", "aws_ecs_task_definition",
                "aws_ecs_capacity_provider"],
        child_addresses={
            "aws_ecs_cluster": "module.{m}.aws_ecs_cluster.this[0]",
            "aws_ecs_service": "module.{m}.module.service[\"{i}\"].aws_ecs_service.this[0]",
            "aws_ecs_task_definition": "module.{m}.module.service[\"{i}\"].aws_ecs_task_definition.this[0]",
        },
        inputs={"cluster_name": "name", "tags": "tags"},
        difficulty="hard",
        notes="Task definitions are versioned; importing pins revision N and the "
              "module will immediately plan revision N+1 unless the container "
              "definition matches byte-for-byte. Expect one intentional revision bump.",
        computed=["arn", "id", "revision"],
    ),
    "autoscaling": ModuleSpec(
        key="autoscaling",
        source="terraform-aws-modules/autoscaling/aws",
        version="~> 8.1",
        covers=["aws_autoscaling_group", "aws_launch_template", "aws_autoscaling_policy"],
        child_addresses={
            "aws_autoscaling_group": "module.{m}.aws_autoscaling_group.this[0]",
            "aws_launch_template": "module.{m}.aws_launch_template.this[0]",
            "aws_autoscaling_policy": "module.{m}.aws_autoscaling_policy.this[\"{i}\"]",
        },
        inputs={"name": "name", "min_size": "min_size", "max_size": "max_size",
                "desired_capacity": "desired_capacity",
                "vpc_zone_identifier": "vpc_zone_identifier", "tags": "tags"},
        difficulty="hard",
        notes="Launch configurations are EOL -- terraformer may emit "
              "aws_launch_configuration, which has no module equivalent and must be "
              "rewritten as a launch template (that is a real replace, not drift).",
        computed=["arn", "id", "latest_version", "default_version"],
    ),
    "elasticache": ModuleSpec(
        key="elasticache",
        source="terraform-aws-modules/elasticache/aws",
        version="~> 1.4",
        covers=["aws_elasticache_cluster", "aws_elasticache_replication_group",
                "aws_elasticache_subnet_group", "aws_elasticache_parameter_group"],
        child_addresses={
            "aws_elasticache_cluster": "module.{m}.aws_elasticache_cluster.this[0]",
            "aws_elasticache_replication_group": "module.{m}.aws_elasticache_replication_group.this[0]",
            "aws_elasticache_subnet_group": "module.{m}.aws_elasticache_subnet_group.this[0]",
        },
        inputs={"cluster_id": "cluster_id", "engine": "engine",
                "engine_version": "engine_version", "node_type": "node_type",
                "tags": "tags"},
        difficulty="medium",
        notes="",
        computed=["arn", "id", "configuration_endpoint_address", "primary_endpoint_address",
                  "reader_endpoint_address", "cache_nodes", "member_clusters"],
    ),
    "cloudfront": ModuleSpec(
        key="cloudfront",
        source="terraform-aws-modules/cloudfront/aws",
        version="~> 3.4",
        covers=["aws_cloudfront_distribution", "aws_cloudfront_origin_access_identity",
                "aws_cloudfront_origin_access_control"],
        child_addresses={
            "aws_cloudfront_distribution": "module.{m}.aws_cloudfront_distribution.this[0]",
            "aws_cloudfront_origin_access_identity": "module.{m}.aws_cloudfront_origin_access_identity.this[\"{i}\"]",
            "aws_cloudfront_origin_access_control": "module.{m}.aws_cloudfront_origin_access_control.this[\"{i}\"]",
        },
        inputs={"comment": "comment", "enabled": "enabled", "aliases": "aliases",
                "price_class": "price_class", "tags": "tags"},
        difficulty="hard",
        notes="Origins are keyed by origin_id; ordered_cache_behavior order is "
              "significant and a reorder is a real change, not drift.",
        computed=["arn", "id", "domain_name", "etag", "hosted_zone_id",
                  "last_modified_time", "status", "caller_reference", "trusted_key_groups"],
    ),
    "efs": ModuleSpec(
        key="efs",
        source="terraform-aws-modules/efs/aws",
        version="~> 1.6",
        covers=["aws_efs_file_system", "aws_efs_mount_target", "aws_efs_access_point",
                "aws_efs_backup_policy"],
        child_addresses={
            "aws_efs_file_system": "module.{m}.aws_efs_file_system.this[0]",
            "aws_efs_mount_target": "module.{m}.aws_efs_mount_target.this[\"{i}\"]",
            "aws_efs_access_point": "module.{m}.aws_efs_access_point.this[\"{i}\"]",
        },
        inputs={"name": "creation_token", "encrypted": "encrypted",
                "kms_key_arn": "kms_key_id", "performance_mode": "performance_mode",
                "tags": "tags"},
        difficulty="medium",
        notes="Mount targets are keyed by subnet id.",
        computed=["arn", "id", "dns_name", "size_in_bytes", "number_of_mount_targets",
                  "owner_id", "availability_zone_id"],
    ),
}

#: raw resource type -> catalog key
_TYPE_INDEX: dict[str, list[str]] = {}
for _spec in CATALOG.values():
    for _t in _spec.covers:
        _TYPE_INDEX.setdefault(_t, []).append(_spec.key)


# ---------------------------------------------------------------------------
# Drift taxonomy
#
# v2 lumped real configuration attributes (vpc_id, subnet_id, password,
# allocated_storage, security_groups) in with provider-computed ones, so genuine
# drift -- an instance moving subnet -- was reported as benign. These sets are
# deliberately narrow: only attributes AWS itself assigns.
# ---------------------------------------------------------------------------

#: Attributes the provider always computes. Safe to ignore in a diff.
GLOBAL_COMPUTED = {
    "arn", "id", "unique_id", "owner_id", "caller_reference",
    "create_date", "created_at", "creation_date", "last_modified", "last_modified_time",
    "etag", "status", "state", "arn_suffix",
    "dns_name", "domain_name", "endpoint", "address", "hosted_zone_id", "zone_id",
    "public_dns", "public_ip", "private_dns", "primary_network_interface_id",
    "network_interface_id", "association_id", "allocation_id",
    "invoke_arn", "qualified_arn", "source_code_size",
    "bucket_domain_name", "bucket_regional_domain_name", "website_endpoint",
    "website_domain", "region",
    "default_route_table_id", "default_network_acl_id", "default_security_group_id",
    "main_route_table_id", "availability_zone_id", "dhcp_options_id",
    "resource_id", "dbi_resource_id", "latest_restorable_time", "ca_cert_identifier",
    "name_servers", "certificate_authority", "platform_version", "identity",
    "policy_id", "default_version_id", "attachment_count",
    "stream_label", "stream_arn", "key_id", "version_id",
}

#: Attributes that look computed but are real configuration. Never auto-ignore.
NEVER_IGNORE = {
    "cidr_block", "vpc_id", "subnet_id", "subnet_ids", "vpc_security_group_ids",
    "security_groups", "instance_type", "ami", "engine", "engine_version",
    "instance_class", "allocated_storage", "iam_instance_profile", "key_name",
    "user_data", "user_data_base64", "policy", "assume_role_policy",
    "ingress", "egress", "deletion_protection", "storage_encrypted",
    "publicly_accessible", "multi_az", "backup_retention_period", "kms_key_id",
    "acl", "versioning", "runtime", "handler", "memory_size", "timeout",
    "desired_capacity", "min_size", "max_size", "port", "protocol",
    # v2 classified `password` as computed, so a changed DB password read as
    # benign drift. It is stored in state and a diff on it is real.
    "password", "master_password", "availability_zone", "cidr_blocks",
}

TAG_ATTRS = {"tags", "tags_all"}

#: Attributes that are timestamps/versions AWS bumps on its own.
VOLATILE = {
    "source_code_hash", "version", "latest_version", "default_version", "revision",
    "last_used", "role_last_used", "password_data", "master_user_secret",
}


# ---------------------------------------------------------------------------
# Terraformer output ingestion
# ---------------------------------------------------------------------------


@dataclass
class RawResource:
    type: str
    name: str  # terraformer's name, e.g. tfer--vpc-002D-01
    clean_name: str
    id: str
    attributes: dict
    source_file: str
    service: str

    def as_dict(self) -> dict:
        return {
            "type": self.type,
            "name": self.name,
            "clean_name": self.clean_name,
            "id": self.id,
            "service": self.service,
            "source_file": self.source_file,
            "attributes": self.attributes,
        }


def _load_state_resources(state_path: Path) -> list[dict]:
    """Read a terraform state file into a flat list of managed resource instances."""
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for res in state.get("resources", []):
        if res.get("mode") != "managed":
            continue
        for inst in res.get("instances", []):
            attrs = inst.get("attributes") or {}
            out.append(
                {
                    "type": res.get("type", ""),
                    "name": res.get("name", ""),
                    "index_key": inst.get("index_key"),
                    "id": str(attrs.get("id", "")),
                    "attributes": attrs,
                }
            )
    return out


def _scan_terraformer_tree(root: Path) -> list[RawResource]:
    """Walk a terraformer output tree, preferring tfstate over HCL for values."""
    resources: list[RawResource] = []
    seen: set[tuple[str, str]] = set()

    for state_path in sorted(root.rglob("terraform.tfstate")):
        service = state_path.parent.name
        rel = str(state_path.relative_to(root))
        for item in _load_state_resources(state_path):
            key = (item["type"], item["name"])
            if key in seen:
                continue
            seen.add(key)
            resources.append(
                RawResource(
                    type=item["type"],
                    name=item["name"],
                    clean_name=_sanitize_name(item["name"]),
                    id=item["id"],
                    attributes=item["attributes"],
                    source_file=rel,
                    service=service,
                )
            )

    # HCL fallback for anything not represented in state
    for tf_path in sorted(root.rglob("*.tf")):
        if ".terraform" in tf_path.parts:
            continue
        try:
            text = tf_path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(tf_path.relative_to(root))
        for block in _iter_blocks(text):
            if block.kind != "resource" or len(block.labels) < 2:
                continue
            key = (block.labels[0], block.labels[1])
            if key in seen:
                continue
            seen.add(key)
            attrs = {k: _unquote(v) for k, v in _parse_attributes(block.text).items()}
            resources.append(
                RawResource(
                    type=block.labels[0],
                    name=block.labels[1],
                    clean_name=_sanitize_name(block.labels[1]),
                    id="",  # not present in terraformer HCL
                    attributes=attrs,
                    source_file=rel,
                    service=tf_path.parent.name,
                )
            )

    return resources


def _workspace_inventory(ws: Path) -> list[RawResource]:
    """Inventory the ingested raw tree inside a workspace."""
    raw_root = ws / "raw"
    if not raw_root.is_dir():
        raw_root = ws
    return _scan_terraformer_tree(raw_root)


def _dig(attrs: dict, dotted: str) -> Any:
    """Fetch ``a.b.c`` out of nested dicts/single-element lists."""
    cur: Any = attrs
    for part in dotted.split("."):
        if isinstance(cur, list) and len(cur) == 1:
            cur = cur[0]
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


# ---------------------------------------------------------------------------
# Project scaffolding templates
# ---------------------------------------------------------------------------

_GITIGNORE = """\
.terraform/
.terraform.lock.hcl.bak
*.tfstate
*.tfstate.*
*.tfplan
plan.out
crash.log
crash.*.log
override.tf
override.tf.json
*_override.tf
*_override.tf.json
.terraformrc
terraform.rc
*.auto.tfvars
!example.auto.tfvars
.env
"""

_PRECOMMIT = """\
repos:
  - repo: https://github.com/antonbabenko/pre-commit-terraform
    rev: v1.96.2
    hooks:
      - id: terraform_fmt
      - id: terraform_validate
      - id: terraform_docs
        args: ["--hook-config=--path-to-file=README.md", "--hook-config=--add-to-existing-file=true"]
      - id: terraform_tflint
      - id: terraform_trivy
        args: ["--args=--severity=HIGH,CRITICAL"]
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: end-of-file-fixer
      - id: trailing-whitespace
      - id: check-merge-conflict
      - id: detect-private-key
"""

_MAKEFILE = """\
ENV ?= dev
DIR := envs/$(ENV)

.PHONY: init fmt validate plan apply lint sec docs

init:
\tterraform -chdir=$(DIR) init -input=false

fmt:
\tterraform fmt -recursive

validate: init
\tterraform -chdir=$(DIR) validate

plan: init
\tterraform -chdir=$(DIR) plan -input=false -out=plan.out

apply:
\tterraform -chdir=$(DIR) apply -input=false plan.out

lint:
\ttflint --recursive

sec:
\ttrivy config --severity HIGH,CRITICAL .

docs:
\tterraform-docs markdown table --output-file README.md --output-mode inject $(DIR)
"""

_TFLINT = """\
plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.32.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}
"""


def _versions_tf(tf_version: str, aws_version: str) -> str:
    return f"""\
terraform {{
  required_version = "{tf_version}"

  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "{aws_version}"
    }}
  }}
}}
"""


def _backend_tf(bucket: str, key: str, region: str, dynamodb_table: Optional[str]) -> str:
    lock = (
        f'    dynamodb_table = "{dynamodb_table}"\n' if dynamodb_table else
        "    use_lockfile   = true\n"
    )
    return f"""\
terraform {{
  backend "s3" {{
    bucket         = "{bucket}"
    key            = "{key}"
    region         = "{region}"
    encrypt        = true
{lock}  }}
}}
"""


def _providers_tf(region_var: str = "var.region") -> str:
    return f"""\
provider "aws" {{
  region = {region_var}

  default_tags {{
    tags = local.common_tags
  }}
}}
"""


def _variables_tf(project: str, environment: str, region: str) -> str:
    return f"""\
variable "project" {{
  description = "Project identifier, used as a name prefix for all resources."
  type        = string
  default     = "{project}"
}}

variable "environment" {{
  description = "Deployment environment (dev/staging/prod)."
  type        = string
  default     = "{environment}"

  validation {{
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }}
}}

variable "region" {{
  description = "AWS region for all regional resources."
  type        = string
  default     = "{region}"
}}

variable "owner" {{
  description = "Team or individual accountable for this stack."
  type        = string
  default     = "platform"
}}

variable "cost_center" {{
  description = "Cost allocation tag value."
  type        = string
  default     = "unassigned"
}}

variable "additional_tags" {{
  description = "Extra tags merged into every resource."
  type        = map(string)
  default     = {{}}
}}
"""


_LOCALS_TF = """\
locals {
  name_prefix = "${var.project}-${var.environment}"

  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Owner       = var.owner
      CostCenter  = var.cost_center
      ManagedBy   = "terraform"
    },
    var.additional_tags,
  )
}
"""


def _readme(project: str, environment: str, region: str) -> str:
    return f"""\
# {project} — Terraform

Imported from live AWS with `terraformer`, refactored onto the
[terraform-aws-modules](https://github.com/terraform-aws-modules) community modules,
and adopted into state with `import` blocks.

## Layout

```
envs/{environment}/   root module for the {environment} environment ({region})
  main.tf             community module calls
  imports.tf          import blocks adopting the pre-existing objects
  variables.tf        inputs
  locals.tf           naming + common tags
  versions.tf         terraform + provider version pins
  backend.tf          remote state
  outputs.tf          exported values
raw/                  untouched terraformer output — reference only, never applied
```

## Adoption procedure

The `import` blocks in `imports.tf` adopt objects that already exist. They are
idempotent but single-use: once `terraform apply` has run and the objects are in
state, delete `imports.tf`.

```bash
make -e ENV={environment} init
make -e ENV={environment} plan     # MUST show 0 to add, 0 to change, 0 to destroy
make -e ENV={environment} apply
```

A plan that proposes **any** destroy or replace means an import block is pointing
at the wrong address, or a module input does not match reality. Fix the code —
never apply through it.

## Before this is production ready

- [ ] `backend.tf` points at a real, versioned, encrypted state bucket
- [ ] no credentials in HCL or tfvars (`grep -riE 'password|secret|token' *.tf`)
- [ ] every module call pins `version`
- [ ] `.terraform.lock.hcl` committed
- [ ] `make sec` and `make lint` clean
- [ ] `terraform plan` is empty on a fresh clone
"""


# ---------------------------------------------------------------------------
# Tools: ingest & inspect
# ---------------------------------------------------------------------------


@mcp.tool()
def ingest_terraformer(
    workspace_dir: str,
    source_dir: str,
    overwrite: bool = False,
) -> str:
    """Copy a terraformer output directory into a workspace as read-only source
    material, then inventory it.

    This is the entry point: point it at whatever directory terraformer wrote
    (the one containing ``generated/aws/<service>/`` or the ``<service>/``
    directories themselves) and it lands under ``<workspace>/raw/``.

    Args:
        workspace_dir: Workspace name or path under the server's workspace root.
        source_dir: Absolute path to the terraformer output. Read-only; never
            modified. Restricted by TF_MCP_SOURCE_ROOTS if that env var is set.
        overwrite: Replace an existing ``raw/`` directory instead of refusing.

    Returns:
        Counts by resource type, the services found, how many resources carry a
        real ID from state, and anything the catalog cannot map yet.
    """
    try:
        ws = _resolve_workspace(workspace_dir)
        src = _resolve_source(source_dir)
    except ValueError as exc:
        return _err(str(exc))

    raw_root = ws / "raw"
    if raw_root.exists():
        if not overwrite:
            return _err(
                f"{raw_root} already exists. Pass overwrite=true to replace it.",
                existing_files=len(list(raw_root.rglob("*"))),
            )
        shutil.rmtree(raw_root)

    ws.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        src, raw_root, ignore=shutil.ignore_patterns(".terraform", ".git", "*.tfplan")
    )

    resources = _scan_terraformer_tree(raw_root)
    by_type: dict[str, int] = {}
    for r in resources:
        by_type[r.type] = by_type.get(r.type, 0) + 1

    with_ids = sum(1 for r in resources if r.id)
    unmapped = sorted({r.type for r in resources if r.type not in _TYPE_INDEX})

    return _ok(
        workspace=str(ws),
        raw_root=str(raw_root),
        source=str(src),
        services=sorted({r.service for r in resources}),
        resource_count=len(resources),
        resources_with_state_id=with_ids,
        by_type=dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        unmapped_types=unmapped,
        warnings=(
            []
            if with_ids == len(resources)
            else [
                f"{len(resources) - with_ids} resources have no ID because no "
                f"terraform.tfstate was found beside their .tf files. Import blocks "
                f"for those cannot be generated automatically — re-run terraformer "
                f"without --compact-state, or supply IDs manually."
            ]
        ),
        next_step="Call plan_conversion to see the module grouping.",
    )


@mcp.tool()
def inventory(
    workspace_dir: str,
    resource_type: Optional[str] = None,
    name_glob: Optional[str] = None,
    include_attributes: bool = False,
    limit: int = MAX_ITEMS,
) -> str:
    """List the ingested raw resources with their real IDs, read from tfstate.

    Args:
        workspace_dir: Workspace containing an ingested ``raw/`` tree.
        resource_type: Filter to one type, e.g. "aws_instance".
        name_glob: Filter names with a glob, e.g. "prod-*".
        include_attributes: Include full attribute maps. Large — leave off until
            you have narrowed to a handful of resources.
        limit: Max resources returned.
    """
    try:
        ws = _resolve_workspace(workspace_dir)
    except ValueError as exc:
        return _err(str(exc))

    resources = _workspace_inventory(ws)
    if resource_type:
        resources = [r for r in resources if r.type == resource_type]
    if name_glob:
        resources = [
            r for r in resources
            if fnmatch.fnmatch(r.name, name_glob) or fnmatch.fnmatch(r.clean_name, name_glob)
        ]

    total = len(resources)
    resources = resources[:limit]
    items = []
    for r in resources:
        d = r.as_dict()
        if not include_attributes:
            d.pop("attributes")
            d["attribute_keys"] = sorted(r.attributes)[:60]
        items.append(d)

    return _ok(total=total, returned=len(items), resources=items)


@mcp.tool()
def catalog_lookup(
    resource_type: Optional[str] = None,
    module_key: Optional[str] = None,
    difficulty: Optional[str] = None,
) -> str:
    """Look up which community module owns a raw resource type, and the exact
    in-module addresses its objects live at.

    Args:
        resource_type: Raw type, e.g. "aws_db_instance".
        module_key: Catalog key, e.g. "rds".
        difficulty: One of easy/medium/hard.
    """
    keys: Iterable[str]
    if resource_type:
        keys = _TYPE_INDEX.get(resource_type, [])
        if not keys:
            return _ok(
                matches={},
                note=f"No community module mapped for {resource_type}. Keep it as a "
                     f"plain resource in the root module — that is a legitimate "
                     f"outcome, not every resource belongs in a module.",
            )
    elif module_key:
        keys = [module_key] if module_key in CATALOG else []
    else:
        keys = CATALOG.keys()

    out = {
        k: CATALOG[k].as_dict()
        for k in keys
        if k in CATALOG and (not difficulty or CATALOG[k].difficulty == difficulty)
    }
    return _ok(matches=out, count=len(out))


@mcp.tool()
def registry_lookup(namespace: str, name: str, provider: str = "aws") -> str:
    """Query the public Terraform Registry for a module's latest version.

    Use this before committing to the version pins in the catalog — they are
    accurate as of authoring but the registry moves. Requires outbound HTTPS to
    registry.terraform.io; fails soft if there is no network.

    Args:
        namespace: e.g. "terraform-aws-modules".
        name: e.g. "vpc".
        provider: e.g. "aws".
    """
    url = f"https://registry.terraform.io/v1/modules/{namespace}/{name}/{provider}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return _err(f"registry lookup failed: {exc}", url=url,
                    hint="Offline or blocked; fall back to the catalog's pinned version.")

    versions = data.get("versions", [])
    return _ok(
        source=f"{namespace}/{name}/{provider}",
        latest=data.get("version"),
        published_at=data.get("published_at"),
        recent_versions=versions[-15:] if isinstance(versions, list) else versions,
        suggested_pin=f"~> {'.'.join(str(data.get('version', '')).split('.')[:2])}",
    )


@mcp.tool()
def plan_conversion(workspace_dir: str) -> str:
    """Group the ingested inventory into community module calls and report gaps.

    Produces the conversion plan: which module call each raw resource should end
    up inside, in dependency order, plus the resources no module covers and the
    ones with no importable ID.

    Args:
        workspace_dir: Workspace containing an ingested ``raw/`` tree.
    """
    try:
        ws = _resolve_workspace(workspace_dir)
    except ValueError as exc:
        return _err(str(exc))

    resources = _workspace_inventory(ws)
    if not resources:
        return _err("No resources found. Run ingest_terraformer first.")

    groups: dict[str, dict] = {}
    unmapped: list[dict] = []
    no_id: list[dict] = []

    for r in resources:
        if not r.id:
            no_id.append({"type": r.type, "name": r.name, "file": r.source_file})
        keys = _TYPE_INDEX.get(r.type)
        if not keys:
            unmapped.append(
                {"type": r.type, "name": r.clean_name, "id": r.id, "file": r.source_file}
            )
            continue
        key = keys[0]
        g = groups.setdefault(
            key,
            {
                "module_key": key,
                "source": CATALOG[key].source,
                "version": CATALOG[key].version,
                "difficulty": CATALOG[key].difficulty,
                "notes": CATALOG[key].notes,
                "members": [],
            },
        )
        g["members"].append(
            {"type": r.type, "name": r.clean_name, "id": r.id, "raw_name": r.name}
        )

    # Rough dependency order: network first, identity next, then workloads.
    order = [
        "vpc", "kms", "iam-policy", "iam-assumable-role", "security-group", "acm",
        "route53", "s3-bucket", "dynamodb-table", "sqs", "sns", "efs", "elasticache",
        "rds", "alb", "autoscaling", "ec2-instance", "lambda", "ecs", "eks",
        "cloudfront",
    ]
    ordered = sorted(
        groups.values(),
        key=lambda g: (order.index(g["module_key"]) if g["module_key"] in order else 99),
    )
    for g in ordered:
        g["member_count"] = len(g["members"])

    hard = [g["module_key"] for g in ordered if g["difficulty"] == "hard"]

    return _ok(
        module_groups=ordered,
        apply_order=[g["module_key"] for g in ordered],
        unmapped_resources=unmapped[:MAX_ITEMS],
        unmapped_count=len(unmapped),
        resources_without_id=no_id[:MAX_ITEMS],
        needs_care=hard,
        guidance=[
            "Work one module group at a time, in apply_order, and reach a zero-diff "
            "plan before starting the next. Batching them makes failures unattributable.",
            "unmapped_resources are not failures — keep them as plain resources in the "
            "root module. A community module you have to fight is worse than a resource.",
            f"{len(hard)} group(s) are marked hard: {hard}. Read their catalog notes "
            f"before generating anything; their in-module addresses are conditional.",
        ],
        next_step="scaffold_project, then emit_module_call per group.",
    )


# ---------------------------------------------------------------------------
# Tools: scaffolding & code generation
# ---------------------------------------------------------------------------


@mcp.tool()
def scaffold_project(
    workspace_dir: str,
    project: str,
    environment: str = "prod",
    region: str = "eu-central-1",
    terraform_version: str = ">= 1.9",
    aws_provider_version: str = "~> 5.70",
    backend_bucket: Optional[str] = None,
    backend_key: Optional[str] = None,
    backend_dynamodb_table: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Write the production repo skeleton: env root module, version pins,
    provider with default_tags, locals, variables, gitignore, pre-commit,
    tflint, Makefile, README.

    Args:
        workspace_dir: Target workspace.
        project: Project slug, used as the name prefix and in tags.
        environment: dev/staging/prod. Determines ``envs/<environment>/``.
        region: Default AWS region.
        terraform_version: Constraint for required_version.
        aws_provider_version: Constraint for the aws provider.
        backend_bucket: S3 bucket for remote state. Omitted means no backend.tf
            is written and state stays local — acceptable while converging, not
            acceptable for production.
        backend_key: State key. Defaults to "<project>/<environment>/terraform.tfstate".
        backend_dynamodb_table: Legacy DynamoDB lock table. Omit to use S3 native
            locking (use_lockfile), which is preferred on modern providers.
        overwrite: Overwrite files that already exist.
    """
    try:
        ws = _resolve_workspace(workspace_dir)
    except ValueError as exc:
        return _err(str(exc))

    env_dir = ws / "envs" / environment
    env_dir.mkdir(parents=True, exist_ok=True)

    files: dict[Path, str] = {
        env_dir / "versions.tf": _versions_tf(terraform_version, aws_provider_version),
        env_dir / "providers.tf": _providers_tf(),
        env_dir / "variables.tf": _variables_tf(project, environment, region),
        env_dir / "locals.tf": _LOCALS_TF,
        env_dir / "main.tf": (
            "# Community module calls live here.\n"
            "# Generate them with emit_module_call, one group at a time,\n"
            "# in the order returned by plan_conversion.\n"
        ),
        env_dir / "outputs.tf": "# Export what other stacks consume. Keep it minimal.\n",
        ws / ".gitignore": _GITIGNORE,
        ws / ".pre-commit-config.yaml": _PRECOMMIT,
        ws / ".tflint.hcl": _TFLINT,
        ws / "Makefile": _MAKEFILE,
        ws / "README.md": _readme(project, environment, region),
    }

    if backend_bucket:
        key = backend_key or f"{project}/{environment}/terraform.tfstate"
        files[env_dir / "backend.tf"] = _backend_tf(
            backend_bucket, key, region, backend_dynamodb_table
        )

    written, skipped = [], []
    for path, content in files.items():
        if path.exists() and not overwrite:
            skipped.append(str(path.relative_to(ws)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        written.append(str(path.relative_to(ws)))

    warnings = []
    if not backend_bucket:
        warnings.append(
            "No backend configured — state will be local. Set backend_bucket before "
            "calling this production ready."
        )

    return _ok(
        workspace=str(ws),
        env_dir=str(env_dir.relative_to(ws)),
        written=sorted(written),
        skipped=sorted(skipped),
        warnings=warnings,
    )


@mcp.tool()
def emit_module_call(
    workspace_dir: str,
    module_key: str,
    module_call_name: str,
    resource_names: list[str],
    relative_path: Optional[str] = None,
    extra_inputs: Optional[dict] = None,
    tiers: Optional[dict] = None,
    address_overrides: Optional[dict] = None,
    append: bool = True,
) -> str:
    """Generate a community module call from real state values, plus the exact
    import blocks that adopt the live objects into it.

    Import indices are assigned deterministically — resources are sorted by
    availability zone, then CIDR, then name — because the community modules take
    positional lists (``private_subnets``, ``azs``). Build those input lists in
    the same order and the indices line up; assign them in discovery order, as a
    naive generator does, and you silently import subnet A onto subnet B.

    Args:
        workspace_dir: Workspace with an ingested ``raw/`` tree.
        module_key: Catalog key, e.g. "vpc" or "rds".
        module_call_name: HCL name for the call, e.g. "vpc" -> module.vpc.
        resource_names: Raw or cleaned resource names from ``inventory`` that
            this call should absorb.
        relative_path: Where to write, e.g. "envs/prod/main.tf". If omitted,
            nothing is written and the HCL is only returned for review.
        extra_inputs: Inputs to add or override, e.g.
            {"azs": 'data.aws_availability_zones.available.names'}. Values that
            look like HCL expressions are emitted unquoted.
        tiers: For types the module splits by tier, map each resource name to
            its tier, e.g. {"prod_private_a": "private", "prod_public_a": "public"}.
            Required for VPC subnets and route tables — the module keeps public,
            private, database and intra subnets in separate lists and only you
            know which is which.
        address_overrides: Escape hatch mapping a resource name to a literal
            import address, for anything the catalog cannot express.
        append: Append to the file rather than replacing it.

    Returns:
        The module HCL, the import blocks with real IDs, the ordered index
        assignment (so you can build matching input lists), and every attribute
        the catalog did not map so you can decide what still needs expressing.
    """
    try:
        ws = _resolve_workspace(workspace_dir)
    except ValueError as exc:
        return _err(str(exc))

    spec = CATALOG.get(module_key)
    if not spec:
        return _err(
            f"Unknown module_key {module_key!r}.", known=sorted(CATALOG),
        )

    wanted = set(resource_names)
    matched = [
        r for r in _workspace_inventory(ws)
        if r.name in wanted or r.clean_name in wanted
    ]
    if not matched:
        return _err(
            "None of resource_names matched the inventory.",
            requested=resource_names,
            hint="Call inventory to see available names.",
        )

    # The primary resource drives the module inputs: the covered type listed
    # first in the spec, if present.
    primary = next(
        (r for t in spec.covers for r in matched if r.type == t), matched[0]
    )

    inputs: dict[str, Any] = {}
    for input_name, attr_path in spec.inputs.items():
        value = _dig(primary.attributes, attr_path)
        if value in (None, "", [], {}):
            continue
        inputs[input_name] = value

    # tags always route through the shared locals
    if "tags" in spec.inputs:
        inputs["tags"] = "local.common_tags"

    if extra_inputs:
        inputs.update(extra_inputs)

    lines = [f'module "{module_call_name}" {{']
    lines.append(f'  source  = "{spec.source}"')
    lines.append(f'  version = "{spec.version}"')
    lines.append("")
    width = max((len(k) for k in inputs), default=0)
    for k, v in inputs.items():
        lines.append(f"  {k:<{width}} = {_hcl_literal(v)}")
    lines.append("}")
    module_hcl = "\n".join(lines) + "\n"

    # -- import blocks ----------------------------------------------------
    # Sort deterministically so index assignment is reproducible and matches a
    # correspondingly sorted input list (azs / private_subnets / ...).
    tiers = tiers or {}
    address_overrides = address_overrides or {}

    def sort_key(r: RawResource) -> tuple:
        a = r.attributes
        return (
            str(a.get("availability_zone") or a.get("availability_zone_id") or ""),
            str(a.get("cidr_block") or ""),
            r.clean_name,
        )

    imports: list[dict] = []
    unresolved: list[dict] = []
    ordering: dict[str, list[dict]] = {}
    counters: dict[str, int] = {}

    def lookup(table: dict, r: RawResource) -> Optional[str]:
        """Match by any identifier a caller might reasonably use: the cleaned
        name, terraformer's raw name, the Name tag, or the resource ID."""
        tag_name = (r.attributes.get("tags") or {}).get("Name") if isinstance(
            r.attributes.get("tags"), dict
        ) else None
        for key in (r.clean_name, r.name, tag_name, r.id):
            if key and key in table:
                return table[key]
        return None

    for r in sorted(matched, key=sort_key):
        tier = lookup(tiers, r)
        slot = f"{r.type}.{tier}" if tier else r.type

        override = lookup(address_overrides, r)
        template = override or spec.child_addresses.get(slot)

        if not template:
            # Some types are only addressable once you say which tier they are
            # in (VPC keeps public/private/database/intra subnets in separate
            # lists). Surface the choices rather than guessing.
            candidates = sorted(
                k.split(".", 1)[1]
                for k in spec.child_addresses
                if k.startswith(f"{r.type}.")
            )
            unresolved.append({
                "type": r.type,
                "name": r.clean_name,
                "id": r.id,
                "reason": (
                    f"this module splits {r.type} by tier; pass tiers="
                    f'{{"{r.clean_name}": "<tier>"}}'
                    if candidates else
                    "the catalog has no address template for this type in this module"
                ),
                "candidate_tiers": candidates,
                "hint_attributes": {
                    k: r.attributes.get(k)
                    for k in ("cidr_block", "availability_zone", "map_public_ip_on_launch",
                              "tags")
                    if r.attributes.get(k) is not None
                },
            })
            continue

        if not r.id:
            unresolved.append({
                "type": r.type, "name": r.clean_name,
                "reason": "no ID in state — cannot be imported automatically",
            })
            continue

        idx = counters.get(slot, 0)
        counters[slot] = idx + 1
        addr = template.format(m=module_call_name, i=idx)
        entry = {
            "to": addr, "id": r.id, "from_type": r.type,
            "from_name": r.clean_name, "index": idx, "slot": slot,
        }
        imports.append(entry)
        ordering.setdefault(slot, []).append({
            "index": idx,
            "name": r.clean_name,
            "cidr_block": r.attributes.get("cidr_block"),
            "availability_zone": r.attributes.get("availability_zone"),
        })

    import_hcl = "".join(
        f'import {{\n  to = {imp["to"]}\n  id = "{imp["id"]}"\n}}\n\n' for imp in imports
    )

    mapped_attrs = {a.split(".")[0] for a in spec.inputs.values()}
    unmapped_attrs = sorted(
        k for k in primary.attributes
        if k not in mapped_attrs
        and k not in GLOBAL_COMPUTED
        and k not in VOLATILE
        and primary.attributes.get(k) not in (None, "", [], {}, False)
    )

    written = None
    if relative_path:
        try:
            target = _resolve_in(ws, relative_path, label="relative_path")
        except ValueError as exc:
            return _err(str(exc))
        target.parent.mkdir(parents=True, exist_ok=True)
        prefix = target.read_text() if append and target.exists() else ""
        sep = "\n" if prefix and not prefix.endswith("\n\n") else ""
        target.write_text(prefix + sep + module_hcl)
        written = str(target.relative_to(ws))

    return _ok(
        module_key=module_key,
        module_call=f"module.{module_call_name}",
        module_hcl=module_hcl,
        import_blocks_hcl=import_hcl,
        import_count=len(imports),
        imports=imports,
        index_assignment=ordering,
        written_to=written,
        unresolved_imports=unresolved,
        unmapped_attributes=unmapped_attrs,
        module_notes=spec.notes,
        warnings=[
            w for w in [
                (f"{len(unresolved)} object(s) could not be given an import block; "
                 f"they will be CREATED (duplicating live infrastructure) unless you "
                 f"handle them.") if unresolved else None,
                ("index_assignment shows the order indices were assigned in. The "
                 "module's positional inputs (azs, *_subnets) MUST be built in that "
                 "exact order or the imports land on the wrong objects.")
                if ordering else None,
                (f"{len(unmapped_attrs)} live attribute(s) are not expressed in the "
                 f"module inputs; each is a potential plan diff.") if unmapped_attrs else None,
                spec.notes if spec.difficulty == "hard" else None,
            ] if w
        ],
        next_step="Write the import blocks with write_import_blocks, then tf_plan.",
    )


@mcp.tool()
def write_import_blocks(
    workspace_dir: str,
    imports: list[dict],
    relative_path: str = "envs/prod/imports.tf",
    append: bool = True,
) -> str:
    """Write ``import`` blocks to a file.

    Args:
        workspace_dir: Target workspace.
        imports: List of {"to": "module.vpc.aws_vpc.this[0]", "id": "vpc-0abc"}.
        relative_path: Destination, conventionally ``imports.tf`` in the env dir.
        append: Append rather than replace, so several module groups accumulate.
    """
    try:
        ws = _resolve_workspace(workspace_dir)
        target = _resolve_in(ws, relative_path, label="relative_path")
    except ValueError as exc:
        return _err(str(exc))

    bad = [i for i in imports if not i.get("to") or not i.get("id")]
    if bad:
        return _err("every import needs both 'to' and 'id'", invalid=bad)

    existing = target.read_text() if target.exists() else ""
    already = set(re.findall(r"^\s*to\s*=\s*(.+)$", existing, re.M))

    lines, skipped = [], []
    for imp in imports:
        if imp["to"].strip() in {a.strip() for a in already}:
            skipped.append(imp["to"])
            continue
        lines.append(f'import {{\n  to = {imp["to"]}\n  id = "{imp["id"]}"\n}}\n')

    header = (
        "# Adoption of pre-existing AWS objects.\n"
        "# Single-use: delete this file once `terraform apply` has run and the\n"
        "# objects are in state.\n\n"
    )
    body = "\n".join(lines)
    target.parent.mkdir(parents=True, exist_ok=True)
    if append and existing:
        target.write_text(existing.rstrip("\n") + "\n\n" + body)
    else:
        target.write_text(header + body)

    return _ok(
        path=str(target.relative_to(ws)),
        written=len(lines),
        skipped_duplicates=skipped,
        reminder="Run tf_plan and confirm every one of these shows as an import, "
                 "not a create.",
    )


# ---------------------------------------------------------------------------
# Tools: terraform lifecycle
# ---------------------------------------------------------------------------


def _plan_dir(workspace_dir: str, plan_subdir: Optional[str]) -> Path:
    ws = _resolve_workspace(workspace_dir)
    return _resolve_in(ws, plan_subdir, label="plan_subdir")


@mcp.tool()
def tf_init(
    workspace_dir: str,
    plan_subdir: Optional[str] = None,
    backend_config: Optional[dict] = None,
    upgrade: bool = False,
) -> str:
    """Run ``terraform init``.

    Args:
        workspace_dir: Workspace.
        plan_subdir: Root module dir relative to the workspace, e.g. "envs/prod".
        backend_config: Extra ``-backend-config=k=v`` pairs.
        upgrade: Pass ``-upgrade`` to re-resolve module/provider versions.
    """
    try:
        target = _plan_dir(workspace_dir, plan_subdir)
    except ValueError as exc:
        return _err(str(exc))
    if not target.is_dir():
        return _err(f"not a directory: {target}")

    cmd = [TERRAFORM_BIN, "init", "-input=false"]
    if upgrade:
        cmd.append("-upgrade")
    for k, v in (backend_config or {}).items():
        cmd.append(f"-backend-config={k}={v}")
    result = _run(cmd, cwd=target)
    return _dump({"ok": result["returncode"] == 0, "init": _public(result)})


@mcp.tool()
def tf_fmt(workspace_dir: str, plan_subdir: Optional[str] = None, check: bool = False) -> str:
    """Run ``terraform fmt -recursive``.

    Args:
        workspace_dir: Workspace.
        plan_subdir: Subdirectory to format. Defaults to the whole workspace.
        check: Report what would change without rewriting anything.
    """
    try:
        target = _plan_dir(workspace_dir, plan_subdir)
    except ValueError as exc:
        return _err(str(exc))
    cmd = [TERRAFORM_BIN, "fmt", "-recursive"]
    if check:
        cmd.append("-check")
    result = _run(cmd, cwd=target)
    return _dump({"ok": result["returncode"] == 0, "fmt": _public(result)})


@mcp.tool()
def tf_validate(workspace_dir: str, plan_subdir: Optional[str] = None) -> str:
    """Run ``terraform validate -json``. Cheap syntax/consistency check; does not
    talk to AWS. Run this after every generation step, before tf_plan.

    Args:
        workspace_dir: Workspace.
        plan_subdir: Root module dir, e.g. "envs/prod".
    """
    try:
        target = _plan_dir(workspace_dir, plan_subdir)
    except ValueError as exc:
        return _err(str(exc))

    if not (target / ".terraform").exists():
        init = _run([TERRAFORM_BIN, "init", "-input=false", "-backend=false"], cwd=target)
        if init["returncode"] != 0:
            return _err("terraform init failed", init=_public(init))

    result = _run([TERRAFORM_BIN, "validate", "-json"], cwd=target)
    try:
        parsed = json.loads(result["_stdout_full"]) if result["_stdout_full"] else None
    except json.JSONDecodeError:
        parsed = None

    valid = bool(parsed and parsed.get("valid"))
    return _dump(
        {
            "ok": valid,
            "valid": valid,
            "error_count": (parsed or {}).get("error_count"),
            "warning_count": (parsed or {}).get("warning_count"),
            "diagnostics": (parsed or {}).get("diagnostics", [])[:40],
            "raw": _public(result) if parsed is None else None,
        }
    )


@mcp.tool()
def tf_plan(
    workspace_dir: str,
    plan_subdir: Optional[str] = None,
    var_file: Optional[str] = None,
    plan_file: str = "plan.out",
    refresh: bool = True,
    targets: Optional[list[str]] = None,
) -> str:
    """Run ``terraform plan``, saving the plan for classify_plan to dissect.

    Unlike a naive wrapper, this reports ``ok: false`` when terraform exits
    non-zero — a failed plan produces no change_summary, and treating that as
    "zero drift" is the single most dangerous thing this server could do.

    Args:
        workspace_dir: Workspace.
        plan_subdir: Root module dir, e.g. "envs/prod".
        var_file: ``-var-file`` path relative to the plan dir.
        plan_file: Where to save the binary plan.
        refresh: Set False to skip refresh (faster, but drift is invisible).
        targets: ``-target`` addresses. Use only to isolate one module group
            while converging; never as a normal workflow.
    """
    try:
        target = _plan_dir(workspace_dir, plan_subdir)
    except ValueError as exc:
        return _err(str(exc))
    if not target.is_dir():
        return _err(f"not a directory: {target}")

    if not (target / ".terraform").exists():
        init = _run([TERRAFORM_BIN, "init", "-input=false"], cwd=target)
        if init["returncode"] != 0:
            return _err("terraform init failed", init=_public(init))

    cmd = [TERRAFORM_BIN, "plan", "-input=false", "-json", f"-out={plan_file}"]
    if not refresh:
        cmd.append("-refresh=false")
    if var_file:
        cmd.append(f"-var-file={var_file}")
    for t in targets or []:
        cmd.append(f"-target={t}")

    result = _run(cmd, cwd=target)

    summary = {"add": 0, "change": 0, "destroy": 0, "import": 0}
    changes: list[dict] = []
    drift: list[dict] = []
    diagnostics: list[dict] = []
    saw_summary = False

    for line in result["_stdout_full"].splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        mtype = obj.get("type")
        if mtype == "diagnostic":
            diagnostics.append(obj.get("diagnostic", {}))
        elif mtype in ("planned_change", "resource_drift"):
            change = obj.get("change", {})
            # terraform emits a singular string "action"; older/other emitters
            # use a list "actions". Joining a bare string yields "u,p,d,a,t,e".
            action = change.get("action")
            if action is None:
                acts = change.get("actions") or []
                action = ",".join(acts) if isinstance(acts, list) else str(acts)
            entry = {
                "address": change.get("resource", {}).get("addr"),
                "action": action,
                "previous_address": change.get("previous_resource", {}).get("addr"),
            }
            (drift if mtype == "resource_drift" else changes).append(entry)
        elif mtype == "change_summary":
            saw_summary = True
            cs = obj.get("changes", {})
            summary = {
                "add": cs.get("add", 0),
                "change": cs.get("change", 0),
                "destroy": cs.get("remove", 0),
                "import": cs.get("import", 0),
            }

    failed = result["returncode"] != 0
    zero_drift = (
        not failed
        and saw_summary
        and summary["add"] == 0
        and summary["change"] == 0
        and summary["destroy"] == 0
    )
    destructive = summary["destroy"] > 0 or any(
        "delete" in (c["action"] or "") or "replace" in (c["action"] or "") for c in changes
    )

    return _dump(
        {
            "ok": not failed,
            "plan_dir": str(target),
            "plan_file": plan_file,
            "summary": summary,
            "planned_changes": changes[:MAX_ITEMS],
            "detected_drift": drift[:MAX_ITEMS],
            "diagnostics": [
                {k: d.get(k) for k in ("severity", "summary", "detail", "address")}
                for d in diagnostics
            ][:40],
            "zero_drift": zero_drift,
            "destructive": destructive,
            "stderr": result["stderr"] or None,
            "verdict": (
                "PLAN FAILED — fix the diagnostics; the summary below is meaningless."
                if failed else
                "STOP: this plan destroys or replaces real infrastructure. An import "
                "block is pointing at the wrong address, or a module input does not "
                "match reality. Do not apply."
                if destructive else
                "Converged: zero drift." if zero_drift else
                "Drift remains — run classify_plan to see what is real vs cosmetic."
            ),
        }
    )


@mcp.tool()
def classify_plan(
    workspace_dir: str,
    plan_subdir: Optional[str] = None,
    plan_file: str = "plan.out",
    max_fields: int = 20,
) -> str:
    """Dissect a saved plan and sort every field diff into real / computed /
    ordering / tag / volatile, with a concrete remedy per resource.

    The classification is deliberately conservative: configuration attributes
    (subnet_id, vpc_security_group_ids, instance_type, allocated_storage,
    policies, ingress/egress...) are always "real" even though they look
    infrastructure-ish, because silently lifecycle-ignoring them is how an
    import job quietly diverges from production.

    Args:
        workspace_dir: Workspace.
        plan_subdir: Root module dir.
        plan_file: Plan saved by tf_plan.
        max_fields: Field diffs reported per resource.
    """
    try:
        target = _plan_dir(workspace_dir, plan_subdir)
    except ValueError as exc:
        return _err(str(exc))

    plan_path = target / plan_file
    if not plan_path.exists():
        return _err(f"{plan_path} not found — run tf_plan first.")

    show = _run([TERRAFORM_BIN, "show", "-json", plan_file], cwd=target)
    if show["returncode"] != 0:
        return _err("terraform show failed", details=show["stderr"])
    try:
        plan_json = json.loads(show["_stdout_full"])
    except json.JSONDecodeError:
        return _err("could not parse terraform show output as JSON")

    def classify_leaf(path: str, unknown: Any) -> str:
        leaf = path.split(".")[-1].split("[")[0]
        if leaf in TAG_ATTRS or ".tags" in path or ".tags_all" in path:
            return "tag"
        if unknown is True:
            return "computed"
        if leaf in NEVER_IGNORE:
            return "real"
        if leaf in VOLATILE:
            return "volatile"
        if leaf in GLOBAL_COMPUTED:
            return "computed"
        return "real"

    def walk(before: Any, after: Any, unknown: Any, path: str, out: list) -> None:
        if len(out) > max_fields * 3:
            return
        if isinstance(before, dict) and isinstance(after, dict):
            for k in sorted(set(before) | set(after)):
                sub_unknown = unknown.get(k) if isinstance(unknown, dict) else False
                walk(before.get(k), after.get(k), sub_unknown,
                     f"{path}.{k}" if path else k, out)
            return
        if isinstance(before, list) and isinstance(after, list):
            key = lambda x: json.dumps(x, sort_keys=True, default=str)  # noqa: E731
            if before != after and sorted(before, key=key) == sorted(after, key=key):
                out.append({"path": path, "category": "ordering",
                            "note": "same elements, different order"})
                return
            if len(before) != len(after):
                out.append({
                    "path": path,
                    "category": classify_leaf(path, unknown),
                    "note": f"length {len(before)} -> {len(after)}",
                    "before": _brief(before), "after": _brief(after),
                })
                return
            for i, (b, a) in enumerate(zip(before, after)):
                sub = unknown[i] if isinstance(unknown, list) and i < len(unknown) else False
                walk(b, a, sub, f"{path}[{i}]", out)
            return
        if before != after:
            cat = classify_leaf(path, unknown)
            entry: dict[str, Any] = {"path": path, "category": cat}
            if cat != "computed":
                entry["before"] = _brief(before)
                entry["after"] = _brief(after)
            out.append(entry)

    findings = []
    destructive_addrs = []

    for rc in plan_json.get("resource_changes", []):
        change = rc.get("change", {})
        actions = change.get("actions", [])
        if actions in ([], ["no-op"], ["read"]):
            continue
        addr = rc.get("address")

        if set(actions) & {"delete"}:
            destructive_addrs.append({"address": addr, "actions": actions})
            findings.append({
                "address": addr, "actions": actions, "verdict": "DESTRUCTIVE",
                "diffs": [],
                "remedy": [
                    "STOP. Do not apply.",
                    "If this object should be adopted, it is missing an import block "
                    "or the import block's `to` address does not match the module's "
                    "internal address — check catalog_lookup child_addresses.",
                    "If it should genuinely go away, that is a decision for a human.",
                ],
            })
            continue

        if actions == ["create"]:
            findings.append({
                "address": addr, "actions": actions, "verdict": "UNADOPTED",
                "diffs": [],
                "remedy": [
                    "This module object has no import block, so terraform will create "
                    "a duplicate of something that already exists in AWS.",
                    "Add an import block for it (emit_module_call reports these as "
                    "unresolved_imports), or set the module input that disables it.",
                ],
            })
            continue

        diffs: list[dict] = []
        walk(
            change.get("before") or {},
            change.get("after") or {},
            change.get("after_unknown") or {},
            "",
            diffs,
        )
        cats = {d["category"] for d in diffs}
        importing = "import" in str(rc.get("change", {}).get("importing", "")) or bool(
            rc.get("change", {}).get("importing")
        )

        if "real" in cats:
            verdict = "REAL_DRIFT"
        elif cats & {"computed", "ordering", "volatile"}:
            verdict = "COSMETIC"
        elif cats == {"tag"}:
            verdict = "TAG_ONLY"
        elif not cats:
            verdict = "NO_FIELD_DIFF"
        else:
            verdict = "UNKNOWN"

        remedy = []
        if "real" in cats:
            fields = [d["path"] for d in diffs if d["category"] == "real"]
            remedy.append(
                f"Real configuration drift on {fields[:8]}. Fix the module inputs to "
                f"match live values — do NOT lifecycle-ignore these."
            )
        if "ordering" in cats:
            remedy.append(
                "List ordering differs. Sort the input list deterministically (by AZ, "
                "then CIDR) or switch the module to a keyed map input. Ignoring order "
                "hides real membership changes."
            )
        if "computed" in cats:
            fields = [d["path"] for d in diffs if d["category"] == "computed"]
            remedy.append(
                f"Provider-computed attributes {fields[:8]}. These normally settle on "
                f"the next apply. Only if they persist, add ignore_changes — and note "
                f"you cannot add a lifecycle block to a resource inside a community "
                f"module; use the module's own ignore/*-input, or wrap the resource."
            )
        if "volatile" in cats:
            remedy.append(
                "Volatile attributes (hashes/versions/revisions) — one intentional "
                "bump is expected; a repeating bump means the input differs."
            )
        if cats == {"tag"}:
            remedy.append(
                "Tag-only drift. Preferred fix is to fold the live tags into "
                "local.common_tags / var.additional_tags so the code becomes the "
                "source of truth, rather than ignoring tags."
            )
        if importing:
            remedy.append("This resource is being imported — expected on first apply.")

        findings.append({
            "address": addr,
            "actions": actions,
            "verdict": verdict,
            "diffs": diffs[:max_fields],
            "diff_count": len(diffs),
            "remedy": remedy,
        })

    if destructive_addrs:
        status = "DESTRUCTIVE"
    elif any(f["verdict"] == "UNADOPTED" for f in findings):
        status = "UNADOPTED_RESOURCES"
    elif any(f["verdict"] == "REAL_DRIFT" for f in findings):
        status = "REAL_DRIFT"
    elif any(f["verdict"] == "COSMETIC" for f in findings):
        status = "COSMETIC_ONLY"
    elif any(f["verdict"] == "TAG_ONLY" for f in findings):
        status = "TAG_ONLY"
    elif not findings:
        status = "CONVERGED"
    else:
        status = "UNKNOWN"

    instruction = {
        "DESTRUCTIVE": "STOP and surface this to the user. Do not apply.",
        "UNADOPTED_RESOURCES": "Add the missing import blocks, then re-plan.",
        "REAL_DRIFT": "Correct the module inputs to match live values, then re-plan.",
        "COSMETIC_ONLY": "Address ordering first, then re-plan; computed fields often "
                         "resolve themselves.",
        "TAG_ONLY": "Fold live tags into common_tags, then re-plan.",
        "CONVERGED": "Zero drift. Proceed to audit_production_readiness.",
        "UNKNOWN": "Inspect the diffs manually.",
    }[status]

    return _ok(
        status=status,
        instruction=instruction,
        summary=plan_json.get("errored") and {"errored": True} or plan_json.get("changes", {}),
        destructive=destructive_addrs,
        findings=findings[:MAX_ITEMS],
        finding_count=len(findings),
    )


def _brief(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


@mcp.tool()
def tf_apply_imports(
    workspace_dir: str,
    plan_subdir: Optional[str] = None,
    plan_file: str = "plan.out",
    confirm: bool = False,
) -> str:
    """Apply a saved plan, but only if it is import-only.

    Refuses unless every resource change is an import or a no-op — no creates,
    updates, deletes or replaces. This is the one tool here that mutates AWS
    state, and the guard is the point.

    Args:
        workspace_dir: Workspace.
        plan_subdir: Root module dir.
        plan_file: Plan saved by tf_plan.
        confirm: Must be True. Exists so an accidental call is a no-op.
    """
    try:
        target = _plan_dir(workspace_dir, plan_subdir)
    except ValueError as exc:
        return _err(str(exc))

    if not confirm:
        return _err("confirm=true is required — this writes to real state.")

    if not (target / plan_file).exists():
        return _err(f"{plan_file} not found in {target} — run tf_plan first.")

    show = _run([TERRAFORM_BIN, "show", "-json", plan_file], cwd=target)
    if show["returncode"] != 0:
        return _err("could not inspect the plan", details=show["stderr"])
    try:
        plan_json = json.loads(show["_stdout_full"])
    except json.JSONDecodeError:
        return _err("could not parse the plan")

    offending = [
        {"address": rc.get("address"), "actions": rc.get("change", {}).get("actions")}
        for rc in plan_json.get("resource_changes", [])
        if set(rc.get("change", {}).get("actions", [])) - {"no-op", "read"}
    ]
    if offending:
        return _err(
            "Refusing: this plan is not import-only.",
            offending_changes=offending[:MAX_ITEMS],
            guidance="Converge to a zero-diff plan first. If a change is genuinely "
                     "wanted, apply it deliberately outside this server.",
        )

    result = _run([TERRAFORM_BIN, "apply", "-input=false", "-auto-approve", plan_file],
                  cwd=target)
    return _dump({
        "ok": result["returncode"] == 0,
        "apply": _public(result),
        "next_step": "Delete imports.tf, then tf_plan again — it must still be empty.",
    })


@mcp.tool()
def tf_generate_config(
    workspace_dir: str,
    plan_subdir: Optional[str] = None,
    out_file: str = "generated_from_import.tf",
) -> str:
    """Run ``terraform plan -generate-config-out`` to have terraform itself write
    HCL for every ``import`` block that has no matching configuration.

    Useful for resources the catalog does not cover: write a bare import block,
    let terraform generate the resource body, then hand-refactor it. The output
    is a starting point, not production code — it includes every computed
    attribute and no references.

    Args:
        workspace_dir: Workspace.
        plan_subdir: Root module dir containing the import blocks.
        out_file: File terraform writes the generated config to. Must not exist.
    """
    try:
        target = _plan_dir(workspace_dir, plan_subdir)
    except ValueError as exc:
        return _err(str(exc))

    out_path = target / out_file
    if out_path.exists():
        return _err(f"{out_file} already exists; terraform refuses to overwrite it.")

    result = _run(
        [TERRAFORM_BIN, "plan", "-input=false", f"-generate-config-out={out_file}"],
        cwd=target,
    )
    return _dump({
        "ok": out_path.exists(),
        "generated": str(out_file) if out_path.exists() else None,
        "bytes": out_path.stat().st_size if out_path.exists() else 0,
        "run": _public(result),
        "caveat": "Generated config is verbose and unreferenced. Strip computed "
                  "attributes, replace literal IDs with references, and fold it into "
                  "a module call where one exists.",
    })


# ---------------------------------------------------------------------------
# Tools: refactoring helpers
# ---------------------------------------------------------------------------


@mcp.tool()
def patch_lifecycle(
    workspace_dir: str,
    relative_path: str,
    resource_type: str,
    resource_name: str,
    ignore_attributes: list[str],
    allow_never_ignore: bool = False,
) -> str:
    """Add or merge ``lifecycle { ignore_changes = [...] }`` on a plain resource.

    Only works on resources you own. A resource *inside* a community module
    cannot be patched this way — Terraform has no mechanism for injecting a
    lifecycle block into someone else's module — so this refuses addresses that
    look like module children and tells you what to do instead.

    Args:
        workspace_dir: Workspace.
        relative_path: File containing the resource.
        resource_type: e.g. "aws_instance".
        resource_name: The HCL name.
        ignore_attributes: Attributes to ignore.
        allow_never_ignore: Permit ignoring real configuration attributes
            (subnet_id, policy, ingress...). Off by default because ignoring
            those is how imported code silently diverges from production.
    """
    try:
        ws = _resolve_workspace(workspace_dir)
        target = _resolve_in(ws, relative_path, label="relative_path")
    except ValueError as exc:
        return _err(str(exc))

    if resource_type.startswith("module."):
        return _err(
            "Cannot inject a lifecycle block into a resource inside a module.",
            alternatives=[
                "Use the module's own input if it exposes one (many terraform-aws-modules "
                "expose ignore_* or *_use_name_prefix style toggles).",
                "Fix the module input so there is no diff to ignore.",
                "If the module genuinely cannot express the live object, take the "
                "resource out of the module and manage it directly.",
            ],
        )

    if not target.is_file():
        return _err(f"file not found: {target}")

    risky = sorted(set(ignore_attributes) & NEVER_IGNORE)
    if risky and not allow_never_ignore:
        return _err(
            f"Refusing to ignore real configuration attributes: {risky}.",
            reason="Ignoring these makes the plan lie: AWS can drift arbitrarily and "
                   "terraform will report converged.",
            override="Pass allow_never_ignore=true if you have decided this is right.",
        )

    ok, message, preview = _inject_lifecycle(
        target, resource_type, resource_name, ignore_attributes
    )
    return _dump({
        "ok": ok,
        "message": message,
        "path": str(target.relative_to(ws)),
        "resulting_block": preview,
        "risky_attributes": risky,
    })


def _inject_lifecycle(
    file_path: Path, resource_type: str, resource_name: str, ignores: list[str]
) -> tuple[bool, str, Optional[str]]:
    """Insert or merge ignore_changes on one resource. Brace matching runs on the
    masked source so policy JSON in the body cannot throw it off."""
    text = file_path.read_text()
    lines = text.splitlines()

    block = next(
        (
            b for b in _iter_blocks(text)
            if b.kind == "resource" and b.labels[:2] == [resource_type, resource_name]
        ),
        None,
    )
    if block is None:
        return False, f"resource {resource_type}.{resource_name} not found", None

    header_indent = len(lines[block.start]) - len(lines[block.start].lstrip())
    indent = " " * (header_indent + 2)
    body_lines = lines[block.start + 1 : block.end]
    mbody = _mask("\n".join(body_lines)).splitlines()

    life_start = life_end = None
    for i, ml in enumerate(mbody):
        if re.match(r"^\s*lifecycle\s*\{", ml):
            depth = ml.count("{") - ml.count("}")
            j = i
            while depth > 0 and j + 1 < len(mbody):
                j += 1
                depth += mbody[j].count("{") - mbody[j].count("}")
            life_start, life_end = i, j
            break

    wanted = sorted(set(ignores))

    if life_start is None:
        new_block = [
            "",
            f"{indent}lifecycle {{",
            f"{indent}  ignore_changes = {json.dumps(wanted)}",
            f"{indent}}}",
        ]
        out = lines[: block.end] + new_block + lines[block.end :]
        file_path.write_text("\n".join(out) + "\n")
        return True, "added lifecycle block", "\n".join(new_block).strip()

    existing_text = "\n".join(body_lines[life_start : life_end + 1])
    if re.search(r"ignore_changes\s*=\s*all\b", existing_text):
        return True, "lifecycle already ignores all changes", existing_text

    # NB: the v2 pattern here was corrupted ($$[^$$]*\]) and matched nothing, so
    # every merge silently fell through to appending a second ignore_changes.
    match = re.search(r"ignore_changes\s*=\s*(\[[^\]]*\])", existing_text, re.S)
    if match:
        current = [
            item.strip().strip("\"'")
            for item in match.group(1).strip("[]").split(",")
            if item.strip()
        ]
        merged = sorted(set(current) | set(wanted))
        new_existing = (
            existing_text[: match.start(1)] + json.dumps(merged) + existing_text[match.end(1) :]
        )
        message = f"merged into existing ignore_changes ({len(current)} -> {len(merged)})"
    else:
        inner = body_lines[life_start : life_end + 1]
        inner.insert(len(inner) - 1, f"{indent}  ignore_changes = {json.dumps(wanted)}")
        new_existing = "\n".join(inner)
        message = "added ignore_changes to existing lifecycle block"

    new_body = (
        body_lines[:life_start] + new_existing.splitlines() + body_lines[life_end + 1 :]
    )
    # v2 sliced lines[block.end + 1:] here and dropped the resource's closing brace.
    out = lines[: block.start + 1] + new_body + lines[block.end :]
    file_path.write_text("\n".join(out) + "\n")
    return True, message, new_existing


@mcp.tool()
def dereference_ids(
    workspace_dir: str,
    relative_path: str,
    replacements: dict[str, str],
    dry_run: bool = True,
) -> str:
    """Replace hardcoded AWS IDs with Terraform references.

    Substitution is word-boundary anchored and skips ``import`` blocks and
    comments: an import block's ``id`` must stay a literal, and a naive
    string replace breaks every adoption in the file.

    Args:
        workspace_dir: Workspace.
        relative_path: File to rewrite.
        replacements: {"subnet-0abc123": "module.vpc.private_subnets[0]"}.
        dry_run: Report what would change without writing. Default True.
    """
    try:
        ws = _resolve_workspace(workspace_dir)
        target = _resolve_in(ws, relative_path, label="relative_path")
    except ValueError as exc:
        return _err(str(exc))
    if not target.is_file():
        return _err(f"file not found: {target}")

    text = target.read_text()
    lines = text.splitlines()
    masked = _mask(text).splitlines()

    # line indices that belong to an import block — off limits
    protected: set[int] = set()
    for block in _iter_blocks(text):
        if block.kind == "import":
            protected.update(range(block.start, block.end + 1))

    hits: list[dict] = []
    for i, line in enumerate(lines):
        if i in protected:
            continue
        # only touch the code part of the line; the mask blanks comments
        comment_at = len(masked[i].rstrip()) if masked[i].strip() != line.strip() else None
        new_line = line
        for old_id, new_ref in replacements.items():
            if old_id not in new_line:
                continue
            pattern = re.compile(rf'"?{re.escape(old_id)}"?(?![\w-])')
            candidate = pattern.sub(new_ref, new_line)
            if candidate != new_line:
                hits.append({
                    "line": i + 1, "id": old_id, "ref": new_ref,
                    "before": line.strip()[:160], "after": candidate.strip()[:160],
                })
                new_line = candidate
        lines[i] = new_line
        _ = comment_at

    skipped_in_imports = [
        {"id": oid, "lines": [i + 1 for i in sorted(protected) if oid in lines[i]]}
        for oid in replacements
        if any(oid in lines[i] for i in protected)
    ]

    if not dry_run and hits:
        target.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""))

    return _ok(
        path=str(target.relative_to(ws)),
        dry_run=dry_run,
        replacements_applied=len(hits),
        changes=hits[:MAX_ITEMS],
        preserved_in_import_blocks=skipped_in_imports,
        note="import block IDs were left literal on purpose — they address real AWS "
             "objects, not Terraform state.",
    )


@mcp.tool()
def write_moved_blocks(
    workspace_dir: str,
    moves: list[dict],
    relative_path: str = "envs/prod/moved.tf",
) -> str:
    """Write ``moved`` blocks for state already under old addresses.

    Use this instead of ``import`` when the objects are *already* in your state
    at a plain-resource address and you are relocating them into a module.
    ``moved`` is refactor-safe and reviewable; ``terraform state mv`` is not.

    Args:
        workspace_dir: Workspace.
        moves: [{"from": "aws_vpc.main", "to": "module.vpc.aws_vpc.this[0]"}].
        relative_path: Destination file.
    """
    try:
        ws = _resolve_workspace(workspace_dir)
        target = _resolve_in(ws, relative_path, label="relative_path")
    except ValueError as exc:
        return _err(str(exc))

    bad = [m for m in moves if not m.get("from") or not m.get("to")]
    if bad:
        return _err("every move needs both 'from' and 'to'", invalid=bad)

    body = "\n".join(
        f'moved {{\n  from = {m["from"]}\n  to   = {m["to"]}\n}}\n' for m in moves
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# State relocations. Safe to keep for a release, then remove.\n\n" + body
    )
    return _ok(path=str(target.relative_to(ws)), moves_written=len(moves))


@mcp.tool()
def tf_state_list(
    workspace_dir: str, plan_subdir: Optional[str] = None, filter_glob: Optional[str] = None
) -> str:
    """List addresses in the current state.

    Args:
        workspace_dir: Workspace.
        plan_subdir: Root module dir.
        filter_glob: Glob to narrow the list, e.g. "module.vpc.*".
    """
    try:
        target = _plan_dir(workspace_dir, plan_subdir)
    except ValueError as exc:
        return _err(str(exc))
    result = _run([TERRAFORM_BIN, "state", "list"], cwd=target)
    if result["returncode"] != 0:
        return _err("state list failed", details=result["stderr"])
    addresses = [a for a in result["_stdout_full"].splitlines() if a.strip()]
    if filter_glob:
        addresses = [a for a in addresses if fnmatch.fnmatch(a, filter_glob)]
    return _ok(count=len(addresses), addresses=addresses[:1000])


@mcp.tool()
def show_state_resource(
    workspace_dir: str, address: str, plan_subdir: Optional[str] = None
) -> str:
    """Dump one resource's current state values, to compare against module inputs.

    Args:
        workspace_dir: Workspace.
        address: Full address, e.g. "module.vpc.aws_vpc.this[0]".
        plan_subdir: Root module dir.
    """
    try:
        target = _plan_dir(workspace_dir, plan_subdir)
    except ValueError as exc:
        return _err(str(exc))

    show = _run([TERRAFORM_BIN, "show", "-json"], cwd=target)
    if show["returncode"] != 0:
        return _err("state read failed", details=show["stderr"])
    try:
        state = json.loads(show["_stdout_full"])
    except json.JSONDecodeError:
        return _err("could not parse state JSON")

    def find(node: dict) -> Optional[dict]:
        for res in node.get("resources", []):
            if res.get("address") == address:
                return res
        for child in node.get("child_modules", []):
            hit = find(child)
            if hit:
                return hit
        return None

    values = state.get("values") or {}
    found = find(values.get("root_module", {}))
    if not found:
        return _err(f"address {address!r} not found in state",
                    hint="Use tf_state_list to see what is there.")
    return _ok(resource=found)


# ---------------------------------------------------------------------------
# Tools: production readiness
# ---------------------------------------------------------------------------

_SECRET_RE = re.compile(
    r'(?i)\b(password|secret|secret_key|access_key|token|private_key|passphrase)\b'
    r'\s*=\s*"(?!\s*\$\{)([^"]{6,})"'
)
_HARDCODED_ID_RE = re.compile(
    r'"((?:vpc|subnet|sg|i|ami|rtb|igw|nat|eni|vol|snap|acl|pl)-[0-9a-f]{8,17})"'
)
_AWS_KEY_RE = re.compile(r'\b((?:AKIA|ASIA)[0-9A-Z]{16})\b')
_ACCOUNT_RE = re.compile(r'arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:(\d{12}):')


@mcp.tool()
def audit_production_readiness(
    workspace_dir: str,
    plan_subdir: Optional[str] = None,
) -> str:
    """Audit the generated Terraform against a production checklist.

    Checks: version pinning (terraform, provider, every module call), remote
    state backend, committed lock file, hardcoded credentials and AWS keys,
    hardcoded resource IDs and account IDs, unpinned module sources, leftover
    import/raw files, tagging, and blanket ``ignore_changes = all``.

    Args:
        workspace_dir: Workspace.
        plan_subdir: Root module dir to audit. Defaults to the whole workspace,
            excluding ``raw/``.
    """
    try:
        ws = _resolve_workspace(workspace_dir)
        scope = _plan_dir(workspace_dir, plan_subdir)
    except ValueError as exc:
        return _err(str(exc))

    tf_files = [
        p for p in sorted(scope.rglob("*.tf"))
        if ".terraform" not in p.parts and "raw" not in p.relative_to(ws).parts
    ]
    if not tf_files:
        return _err(f"no .tf files under {scope}")

    findings: list[dict] = []

    def add(severity: str, check: str, message: str, **extra: Any) -> None:
        findings.append({"severity": severity, "check": check, "message": message, **extra})

    has_required_version = False
    has_provider_pin = False
    has_backend = False
    module_calls = 0
    unpinned_modules: list[dict] = []
    tagged_calls = 0

    for path in tf_files:
        rel = str(path.relative_to(ws))
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        masked_lines = _mask(text).splitlines()
        lines = text.splitlines()

        if re.search(r"required_version\s*=", text):
            has_required_version = True
        if re.search(r'source\s*=\s*"hashicorp/aws"', text) and re.search(
            r'version\s*=\s*"[^"]+"', text
        ):
            has_provider_pin = True
        if re.search(r'backend\s+"[a-z0-9]+"\s*\{', text):
            has_backend = True

        for block in _iter_blocks(text):
            if block.kind != "module":
                continue
            module_calls += 1
            name = block.labels[0] if block.labels else "?"
            attrs = _parse_attributes(block.text)
            source = _unquote(attrs.get("source", ""))
            version = _unquote(attrs.get("version", ""))
            if "tags" in attrs:
                tagged_calls += 1
            is_registry = bool(re.match(r"^[\w-]+/[\w-]+/[\w-]+", str(source)))
            if is_registry and not version:
                unpinned_modules.append({"file": rel, "module": name, "source": source})
            elif str(source).startswith(("git::", "github.com")) and "?ref=" not in str(source):
                unpinned_modules.append(
                    {"file": rel, "module": name, "source": source,
                     "note": "git source without ?ref= pin"}
                )

        for i, line in enumerate(lines, start=1):
            code = masked_lines[i - 1] if i - 1 < len(masked_lines) else ""
            if not code.strip() and "=" not in line:
                continue
            for m in _SECRET_RE.finditer(line):
                add("critical", "hardcoded-secret",
                    f"{m.group(1)} assigned a literal value", file=rel, line=i)
            for m in _AWS_KEY_RE.finditer(line):
                add("critical", "aws-access-key",
                    f"AWS key id {m.group(1)[:8]}... in source", file=rel, line=i)
            for m in _HARDCODED_ID_RE.finditer(line):
                if "import" in line or "moved" in line:
                    continue
                add("medium", "hardcoded-id",
                    f"literal {m.group(1)} — should be a reference or a variable",
                    file=rel, line=i)
            for m in _ACCOUNT_RE.finditer(line):
                add("low", "hardcoded-account-id",
                    f"account id {m.group(1)} inline — prefer "
                    f"data.aws_caller_identity.current.account_id",
                    file=rel, line=i)
            if re.search(r"ignore_changes\s*=\s*all\b", code):
                add("high", "ignore-all",
                    "ignore_changes = all disables drift detection entirely",
                    file=rel, line=i)

    if not has_required_version:
        add("high", "no-required-version",
            "no required_version constraint — builds are not reproducible")
    if not has_provider_pin:
        add("high", "unpinned-provider",
            "aws provider has no version constraint")
    if not has_backend:
        add("high", "no-remote-backend",
            "no backend block — state is local, so it is unshared, unlocked and "
            "unbacked-up")
    for um in unpinned_modules:
        add("high", "unpinned-module",
            f'module "{um["module"]}" source {um["source"]} has no version pin', **um)

    lock_files = list(scope.rglob(".terraform.lock.hcl"))
    if not lock_files:
        add("medium", "no-lock-file",
            "no .terraform.lock.hcl — run terraform init and commit the lock file")

    if (ws / "raw").exists():
        add("info", "raw-tree-present",
            "raw/ terraformer output is still in the workspace. Keep it out of the "
            "applied root module (it is excluded from this audit) and consider "
            "deleting it once conversion is done.")

    leftover_imports = [
        str(p.relative_to(ws)) for p in scope.rglob("imports.tf")
    ]
    if leftover_imports:
        add("info", "imports-present",
            "import blocks still present. Delete them after the adoption apply, "
            "otherwise every future plan re-checks objects that are already managed.",
            files=leftover_imports)

    if module_calls and tagged_calls < module_calls:
        add("low", "untagged-modules",
            f"{module_calls - tagged_calls} of {module_calls} module calls pass no "
            f"tags. Provider default_tags covers most cases; confirm that is enough.")

    if not (ws / ".gitignore").exists():
        add("medium", "no-gitignore",
            "no .gitignore — *.tfstate and .terraform/ risk being committed")

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    counts = {sev: sum(1 for f in findings if f["severity"] == sev) for sev in order}

    blocking = counts["critical"] + counts["high"]
    return _ok(
        scope=str(scope.relative_to(ws)) or ".",
        files_audited=len(tf_files),
        module_calls=module_calls,
        counts=counts,
        production_ready=blocking == 0,
        findings=findings[:MAX_ITEMS],
        verdict=(
            "Ready: no critical or high findings. Still run tflint and trivy "
            "(`make lint`, `make sec`) — this audit does not check AWS-specific "
            "security posture like public S3 or open security groups."
            if blocking == 0
            else f"Not production ready: {blocking} critical/high finding(s)."
        ),
    )


@mcp.tool()
def list_workspace(workspace_dir: str, include_raw: bool = False) -> str:
    """List files in a workspace, for orientation.

    Args:
        workspace_dir: Workspace.
        include_raw: Include the ``raw/`` terraformer tree, which is usually
            large and rarely what you want to page through.
    """
    try:
        ws = _resolve_workspace(workspace_dir)
    except ValueError as exc:
        return _err(str(exc))
    if not ws.exists():
        return _err(f"workspace does not exist: {ws}", root=str(WORKSPACE_ROOT))

    files = []
    for p in sorted(ws.rglob("*")):
        if not p.is_file() or ".terraform" in p.parts:
            continue
        rel = p.relative_to(ws)
        if not include_raw and rel.parts and rel.parts[0] == "raw":
            continue
        files.append({"path": str(rel), "bytes": p.stat().st_size})

    return _ok(workspace=str(ws), file_count=len(files), files=files[:1000])


@mcp.tool()
def tf_read_file(workspace_dir: str, relative_path: str, max_bytes: int = 120_000) -> str:
    """Read a file from the workspace.

    Args:
        workspace_dir: Workspace.
        relative_path: Path relative to the workspace.
        max_bytes: Truncate beyond this size.
    """
    try:
        ws = _resolve_workspace(workspace_dir)
        target = _resolve_in(ws, relative_path, label="relative_path")
    except ValueError as exc:
        return _err(str(exc))
    if not target.is_file():
        return _err(f"not a file: {target}")
    try:
        content = target.read_text()
    except UnicodeDecodeError:
        return _err("file is not UTF-8 text")
    return _ok(
        path=str(target.relative_to(ws)),
        truncated=len(content) > max_bytes,
        content=content[:max_bytes],
    )


@mcp.tool()
def tf_write_file(
    workspace_dir: str, relative_path: str, content: str, overwrite: bool = True
) -> str:
    """Write a file into the workspace.

    Args:
        workspace_dir: Workspace.
        relative_path: Destination relative to the workspace.
        content: Full file content.
        overwrite: If False and the file exists, refuse and return the existing
            content so it can be merged rather than clobbered.
    """
    try:
        ws = _resolve_workspace(workspace_dir)
        target = _resolve_in(ws, relative_path, label="relative_path")
    except ValueError as exc:
        return _err(str(exc))
    if target.exists() and not overwrite:
        return _err("file exists and overwrite=False", existing=target.read_text()[:20000])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return _ok(path=str(target.relative_to(ws)), bytes=len(content.encode()))


@mcp.tool()
def terraformer_import(
    workspace_dir: str,
    resources: list[str],
    regions: list[str],
    profile: Optional[str] = None,
    filters: Optional[list[str]] = None,
    compact: bool = False,
) -> str:
    """Run terraformer against a live AWS account, writing into ``<workspace>/raw/``.

    Optional — the normal entry point is ingest_terraformer with an existing
    dump. Credentials come from the ambient environment (AWS_PROFILE, SSO,
    instance role); this server never handles them.

    Args:
        workspace_dir: Workspace.
        resources: Terraformer resource names, e.g. ["vpc", "sg", "ec2_instance"].
        regions: e.g. ["eu-central-1"].
        profile: AWS profile name.
        filters: ``--filter`` expressions, e.g.
            ["Name=tags.Environment;Value=production"].
        compact: One file per resource type. Leave False — compact output also
            compacts state, which loses the per-resource IDs import blocks need.
    """
    try:
        ws = _resolve_workspace(workspace_dir)
    except ValueError as exc:
        return _err(str(exc))

    raw_root = ws / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)

    cmd = [
        TERRAFORMER_BIN, "import", "aws",
        "--resources=" + ",".join(resources),
        "--regions=" + ",".join(regions),
    ]
    if profile:
        cmd.append("--profile=" + profile)
    for f in filters or []:
        cmd.append("--filter=" + f)
    if compact:
        cmd.append("--compact")

    result = _run(cmd, cwd=raw_root)
    found = _scan_terraformer_tree(raw_root)
    by_type: dict[str, int] = {}
    for r in found:
        by_type[r.type] = by_type.get(r.type, 0) + 1

    return _dump({
        "ok": result["returncode"] == 0,
        "run": _public(result),
        "raw_root": str(raw_root.relative_to(ws)),
        "resource_count": len(found),
        "by_type": by_type,
        "next_step": "plan_conversion",
    })


@mcp.tool()
def workflow_guide() -> str:
    """Return the end-to-end procedure this server implements, with the failure
    modes that actually bite. Read this first."""
    return _ok(
        steps=[
            {"step": 1, "tool": "ingest_terraformer",
             "do": "Point at the terraformer output directory. IDs are read from "
                   "terraform.tfstate, not the HCL."},
            {"step": 2, "tool": "plan_conversion",
             "do": "Get the module grouping and apply order. Note unmapped_resources "
                   "and resources_without_id."},
            {"step": 3, "tool": "scaffold_project",
             "do": "Write the repo skeleton. Supply backend_bucket or accept local "
                   "state while converging."},
            {"step": 4, "tool": "emit_module_call",
             "do": "One module group at a time, in apply_order. Review "
                   "unmapped_attributes — each one is a future plan diff."},
            {"step": 5, "tool": "write_import_blocks",
             "do": "Write the import blocks emit_module_call produced."},
            {"step": 6, "tool": "tf_validate then tf_plan",
             "do": "Validate before planning; a plan against broken HCL wastes an "
                   "AWS round trip."},
            {"step": 7, "tool": "classify_plan",
             "do": "CONVERGE LOOP: fix the reported remedy, re-plan, repeat until "
                   "status == CONVERGED. Then go back to step 4 for the next group."},
            {"step": 8, "tool": "tf_apply_imports",
             "do": "Only once the plan is import-only. Then delete imports.tf and "
                   "re-plan: it must be empty."},
            {"step": 9, "tool": "audit_production_readiness",
             "do": "Then `make lint` and `make sec` for AWS security posture."},
        ],
        failure_modes=[
            "A 'create' in the plan means a duplicate is about to be built alongside "
            "live infrastructure. It is never benign.",
            "A 'destroy' or 'replace' means an import address is wrong. Stop and ask "
            "the user; never apply through it.",
            "Subnet imports landing on wrong indices is the most common VPC failure: "
            "the module's subnet lists are positional. Sort deterministically.",
            "ignore_changes is a last resort. Every attribute you ignore is drift you "
            "will never be told about again.",
            "You cannot add a lifecycle block to a resource inside a community module. "
            "Fix the input, use the module's own toggle, or stop using the module for "
            "that resource.",
            "Module version pins in the catalog were correct at authoring; run "
            "registry_lookup before trusting them for a new project.",
        ],
        conventions={
            "raw/": "untouched terraformer output, never applied",
            "envs/<env>/": "the root module that is actually applied",
            "imports.tf": "single-use adoption blocks, deleted after the first apply",
            "moved.tf": "state relocations, kept for one release",
        },
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
