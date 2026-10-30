# cleanup-orphan-snapshots

Finds, and optionally destroys, **orphaned automatic ZFS snapshots** on TrueNAS SCALE.

TrueNAS removes snapshots made by a periodic snapshot task once they pass that
task's retention. That cleanup only covers snapshots the task still recognises.
Snapshots get left behind for good when:

- a periodic snapshot task is deleted,
- a task's naming schema is changed,
- a dataset is renamed or moved out of a recursive task,
- a dataset is added to a task's exclude list.

These leftovers keep taking space and nothing will ever remove them. This script
lists them and, when asked, destroys them.

## How a snapshot is classified as orphaned

A snapshot is **orphaned** if all of these are true:

1. **Its name looks automatic.** The name matches a `--pattern` regex
   (default: `^auto-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}`, the TrueNAS default
   `auto-%Y-%m-%d_%H-%M`, plus any suffix).
2. **No periodic snapshot task owns it.** No task, enabled or disabled, covers
   its dataset (directly, or through a recursive parent that does not exclude it)
   with a naming schema that matches the name.
3. **No replication task owns it.** It does not sit under a replication task's
   source or target datasets (including their children) with a name that matches
   one of these:
   - the task's own naming schemas,
   - the schemas of the periodic snapshot tasks linked to it,
   - its name regex.
4. **It is old enough.** It is older than `--min-age-days` (default 7).

Even an orphan is **left alone** if:

- its dataset is excluded with `--exclude`,
- its dataset is **read-only**. TrueNAS makes replication targets read-only by
  default, and this also covers datasets that another NAS pushes to, which this
  machine has no task for. Use `--include-readonly` to override.
- TrueNAS has already scheduled its removal (the `org.truenas:destroy_at` user
  property),
- it has a user hold or a clone.

TrueNAS naming schemas (strftime format) are turned into regexes, so a task using
`auto-%Y-%m-%d_%H-%M-2w` protects `auto-2026-01-01_00-00-2w` and nothing else.

## Requirements

- TrueNAS SCALE, with `midclt` and `zfs` on the `PATH`
- Python 3 (included with TrueNAS)
- Root privileges (the script queries the middleware and destroys snapshots)

## Installation

Copy the script to a dataset on your pool, for example:

```sh
mkdir -p /mnt/storage/scripts
cp zfs_orphan_snapshots.py /mnt/storage/scripts/
```

Call it with `python3 /path/to/script` rather than running it directly, so it
works even if the dataset is mounted `noexec`.

## Usage

```sh
# Dry run (the default): list candidates, destroy nothing
python3 zfs_orphan_snapshots.py

# Only look at one pool or dataset (recursive)
python3 zfs_orphan_snapshots.py --root storage/media

# Never touch some datasets (e.g. a replication target that is not read-only)
python3 zfs_orphan_snapshots.py --exclude backup/offsite --exclude storage/vm

# Report even when there is nothing to do, with the count of orphans left alone
python3 zfs_orphan_snapshots.py --verbose

# Destroy the candidates (at most 50, see --max-destroy)
python3 zfs_orphan_snapshots.py --destroy
```

### Options

| Option               | Default                               | Description                                                                                                                                                                    |
| -------------------- | ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--root DATASET`     | all pools                             | Only check this pool/dataset and its children.                                                                                                                                 |
| `--pattern REGEX`    | `^auto-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}` | Regex (Python `re.search`) that a snapshot name must match to count as automatic. Repeatable. Setting it replaces the default.                                                 |
| `--exclude DATASET`  | none                                  | Never touch snapshots of this dataset or its children. Repeatable. The script stops with an error if the dataset does not exist, so a typo cannot leave a dataset unprotected. |
| `--include-readonly` | off                                   | Also consider read-only datasets. Only use it if you are sure none of them is a replication target.                                                                            |
| `--min-age-days N`   | `7`                                   | Never touch snapshots younger than this. Decimals allowed.                                                                                                                     |
| `--max-destroy N`    | `50`                                  | If there are more candidates than this, destroy nothing and exit with code 2. A dry run prints a warning instead.                                                              |
| `--destroy`          | off                                   | Actually destroy the candidates. Without it, the script only reports.                                                                                                          |
| `--verbose`          | off                                   | Print a summary line even when no orphans are found.                                                                                                                           |

If your tasks use their own naming schemes, pass one `--pattern` for each, for
example:

```sh
python3 zfs_orphan_snapshots.py --pattern '^auto-' --pattern '^daily-\d{8}'
```

Anchor your patterns with `^`. Without it, `re.search` matches anywhere in the name.

### Sample output

Messages are in French and use ASCII only:

```
== Snapshots orphelins - SIMULATION (rien n'est supprime) ==
3 candidats sur 1284 snapshots examines. 'used' = espace propre a chaque snapshot ; l'espace reellement libere peut etre superieur (blocs partages).
Orphelins ignores : 412 sur dataset en lecture seule, 1 proteges par hold/clone.
  storage/old-share: 3 snapshots, ~1.2 Gio
    storage/old-share@auto-2026-02-28_00-00  (244 j, 512.0 Mio)
    storage/old-share@auto-2026-03-07_00-00  (237 j, 400.0 Mio)
    storage/old-share@auto-2026-03-14_00-00  (230 j, 317.0 Mio)

Relancer avec --destroy pour supprimer apres verification.
```

The `Orphelins ignores` line counts orphans that were spared, and why. Those
snapshots are not listed.

Per-dataset totals add up each snapshot's `used` property, which counts only the
space unique to that snapshot. Destroying several adjacent snapshots usually
frees more than that total, because blocks shared only among them are released too.

## Exit codes

| Code | Meaning                                                                                                           |
| ---- | ----------------------------------------------------------------------------------------------------------------- |
| `0`  | Success: nothing to do, dry run finished, or all destroys succeeded                                               |
| `1`  | Error: could not query the middleware or `zfs`, unknown `--exclude` dataset, or at least one `zfs destroy` failed |
| `2`  | Safety stop: more candidates than `--max-destroy`, nothing destroyed                                              |

## Running it as a TrueNAS cron job

When there is nothing to report, the script prints nothing, so TrueNAS only
sends an email when there is something to read.

1. Go to **System → Advanced Settings → Cron Jobs → Add**.
2. Command: `python3 /mnt/storage/scripts/zfs_orphan_snapshots.py`
3. Run As User: `root`
4. Leave **Hide Standard Output** and **Hide Standard Error** unchecked, so you
   get the report by email.
5. Choose a schedule, for example weekly.

Recommended rollout:

1. Run `--verbose` by hand once. Check that the number of orphans skipped on
   read-only datasets matches your replication targets.
2. Run in dry-run mode for a few cycles and read every report.
3. When the candidates look right, add `--destroy` to the command.
4. If a run exits with code 2, find out why so many snapshots suddenly look
   orphaned before you raise `--max-destroy`. A common cause is a task that was
   deleted or edited by mistake.

## Safety features

- **Dry run by default.** Nothing is destroyed without `--destroy`.
- **Mass-deletion guard.** `--max-destroy` stops the run if too many candidates
  appear at once, and a dry run warns you ahead of time.
- **Replication targets are protected.** Two layers cover them: the source and
  target datasets of every local replication task, and every read-only dataset,
  which also covers pushes from other machines.
- **Fails closed.** If the replication tasks cannot be read, an `--exclude`
  dataset does not exist, or a snapshot's dataset cannot be found, the script
  stops or skips the snapshot instead of guessing.
- **Disabled tasks still protect.** A disabled task's snapshots are kept, in case
  you turn the task back on.
- **TrueNAS-scheduled removals are respected.** Snapshots TrueNAS will remove on
  its own are left alone.
- **Holds and clones are respected.** Snapshots with a hold or a clone are
  skipped.
- **Minimum age.** Recent snapshots are never touched.

## Caveats

- **Replication that TrueNAS doesn't know about is invisible.** This covers
  `zfs send` scripts and tools like syncoid, as well as targets whose TrueNAS
  read-only policy is set to _Ignore_. Protect those datasets with `--exclude`.
  If you destroy the last snapshot a source and target have in common, the next
  incremental send fails.
- **`--include-readonly` turns off the push-target guard.** Snapshots received
  from another NAS look orphaned on this machine.
- **Replication protection covers whole subtrees.** Every child of a replication
  source or target is protected, even children the replication excludes. This
  can hide some real orphans, which is the safe trade-off.
- **Only names that look automatic are checked.** Manual snapshots and snapshots
  from other features (cloud sync, apps, etc.) are ignored unless a `--pattern`
  matches them.
