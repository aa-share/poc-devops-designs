# Phase 2: Forensic Reconstruction & The Zero-Diff Pipeline

Phase 2 is where you separate engineers who import infrastructure from engineers who own infrastructure. Terraformer’s output is a corpse. You are performing forensic reconstruction. The goal is not “clean HCL”—the goal is a zero-diff plan after a targeted destroy-and-recreate would yield zero downtime.

Here is the godmode pipeline.

---

## 1. The Autopsy: What Terraformer Actually Emits

Before touching anything, understand the pathology. You will see:

```hcl
# 1. COMPUTED GARBAGE
resource "aws_instance" "tfer--i-0a1b2c3d4e5f" {
  ami                         = "ami-12345678"
  arn                         = "arn:aws:ec2:us-east-1:123456789:instance/i-0a1b2c3d4e5f"  # COMPUTED
  associate_public_ip_address = true
  availability_zone           = "us-east-1a"  # Keep? Only if not from subnet
  cpu_core_count              = 4              # COMPUTED from instance type
  disable_api_stop            = false
  disable_api_termination     = false
  ebs_optimized               = false          # COMPUTED default; omit
  host_id                     = ""             # NULL NOISE
  iam_instance_profile        = "MyProfile"    # HARDCODED STRING
  instance_type               = "m5.xlarge"
  monitoring                  = false
  private_ip                  = "10.0.1.5"      # HARDCODED; conflict if subnet cidr changes
  secondary_private_ips       = []             # EMPTY NOISE
  security_groups             = ["sg-123", "sg-456"]  # HARDCODED IDs!
  subnet_id                   = "subnet-0abc"   # HARDCODED ID
  tags                        = { Name = "web-1" }
  # ... 40 more lines of noise
}
```

### The Sins

* **Computed/Read-Only Attrs:** `arn`, `owner_id`, `cpu_core_count`, `ebs_optimized` (sometimes), empty lists.
* **Hardcoded Physical IDs Everywhere:** `subnet_id`, `vpc_id`, `security_groups`, `iam_instance_profile` (name is fine, but ARN is computed), `kms_key_id`.
* **Synthetic Resource Names:** `tfer--` prefixed with physical IDs embedded. Breaks `for_each`, destroys history.
* **Inline Policies & Rules:** Security group rules inside `aws_security_group`, IAM policies as giant heredocs.
* **Missing Dependencies:** No references between resources. Flat, inert HCL.
* **Stale Provider Constraints:** Stuck at 0.13 syntax.

> **Your mission:** Strip → Map → Rename → Wire → Validate.

---

## 2. The Assembly Line

### Step 2.1: Bootstrap the repo & lock the backend

Before any code changes, establish the cathedral:

```hcl
terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  backend "s3" {
    bucket         = "mycompany-tfstate-prod"
    key            = "Foundation/networking/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "terraform-locks"
  }
}

# IMMEDIATELY enforce tagging and region
provider "aws" {
  region = var.aws_region
  default_tags {
    tags = local.mandatory_tags
  }
}
```

> 💡 **Godmode rule:** If terraformer dumped the state locally, migrate it to S3 now with `terraform init -migrate-state`. Never refactor against local state.

---

### Step 2.2: Strip the Corpse (Automated Noise Removal)

Do not hand-edit 10,000 lines. Parse and strip.

**Toolchain:** `hcl2json` → analyze structure → Python/Go script → rewrite HCL.

Install the parser:

```bash
brew install hcl2json  # or go install github.com/tmccombs/hcl2json@latest
```

Target list of attributes to always strip per resource type (maintain this in a YAML/JSON "scalpel config"):

```yaml
aws_instance:
  drop_computed:
    - arn
    - owner_id
    - cpu_core_count
    - cpu_threads_per_core
    - ebs_optimized  # unless explicitly true and non-default
    - public_dns
    - public_ip
    - private_dns
    - ipv6_addresses
    - placement_group
    - host_id
    - tenancy  # if "default"
    - outpost_arn
  drop_if_empty:
    - secondary_private_ips
    - source_dest_check  # if true (default)
    - user_data_replace_on_change  # if false
    - instance_market_options
aws_security_group:
  drop_computed:
    - arn
    - owner_id
    - egress  # WILL SEPARATE INTO aws_security_group_rule
    - ingress  # WILL SEPARATE INTO aws_security_group_rule
aws_subnet:
  drop_computed:
    - arn
    - ipv6_cidr_block_association_id
    - owner_id
    - available_ip_address_count
```

Python stripping script (`scalpel.py`):

```python
#!/usr/bin/env python3
import hcl2, re, sys, json
from pathlib import Path

# Map of resource_type -> set of attrs to delete
DROP = {
    "aws_instance": {"arn", "owner_id", "cpu_core_count", "cpu_threads_per_core",
                     "public_dns", "public_ip", "private_dns", "host_id"},
}

def strip_resource(block: dict):
    for res_type, resources in block.get("resource", {}).items():
        if res_type not in DROP:
            continue
        for name, attrs in resources.items():
            for attr in list(attrs.keys()):
                if attr in DROP[res_type]:
                    del attrs[attr]
                elif isinstance(attrs[attr], list) and attrs[attr] == []:
                    del attrs[attr]
                elif isinstance(attrs[attr], str) and attrs[attr] == "":
                    del attrs[attr]
    return block

with open(sys.argv[1]) as f:
    obj = hcl2.load(f)

# WARNING: hcl2 library writes; for production use hclwrite (Go) or fmt carefully
```

Better approach: Use the Go tool `hclwrite` for lossless round-tripping. If you don't want to write Go, use `terrafmt` + a bash pipeline.

Bash scalpel for quick wins:

```bash
# Strip obvious computed AWS attrs globally
perl -i -pe 's/^\s+(arn|owner_id|id)\s+=\s+".*"
//g' *.tf

# Strip empty lists/maps unless intentional
perl -i -0pe 's/\w+\s+=\s+\[\s*\]
//g' *.tf
perl -i -0pe 's/\w+\s+=\s+\{\s*\}
//g' *.tf

# Strip default bools (dangerous: verify plan after)
perl -i -pe 's/^\s+(monitoring|ebs_optimized|source_dest_check)\s+=\s+false
//g' *.tf
```

After stripping: `terraform fmt && terraform validate`. Then `terraform plan`.

If plan shows "No changes": you have a clean baseline. If it shows deltas, the attribute was not computed—it was user-managed. Restore it.

---

### Step 2.3: The Great De-Hardcoding (ID Archaeology)

This is the hardest part. Terraformer gave you:

```hcl
subnet_id = "subnet-0a1b2c3d4e5f6789a"
```

You need:

```hcl
subnet_id = aws_subnet.private_a.id
```

#### Strategy: The ID Registry

First, build a lookup table of every resource Terraformer emitted.

```bash
# Extract all aws_subnet resources and their IDs from state
terraform show -json > state.json

# Or directly from terraformer output (since IDs are in the code)
grep -E '^\s+id\s+=' *.tf | sort -u > id_registry.txt
```

You now have a mapping file:

```text
aws_subnet.tfer--subnet-0a1b2c3d = "subnet-0a1b2c3d"
aws_vpc.tfer--vpc-12345678 = "vpc-12345678"
aws_security_group.tfer--sg-12345678 = "sg-12345678"
```

Rewrite rules (automated):

```bash
#!/bin/bash
# dehardcode.sh

# Load mapping: PHYSICAL_ID -> TF_ADDRESS
declare -A MAP
while IFS="=" read -r addr id; do
  id=$(echo "$id" | tr -d ' "' )
  addr=$(echo "$addr" | tr -d ' ')
  MAP["$id"]="$addr"
done < <(terraform state list | while read r; do
  id=$(terraform state show -no-color "$r" | grep -E '^\s+id\s+=' | head -1 | cut -d= -f2 | tr -d ' "')
  echo "${r}=${id}"
done)

# Replace in all .tf files
for phys_id in "${!MAP[@]}"; do
  tf_ref="${MAP[$phys_id]}"
  # Replace quoted IDs with references ONLY in resource bodies
  # Be careful not to replace inside 'id = "..."' for the resource itself
  sed -i '' -E "s/"${phys_id}"/${tf_ref}/g" *.tf
done
```

This is naive. You will break things. Here is the godmode refinement:

Only replace in value positions, not in resource declarations. Use `hclwrite` or `awk` to target right-hand sides.

#### Resource-Type-Specific Reconstruction

##### VPC & Networking

```hcl
# BEFORE
resource "aws_subnet" "private_a" {
  vpc_id            = "vpc-123"  # HARDCODED
  cidr_block        = "10.0.1.0/24"
  availability_zone = "us-east-1a"
}

# AFTER
locals {
  vpc_id = aws_vpc.main.id  # Or data.aws_vpc.main.id if VPC is not in this root module
}

resource "aws_subnet" "private_a" {
  vpc_id            = local.vpc_id
  cidr_block        = "10.0.1.0/24"
  availability_zone = "${var.aws_region}a"

  # EXPLICIT: if this subnet was imported and has dependencies
  depends_on = [aws_vpc_ipv4_cidr_block_association.main]
}
```

AZ pattern: Never hardcode `us-east-1a`. Use `data.aws_availability_zones.available.names[0]` or maps.

##### Security Groups: The Inline Rule Massacre

Terraformer dumps rules inline. This causes:
* Cycle nightmares on destroy
* No referencing individual rules
* Spurious diffs on description changes

**Mandatory Operation:** Separate them.

```python
# Generate aws_security_group_rule resources from inline blocks
# Use a Python script on state JSON:

# sg_rule_extractor.py
import json, sys
state = json.load(sys.stdin)
for r in state["values"]["root_module"]["resources"]:
    if r["type"] == "aws_security_group":
        name = r["name"]
        sg_id = r["values"]["id"]
        vpc_id = r["values"]["vpc_id"]
        for rule in r["values"].get("ingress", []):
            # emit HCL
            print(f'''resource "aws_security_group_rule" "{name}_ingress_{rule['from_port']}_{rule['to_port']}_{rule['protocol']}" {{
  type              = "ingress"
  from_port         = {rule['from_port']}
  to_port           = {rule['to_port']}
  protocol          = "{rule['protocol']}"
  cidr_blocks       = {json.dumps(rule.get('cidr_blocks', []))}
  security_group_id = aws_security_group.{name}.id
  description       = "{rule.get('description', '') or 'Managed by Terraform'}"
}}''')
```

Run it, remove `ingress`/`egress` blocks from `aws_security_group`, validate, plan. Now rules are independent and can be referenced.

##### IAM: Heredoc to jsonencode

Terraformer dumps:

```hcl
policy = <<POLICY
{"Version":"2012-10-17","Statement":[...]}
POLICY
```

Convert immediately:

```hcl
policy = jsonencode({
  Version = "2012-10-17"
  Statement = [
    {
      Effect   = "Allow"
      Action   = ["ec2:DescribeInstances"]
      Resource = "*"
      Sid      = "VisualEditor0"
    }
  ]
})
```

Scripted conversion using `hcl2json` + `jq` + `jsonencode` reconstruction is possible, but for IAM, hand-curate the first few then copy-paste. IAM is too sensitive for blind regex.

---

### Step 2.4: Synthetic Name Massacre (tfer-- Removal)

Names like `tfer--i-0a1b2c3d` are unusable. Rename to logical names, but do not let Terraform destroy and recreate.

Batch generate `moved` blocks (Terraform 1.1+):

```bash
#!/bin/bash
# generate_moved.sh

terraform state list | grep "tfer--" | while read -r old_addr; do
  # Logic: aws_instance.tfer--i-0a1b2c3d -> aws_instance.web_server
  # You need a mapping file: old_addr -> new_addr
  :
done
```

Better: If you have 200 resources, do a state migration file.

Create `moved.tf`:

```hcl
moved {
  from = aws_instance.tfer--i-0a1b2c3d4e5f6789a
  to   = aws_instance.web_server
}
```

Rename the resource block in `.tf` files.
Run `terraform plan`. You should see: *Note: Objects have changed outside of Terraform ONLY, and Plan: 0 to add, 0 to change, 0 to destroy.*

If Terraform version < 1.1 (legacy), use `terraform state mv` in a script:

```bash
#!/bin/bash
declare -a MOVES=(
  "aws_instance.tfer--i-0a1b2c stuff:aws_instance:web_server"
)

for pair in "${MOVES[@]}"; do
  IFS=: read -r old new <<< "$pair"
  terraform state mv "$old" "$new"
done
```

> ⚠️ **Critical:** Do `terraform state mv` before renaming in HCL, or Terraform will think the resource was deleted.

---

### Step 2.5: Normalize Variables, Locals, and Tags

After de-hardcoding, you will have literal strings everywhere. Systematize.

Mandatory `variables.tf` scaffold:

```hcl
variable "environment" {
  type        = string
  description = "Deployment environment (dev,staging,prod)"
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

variable "aws_region" {
  type        = string
  default     = "us-east-1"
  description = "AWS region for resource deployment"
}

variable "default_tags" {
  type        = map(string)
  default     = {}
  description = "Additional tags merged into default_tags"
}
```

Mandatory `locals.tf`:

```hcl
locals {
  naming_prefix = "${var.project_name}-${var.environment}"

  mandatory_tags = merge(
    {
      Environment = var.environment
      ManagedBy   = "terraform"
      Repository  = var.repo_url
      CostCenter  = var.cost_center
    },
    var.default_tags
  )

  # Common AZ map for region-agnostic configs
  azs = slice(data.aws_availability_zones.available.names, 0, 3)
}
```

Retrofit all resources:

```bash
# Replace tags blocks with local reference
perl -i -0pe 's/tags\s+=\s+\{[^\}]+\}/tags = local.mandatory_tags/s' *.tf
# WARNING: Too blunt. Better: enforce via provider default_tags and strip all inline tags.
```

Godmode tagging: Remove all inline tags except `Name`. Let `provider "aws" { default_tags { ... } }` handle the rest. If a resource needs an additional tag, use:

```hcl
tags = merge(local.mandatory_tags, {
  Name       = "specific-name"
  SpecialTag = "override"
})
```

---

### Step 2.6: Reference Reconstruction (The Graph)

This is where you turn flat HCL into a DAG.

Replace patterns:

| Anti-Pattern | Godmode Pattern |
| :--- | :--- |
| `vpc_id = "vpc-xxx"` | `vpc_id = aws_vpc.this.id` or `data.aws_vpc.target.id` |
| `subnet_id = "subnet-xxx"` | `subnet_id = aws_subnet.private["a"].id` (use `for_each`) |
| `security_groups = ["sg-xxx"]` | `vpc_security_group_ids = [aws_security_group.web.id]` |
| `ami = "ami-123"` | `ami = data.aws_ami.amazon_linux_2023.id` |
| `iam_instance_profile = "Name"` | `iam_instance_profile = aws_iam_instance_profile.web.name` |
| `user_data = "..."` | `user_data = base64encode(templatefile("${path.module}/bootstrap.sh", { var = ... }))` |

> 📌 **Important:** If the referenced resource is in a different root module, you must use data sources or `terraform_remote_state`. Do NOT leave hardcoded IDs as a "temporary" measure. Temporary becomes permanent.

---

### Step 2.7: The Zero-Plan Lock (The Invariant)

After every sub-step, the rule is: `terraform plan` MUST show 0 changes.

If it shows:
* **Refresh-only changes:** *Objects have changed outside of Terraform* — acceptable once after import. Run `terraform apply -refresh-only` to sync state, then lock it.
* **Update in-place:** You mutated a user-managed attribute. Compare `terraform show` before/after. Decide: is terraformer wrong, or are you? Often AWS normalizes JSON policies (whitespace, field ordering). Use `jsonencode()` to guarantee canonical form.
* **Replace:** Catastrophic. Usually caused by changing a `ForceNew` attribute (like `subnet_id` on an instance, or renaming without `moved`). Stop. Fix.

CI Gate for this phase:

```yaml
# .github/workflows/phase2.yml (excerpt)
- name: Terraform Plan
  run: |
    terraform plan -no-color -out=plan.tfplan
    terraform show -no-color plan.tfplan > plan.txt

- name: Assert Zero Changes
  run: |
    if grep -q "Plan: 0 to add, 0 to change, 0 to destroy" plan.txt; then
      echo "Phase 2 invariant maintained."
    else
      echo "FAIL: Phase 2 produced a non-zero plan."
      cat plan.txt
      exit 1
    fi
```

---

## 3. Resource-Specific Godmode Patterns

### EC2 / Launch Templates

* **`root_block_device`:** Terraformer flattens it. Ensure `volume_type`, `volume_size`, `encrypted`, `kms_key_id` are explicit. If missing `kms_key_id`, add it now (security baseline).
* **`metadata_options`:** Terraformer never sets it. Add it to enforce IMDSv2:

```hcl
metadata_options {
  http_endpoint               = "enabled"
  http_tokens                 = "required"
  http_put_response_hop_limit = 1
  instance_metadata_tags      = "disabled"
}
```

This WILL show an update. It's a one-time security hardening. Accept it.

### S3 Buckets

Terraformer creates `aws_s3_bucket` with inline policy, versioning, ACL, etc. AWS Provider 4.x+ split these. You must split them or plan is perpetual diff.

```hcl
# Remove from aws_s3_bucket:
# policy, acl, versioning, lifecycle_rule, replication_configuration, server_side_encryption_configuration

# Create separate resources:
resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.this.arn
      sse_algorithm     = "aws:kms"
    }
  }
}
```

If bucket was imported with old inline config, the first plan after splitting will show `~ update in-place` to remove the legacy attributes. Apply once.

### RDS

* **`password`:** Terraformer dumps `password = "plaintext"` if it was in state (rare). Immediately rotate the password and switch to:

```hcl
password = data.aws_secretsmanager_secret_version.db_password.secret_string
```

Mark variable `sensitive = true`.
* **`snapshot_identifier`:** Add `lifecycle { prevent_destroy = true }` now, before any further changes.

---

## 4. Toolchain Rocket Fuel

| Tool | Phase 2 Function |
| :--- | :--- |
| `hcl2json` / `hclwrite` | Lossless parsing of terraformer output for automated surgery |
| `terraform plan -generate-config-out` | If starting over from state, generates modern HCL better than terraformer |
| `tflint` + `tflint-ruleset-aws` | Catch missing references, invalid instance types, deprecated syntax |
| `terrafmt` | Format embedded HCL in policy docs (rarely needed but clean) |
| `pre-commit-terraform` | Runs fmt, validate, tflint, tfsec/trivy on every commit |
| `jq` + `gron` | Inspect state JSON to find physical IDs and map dependencies |

Pre-commit config (`.pre-commit-config.yaml`) for this phase:

```yaml
repos:
  - repo: https://github.com/antonbabenko/pre-commit-terraform
    rev: v1.96.0
    hooks:
      - id: terraform_fmt
      - id: terraform_validate
      - id: terraform_tflint
        args:
          - --args=--config=__GIT_WORKING_DIR__/.tflint.hcl
      - id: terraform_trivy
      - id: terraform_docs
```

`.tflint.hcl`:

```hcl
plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
plugin "aws" {
  enabled = true
  version = "0.31.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}

# Enforce naming conventions
rule "terraform_naming_convention" {
  enabled = true
  format  = "snake_case"
}
```

---

## 5. Execution Order (The Daily Workflow)

### Day 1:
```bash
# 1. Backup state
terraform state pull > state-backup-$(date +%s).json

# 2. Strip & fmt
python scalpel.py raw/ > clean/
cd clean && terraform fmt -recursive

# 3. Establish backend, init
terraform init

# 4. Verify zero plan
terraform plan -no-color > plan.log
# Inspect. If zero, commit: "chore: baseline terraformer output stripped"
```

### Day 2 (IDs):
```bash
# 5. Build ID registry
./build_id_registry.sh > registry.txt

# 6. Automated de-hardcoding (dry run first)
./dehardcode.sh --dry-run
# Hand-review diff. Then run for real.

# 7. Replace hardcoded SG rules with extracted resources
python sg_rule_extractor.py < state.json > security_group_rules.tf
terraform validate
terraform plan # expect only SG rule additions; apply to lock them
```

### Day 3 (Naming & Structure):
```bash
# 8. Generate moved blocks
./generate_moved.sh > moved.tf
terraform plan # verify 0 changes
# Remove moved.tf after 1 successful apply if desired (optional) or keep forever

# 9. Normalize vars/locals
# 10. Add provider default_tags, strip inline tags, re-plan
```

### Day 4 (Policy & Security):
```bash
# 11. Convert heredoc IAM to jsonencode
# 12. Add metadata_options, encryption, secrets manager refs
# 13. trivy/checkov scan
# 14. Final zero-plan PR
```

---

## 6. Common Phase 2 Killers

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| **Plan: 1 to add, 1 to destroy on rename** | Forgot `moved` block or `state mv` | **Stop.** Add `moved`. Never apply a replace during Phase 2. |
| **Invalid provider configuration** | Terraformer used `region = "us-east-1"` inline in every resource | Remove provider aliases from resources; centralize. |
| **Diff on `jsonencode` policy** | AWS reorders keys; terraformer used stringified JSON | Normalize with `jsonencode({})` and canonical field order. |
| **Tag drift after stripping** | `default_tags` in provider conflicts with empty `tags = {}` on resource | Remove empty `tags` blocks entirely. |
| **Value for unconfigurable attribute** | You stripped an attr that is Required but looked computed | Check provider docs. Terraformer sometimes omits defaults. |

---

> **Bottom line:** Phase 2 is not editing. It is programmatic surgery with a single invariant: **the plan must be zero**. If you break the invariant, revert and bisect. The terraform state is the patient—keep it alive while you reconstruct the body around it.
