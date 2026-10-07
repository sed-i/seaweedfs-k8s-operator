# seaweedfs-k8s-operator
[![Charmhub Badge](https://charmhub.io/seaweedfs-k8s/badge.svg)](https://charmhub.io/seaweedfs-k8s)

## Purpose
A Juju sidecar charm that deploys [SeaweedFS](https://github.com/seaweedfs/seaweedfs) as a
drop-in S3-compatible object store. It replaces `ceph/radosgw` and `minio` in Juju-based
deployments: `juju deploy seaweedfs-k8s` gives a working S3 endpoint with zero config.

It scales horizontally via a peer mesh: every unit serves S3/volume/filer, and up to 3 units
run Raft masters (SeaweedFS/Raft requires an odd-sized quorum, and more than 3 masters adds
churn without benefit). See [`spec.md`](spec.md) for the full design.

## Relations
- `s3-credentials` (provides, `s3` interface): each related application gets its own bucket
  and a unique credential pair, published via
  [`charms.data_platform_libs.v0.s3`](https://charmhub.io/data-platform-libs) — a drop-in
  replacement for `radosgw`/`minio` consumers.
- `certificates` (requires, `tls-certificates`, optional): enables HTTPS on the S3 gateway.
- `ingress` (requires, `ingress`, optional): exposes the S3 port outside the cluster via
  traefik-k8s.
- `seaweedfs-peers` (peer): cluster membership and master-set coordination.

## Config
| Option | Default | Description |
|---|---|---|
| `replication` | `""` (auto) | SeaweedFS default replication (`xyz`). Empty means `000` with 1 unit, `001` with 2+ units. |
| `volume-size-limit-mb` | `1024` | Max volume file size in MB. |

## Actions
- `get-admin-credentials` / `rotate-admin-credentials`: manage the cluster-wide admin S3 keys.
- `rotate-relation-credentials --relation-id=<id>`: rotate one consumer's credentials.
- `pre-upgrade-check`: verify Raft has a leader before `juju refresh`.

## Usage example
```bash
juju deploy seaweedfs-k8s --trust
juju deploy mimir-coordinator-k8s mc
juju integrate mc:s3 seaweedfs-k8s:s3-credentials
```

## Manual testing
```bash
juju ssh --container seaweedfs seaweedfs-k8s/0 /charm/bin/pebble logs -f | grep -iE "error|fail"

# Make sure size is growing
juju ssh --container seaweedfs seaweedfs-k8s/0 du -hc /data
```

```bash
UNIT=$(juju status --format=yaml | yq '.applications.seaweedfs-k8s.units."seaweedfs-k8s/0".address')

# Master server
curl -s http://$UNIT:9333/cluster/status

# Volume server
curl -s http://$UNIT:8080/status

# Filer
curl -s http://$UNIT:8888/

# S3 server
curl -s http://$UNIT:8333/healthz
```

```bash
juju run seaweedfs-k8s/0 get-admin-credentials

sudo apt install s3cmd
s3cmd --host=$UNIT:8333 --access_key=<access-key> --secret_key=<secret-key> --host-bucket= --no-ssl ls
```
