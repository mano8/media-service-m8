# Data migration runbook — MinIO → SeaweedFS (object bytes)

`T25-data-migration-runbook` of the object-storage backend migration plan.
This is the procedure for carrying **existing objects** from a stack that ran
MinIO onto the SeaweedFS backend every `media-service-m8` / `fa-ui-m8` compose
stack now ships (Wave 3). It is written for `hardened_media_m8`; the other
stacks have the same shape and take the same steps with their own directory
and env files.

Skip it entirely for a **clean start** (new deployment, or a dev stack whose
objects nobody needs): boot the stack, `storage-init` creates the five empty
buckets, done. The plan's own risk table says so — a clean start deletes this
document and most of the risk.

**Read before touching anything:**

* The point of no return is not the copy — it is **decommissioning the frozen
  MinIO** at the end of the rollback window (§7). Until then every step is
  reversible and the rollback in §8 is a compose/env flip plus one reverse
  copy.
* Nothing here changes application code or the compose stack this branch
  already ships. The only additions are an overlay (`docker-compose.migration.yml`)
  that adds the frozen MinIO and an `rclone` one-shot behind the `migration`
  profile, and the digest join script `verify_migration_digests.py`.
* Every check in §5 was run for real against a frozen MinIO
  (`RELEASE.2025-09-07T16-13-09Z.hotfix.7aa24e772`) and this stack's live
  `chrislusf/seaweedfs:4.45` through the shipped overlay on 2026-09-13 —
  copy, count/bytes parity, byte-for-byte `check --download`, `hashsum sha256
  --download` joined against a DB-shaped digest export (matched / mismatch /
  missing / uncovered all exercised), and `Content-Type` preservation across
  the copy (a custom `application/x-…` type survived S3→S3). Numbers in the
  parity table below are yours to fill; the commands are not hypothetical.

---

## 1. Roles, names, what is being moved

| Term | Meaning here |
| --- | --- |
| **SRC** | The frozen MinIO — `minio-frozen` in the overlay, the exact pre-migration image on the old `./minio/data` volume, `data_net` only, no host port, no console. Reached by rclone as `src:`. |
| **DST** | The SeaweedFS `storage` service the base compose file already runs. Reached by rclone as `dst:`. |
| **Buckets** | `S3_BUCKET_PUBLIC` `S3_BUCKET_PRIVATE` `S3_BUCKET_SENSITIVE` `S3_BUCKET_TEMP` `S3_BUCKET_ARCHIVE` from `media.env` — defaults `public-media` `private-media` `sensitive-media` `temp-media` `archive-media`. All five are copied; `archive-media` is reserved and normally empty, `temp-media` holds transfer exports (`<owner>/exports/<job>.zip`) that expire. |
| **DB truth** | `<TABLES_PREFIX>_media_object` (`storage_bucket`, `object_key`, `sha256`, `deleted_at`), `<TABLES_PREFIX>_media_variant` (`storage_bucket`, `object_key`), `<TABLES_PREFIX>_export_job` (`storage_bucket`, `object_key`, `expires_at`). `TABLES_PREFIX` defaults to `app`, so `app_media_object` etc. The **required set** is the live rows (`deleted_at IS NULL`), their variants, and unexpired exports — the same set the reconciler guarantees. Soft-deleted rows are copied like everything else if their bytes still exist, but a public object's bytes are removed at soft-delete time by design, so they are not a parity requirement. |
| **`sha256` column** | Set only when the client declared a digest at `complete` and the service verified it against the streamed bytes. Rows without one are **uncovered** by the digest join and rely on the byte-for-byte check instead. Both checks run; neither is optional. |
| **Writers** | `media_service` (uploads, visibility moves, deletes), `media_service_worker` (arq crons: `hard_purge_expired`, `expire_stale_uploads`, `reconcile_orphans`, `deliver_outbox`), `media_worker` (variants, archive builds). All three must be **stopped** during the freeze (§4). |
| **Reports** | `./migration-reports/` (gitignored). Every command below writes there so the evidence outlives the terminal. |

MinIO keeps objects in its own `xl.meta` erasure layout — you cannot copy
`./minio/data` into `./seaweedfs/data`. The move is S3 → S3, through the API,
which is what rclone does.

---

## 2. Preconditions (do not start without all of them)

* [ ] **The DST stack is already the one this branch ships and has passed
      its verification:** `T23` live suite (40/40) and the `T24` matrix
      (15/15, `SECURITY_REGRESSION_MATRIX.md`) on *this* instance or an
      identical one. The migration is a data step, not the place to find a
      backend bug.
* [ ] **`minio/data` is intact** and has not been touched since the last
      MinIO shutdown. `du -sh minio/data` — record the size in §3.1.
* [ ] **Free space on the DST volume ≥ 1.2 × that size** (SeaweedFS
      pre-allocates volume files; give it headroom).
* [ ] **The frozen MinIO's original root credentials.** MinIO encrypts
      `.minio.sys` with them and will not start under different ones. Put
      exactly the pair it last ran with in `.env` as `MINIO_ROOT_USER` /
      `MINIO_ROOT_PASSWORD` for the migration window (they were there before
      `T16` renamed the *SeaweedFS* admin pair to `S3_ROOT_*`; the two pairs
      may or may not share values — keep both lines). Under the production
      overlay `MINIO_ROOT_USER_FILE` / `MINIO_ROOT_PASSWORD_FILE` work too.
* [ ] **DB access:** `MEDIA_DB_USER` / `MEDIA_DB_PASSWORD` / `MEDIA_DB_NAME`
      from `.env`, via `docker compose exec m8_db psql`.
* [ ] **The rclone image pulled and its digest recorded:**
      `docker pull rclone/rclone:1.75.1 && docker image inspect
      rclone/rclone:1.75.1 --format '{{index .RepoDigests 0}}'` → §3.1.
* [ ] **A snapshot of the pre-migration stack directory** — `.env`,
      `media.env`, `worker.env`, `auth.env` and `traefik/` copied to a
      location **outside the repo** (they are gitignored; a rollback needs
      the MinIO-era values). Also record `git rev-parse HEAD` of this branch
      and the pre-migration commit you would roll back to (for this fleet's
      history that is `30212ca`, the last `main` before the plan branch).
* [ ] **A maintenance window** for §4–§6. Its length is the delta copy plus
      verification — minutes for a small stack, and the bulk copy in §3.3
      happens *before* the window so the delta is small.

Define the compose invocation once and use it everywhere below (add
`-f docker-compose.production.yml` between the two if that is what runs):

```sh
C="docker compose -f docker-compose.yml -f docker-compose.migration.yml --profile migration"
R="$C run --rm rclone"
mkdir -p migration-reports
```

`$R …` runs one rclone command with `src:` and `dst:` already configured
from `.env`/`media.env` (see the overlay's header). It exits non-zero if
either credential pair is missing.

---

## 3. Phase A — pre-flight and bulk copy (stack stays live)

### 3.1 Record the starting state

Fill this in `migration-reports/STATE.md` (or the ticket):

| Item | Value |
| --- | --- |
| Date / operator | |
| Branch + commit | `git rev-parse --abbrev-ref HEAD` / `git rev-parse HEAD` |
| Rollback commit | (see §2) |
| `minio/data` size | `du -sh minio/data` |
| MinIO image | `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z.hotfix.7aa24e772` |
| SeaweedFS image | `chrislusf/seaweedfs:4.45` (`docker compose images storage`) |
| rclone image + digest | |
| `MEDIA_RETENTION_PURGE_DAYS` | from `media.env` — sets the floor of the rollback window (§7) |

### 3.2 Bring up the frozen MinIO and prove both ends answer

```sh
$C up -d minio-frozen
$C ps minio-frozen                       # wait for (healthy)
$R lsd src:  | tee migration-reports/src-buckets.txt
$R lsd dst:  | tee migration-reports/dst-buckets.txt
```

`src:` must list the five buckets you expect; `dst:` must list the same five
(created by `storage-init`). A missing bucket on `src:` means the wrong data
directory or the wrong root credentials — stop and fix §2.

### 3.3 Source inventory — what the DB expects vs what MinIO holds

Object counts and bytes per bucket on SRC:

```sh
for b in public-media private-media sensitive-media temp-media archive-media; do
  printf '%s ' "$b"; $R size "src:$b" --json
done | tee migration-reports/src-size.txt
```

What the DB expects to find — the required set (§1): every key a live
object, one of its variants, or an unexpired export points at. Export it
once; §5.3 reuses the same file. (`COPY … TO STDOUT` needs only `SELECT`
on the tables; the multi-line statement is fine through `-c`.)

```sh
DBX="$C exec -T -e PGPASSWORD=$MEDIA_DB_PASSWORD m8_db psql -U $MEDIA_DB_USER -d $MEDIA_DB_NAME -At"

$DBX -c "COPY (
  SELECT storage_bucket, object_key, sha256 FROM app_media_object
     WHERE deleted_at IS NULL
  UNION ALL SELECT v.storage_bucket, v.object_key, NULL FROM app_media_variant v
     JOIN app_media_object o ON o.id = v.media_object_id WHERE o.deleted_at IS NULL
  UNION ALL SELECT storage_bucket, object_key, NULL FROM app_export_job
     WHERE object_key IS NOT NULL AND (expires_at IS NULL OR expires_at > now())
  ORDER BY 1, 2
) TO STDOUT CSV HEADER" > migration-reports/digests.csv
wc -l migration-reports/digests.csv
```

Then the **baseline**: which of those rows have no bytes on SRC *today*
(the old stack's own DB-orphans — public objects soft-deleted keep no
bytes by design, quarantined uploads may have had theirs removed, and a
row can simply be stale). A listing is enough, no download:

```sh
L=""
for b in public-media private-media sensitive-media temp-media archive-media; do
  $R lsf -R --files-only "src:$b" > "migration-reports/lsf-src-$b.txt"
  L="$L --listing $b=migration-reports/lsf-src-$b.txt"
done
python verify_migration_digests.py --digests migration-reports/digests.csv $L \
  --missing-out migration-reports/missing-src.txt | tee migration-reports/parity-src.txt
wc -l migration-reports/missing-src.txt
```

`missing-src.txt` is the baseline. Every key in it is one the migration
**cannot** carry because the source does not have it; every key *not* in
it must arrive on DST (§5.3 proves that with a `diff`). The script exits
`1` whenever `missing` is non-zero — expected here if the baseline is not
empty; the number is what you record. Per bucket, `src size.count` is
normally **≥** the required rows for it (storage orphans, in-flight
uploads, expired exports and soft-deleted bytes are all extra); a large
baseline is a pre-existing problem worth a look before you carry it, not a
migration issue.

The reconciler gives the same answer for live media objects older than its
grace window, and it is the number you will compare after cutover:

```sh
# On the OLD stack if it is still running, or on the new one after cutover
# (the report is read-only either way):
curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$MEDIA_BASE/v1/admin/maintenance/orphans" | tee migration-reports/orphans-before.json
```

`db_orphan_count` here is your baseline for **R3**; the same number after
cutover is expected, a larger one is a rollback. (It is normally smaller
than `missing-src.txt`'s line count: it skips rows inside the in-flight
grace window and does not look at variants or exports.)

### 3.4 Keys the public route cannot serve (finding F1)

Objects uploaded before the `keys.py` fix carry `;` `%` `?` or `#` in their
key and were never downloadable through Traefik. The migration copies them
as they are; this only tells you how many exist so the number is not a
surprise in a support ticket later:

```sh
$DBX -c "SELECT count(*) FROM app_media_object WHERE object_key ~ '[;%?#]'" \
  | tee migration-reports/f1-legacy-keys.txt
```

Re-keying them is a separate, DB-and-storage change — not part of this
runbook.

### 3.5 Bulk copy, live

Writers keep running. Anything that changes after this pass is picked up by
the delta copy in §4.

```sh
for b in public-media private-media sensitive-media temp-media archive-media; do
  $R copy "src:$b" "dst:$b" --checksum --transfers 8 --checkers 16 \
     --stats 30s --stats-one-line -v \
     --log-file "/reports/copy-bulk-$b.log"
done
```

* `copy`, never `sync` — `sync` deletes on the destination and there is no
  reason to hand rclone a delete verb in either direction.
* `--checksum` makes a re-run skip objects whose size and hash already
  match, so the loop is safe to repeat after an interruption.
* rclone carries `Content-Type` across (measured: a custom type set on SRC
  read back identical on DST). That matters — `S10` pins the served type
  from the stored object.
* Throughput is bounded by the Docker host; raise `--transfers` on a
  machine with the headroom. Watch `docker stats storage` — the SeaweedFS
  block has a 1 GiB memory limit.

### 3.6 First full verification, still live

```sh
for b in public-media private-media sensitive-media temp-media archive-media; do
  $R check "src:$b" "dst:$b" --download --one-way \
     --combined "/reports/check-bulk-$b.txt" \
     --log-file "/reports/check-bulk-$b.log" ; echo "$b exit=$?"
done
```

`--download` compares **bytes**, not ETags — MinIO's and SeaweedFS's ETags
differ for multipart objects, so a hash check would raise false alarms.
`--one-way` because objects that exist only on DST are impossible at this
point unless the new stack has already been written to.

In each `check-bulk-*.txt`: `=` identical, `-` missing on DST, `*` differ,
`!` error. Some `-`/`*` lines are expected here — they are objects written
or replaced on SRC since §3.5 — and the delta copy resolves them. **A `!`
line is not expected**: read the `.log`, fix (usually a transient), re-run
that bucket.

---

## 4. Phase B — freeze and delta (maintenance window opens)

### 4.1 Stop every writer

```sh
$C stop media_service media_service_worker media_worker
$C ps            # the three must be Exited; storage, minio-frozen, m8_db stay Up
```

From here no upload can be initiated, completed or purged. The browser sees
Traefik's `502`/`404` for the API; presigned links minted earlier still
resolve against **SeaweedFS** (the public route already points there), so a
download of an object that existed at the bulk copy keeps working.

### 4.2 Delta copy

```sh
for b in public-media private-media sensitive-media temp-media archive-media; do
  $R copy "src:$b" "dst:$b" --checksum --transfers 8 --checkers 16 \
     --stats-one-line -v --log-file "/reports/copy-delta-$b.log"
done
```

Small by construction: only what changed on SRC between §3.5 and §4.1.

### 4.3 Reclaimed objects

Between §3.5 and §4.1 the retention purge and stale-upload expiry may have
**deleted** objects on SRC that the bulk copy had already carried to DST.
They are now DST-only orphans — harmless (the reconciler will find them
after cutover) but they distort the count parity below. List them without
deleting anything:

```sh
for b in public-media private-media sensitive-media temp-media archive-media; do
  $R check "src:$b" "dst:$b" --size-only --missing-on-src "/reports/dst-only-$b.txt" \
     --log-file "/reports/dst-only-$b.log" || true
done
wc -l migration-reports/dst-only-*.txt
```

Delete them only if you want an exact count match and only from the listed
keys — `$R delete dst:<bucket> --files-from /reports/dst-only-<bucket>.txt --dry-run`
first, then without `--dry-run`. Otherwise carry the number into the table
and let `reconcile_orphans` remove them after cutover.

---

## 5. Phase C — verify parity (still frozen)

Three independent checks. **All three must be clean before §6.** Fill the
table as you go; it is the sign-off.

### 5.1 Count and byte parity per bucket

```sh
for b in public-media private-media sensitive-media temp-media archive-media; do
  printf '%s src=' "$b"; $R size "src:$b" --json
  printf '%s dst=' "$b"; $R size "dst:$b" --json
done | tee migration-reports/size-final.txt
```

`count` and `bytes` must be **equal** per bucket, or differ by exactly the
`dst-only-*` lines of §4.3 if you did not delete them.

### 5.2 Byte-for-byte parity per bucket

```sh
for b in public-media private-media sensitive-media temp-media archive-media; do
  $R check "src:$b" "dst:$b" --download --one-way \
     --combined "/reports/check-final-$b.txt" \
     --log-file "/reports/check-final-$b.log" ; echo "$b exit=$?"
done
grep -l '^[-*!]' migration-reports/check-final-*.txt || echo "all buckets identical"
```

Exit `0` and **no `-`, `*` or `!` line in any file**. This is the check that
covers every object, including the rows the DB has no digest for.

Large data sets: SRC is frozen, so re-downloading everything is honest but
slow. If §3.6 was clean except for since-changed objects, you may scope this
pass with `--max-age <duration since §3.5 started>` and keep the §3.6 files
as the evidence for the rest — write which you did in the table.

### 5.3 Digest parity against the DB `sha256` column

The required set is frozen with the writers, so re-export it (the §3.3
command, same file name) — a row created between §3.3 and §4.1 must be in
it. Then hash every object on **DST** (the copy, not the source) and join:

```sh
$DBX -c "COPY ( … the §3.3 statement … ) TO STDOUT CSV HEADER" > migration-reports/digests.csv

H=""
for b in public-media private-media sensitive-media temp-media archive-media; do
  $R hashsum sha256 --download "dst:$b" --output-file "/reports/sha256-$b.txt"
  H="$H --hashsum $b=migration-reports/sha256-$b.txt"
done
python verify_migration_digests.py --digests migration-reports/digests.csv $H \
  --missing-out migration-reports/missing-dst.txt | tee migration-reports/parity-dst.txt

diff migration-reports/missing-src.txt migration-reports/missing-dst.txt \
  && echo "nothing lost: DST is missing exactly what SRC was missing"
```

`parity-dst.txt` ends with `rows=N matched=A mismatch=B missing=C
uncovered=D`. Required:

* **`mismatch=0`** — a stored digest that does not match the bytes on DST
  is a corrupted copy, full stop.
* **`diff` empty** — `missing-dst.txt` equals the §3.3 baseline. A key in
  `missing-dst.txt` that is *not* in `missing-src.txt` is an object the
  source had and the destination does not: rollback trigger **R2**. (If the
  baseline was empty, this is the same as `missing=0`.)
* `uncovered` is informational: rows whose bytes are present on DST but
  carry no stored digest. Most fleets' clients never declare one, so
  expect this to be most or all rows — that is exactly why §5.2 (bytes
  against the source) is mandatory and not a fallback.

The script exits `1` on any `mismatch` or `missing`; with a non-empty
baseline, read the `diff` — that is the verdict — and record both.

### 5.4 Sign-off table

| Bucket | SRC count / bytes | DST count / bytes | §5.2 exit + `-*!` lines | §5.3 matched / mismatch / uncovered | §5.3 `diff` (baseline vs DST) | dst-only (§4.3) | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| public-media | | | | | | | |
| private-media | | | | | | | |
| sensitive-media | | | | | | | |
| temp-media | | | | | | | |
| archive-media | | | | | | | |

A row with a non-zero `mismatch`, a non-empty `diff`, a `*` or a `!` line
is **red** and stops the migration at this point: writers are still down,
nothing has been cut over, and the fix is to re-copy the named keys
(`$R copyto src:<b>/<key> dst:<b>/<key>`) and re-run §5 for that bucket.
If it does not converge, go to §8 — which at this stage is just "start the
writers against MinIO again" (§8.2 with no reverse copy).

---

## 6. Phase D — cutover

The compose file, Traefik route and every env file already point at
SeaweedFS (Wave 3). Cutover is starting the writers back up:

```sh
$C up -d storage-init            # idempotent: 5 × "already exists", CORS re-pinned, exit 0
$C up -d media_service media_service_worker media_worker
$C ps                            # all Up; minio-frozen stays Up (read-only, §7)
```

Immediately after — the maintenance window closes only when all four pass:

1. **Health:** `curl -fsS $MEDIA_BASE/health` → `{"status":"ok"}`.
2. **Reconcile, read-only:** `GET $MEDIA_BASE/v1/admin/maintenance/orphans`
   → `migration-reports/orphans-after.json`. `db_orphan_count` must equal
   the §3.3 baseline (**R3** if larger). `storage_orphan_count` may be the
   §4.3 dst-only number plus the pre-existing storage orphans — fine.
3. **The `T23` workflow suite** against this stack (browser-direct upload,
   scan gating, variants, share links, cross-bucket move, export, orphan
   repair, hard-purge):
   `cd ../shared_live_tests && STORAGE_LIVE_TEST_… pytest tests/live_storage/test_storage_workflow_live.py -p no:security_tests_m8`
   — exercising new writes on DST end to end.
4. **The `T24` wire-level invariants** (`test_storage_invariants_live.py`)
   — in particular the S9/S10/S11/S12 rows on the *app-minted* URLs and
   the F1 rows. Any failure on S9–S12 is **R4**.

Record the timestamp of step 4 passing as **`CUTOVER_TS`** in `STATE.md`. It
is the boundary the reverse copy in §8 uses.

---

## 7. Phase E — dual-read (rollback) window

`media_service` reads from exactly one `S3_ENDPOINT`; it does not fan reads
out to two backends. "Dual-read" here means what it can honestly mean: **both
backends hold a verified-identical copy of everything as of `CUTOVER_TS`,
MinIO stays up read-only on `data_net`, and any object can be read from
either by an operator** — so the migration can be undone without data loss
(for pre-cutover objects trivially; for post-cutover objects via the
reverse copy in §8) at any point inside the window.

**Length:** at least `MEDIA_RETENTION_PURGE_DAYS + 7` days, and never shorter
than 14. The retention term is the floor because a hard-purge on DST inside
the window is the one operation that makes the two copies diverge in the
*deleting* direction; after the term the objects it would have purged are
also past retention on SRC.

**Rules during the window:**

* `minio-frozen` stays `Up`. Nothing writes to it: the application has no
  route to it, and the only credential that reaches it is the root pair in
  `.env`, used by `$R` alone.
* Do **not** delete `minio/data`, do not `docker compose down -v`, do not
  remove `MINIO_ROOT_*` from `.env`.
* A single object that turns out wrong on DST (a `409`/`404` that should be
  a `200`, a digest complaint) is re-copied from SRC by key —
  `$R copyto "src:$b/$key" "dst:$b/$key"` — and the incident is noted in
  `STATE.md`. More than **three** such repairs, or one you cannot explain,
  is **R5**.
* **Signals to watch** (the same ones that fire R1/R6): `docker compose ps
  storage` health and restart count; Traefik access log `4xx`/`5xx` ratio on
  the `Host(storage.*)` router against the pre-migration baseline;
  `media_download_url_generated` vs. actual download errors reported by the
  UI; `GET …/maintenance/orphans` `db_orphan_count` once a day (it must not
  grow).

**Closing the window (the point of no return):**

```sh
$C stop minio-frozen && $C rm -f minio-frozen
mv minio/data "/backups/minio-data-$(date +%Y%m%d)"     # offline copy, outside the stack dir
```

Then remove the `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` lines from `.env`,
delete `migration-reports/` or archive it with the ticket, and append the
final `STATE.md` line: `window closed <date>, minio/data archived to <path>`.
After this the rollback in §8 is no longer a flip — it is a restore from the
offline copy plus the reverse copy, and it loses nothing only if that copy is
intact.

---

## 8. Rollback

### 8.1 Triggers — exact conditions, decided in advance

Any **one** of these, observed at the stage named, means roll back. Do not
argue the number in the moment; the point of writing them down is that the
decision is already made.

| # | Stage | Condition |
| --- | --- | --- |
| **R1** | §3–§5 | `storage` (SeaweedFS) becomes unhealthy or restarts during the copy, or `docker stats` shows it pinned at its 1 GiB limit for more than 5 minutes. |
| **R2** | §5.3 | Any `mismatch`, or a non-empty `diff missing-src.txt missing-dst.txt` (a key the source had that the destination does not), and a targeted `copyto` + re-run does not clear it. |
| **R3** | §6 step 2, or daily in §7 | `db_orphan_count` after cutover is **greater** than the §3.3 baseline. Live rows now point at bytes the new backend does not have. |
| **R4** | §6 step 4 | Any S9, S10, S11 or S12 row of `test_storage_invariants_live.py` fails against the migrated stack. A silent regression of those is the failure mode the whole plan exists to prevent. |
| **R5** | §7 | More than three single-object repairs, or one repair you cannot attribute to a known cause. |
| **R6** | §7 | For 15 consecutive minutes, the `storage.*` router's `4xx`+`5xx` share exceeds twice the pre-migration baseline **and** the `T24` invariants module still passes (so it is availability, not a configuration slip you can fix in place). |

Before cutover (R1, R2) the rollback is trivial: §8.2 without the reverse
copy. After cutover (R3–R6) the reverse copy is mandatory or every upload
since `CUTOVER_TS` is lost.

### 8.2 Procedure

1. **Freeze again:** `$C stop media_service media_service_worker media_worker`.
   Note the time as `ROLLBACK_TS`.
2. **Reverse delta — only if cutover happened.** Everything created on DST
   since `CUTOVER_TS` must go back to SRC. Bucket by bucket, copy only what
   is newer than the cutover:

   ```sh
   # GNU date; on macOS use `date -j -f "%Y-%m-%dT%H:%M:%S" "$CUTOVER_TS" +%s`
   AGE="$(( ($(date +%s) - $(date -d "$CUTOVER_TS" +%s)) / 60 + 5 ))m"   # minutes since cutover, +5 slack
   for b in public-media private-media sensitive-media temp-media archive-media; do
     $R copy "dst:$b" "src:$b" --checksum --max-age "$AGE" --transfers 8 \
        --stats-one-line -v --log-file "/reports/rollback-copy-$b.log"
     $R check "dst:$b" "src:$b" --download --one-way --max-age "$AGE" \
        --combined "/reports/rollback-check-$b.txt"; echo "$b exit=$?"
   done
   ```

   Exit `0` and no `-*!` line, same standard as §5.2. Objects **deleted** on
   DST since cutover (user deletes, purges) still exist on SRC; the
   application's `reconcile_orphans` will report them as storage orphans on
   MinIO after the flip and `repair?confirm=true` removes them — nothing to
   do by hand.
3. **Flip the stack back to MinIO.** Restore the pre-migration compose
   files and env files from the §2 snapshot:

   ```sh
   git stash list >/dev/null                    # make sure nothing uncommitted is lost
   git checkout <rollback-commit> -- docker-compose.yml docker-compose.production.yml traefik/
   cp /path/to/snapshot/{.env,media.env,worker.env,auth.env} .
   ```

   The rollback commit's compose block names the service `minio` on the
   same `./minio/data` volume, and its env files speak the `MINIO_*` names
   — which the `2.2.0` service still reads through its deprecation shim, so
   the app image does **not** need to change. `minio-frozen` must be stopped
   first (`$C stop minio-frozen`): two MinIO processes on one data
   directory is the one thing this procedure must never do.
4. **Start:** `docker compose up -d` (the restored file has no `migration`
   profile). `minio-init` re-applies buckets and the `media-rw` policy;
   `media_service` waits for it.
5. **Prove it:** `GET …/maintenance/orphans` — `db_orphan_count` back at the
   §3.3 baseline; a fresh upload → scan → download through the route; the
   `T23` workflow suite if time allows.
6. **Keep DST.** Do not delete `seaweedfs/data`; the attempt's evidence and
   the copy are what the next attempt starts from (`--checksum` makes the
   bulk copy a no-op for everything already there).

Write `rollback executed <ROLLBACK_TS>, trigger R<n>, reverse copy
<count> objects` in `STATE.md` and reopen the plan.

---

## 9. Files this runbook ships and where things go

| File | Role |
| --- | --- |
| `docker-compose.migration.yml` | The overlay: `minio-frozen` + `rclone` behind the `migration` profile. Never part of a plain `up`. |
| `verify_migration_digests.py` | §3.3's baseline and §5.3's join. Stdlib only; run on the host. |
| `migration-reports/` | Every `tee`/`--log-file`/`--combined`/`--output-file` above. Gitignored. Archive with the ticket. |
| `.env` (`MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`) | The frozen MinIO's original root pair, for the window only. Remove at §7 close. |
| `minio/data` | The source. Untouched until §7 close; then moved to an offline location, never deleted in place. |

The step names above (`§3.5`, `R2`, …) are what `STATE.md` and the plan's
progress log refer to.
