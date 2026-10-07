# SeaweedFS S3 Charm — Specification

## 1. Overview

A Juju sidecar charm that deploys SeaweedFS as a drop-in S3-compatible object
store. It replaces `ceph/radosgw` and `minio` in Juju-based deployments.
Designed for **zero-config deployment** — `juju deploy seaweedfs-k8s` →
working S3 endpoint. Supports **horizontal scaling** via a peer mesh (every
unit serves S3/volume/filer; up to 3 units run Raft masters).

**Target users:** Home-lab operators, integration testers, and anyone who needs
reliable S3 storage without the complexity of Ceph.

---

## 2. Architecture

### 2.1 Component Model

Each unit runs SeaweedFS as a single `weed server` process containing:

| Component | Port | Role | Runs on |
|-----------|------|------|---------|
| Master    | 9333 | Cluster coordination, Raft consensus | Master-eligible units only (see 2.2) |
| Volume    | 8080 | Object data storage | All units |
| Filer     | 8888 | Metadata / hierarchical namespace | All units |
| S3 Gateway| 8333 | S3 API endpoint | All units |

### 2.2 Scaling (Peer Mesh, Capped Master Set)

- Every unit runs volume + filer + S3 gateway. **At most 3 units run a
  master** (Raft requires an odd-sized quorum; >3 masters adds churn without
  meaningful benefit for target users).
- Master count by cluster size:
  - 1–2 units → **1 master** (lowest ordinal). Never run 2 masters.
  - ≥3 units → **3 masters** (3 lowest ordinals). The 1→3 transition happens
    in a single coordinated restart.
- Once ≥3 units exist, the master set is fixed and further scaling does not
  restart Raft.
- **Master unit removed:** the next-lowest-ordinal non-master unit is promoted.
  Masters are restarted one at a time (rolling) with the new peer list; use
  `weed shell cluster.raft.remove` / `cluster.raft.add` where possible to
  avoid full Raft restart.
- A `seaweedfs-peers` peer relation carries each unit's **stable pod DNS name**
  (`{app}-{n}.{app}-endpoints.{model}.svc.cluster.local`), never pod IPs.
- **Each unit builds its own Pebble layer** from peer relation data (`ops`
  cannot replan another unit's container). The leader may publish the
  authoritative master list in app relation data to avoid disagreement during
  transitions.
- Non-master units run `weed server -master=false` (or equivalent) and point
  `-volume.mserver` / `-filer.master` at the master list.
- Volumes are distributed across all volume servers automatically by the master.
- Adding a unit (`juju add-unit -n 2`) joins it to the mesh automatically.
- Removing a unit: see §2.4 (scale-down).

### 2.2.1 Filer Metadata

- Each unit's filer uses a **local leveldb store** on `/data/filer`.
- Filers keep metadata consistent via SeaweedFS's built-in peer metadata
  aggregation (filers discover each other through the master).
- No external metadata store dependency.

### 2.4 Scale-Down & Replication Changes

- SeaweedFS does **not** auto-rebalance when a volume server disappears.
- On `storage-detaching` (and `stop` as fallback), the departing unit runs
  `weed shell volume.server.evacuate -node <self>` with a bounded timeout to
  move its volumes to remaining units.
- If evacuation fails or times out, log an error and proceed (Juju cannot be
  blocked indefinitely). With `000` replication this may lose data — documented.
- When auto-replication changes the default (e.g. 1→2 units: `000`→`001`), the
  leader runs `weed shell volume.fix.replication` once so existing volumes are
  brought up to the new replication level.

### 2.3 Storage

| Resource | Mount | Purpose |
|----------|-------|---------|
| `data`   | `/data` | Volume data storage (single PVC per unit) |

Storage is a Juju-managed `filesystem` resource. SeaweedFS manages replication
internally via `-master.defaultReplication`.

---

## 3. Charm Metadata

### 3.1 Config Options

| Option                | Type   | Default | Description |
|-----------------------|--------|---------|-------------|
| `replication`         | string | `""`    | SeaweedFS default replication (`xyz`: x=copies in other DCs, y=other racks, z=other servers same rack). Empty = **auto**: `000` with 1 unit, `001` with ≥2 units. Any explicit value overrides auto. |
| `volume-size-limit-mb`| int    | `1024`  | Max volume file size in MB |

The charm always passes `-volume.max=0` so SeaweedFS auto-sizes the volume
count from free disk space on `/data`.

### 3.2 Relations

| Name              | Role  | Interface | Purpose |
|-------------------|-------|-----------|---------|
| `seaweedfs-peers` | peer  | `seaweedfs-peers` | Cluster membership and peer discovery |
| `s3-credentials`  | provides | `s3`     | S3 credentials for consumer charms (`charms.data_platform_libs.v0.s3` provider — drop-in compatible with radosgw/minio consumers) |
| `certificates`    | requires | `tls-certificates` | Optional TLS certificates |
| `ingress`         | requires | `ingress` | Optional external access to S3 port via traefik-k8s |

### 3.3 Actions

| Action                       | Description |
|------------------------------|-------------|
| `get-admin-credentials`      | Retrieve the current admin S3 access/secret keys |
| `rotate-admin-credentials`   | Generate new admin credentials, re-sync identities to filer (hot reload) |
| `rotate-relation-credentials`| Param `relation-id` (int). Regenerate creds for one s3 relation, update secret + relation data, re-sync identities |
| `pre-upgrade-check`          | Verify cluster health (Raft leader present, all masters up) before `juju refresh` |

### 3.4 Resources

| Name               | Type      | Description |
|--------------------|-----------|-------------|
| `seaweedfs-image`  | OCI image | Upstream SeaweedFS image, **pinned by digest** (`chrislusf/seaweedfs:3.97@sha256:...`). Bumped with charm releases. |

### 3.5 Storage

| Name   | Type       | Mount   | Minimum |
|--------|------------|---------|---------|
| `data` | filesystem | `/data` | 10Gi    |

---

## 4. Reconciliation Logic

### 4.1 Event Handlers

Standard Ops framework observer pattern (not fire-and-forget):

| Event                       | Handler Behavior |
|-----------------------------|------------------|
| `on_pebble_ready`           | Add Pebble layer, start workload |
| `on_config_changed`         | Update Pebble layer, replan |
| `on_leader_elected`         | Generate admin creds (if missing), sync identities to filer, publish relation data |
| `on_peer_relation_changed`  | Collect peer DNS names, recompute master set, replan own layer if changed |
| `on_peer_relation_departed` | Remove departing peer; promote new master if needed |
| `on_storage_detaching`      | Evacuate local volumes (`volume.server.evacuate`) |
| `on_s3_credentials_requested` (lib) | Generate per-relation creds → app secret, create bucket (`weed shell s3.bucket.create`, idempotent), sync identities, publish data |
| `on_s3_relation_broken`     | Remove identity from filer, delete secret; **bucket and data retained** |
| `on_certificates_relation_joined` | Request TLS cert, configure HTTPS |
| `on_ingress_ready`          | Log external URL (consumers still receive Service DNS) |
| `on_upgrade_charm`          | Re-reconcile (image changes arrive via `juju refresh --resource`) |
| `pre_upgrade_check_action`  | Verify Raft has a leader and all masters healthy before refresh |
| `get_admin_credentials_action`  | Return creds from Juju secret |
| `rotate_admin_credentials_action`| Generate new creds → update secret → re-sync identities |
| `rotate_relation_credentials_action`| Regenerate creds for given relation → update secret + relation data → re-sync identities |

### 4.2 Startup Sequence

1. Pebble signals ready → handler fires.
2. Leader generates admin access/secret keys (if not already present) via
   `secrets.token_urlsafe(16)`, stores in app-owned Juju secret `s3-admin`.
3. Add Pebble layer with `weed server` command (including master/peer flags).
4. Wait for HTTP health check (`:8333`) to pass.
5. Leader writes identities (admin + per-relation) to the filer via
   `weed shell s3.configure` (stored at `/etc/iam/identity.json` in the filer).
   All S3 gateways hot-reload — no per-unit config file, no restart.
6. Set status to `active`.

### 4.3 Peer Discovery

- On `peer-relation-joined`: unit publishes its stable pod DNS name in
  `relation.data[self]["peer-address"]`.
- On `peer-relation-changed`: **every unit** reads all peer addresses,
  determines the master set (3 lowest ordinals), and replans its own Pebble
  layer if the resulting command changed.
- `-master.peers` includes **all master-eligible units including self**
  (SeaweedFS requirement).

### 4.4 Re-reconciliation Trigger

Any change to config, peers, relations, or secrets triggers a full
reconciliation: recompute Pebble layer → replan if changed; leader re-syncs
identities to the filer.

---

## 5. S3 Integration

### 5.1 Relation Data (s3-credentials)

Published by the leader for each `s3-credentials` relation:

| Key          | Value |
|--------------|-------|
| `endpoint`   | `http://{app}.{model}.svc.cluster.local:8333` (K8s Service DNS, load-balanced across all units; `https://` if TLS enabled) |
| `bucket`     | Consumer-requested name or auto-generated `s3-{app}-{id}` |
| `access-key` | Credential ID (plain relation data, per `s3` v0 interface) |
| `secret-key` | Credential secret (plain relation data, per `s3` v0 interface) |
| `s3-uri-style` | Always `path` (virtual-hosted style not supported) |

Interface: `charms.data_platform_libs.v0.s3` (`S3Provider`). This matches what
existing radosgw/minio consumers expect.

### 5.2 Per-Relation Identity Model

- Each `s3-credentials` relation creates a **unique credential pair**.
- Source of truth: app-owned Juju secret `s3-rel-{relation_id}`; values are
  copied into relation data as required by the `s3` v0 interface.
- Bucket name handling:
  - If consumer sets `bucket` in their relation data, it is used as-is.
  - Otherwise, auto-generated as `s3-{remote-app-name}-{relation_id}`. This
    name is **not stable** across consumer redeploys (see §5.4); consumers
    that need data continuity must set `bucket` explicitly.
  - Multiple consumers **may** request the same bucket name → shared access.
    No ownership checks (accepted risk: any related app can access any bucket
    by name).
- Identity permissions are scoped to the consumer's bucket.
- On relation broken: identity removed and secret deleted; **bucket and its
  data are retained**. Operator deletes buckets manually.

### 5.3 Admin Credentials

- Auto-generated on first leader election.
- Stored in app-owned Juju secret `s3-admin` (accessible only by this application).
- Retrievable via `get-admin-credentials` action.
- Rotatable via `rotate-admin-credentials` action → regenerates keys, updates
  secret, re-syncs identities to filer (hot reload, no restart).

### 5.4 Data Durability Across Consumer Lifecycle

- **Goal:** data survives `juju remove-application` of a **consumer** app.
- On relation broken, the bucket and its data are retained (§5.2).
- When the consumer is redeployed and related again, it receives **new
  credentials**. To reattach to its previous data it must request the **same
  bucket name explicitly** in relation data (auto-generated names include the
  relation ID and will differ).
- Orphaned auto-named buckets remain accessible via admin credentials.

### 5.5 Out of Scope: Removing SeaweedFS Itself

- `juju remove-application seaweedfs-k8s` **destroys all data** (PVCs are
  removed with the app on Kubernetes). This is documented prominently.
- No admin credential override or storage reattachment is provided. Users
  needing protection against this should back up via external S3 tools.

---

## 6. Security

### 6.1 TLS (Optional)

- **Scope: S3 gateway (8333) only.** Internal master/volume/filer gRPC and HTTP
  remain plaintext within the cluster network.
- If a `certificates` relation is present, each unit requests a server cert
  with SANs: Service DNS (`{app}.{model}.svc.cluster.local`) and its pod DNS
  name.
- SeaweedFS S3 gateway is configured with `-s3.cert.file` and
  `-s3.key.file`.
- Published endpoint changes from `http://` to `https://`, and the CA chain is
  published in the `tls-ca-chain` field of the s3 relation.
- Without the relation, plain HTTP is used (appropriate for home-lab /
  internal deployments).

### 6.1.1 External Access (Optional)

- Optional `ingress` relation (traefik-k8s) exposes the S3 port outside the
  cluster.
- Without ingress, S3 is reachable only inside the cluster.
- Consumers on the `s3-credentials` relation always receive the in-cluster
  Service DNS endpoint, regardless of ingress.

### 6.2 Credential Storage

- Source of truth for all credentials is Juju secrets; never in plain config.
- **Exception:** per-relation credentials are also written to plain relation
  data, because the `s3` v0 interface requires it for drop-in compatibility.
  Relation data is only visible to the two related applications.

---

## 7. Health & Status

### 7.1 Pebble Health Check

- Pebble HTTP check at `http://localhost:8333/healthz` (expects 2xx),
  threshold 3, interval 10s. (Plain `/` returns 403 to anonymous requests.)
- During reconcile, the charm additionally queries master
  `/cluster/status`; if there is no Raft leader, unit status is `waiting`.

### 7.1.1 Upgrades

- Juju StatefulSet rolling update (highest ordinal first).
- Operator runs `pre-upgrade-check` before `juju refresh`; it fails if Raft has
  no leader or any master is unhealthy.

### 7.2 Status Mapping

| Condition | Status |
|-----------|--------|
| Pebble not ready | `maintenance: "Waiting for container..."` |
| Workload starting | `maintenance: "Starting SeaweedFS..."` |
| S3 healthy, 1 unit | `active: "S3 ready (standalone)"` |
| S3 healthy, ≥2 units | `active: "S3 ready ({n} units, {m} masters)"` |
| Raft has no leader | `waiting: "Waiting for master quorum"` |
| S3 unhealthy | `waiting: "S3 endpoint unreachable"` |

### 7.3 Workload Version

Set from `weed version` on each reconciliation cycle.

---

## 8. Testing Strategy

### 8.0 Tooling

- Unit tests: `ops.testing` (Scenario-style state-transition tests). No Harness.
- Integration tests: `jubilant` + `pytest`.

### 8.1 Unit Tests (`tests/unit/test_charm.py`)

**Charm Initialization:**
- Default config values are correctly applied.
- Admin credentials generated on leader election when none exist.
- Admin credentials not regenerated if `s3-admin` secret already exists.

**Peer Relations:**
- `-master.peers` flag is empty with 0 peers.
- 1–2 units → single master (lowest ordinal); never 2 masters.
- `-master.peers` contains the 3 lowest-ordinal units (including self) when ≥3 units.
- Removing a master unit promotes next-lowest ordinal.
- `storage-detaching` triggers `volume.server.evacuate`.
- Scaling 1→2 with auto replication triggers `volume.fix.replication` on leader.
- 4th+ unit runs without a master and points at the master set.
- Peer addresses are stable pod DNS names, not IPs.
- Peer departure removes unit from flag.

**S3 Relations:**
- New relation generates unique credentials, stores in app secret, publishes
  in relation data (s3 v0 format).
- Published endpoint is K8s Service DNS.
- Consumer `bucket` name override is respected.
- Multiple relations to same bucket create distinct credentials.
- Relation broken removes identity and secret, does NOT delete bucket.
- Identity changes are synced to filer via `s3.configure` (no restart).

**Config Changes:**
- Changing `replication` updates `-master.defaultReplication`.
- Empty `replication` → `000` with 1 unit, `001` with ≥2 units.
- Changing `volume-size-limit-mb` updates `-master.volumeSizeLimitMB`.
- `-volume.max=0` is always passed.

**Secrets:**
- Admin secret created with correct content.
- Per-relation secrets created (app-owned).
- Secret rotation produces new values, not old.

**Actions:**
- `get-admin-credentials` returns correct keys.
- `rotate-admin-credentials` generates new keys, re-syncs identities (no restart).
- `get-admin-credentials` returns updated keys after rotation.
- `rotate-relation-credentials` updates only the targeted relation's secret and
  relation data; invalid `relation-id` fails the action.

**TLS:**
- Certificates relation triggers cert request.
- No certificates relation → HTTP endpoint.
- With certificates relation → HTTPS endpoint.

**Non-leader:**
- Non-leader units do not generate admin credentials.
- Non-leader units do not publish S3 relation data.

**Edge Cases:**
- Container not ready → appropriate maintenance status.
- Bucket already exists → no error.
- Empty peer list → standalone mode, no crash.

### 8.2 Integration Tests (`tests/integration/test_charm.py`)

- **Basic deploy:** Deploy, verify `active` status, verify S3 endpoint
  reachable.
- **Scale out:** Deploy 1 unit → scale to 3 → verify all units `active`, verify
  cluster peer count.
- **Scale down:** Write data at 3 units → scale to 1 → verify remaining unit
  is healthy and data is still readable (evacuation worked).
- **Consumer relation:** Deploy consumer charm, relate, verify credentials
  published and bucket accessible.
- **Relation removal:** Remove consumer relation → creds rejected, bucket data
  still present (via admin creds).
- **Shared bucket:** Two consumer charms requesting same bucket name → both
  receive credentials scoped to that bucket.
- **Credential rotation:** Deploy → rotate admin creds → verify old creds fail,
  new creds work.
- **Consumer redeploy:** Consumer with explicit `bucket` writes data → remove
  consumer app → redeploy + relate → new creds, same data readable.
- **TLS:** Deploy with `self-signed-certificates` related → verify HTTPS
  endpoint and `tls-ca-chain` published.
- **Ingress:** Relate traefik-k8s → S3 reachable via ingress URL.

---

## 9. Edge Cases & Failure Modes

| Scenario | Expected Behavior |
|----------|-------------------|
| Single unit, no peer relation | Runs standalone, `active` status |
| Container image pull failure | Not detectable by charm (pebble-ready never fires); visible via `kubectl` / `juju debug-log` |
| Storage PVC not bound | Handled by Juju — charm hooks don't run until storage is attached |
| Peer unit unreachable | Raft tolerates minority failure; charm keeps running |
| All peer units leave | Remaining unit becomes standalone (sole master) |
| Master unit removed | Next-lowest ordinal promoted; rolling master restart |
| Evacuation timeout on scale-down | Log error, proceed with removal |
| Filer identity config lost/corrupted | Leader rewrites from secrets + relations on next reconcile |
| Leader unit restarts | New leader (if different) regenerates relation data |
| `weed server` process crashes | Pebble auto-restarts via `on-failure` policy |
| Bucket already exists on create | Graceful skip, log info |
| Scale to 0 | Cannot be prevented by charm; behavior undefined. Data retained only if PVCs are not destroyed |
| Rapid config changes | Each change triggers replan; last config wins |

---

## 10. Non-Goals

- **No multi-tier storage** — single PVC per unit only. Users wanting tiered
  storage should deploy a separate SeaweedFS cluster.
- **No external filer store** — filers use local leveldb with built-in peer
  metadata sync. Users needing a shared SQL/Redis filer store should deploy a
  dedicated SeaweedFS setup.
- **No custom CRDs** — this is a Juju charm, not a Kubernetes operator.
- **No S3 bucket lifecycle policies, versioning, or replication rules** —
  these are SeaweedFS features available via the S3 API but not configured by
  the charm itself.
- **No dashboard / web UI** — this is a storage charm, not an observability
  platform.
- **No backup/restore automation** — users manage backups via S3 tools.
- **No protection against removing the SeaweedFS app itself** — data is
  destroyed with the app.
- **No COS integration in v1** — no `metrics-endpoint`, `logging`, or
  `grafana-dashboard` relations (candidate for v2).