# SeaweedFS S3 Operator

[![Charmhub Badge](https://charmhub.io/seaweedfs-k8s/badge.svg)](https://charmhub.io/seaweedfs-k8s)

A lightweight, trivially scalable S3-compatible object store charm powered by
[SeaweedFS](https://github.com/seaweedfs/seaweedfs). Drop-in replacement for
MinIO or Ceph RADOS Gateway — suitable for both testing and home-lab production.

## Features

- **S3-compatible API** — works with `s3cmd`, `awscli`, Mimir, Loki, Tempo, and anything that speaks S3
- **Auto-scaling cluster** — `juju add-unit seaweedfs-k8s -n 2` joins new nodes automatically
- **Auto-configured** — admin credentials are generated on first deploy, no manual setup
- **Per-relation isolation** — each related application gets its own bucket with unique credentials
- **Observability** — built-in Prometheus metrics endpoint and Grafana dashboard
- **Lightweight** — single binary, minimal resource footprint

## Quick Start

```bash
juju deploy seaweedfs-k8s

# Connect an S3-consuming application
juju relate seaweedfs-k8s:s3-credentials your-app:s3

# Scale to three nodes
juju add-unit seaweedfs-k8s -n 2

# Get admin credentials for direct S3 access
juju run seaweedfs-k8s/leader get-admin-credentials
```

## Configuration

| Option | Type | Default | Description |
|---|---|---|---|
| `volume-size-limit-mb` | int | 1024 | Maximum volume size in MB |
| `replication` | string | `001` | Replication strategy (e.g. `000`, `001`, `010`) |
| `filer-max-mb` | int | 64 | Maximum filer metadata store size in MB |
| `metrics` | bool | true | Enable Prometheus metrics on port 9321 |
| `admin-access-key` | string | (auto) | Custom admin S3 access key |
| `admin-secret-key` | string | (auto) | Custom admin S3 secret key |

## Observability

```bash
juju relate seaweedfs-k8s:metrics-endpoint prometheus:metrics-endpoint
juju relate seaweedfs-k8s:grafana-dashboard grafana:grafana-dashboard
```

## Manual S3 Access

```bash
# Get credentials
CREDS=$(juju run seaweedfs-k8s/leader get-admin-credentials --format=json | jq -r '.[]')
ACCESS_KEY=$(echo "$CREDS" | jq -r '."access-key"')
SECRET_KEY=$(echo "$CREDS" | jq -r '."secret-key"')

# Use with s3cmd
s3cmd --host=<unit-ip>:8333 \
      --access_key=$ACCESS_KEY \
      --secret_key=$SECRET_KEY \
      --no-ssl ls
```

## Relation Data

For a related charm, the S3 relation data includes:

| Key | Value |
|---|---|
| `access-key` | Auto-generated access key scoped to the bucket |
| `secret-key` | Auto-generated secret key |
| `bucket` | Bucket named `s3-credentials-{relation-id}` |
| `endpoint` | `http://seaweedfs-k8s.<model>.svc.cluster.local:8333` |

Each S3 relation gets unique credentials and a dedicated bucket.

## Architecture

Each unit runs a `weed server` process combining Master, Volume, Filer, and S3
services. Units discover each other via a peer relation and form a Raft consensus
cluster automatically.

```
┌─────────────────────────────────────────┐
│              seaweedfs-k8s              │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐ │
│  │  Unit 0  │  │  Unit 1  │  │  Unit 2  │ │
│  │ master   │◄─┤ master   │◄─┤ master   │ │
│  │ volume   │  │ volume   │  │ volume   │ │
│  │ filer    │  │ filer    │  │ filer    │ │
│  │ s3 :8333 │  │ s3 :8333 │  │ s3 :8333 │ │
│  └─────────┘  └─────────┘  └─────────┘ │
│       ▲            ▲            ▲       │
│       └────────────┼────────────┘       │
│             K8s Service                 │
└─────────────────────────────────────────┘
```

## Development

```bash
tox              # lint, type-check, unit tests
tox run -e fmt   # auto-format
charmcraft pack  # build the charm
```