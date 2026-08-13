#!/usr/bin/env python3
"""
relink.py -- Terraformer de-hardcoding toolkit.

Subcommands:
  registry  state.json                 -> print the ID->reference symbol table
  rewrite   state.json repo/ [--apply] -> replace quoted literals with references (dry-run default)
  graph     state.json repo/           -> emit DOT of the reconstructed dependency DAG + cycle check
  residue   repo/                      -> find AWS-shaped IDs that survived (exit 1 if any)

Ground truth is ALWAYS `terraform show -json` output, never the HCL.
"""
import json, re, sys, os, glob, collections

# ---------------------------------------------------------------- registry --
# Which state attributes are "identifying" and what expression suffix they map to.
IDENTIFYING_ATTRS = {
    "id": "id", "arn": "arn", "name": "name", "url": "url",
    "unique_id": "unique_id", "key_id": "key_id", "bucket": "bucket",
    "endpoint": "endpoint", "dns_name": "dns_name", "zone_id": "zone_id",
    "cidr_block": None,  # collected for ambiguity awareness, never auto-rewritten
}
# Values too generic to ever rewrite automatically.
BLOCKLIST = {"default", "main", "true", "false", "*", "", "enabled", "disabled"}
MIN_LEN = 6  # never rewrite short strings

def load_resources(state_path):
    with open(state_path) as f:
        state = json.load(f)
    out = []
    def walk_module(mod):
        for r in mod.get("resources", []):
            if r.get("mode", "managed") == "managed":
                out.append(r)
        for child in mod.get("child_modules", []):
            walk_module(child)
    walk_module(state["values"]["root_module"])
    return out

def build_registry(state_path):
    """value -> list of (address, attr, ref_expr)"""
    registry = collections.defaultdict(list)
    for r in load_resources(state_path):
        addr = r["address"]
        for attr, suffix in IDENTIFYING_ATTRS.items():
            v = r["values"].get(attr)
            if not isinstance(v, str) or len(v) < MIN_LEN or v.lower() in BLOCKLIST:
                continue
            ref = f"{addr}.{suffix}" if suffix else None
            registry[v].append({"address": addr, "attr": attr, "ref": ref})
    # Dedupe: if all owners of a value are the SAME resource (e.g. iam_role
    # id == name), keep one entry by attribute priority. Real ambiguity is
    # only when DIFFERENT addresses own the same value.
    PRIORITY = {"id": 0, "name": 1, "arn": 2, "url": 3}
    for v, owners in registry.items():
        if len({o["address"] for o in owners}) == 1 and len(owners) > 1:
            owners.sort(key=lambda o: PRIORITY.get(o["attr"], 9))
            registry[v] = owners[:1]
    return registry

# ----------------------------------------------------------------- rewrite --
RESOURCE_RE = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"\s*\{')
HEREDOC_RE  = re.compile(r'<<-?\s*(\w+)\s*$')

def iter_lines_with_context(text):
    """Yield (lineno, line, owner_address, in_heredoc). Tracks resource blocks
    via brace counting and heredoc bodies via terminator tracking."""
    owner, depth, heredoc_end = None, 0, None
    for i, line in enumerate(text.splitlines(keepends=True), 1):
        if heredoc_end:
            yield i, line, owner, True
            if line.strip() == heredoc_end:
                heredoc_end = None
            continue
        m = RESOURCE_RE.match(line)
        if m and depth == 0:
            owner = f"{m.group(1)}.{m.group(2)}"
        yield i, line, owner, False
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            depth, owner = 0, owner if depth > 0 else None
        hm = HEREDOC_RE.search(line)
        if hm:
            heredoc_end = hm.group(1)

def rewrite(state_path, repo, apply=False):
    registry = build_registry(state_path)
    report = {"rewritten": [], "embedded": [], "ambiguous": [], "self": []}
    for path in sorted(glob.glob(os.path.join(repo, "**", "*.tf"), recursive=True)):
        text = open(path).read()
        new_lines = []
        for lineno, line, owner, in_heredoc in iter_lines_with_context(text):
            out = line
            for value, owners in registry.items():
                if value not in line:
                    continue
                if len(owners) > 1:  # ambiguous: never auto-rewrite
                    report["ambiguous"].append((path, lineno, value, [o["address"] for o in owners]))
                    continue
                o = owners[0]
                if o["address"] == owner:  # a resource's own identifying attr
                    report["self"].append((path, lineno, value, o["address"]))
                    continue
                if o["ref"] is None:
                    continue
                exact = f'"{value}"'
                if not in_heredoc and exact in out:
                    out = out.replace(exact, o["ref"])
                    report["rewritten"].append((path, lineno, value, o["ref"]))
                elif value in out and not RESOURCE_RE.match(out):
                    # embedded inside a larger string / heredoc body
                    report["embedded"].append((path, lineno, value, o["ref"]))
            new_lines.append(out)
        if apply:
            open(path, "w").write("".join(new_lines))
    return report

# ------------------------------------------------------------------- graph --
def graph(state_path, repo):
    import networkx as nx
    registry = build_registry(state_path)
    G = nx.DiGraph()
    for r in load_resources(state_path):
        G.add_node(r["address"])
    # Edge: consumer -> producer, discovered by scanning state values of every
    # resource for identifying values owned by OTHER resources.
    for r in load_resources(state_path):
        blob = json.dumps(r["values"])
        for value, owners in registry.items():
            if len(owners) != 1 or owners[0]["address"] == r["address"]:
                continue
            if value in blob:
                G.add_edge(r["address"], owners[0]["address"], label=owners[0]["attr"])
    cycles = list(nx.simple_cycles(G))
    print("digraph deps {\n  rankdir=LR;")
    for u, v, d in G.edges(data=True):
        print(f'  "{u}" -> "{v}" [label="{d["label"]}"];')
    print("}")
    print(f"\n// nodes={G.number_of_nodes()} edges={G.number_of_edges()}", file=sys.stderr)
    if cycles:
        print(f"// CYCLES DETECTED: {cycles}", file=sys.stderr)
    else:
        order = list(__import__('networkx').topological_sort(G))
        print("// topological order (leaves last):", file=sys.stderr)
        for n in order:
            print(f"//   {n}", file=sys.stderr)

# ----------------------------------------------------------------- residue --
AWS_ID_RE = re.compile(
    r'\b(?:vpc|subnet|sg|igw|rtb|eni|ami|vol|eipalloc|nat|acl|rtbassoc|vgw|tgw|pcx|dopt|fl|snap|lt)-'
    r'(?:[0-9a-f]{8}|[0-9a-f]{17})\b|\bi-[0-9a-f]{8,17}\b|arn:aws[a-z-]*:[^"\s\\]+')

QUOTED_RE = re.compile(r'"([^"]*)"')

def residue(repo):
    """Flag AWS-shaped IDs only inside quoted strings and heredoc bodies --
    i.e. actual hardcoded values, not reference expressions or resource names.
    Skips resource/data declaration lines (synthetic tfer-- names)."""
    hits = 0
    for path in sorted(glob.glob(os.path.join(repo, "**", "*.tf"), recursive=True)):
        text = open(path).read()
        for lineno, line, owner, in_heredoc in iter_lines_with_context(text):
            if line.lstrip().startswith("#") or RESOURCE_RE.match(line):
                continue
            spans = [line] if in_heredoc else QUOTED_RE.findall(line)
            for span in spans:
                for m in AWS_ID_RE.finditer(span):
                    print(f"{path}:{lineno}: {m.group(0)}")
                    hits += 1
    return hits

# -------------------------------------------------------------------- main --
if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "registry":
        for value, owners in sorted(build_registry(sys.argv[2]).items()):
            flag = "AMBIGUOUS " if len(owners) > 1 else ""
            for o in owners:
                print(f"{flag}{value!r:70} -> {o['ref']}")
    elif cmd == "rewrite":
        rep = rewrite(sys.argv[2], sys.argv[3], apply="--apply" in sys.argv)
        for k in ("rewritten", "embedded", "ambiguous", "self"):
            print(f"\n== {k.upper()} ({len(rep[k])}) ==")
            for row in rep[k]:
                print("  ", *row)
        mode = "APPLIED" if "--apply" in sys.argv else "DRY-RUN (pass --apply to write)"
        print(f"\n{mode}")
    elif cmd == "graph":
        graph(sys.argv[2], sys.argv[3])
    elif cmd == "residue":
        sys.exit(1 if residue(sys.argv[2]) else 0)
