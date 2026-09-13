# Object versioning and Object Lock on `sensitive-media` / `archive-media`

Evaluation and recommendation — `T28-versioning-objectlock-eval`, Wave 5 of the
object-storage backend migration (MinIO → SeaweedFS 4.x, S3-neutral). Wave 5 is
follow-on work: nothing here is required for the cutover, and this document is a
**recommendation, not an implementation**. No bucket configuration was changed,
in this stack or anywhere else.

## 0. Verdict

1. **Do not enable object versioning or Object Lock on any of the five media
   buckets now.** Neither bucket the step names holds a single object today
   (measured, M26), `archive-media` has no producing code path at all (M27), and
   no retention or immutability requirement has been written down. Enabling
   either would change the meaning of every delete this service performs while
   buying nothing that is currently needed.
2. **The conflict the step asks to state up front is real, and it starts one
   step earlier than expected.** Object Lock and `hard_purge` are mutually
   exclusive on the same bucket — but *versioning on its own* already defeats
   `hard_purge`, with no lock configured at all, and it does so **silently**
   (§1). Adopting versioning is not a bucket-configuration change; it is a
   change to the deletion contract of `media_sdk_m8.ObjectStorage` and to the
   five call sites in this service that rely on it.
3. **COMPLIANCE mode is rejected outright** for any bucket holding personal
   data. Measured, not assumed: a COMPLIANCE-retained version cannot be deleted
   by the scoped application credential, cannot be deleted by the **admin**
   credential, cannot be deleted with `x-amz-bypass-governance-retention`, and
   its retention cannot be shortened by anyone (M13–M16). A deletion request
   arriving inside the window could not be honoured by any actor in this stack.
4. **If a requirement does appear, the viable shape is: versioning +
   *GOVERNANCE* Object Lock, on `sensitive-media` only, after the six
   preconditions in §7.** GOVERNANCE is the only mode measured to be compatible
   with erasure: the bypass is refused to the application credential and granted
   to the admin identity (M17–M19), which by invariant S4 exists only inside the
   one-shot bootstrap container. That makes "the app cannot destroy this data,
   an operator with root credentials still can" an enforceable property rather
   than a policy statement.

## 1. The conflict, stated up front

`ObjectStorage.remove_object` issues `delete_object(Bucket=…, Key=…)` with **no
`VersionId`** (`media-sdk-m8/media_sdk_m8/storage/client.py:309-311`). On a
versioned bucket that call does not delete anything: it writes a delete marker
and leaves every prior version's bytes in place (M5, M6). Every deletion this
service performs goes through that one call:

| Call site | Purpose |
| --- | --- |
| `controllers/maintenance.py:278` (`_best_effort_remove`) | the scheduled **hard purge** — the only true delete in the system |
| `controllers/objects.py:379` (`_best_effort_remove`) | PUBLIC soft-delete, which removes the bytes because a public object's URL is known |
| `controllers/uploads.py:132`, `:475` | rejected / aborted upload cleanup |
| `controllers/admin.py:222` | admin stale-upload purge |
| `controllers/transfer_import.py:871` | rejected import cleanup |

So on a versioned bucket:

* `hard_purge_expired` deletes the DB row, logs `media.hard_purge`, and returns
  `purged=N` while **reclaiming nothing**. The bytes of every object a user
  deleted 30 days ago (`MEDIA_RETENTION_PURGE_DAYS`) remain readable to anyone
  holding the storage credential and a `VersionId` (M7).
* Quota is debited at soft-delete (`objects.py:626-631`) and deliberately *not*
  re-debited at purge (`maintenance.py:15-16`), so the owner's accounting says
  the bytes are gone while the disk says otherwise, permanently.
* The reconciler cannot see the discrepancy either: `ListObjectsV2` returns
  neither noncurrent versions nor delete-marked keys (M6), so
  `_find_storage_orphans` will never find them and `db_orphan_count` — the
  metric `DATA_MIGRATION_RUNBOOK.md` §6 uses as a cutover gate — stays flat.
  The stack loses its only measurement of storage-vs-DB truth for that data.
* Adding a lock on top does not surface an error either. A plain `DeleteObject`
  on a COMPLIANCE-retained object still succeeds, because writing a delete
  marker is not deleting a version (M12). And a *version-aware* purge — the
  obvious fix — would receive `AccessDenied` on every locked version, which
  both `_best_effort_remove` implementations log and swallow
  (`maintenance.py:272-282`, `objects.py:373-387`), deleting the row anyway.

The failure mode in all four bullets is the same: **the system reports success
while the opposite of the intended thing happens.** That, not the S3 feature
matrix, is why this evaluation ends in "not now".

## 2. What was measured

| What | Value |
| --- | --- |
| Date | 2026-09-13 |
| Method | a throwaway single-node backend on its own Docker network, with an identity table shaped exactly like the one `storage-config` generates (an `admin` identity and a `media-rw` identity scoped `Read`/`Write`/`List` on the probe buckets and nothing else), driven with `amazon/aws-cli:2.36.40`. Created, measured and destroyed inside this step; volumes and network removed. |
| SeaweedFS | `chrislusf/seaweedfs:4.45` — the tracked compose pin, booted with this stack's own flags (`server -dir=/data -filer -s3 -ip=localhost -ip.bind=127.0.0.1 -s3.ip.bind=0.0.0.0 -s3.config=…`) |
| Garage | `dxflrs/garage:v2.3.0` — the pin `docker-compose.garage.yml` ships (`T27`), single node, `replication_factor = 1` |
| Live stack | **read only**: one `ListObjectsV2` per bucket against the running `hardened_media_m8` instance (M26). Nothing was written, configured or deleted there. |
| Client | `media_sdk_m8.storage.client` was read, not modified; the delete call it issues was reproduced verbatim as `s3api delete-object --bucket … --key …` |

### SeaweedFS 4.45

| id | Probe | Observed |
| --- | --- | --- |
| M1 | `GetBucketVersioning` on a fresh bucket | empty (unversioned) |
| M2 | `PutBucketVersioning Status=Enabled` as **admin** | accepted; `Status: Enabled` reads back |
| M3 | `PutBucketVersioning` Suspended→Enabled as the **scoped `media-rw` key** | **accepted both ways.** SeaweedFS's static identity table has no verb below `Write`, so bucket configuration is inside the application credential's reach |
| M4 | two `PutObject`s on one key | two versions, distinct `VersionId`s, both listed by `ListObjectVersions` |
| M5 | `DeleteObject` **without** a version id — the exact call the SDK issues | `DeleteMarker: true`; a new marker version is created |
| M6 | after M5: `HeadObject` / `ListObjectsV2` / `ListObjectVersions` | 404 · empty listing · **both original versions still present**, `Size` intact |
| M7 | `GetObject --version-id <v1>` with the **scoped** key | returns the "deleted" bytes |
| M8 | versioning enabled on a bucket that **already had** objects, then a plain delete | the pre-existing object becomes `VersionId: "null"`, survives the delete, and is still readable as `--version-id null` |
| M9 | `PutBucketLifecycleConfiguration` with `NoncurrentVersionExpiration` | accepted and stored; reads back intact — see §5.4 for why that is not a reaper |
| M10 | `PutObjectLockConfiguration` on an **existing** versioned bucket | accepted (AWS is stricter here); `CreateBucket --object-lock-enabled-for-bucket` also works and turns versioning on implicitly |
| M11 | object written **before** a default retention was set | `GetObjectRetention` → `ObjectLockConfigurationNotFoundError`. A default retention is **not** retroactive |
| M12 | plain `DeleteObject` on a COMPLIANCE-retained object | **succeeds** — a delete marker is not a version deletion; `HeadObject` then 404s while the retained bytes stay |
| M13 | `DeleteObject --version-id` of a COMPLIANCE-retained version, scoped key | `AccessDenied` |
| M14 | same as **admin** | `AccessDenied` |
| M15 | same as admin **with** `--bypass-governance-retention` | `AccessDenied` — COMPLIANCE binds root, as the S3 model requires |
| M16 | `PutObjectRetention` shortening a COMPLIANCE window, scoped key then admin | `AccessDenied` for both |
| M17 | GOVERNANCE-retained version, `DeleteObject --version-id`, scoped key | `AccessDenied` |
| M18 | same, scoped key **with** `--bypass-governance-retention` | `AccessDenied` — the bypass is gated, not merely a header |
| M19 | same, **admin** with `--bypass-governance-retention` | **deleted**; the version is gone from `ListObjectVersions` |
| M20 | legal hold `ON` via the **scoped** key | accepted; versioned delete then denied — but the same key can set it `OFF` again and delete. Legal hold is not a barrier to the credential that can toggle it |
| M21 | `OP-08` under versioning — `CopyObject` self-copy, `REPLACE`, new `Content-Type` (`set_object_content_type`) | creates a **new version**. The pre-rewrite version keeps `image/svg+xml` and serves it on a `--version-id` GET (§5.2) |
| M22 | presigned GET on a versioned bucket | `200`, latest version, `Content-Type` and `Content-Length` as expected — the data path itself is unaffected |
| M23 | `DeleteBucket` with a retained version present | `BucketNotEmpty` |

### Garage 2.3.0 — the ratified fallback

| id | Probe | Observed |
| --- | --- | --- |
| M24 | `PutBucketVersioning` | `NotImplemented: Unimplemented action: PutBucketVersioning` |
| M25 | `PutObjectLockConfiguration` / `GetObjectLockConfiguration` | `NotImplemented` (both) |
| M25b | `ListObjectVersions` | `NotImplemented` |
| M25c | `PutBucketLifecycleConfiguration` | accepted |

### The buckets this step is about

| id | Probe | Observed |
| --- | --- | --- |
| M26 | `ListObjectsV2` on the live hardened stack | `sensitive-media`: **empty**. `archive-media`: **empty**. (`private-media` 68, `public-media` 7, `temp-media` 7 — the `T23`/`T24`/`T25` working set) |
| M27 | who writes to `archive-media` | **nothing does.** `bucket_for_storage_class(StorageClass.ARCHIVE)` (`storage/buckets.py:30`) has no caller; the only call site uses `TEMP` (`controllers/transfer.py:347`), and export archives land in `temp-media` (`controllers/transfer.py:474-490`). The bucket is created by `storage-init` and swept by the reconciler (`maintenance_worker.py:45`), and that is all |

## 3. Consequences per subsystem

| Subsystem | Under versioning | Additionally under Object Lock |
| --- | --- | --- |
| `hard_purge_expired` | reports `purged=N`, reclaims nothing (M5, M6) | a version-aware purge would be refused; the refusal is swallowed (`maintenance.py:279-282`) |
| PUBLIC soft-delete | bytes stay retrievable by `VersionId` to any credential holder (M7) — the exact exposure `objects.py:611-618` exists to prevent | unchanged, and now irreversible for the window |
| Quota accounting | debited at soft-delete, never reconciled against real usage; drifts monotonically | same |
| Orphan reconciler | cannot see noncurrent versions or delete-marked keys (M6); `db_orphan_count` stops meaning what the runbook's cutover gate assumes | same |
| Stale/aborted uploads, rejected imports | every abandoned upload keeps its bytes forever | same, un-deletable |
| `OP-08` `Content-Type` rewrite | pre-rewrite version keeps the attacker-declared type (M21) | that version becomes undeletable for the window |
| Presigned data path (S5, S9–S12) | unaffected (M22) | unaffected |
| Disk growth | unbounded with no in-stack reaper (§5.4) | unbounded and unreclaimable until retention lapses |
| `T25` migration/rollback | `rclone` copies current versions only; parity checks would pass while history is silently dropped | a locked destination refuses the reverse delta the rollback depends on |

## 4. The erasure question

`MEDIA_RETENTION_PURGE_DAYS` (default 30) is this service's answer to "a user
deleted this; when do the bytes actually go away?". Object Lock is the answer to
the opposite question. Reconciling them is a policy decision, not a technical
one, and the two lock modes behave very differently:

* **COMPLIANCE** — nobody can delete a retained version and nobody can shorten
  the window: not the app credential, not the admin credential, not the admin
  credential with the bypass header (M13–M16). A deletion request arriving
  inside the window cannot be honoured at all. Only appropriate for records the
  organisation is *legally required to keep*, in a bucket that holds nothing
  else. `archive-media` is the only plausible candidate in this stack, and it
  has neither a writer nor a written retention policy (M27).
* **GOVERNANCE** — refused to the scoped `media-rw` key both with and without
  the bypass (M17, M18), granted to the admin identity with the bypass (M19).
  Since `media-rw` is the credential that leaves the trust boundary (it is in
  `media.env`, read by `media_service` and `media_worker`) and the admin
  identity is confined to the one-shot bootstrap container (invariant S4),
  GOVERNANCE buys a real property: **a compromised application cannot destroy
  what is under retention, while an operator can still erase on request.**

Any adoption also has to answer: does the retention window start at upload and
run longer than 30 days (in which case `hard_purge` cannot meet its own
contract), or shorter (in which case the lock adds little)? That question has no
answer today because no requirement names a window.

## 5. Findings that change the shape of the decision

### 5.1 The application credential can configure versioning and holds

`media-rw` enabled and suspended versioning (M3) and set and released a legal
hold (M20). SeaweedFS's static identity table has four verbs — `Admin`, `Read`,
`Write`, `List` — and no separate bucket-configuration or lock verb, so anything
short of `Admin` is reachable by `Write`. Consequences: versioning alone is
**not** a defence against a compromised application credential (it can suspend
versioning, then overwrite), and legal hold is not either (it can release it).
Only `PutObjectRetention`-backed retention held against `media-rw` in the
measurements. If the goal is ransomware resistance, GOVERNANCE retention is the
only mechanism measured to deliver it here.

### 5.2 The `Content-Type` rewrite stops being in-place

`set_object_content_type` is a metadata-only self-copy with
`x-amz-metadata-directive: REPLACE` (contract case `OP-08`) and it is how this
service pins a safe served type. Under versioning it creates a new version and
the pre-rewrite version keeps the original, attacker-declared type — measured:
`image/svg+xml` still served on a `--version-id` GET after the rewrite to
`application/octet-stream` (M21). This is **not** a live exposure today:
nothing in this stack mints a `versionId` URL, and the presigned path resolves
to the latest version (M22). It matters for two reasons: `CONTRACT.md`'s wording
for `OP-08` ("rewrites the stored `Content-Type` **in place**") would become
false on a versioned bucket, and under a lock the unsafe version could not be
removed for the retention window.

### 5.3 Retrofitting is possible but not retroactive

Versioning can be turned on over existing objects (M8) and Object Lock can be
configured on an existing versioned bucket (M10) — SeaweedFS is more permissive
than AWS here. But a default retention applies only to objects written *after*
it is set (M11), so existing objects would need an explicit
`PutObjectRetention` sweep. Moot today: both target buckets are empty (M26).

### 5.4 There is no reaper for noncurrent versions in this stack

SeaweedFS accepts and stores a `NoncurrentVersionExpiration` lifecycle rule
(M9), so "let lifecycle expire old versions" looks available. It is not, here:
in `chrislusf/seaweedfs:4.45` lifecycle expiry is executed by a worker you have
to dispatch — the `weed shell` command `s3.lifecycle.run-shard`, or the separate
admin component's lifecycle workers — and the `storage` service runs
`weed server -dir -filer -s3` and nothing else. A stored rule in this stack is
therefore a stored rule. (Stated as what runs, not as an observed expiry miss: a
`NoncurrentDays` minimum of 1 cannot be observed inside this step.) Bounded
retention of noncurrent versions would have to be built application-side, in the
same maintenance worker that already owns purge, expiry and reconciliation —
which is the honest place for it anyway, since that is where the DB truth is.

### 5.5 Adopting this breaks fallback parity

`.workspace/context/object-storage.md` ratifies SeaweedFS 4.x as the default and
Garage 2.x as the **validated fallback**, and `T27` shipped
`docker-compose.garage.yml` so that switching is a `-f` swap rather than a code
change. Garage 2.3.0 implements none of this: `PutBucketVersioning`,
`Put`/`GetObjectLockConfiguration` and `ListObjectVersions` all answer
`NotImplemented` (M24, M25). Enabling versioning would make the two backends
differ in *data semantics*, not just in configuration — the fallback would
silently drop history and immutability on failover. That is a workspace-contract
decision owned by `.workspace/context/object-storage.md`, not something this
repository can settle on its own.

### 5.6 Both operations are forbidden by the executable contract

`PutBucketVersioning` and `PutObjectLockConfiguration` are listed in
`FORBIDDEN_OPERATIONS` (`media-sdk-m8/tests/conformance/contract.py:237-244`)
and asserted negatively by `test_forbidden_operations_are_never_issued`;
`CONTRACT.md` records the reason ("retention, hard-purge, stale-upload expiry
and orphan reconciliation are application-side arq crons, not S3 lifecycle
rules"). Adoption therefore requires amending `T0`'s contract in
`media-sdk-m8` — so it can never be a `media-service-m8`-only change, and this
evaluation could not have implemented it even if it recommended doing so.

## 6. Options

| | Option | Buys | Costs | Verdict |
| --- | --- | --- | --- | --- |
| **A** | **No adoption** (status quo) | keeps `hard_purge` honest, keeps Garage parity, keeps the contract intact | no protection against a compromised app credential destroying media | **Recommended** |
| **B** | Versioning only, `sensitive-media` | accidental-overwrite recovery | all of §3 column 1; and no ransomware protection, since `media-rw` can suspend versioning (5.1) | Rejected — pays the full cost for the smallest benefit |
| **C** | Versioning + **GOVERNANCE** lock, `sensitive-media` only | the app credential cannot destroy retained data; erasure still possible by an operator (M19) | §3 in full, a version-aware SDK delete path, an app-side reaper, a contract amendment, loss of Garage parity | The path **if** a requirement appears — §7 |
| **D** | **COMPLIANCE** lock, either bucket | true immutability | erasure impossible for anyone, for the whole window (M13–M15) | **Rejected** for personal data. Reconsider only for a dedicated legal-retention bucket with a written policy |

## 7. Preconditions for Option C

Not a plan — the bar any future plan would have to clear.

1. **A written requirement** naming the data, the retention window, the party
   who may erase inside it, and which of ransomware-resistance / accidental-
   overwrite recovery / legal retention is actually being bought.
2. **A version-aware deletion contract in `media-sdk-m8`**: `remove_object`
   either deletes every version of a key or grows an explicit counterpart, and
   the five call sites in §1 choose deliberately between "hide" and "erase".
   `FORBIDDEN_OPERATIONS` and `CONTRACT.md` amended in the same change, and the
   `OP-08` wording (5.2) restated.
3. **A bounded reaper for noncurrent versions**, application-side, in the
   maintenance worker — the stack runs no lifecycle worker (5.4). With a metric,
   since the reconciler is blind to versions (§3).
4. **A workspace decision on fallback divergence** recorded in
   `.workspace/context/object-storage.md` (5.5): either accept that failover to
   Garage loses versioning and immutability, or drop Garage as the fallback.
5. **GOVERNANCE only, never COMPLIANCE**, with the erasure path documented as an
   operator act using the admin identity and the bypass (M19) — consistent with
   S4, which already confines that credential to the one-shot bootstrap.
6. **Quota and reconciliation accounting updated** so "freed" means freed, and
   `db_orphan_count` keeps meaning what `DATA_MIGRATION_RUNBOOK.md` §6 assumes.

Sizing, for planning only: one wave, roughly four steps, spanning `media-sdk-m8`
(contract + client) and `media-service-m8` (call sites, reaper, compose, tests)
— comparable to Wave 1, and strictly larger than the bucket-configuration change
the feature is usually mistaken for.

## 8. Scope — what this step did not do

No compose file, bootstrap script, bucket configuration, SDK or service code was
changed. The live `hardened_media_m8` stack was only listed (M26). Not evaluated
because the step does not name them: server-side encryption, replication,
versioning on `public-media` / `private-media` / `temp-media`, and MinIO's
behaviour (the migration has already left it). The regression guard this step
does ship is `tests/test_storage_versioning_policy.py`, which fails if any
bootstrap or service path starts issuing these operations while this
recommendation stands.

## 9. Open questions for the user

1. **Is there a retention or immutability requirement at all?** Option A assumes
   there is none. If one exists, precondition 7.1 is the missing input, and it
   decides between C and D.
2. **`archive-media` has no writer (M27).** Is it intended to become the
   archival tier (`StorageClass.ARCHIVE` suggests it was), or should it be
   dropped from the bootstrap and the reconciler? Either answer is cheap today;
   it gets expensive once the bucket holds data.
3. **Does failover to Garage have to be semantically identical** to SeaweedFS
   (5.5)? A "yes" closes Option C permanently, without any further analysis.
