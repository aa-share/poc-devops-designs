# Step 2.3 — The Great De-Hardcoding, Done Right

## The core insight most people miss

Never do archaeology on the HCL. The state file is the ground truth. Terraformer's HCL is lossy and inconsistent, but the state (`terraform show -json`) contains every attribute of every resource as AWS actually reports it. This gives you a mathematical guarantee:

> If you replace a literal `"subnet-0123..."` with a reference `aws_subnet.X.id`, and state says `aws_subnet.X.id == "subnet-0123..."`, then the plan is provably zero-diff. The reference resolves to the exact same string.

This transforms de-hardcoding from "risky find-and-replace" into a compiler linking pass: build a symbol table, resolve symbols, report unresolved externals.

---

## The pipeline: Registry → Triage → Rewrite → Residue

### 1. Build the symbol table (ID Registry)

Walk state JSON recursively (including child modules) and collect every identifying attribute — not just `id`:

| Attribute | Why it matters |
| :--- | :--- |
| `id` | The obvious one |
| `arn` | IAM policies, event targets, KMS grants reference ARNs, not IDs |
| `name` | `iam_instance_profile`, `role`, `db_subnet_group_name` take names |
| `url` | SQS queue policies reference the URL, Lambda event sources the ARN |
| `unique_id` | IAM trust conditions (`aws:userid`) |
| `key_id` vs `arn` | KMS: S3 SSE wants the ARN, EBS wants either — one string, two valid refs |

#### Critical guards (each one prevents a production incident):
* **Blocklist + minimum length**: never auto-rewrite `"default"`, `"main"`, `"enabled"`, or anything under ~6 chars. A security group named `default` will match half your codebase.
* **Ambiguity detection**: if two different resources own the same value (two SGs named `web`, identical CIDRs), refuse to rewrite and report. In my test, CIDR blocks are registered but marked non-rewritable for exactly this reason — `10.0.1.0/24` appearing in a route is not necessarily "the subnet's CIDR".
* **Same-address dedup**: `aws_iam_role` has `id == name == "app-role"` — that's not ambiguity, it's one resource with two aliases. Dedupe by address with priority `id` > `name` > `arn` > `url`.

---

### 2. Triage every occurrence into four buckets

This is the part everyone skips and then regrets. A naive `sed` treats all matches identically. The validated tool classifies each hit with block context (it tracks which resource block and whether it's inside a heredoc):

| Bucket | Meaning | Action |
| :--- | :--- | :--- |
| **`REWRITTEN`** | Exact quoted literal, owned by a different resource, outside heredocs | Auto-replace `"subnet-0123..."` → `aws_subnet.X.id` |
| **`SELF`** | A resource's own identifying attr (`name = "web-sg"` inside that SG) | Never touch. Rewriting this creates `aws_security_group.X.name` referencing itself → cycle |
| **`EMBEDDED`** | The value is a substring of a larger string — ARN inside an IAM heredoc, ID inside `user_data` | Cannot be string-replaced. Requires structural rewrite (below) |
| **`AMBIGUOUS`** | Multiple possible owners | Human decision, logged in the report |

Test run results on the fixture: 4 auto-rewritten, 9 self-references correctly protected (including the trap where the VPC ID appears both as `subnet.vpc_id` — rewritable — and as the VPC's own `id` — not), 1 embedded ARN flagged, 0 false ambiguities after dedup.

Run it dry first, commit the report, then `--apply`. The report is your audit trail for the PR.

---

### 3. The attribute-semantics problem (where naive tools fail)

The same physical resource is referenced by different attributes in different contexts. Your rewriter maps `value` → `ref`, but you must review that the chosen suffix matches what the consuming attribute expects:

```hcl
# One IAM role, three different reference forms:
role                 = aws_iam_role.app.name        # aws_iam_role_policy wants name
managed_policy_arns  = [aws_iam_policy.x.arn]        # wants ARN
iam_instance_profile = aws_iam_instance_profile.app.name  # NOT the role!

# KMS — the classic footgun:
kms_master_key_id = aws_kms_key.this.arn   # S3 SSE config: ARN
kms_key_id        = aws_kms_key.this.key_id # EBS volume: key id OK
```

Since `id == name` for IAM roles, the `.id` rewrite is value-identical and plan-safe — but for readability, hand-fix to `.name` where semantics demand it. The zero-diff invariant protects you while you do.

> **ForceNew warning**: the rewrite is only safe because the reference resolves to the identical string. If you "helpfully" change the value while de-hardcoding (e.g., point at a different subnet), you trigger replacement on `ForceNew` attributes. De-hardcoding changes form, never value. Value changes are a separate PR, after Phase 2.

---

### 4. Embedded occurrences: structural rewrite, not substitution

The `EMBEDDED` bucket (ARNs inside IAM heredocs, IDs in `user_data`, `container_definitions` JSON) can't be string-replaced — the literal lives inside an opaque string. The fix is to destroy the heredoc and rebuild with real expressions:

```hcl
# BEFORE (terraformer): flagged as EMBEDDED at iam.tf:11
policy = <<POLICY
{"Statement":[{"Action":"sqs:SendMessage","Resource":"arn:aws:sqs:us-east-1:111122223333:jobs"}]}
POLICY

# AFTER: heredoc → jsonencode, literal → reference, dependency edge now exists
policy = jsonencode({
  Version = "2012-10-17"
  Statement = [{
    Effect   = "Allow"
    Action   = "sqs:SendMessage"
    Resource = aws_sqs_queue.jobs.arn
  }]
})
```

For `user_data`: extract to `templatefile("${path.module}/templates/bootstrap.sh.tftpl", { queue_url = aws_sqs_queue.jobs.url })`. For ECS `container_definitions`: `jsonencode` with references. Semi-automatable at best — generate the `jsonencode` skeleton mechanically, wire the references by hand. IAM canonicalization (AWS reorders keys, collapses single-element arrays) means expect one `terraform plan` iteration to converge to zero-diff.

---

### 5. Residue scan: the machine-checkable exit criterion

"Are we done?" must not be a feeling. The exit gate is a regex scan for AWS-shaped IDs (`vpc-[0-9a-f]{17}`, `subnet-...`, `arn:aws:...`) — but only inside quoted strings and heredoc bodies. First version of my scanner flagged reference expressions and `tfer--` resource names; after constraining to quoted spans, the output was exactly the true worklist:

```text
external.tf:2: rtb-0aaaabbbbccccdddd     ← not in state: unmanaged
external.tf:4: igw-0eeeeffff00001111     ← not in state: unmanaged
iam.tf:11: arn:aws:sqs:...:jobs          ← embedded: needs jsonencode rewrite
instance.tf:2: ami-0abcdef1234567890     ← AMI: needs data source
```

Each residue hit gets a decision from this tree:
* Should be managed here, Terraformer missed it (it skips many types) → `import` block, then it enters the registry and the rewriter resolves it on the next pass.
* Owned by another team/stack → data source (next section).
* Environment-varying literal (AMI) → `data "aws_ami"` with filters, or SSM parameter.
* Deliberate literal (rare: cross-account ARN of a partner) → allowlist with a `# residue-allow:` comment the scanner honors.

Wire it into CI: `relink.py residue . || exit 1`. Hardcoded IDs can now never re-enter the codebase.

---

# Step 2.6 — Reference Reconstruction: Building the Graph

## Compute the DAG before you write it

Here's the trick that makes 2.6 tractable: you already have everything needed to compute the intended dependency graph from state alone — for every resource, scan its state values for identifying values owned by other resources. Each hit is an edge. From my test fixture:

```text
aws_subnet.private_a         -> aws_vpc.main            [vpc_id]
aws_security_group.web       -> aws_vpc.main            [vpc_id]
aws_instance.web_1           -> aws_subnet.private_a    [subnet_id]
aws_instance.web_1           -> aws_security_group.web  [security_groups]
// topological order: vpc → subnet/sg → instance
```

This graph is your specification. The rewriting in 2.3 is just making the code match it. After rewriting, `terraform graph` on the actual code must produce a superset of these edges — diff them; missing edges mean a reference you failed to reconstruct (probably an `EMBEDDED` case you skipped).

---

## The graph drives four decisions

### A. Refactoring order = topological order

Refactor roots first, leaves last: VPC/KMS/IAM layer, then subnets/SGs, then compute. Rewriting a leaf before its dependency has a stable name means touching it twice. The topo sort is your sprint plan — and later, your state-split migration order.

### B. State boundaries = graph cuts (the big architectural payoff)

When you split the monolith into stacks (network / data / compute), every edge your boundary severs becomes a cross-stack reference you must implement. So choose boundaries that minimize the cut set — this is why "network stack at the bottom" is universal: VPC/subnets have massive in-degree and near-zero out-degree. Cutting below them costs a handful of exported IDs; cutting through the compute layer costs dozens.

For each cut edge, pick the contract mechanism:

| Mechanism | Coupling | Use when |
| :--- | :--- | :--- |
| **data source by tags/name** | None — resolves against AWS | Default choice. `data "aws_subnets" { filter { name = "tag:Tier" ... } }` — but tag your resources first, tags are the API of your network stack |
| **SSM Parameter Store** (producer writes `aws_ssm_parameter`, consumer reads it) | Loose, explicit, versioned | Enterprise favorite: works cross-account, auditable, breaks no one when producer refactors internally |
| **`terraform_remote_state`** | Tight — consumer reads producer's whole state | Avoid at scale: grants read access to all outputs incl. sensitive values, couples you to state layout |

**Rule**: inside a stack, always resource references — never data sources for things you manage in the same state. A data source pointing at your own resource hides the dependency edge from Terraform's planner and creates eventual-consistency races.

### C. Cycles = design errors with known fixes

The graph builder runs cycle detection (`networkx.simple_cycles`). In AWS imports, cycles almost always mean one thing: mutual security group references (app-SG allows db-SG, db-SG allows app-SG) that Terraformer captured as inline ingress/egress blocks. The fix is mechanical — hoist rules into standalone resources, which breaks the cycle because rules depend on both SGs but the SGs no longer depend on each other:

```hcl
resource "aws_vpc_security_group_ingress_rule" "db_from_app" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = aws_security_group.app.id
  from_port = 5432, to_port = 5432, ip_protocol = "tcp"
}
```

(Use the modern `aws_vpc_security_group_ingress_rule` — per-rule resources with real IDs — over legacy `aws_security_group_rule`.) Any other cycle the detector finds is a modeling error to fix before you split state, because a cycle spanning a stack boundary is unfixable later.

### D. Structural symmetry = for_each candidates

Resources with isomorphic edge signatures — three subnets each pointing at the same VPC, referenced by parallel route table associations — are your `for_each` collapse candidates:

```hcl
resource "aws_subnet" "private" {
  for_each          = var.private_subnets  # { a = "10.0.1.0/24", b = "10.0.2.0/24", ... }
  vpc_id            = aws_vpc.main.id
  cidr_block        = each.value
  availability_zone = "${var.aws_region}${each.key}"
}
```

Two hard rules: key by stable logical names ("a", "b"), never by list index or anything derived from an ID — index-keyed `for_each` reshuffles on every addition; and migrate state with `moved` blocks to indexed addresses (`moved { from = aws_subnet.tfer--subnet-0123; to = aws_subnet.private["a"] }`), verified by — always — a zero plan.

### E. depends_on — the graph tells you when it's honest

After reconstruction, almost every dependency is expressed through references, so audit every `depends_on` against the computed graph: if an edge already exists via a reference, the `depends_on` is noise — delete it. Keep it only for genuinely hidden dependencies invisible to the value graph: IAM eventual consistency (instance boots before its role policy attaches), NAT gateway ready before instances needing egress bootstrap.

---

## The verification battery (per-PR, non-negotiable)

```bash
terraform validate                                   # catches dangling refs immediately
terraform plan -detailed-exitcode                    # exit 0 = zero diff = invariant holds
python3 relink.py residue .                          # exit 1 = hardcoded IDs remain
terraform graph | dot -Tsvg > actual.svg             # eyeball vs computed spec graph
terraform plan -destroy -out=/dev/null               # sanity: destroy ORDER is sane (never apply!)
```

Plus the meta-rule that makes this whole phase safe: one bucket per PR (one commit for `REWRITTEN`, one per `EMBEDDED` rewrite, one per `for_each` collapse), each with the dry-run report attached and a zero-diff plan as the merge gate. When something goes wrong — and one embedded rewrite will — you bisect in minutes instead of unwinding a 4,000-line mega-commit.

The attached `relink.py` gives you registry, rewrite `--apply`, graph (DOT + topo order + cycle detection), and residue (CI gate) — swap in your real `terraform show -json` output and extend `IDENTIFYING_ATTRS`/`AWS_ID_RE` for the resource types in your estate.
