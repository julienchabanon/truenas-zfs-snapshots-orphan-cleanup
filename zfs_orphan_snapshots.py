#!/usr/bin/env python3
"""
zfs_orphan_snapshots.py - find (and optionally destroy) orphaned ZFS snapshots
on TrueNAS SCALE.

A snapshot is considered orphaned when all of the following hold:
  1. its name looks like an automatic snapshot
     (default pattern: auto-YYYY-MM-DD_HH-MM),
  2. no existing TrueNAS periodic snapshot task (enabled or not) claims it
     (same dataset, or a parent with recursion, and a compatible naming schema),
  3. no replication task (enabled or not) claims it: it does not live under
     one of the task's source or target datasets with a name matching the
     task's naming schemas, its linked periodic tasks' schemas or its regex,
  4. it is older than --min-age-days.

Orphans are still left alone when:
  - their dataset is under an --exclude dataset,
  - their dataset is read-only: replication targets are read-only by default,
    including targets of a push from another machine, which this machine has
    no record of (override with --include-readonly),
  - TrueNAS has already scheduled their removal (org.truenas:destroy_at),
  - they have a hold or a clone.

Default mode is a dry run: nothing is destroyed without --destroy.
Safety net: if the number of candidates exceeds --max-destroy, nothing is
destroyed (a sudden mass detection usually points to a configuration mistake).

The script prints nothing when there is nothing to report, so a TrueNAS cron
job only sends an email when there is something to read.

Exit codes: 0 ok, 1 error, 2 --max-destroy safety net triggered.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter

DEFAULT_PATTERNS = [r"^auto-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}"]

# User property TrueNAS sets on snapshots it will remove itself at a given date
# (e.g. when a task is deleted with the option to keep its snapshots until then).
REMOVAL_DATE_PROP = "org.truenas:destroy_at"

MATCH_ALL = re.compile("")

# strftime directives -> regex fragments (used to turn TrueNAS naming schemas into regexes)
STRFTIME = {
    "Y": r"\d{4}", "y": r"\d{2}", "m": r"\d{2}", "d": r"\d{2}",
    "H": r"\d{2}", "M": r"\d{2}", "S": r"\d{2}", "j": r"\d{3}",
    "z": r"[+-]\d{4}", "Z": r"[A-Za-z]+", "%": "%",
}


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")
    return r.stdout


def midclt(method):
    return json.loads(run(["midclt", "call", method]))


def schema_to_regex(schema):
    """Convert a TrueNAS strftime naming schema (e.g. auto-%Y-%m-%d_%H-%M) to a regex."""
    out, i = [], 0
    while i < len(schema):
        c = schema[i]
        if c == "%" and i + 1 < len(schema):
            out.append(STRFTIME.get(schema[i + 1], r".+?"))
            i += 2
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def under(ds, root, recursive):
    return ds == root or (recursive and ds.startswith(root + "/"))


class Owners:
    """Everything that can legitimately 'own' a snapshot."""

    def __init__(self, tasks, replications):
        # (root datasets, recursive, excluded datasets, name regexes)
        self.owners = []
        by_id = {t.get("id"): t for t in tasks}
        for t in tasks:
            self.owners.append(([t["dataset"]], bool(t.get("recursive")),
                                list(t.get("exclude") or []),
                                [schema_to_regex(t["naming_schema"])]))
        for r in replications:
            rxs = [schema_to_regex(s)
                   for key in ("naming_schema", "also_include_naming_schema")
                   for s in r.get(key) or []]
            for t in r.get("periodic_snapshot_tasks") or []:
                t = t if isinstance(t, dict) else by_id.get(t)
                # A linked task we cannot resolve: protect everything.
                rxs.append(schema_to_regex(t["naming_schema"]) if t else MATCH_ALL)
            if r.get("name_regex"):
                rxs.append(re.compile(r["name_regex"]))
            # Whole source and target subtrees, whatever the direction and
            # transport: a remote name that also exists here only makes us
            # more conservative.
            roots = list(r.get("source_datasets") or [])
            if r.get("target_dataset"):
                roots.append(r["target_dataset"])
            self.owners.append((roots, True, [], rxs or [MATCH_ALL]))

    def owned(self, ds, snap):
        for roots, recursive, exclude, rxs in self.owners:
            if not any(under(ds, root, recursive) for root in roots):
                continue
            if any(under(ds, ex, True) for ex in exclude):
                continue
            if any(rx.match(snap) for rx in rxs):
                return True
        return False


def list_datasets():
    """Return {dataset: is_readonly} for every filesystem and volume."""
    out = run(["zfs", "list", "-H", "-o", "name,readonly", "-t", "filesystem,volume"])
    return {name: ro == "on" for name, ro in (l.split("\t") for l in out.splitlines())}


def list_snapshots(root):
    cmd = ["zfs", "list", "-H", "-p", "-t", "snapshot",
           "-o", f"name,used,userrefs,clones,creation,{REMOVAL_DATE_PROP}"]
    if root:
        cmd += ["-r", root]
    for line in run(cmd).splitlines():
        name, used, userrefs, clones, creation, removal = line.split("\t")
        ds, snap = name.split("@", 1)
        yield {
            "full": name, "ds": ds, "snap": snap,
            "used": int(used), "held": userrefs not in ("0", "-"),
            "has_clones": clones not in ("-", ""), "created": int(creation),
            "removal_scheduled": removal not in ("-", ""),
        }


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="limit to a pool/dataset (recursive), e.g. tank")
    ap.add_argument("--pattern", action="append",
                    help="regex matching 'automatic' snapshot names (repeatable). "
                         f"Default: {DEFAULT_PATTERNS[0]}")
    ap.add_argument("--exclude", action="append", default=[], metavar="DATASET",
                    help="never touch snapshots of this dataset or its children "
                         "(repeatable), e.g. a replication target")
    ap.add_argument("--include-readonly", action="store_true",
                    help="also consider read-only datasets (skipped by default "
                         "because replication targets are read-only)")
    ap.add_argument("--min-age-days", type=float, default=7.0,
                    help="never touch snapshots newer than this (default 7)")
    ap.add_argument("--max-destroy", type=int, default=50,
                    help="refuse to destroy more than this many snapshots (default 50)")
    ap.add_argument("--destroy", action="store_true",
                    help="actually destroy snapshots (default: dry run)")
    ap.add_argument("--verbose", action="store_true",
                    help="also print a message when there is nothing to report")
    args = ap.parse_args()

    patterns = [re.compile(p) for p in (args.pattern or DEFAULT_PATTERNS)]
    excluded = [e.rstrip("/") for e in args.exclude]

    try:
        tasks = midclt("pool.snapshottask.query")
        try:
            repls = midclt("replication.query")
        except RuntimeError as e:
            # Without replication info we cannot guarantee safety, so stop here.
            raise RuntimeError(f"cannot read replication tasks ({e})")
        owners = Owners(tasks, repls)
        datasets = list_datasets()
        # A typo in --exclude would silently leave the dataset unprotected.
        unknown = [e for e in excluded if e not in datasets]
        if unknown:
            raise RuntimeError(f"--exclude: no such dataset: {', '.join(unknown)}")
        snaps = list(list_snapshots(args.root))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    now = time.time()
    min_age = args.min_age_days * 86400
    candidates, skipped = [], Counter()
    for s in snaps:
        if not any(p.search(s["snap"]) for p in patterns):
            continue
        if owners.owned(s["ds"], s["snap"]):
            continue
        if now - s["created"] < min_age:
            continue
        if any(under(s["ds"], ex, True) for ex in excluded):
            skipped["excluded by --exclude"] += 1
        # Unknown dataset (renamed meanwhile?) is treated as read-only.
        elif datasets.get(s["ds"], True) and not args.include_readonly:
            skipped["on a read-only dataset"] += 1
        elif s["removal_scheduled"]:
            skipped["already scheduled for removal by TrueNAS"] += 1
        elif s["held"] or s["has_clones"]:
            skipped["protected by a hold/clone"] += 1
        else:
            candidates.append(s)
    skipped_txt = ", ".join(f"{n} {why}" for why, n in skipped.items()) or "none"

    if not candidates:
        if args.verbose:
            print(f"No orphaned snapshots ({len(snaps)} snapshots checked, "
                  f"orphans skipped: {skipped_txt}).")
        return 0

    # Aggregate per dataset: [count, total unique space]
    by_ds = {}
    for s in candidates:
        d = by_ds.setdefault(s["ds"], [0, 0])
        d[0] += 1
        d[1] += s["used"]

    mode = "DESTROY" if args.destroy else "DRY RUN (nothing is destroyed)"
    print(f"== Orphaned snapshots - {mode} ==")
    print(f"{len(candidates)} candidates out of {len(snaps)} snapshots checked. "
          f"'used' is the space unique to each snapshot; the space actually "
          f"freed can be larger (shared blocks).")
    print(f"Orphans skipped: {skipped_txt}.")
    for ds, (n, used) in sorted(by_ds.items(), key=lambda kv: -kv[1][1]):
        print(f"  {ds}: {n} snapshots, ~{human(used)}")
    for s in sorted(candidates, key=lambda x: x["created"]):
        age = int((now - s["created"]) / 86400)
        print(f"    {s['full']}  ({age} d, {human(s['used'])})")

    too_many = len(candidates) > args.max_destroy
    if not args.destroy:
        if too_many:
            print(f"\nWARNING: {len(candidates)} candidates > --max-destroy "
                  f"({args.max_destroy}): --destroy would refuse to destroy anything.")
        print("\nRun again with --destroy to destroy them once reviewed.")
        return 0

    if too_many:
        print(f"\nREFUSED: {len(candidates)} candidates > --max-destroy "
              f"({args.max_destroy}). Check the task configuration, then run again "
              f"with a higher limit if this is expected.", file=sys.stderr)
        return 2

    failures = 0
    for s in candidates:
        try:
            run(["zfs", "destroy", "--", s["full"]])
            print(f"destroyed: {s['full']}")
        except RuntimeError as e:
            failures += 1
            print(f"failed: {e}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
