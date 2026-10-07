"""Grafana dashboard template for SeaweedFS.

Ref: https://github.com/seaweedfs/seaweedfs/blob/master/other/metrics/grafana_seaweedfs.json
"""

import json


def build_dashboard() -> str:
    """Build a minimal SeaweedFS Grafana dashboard JSON."""
    dashboard = {
        "title": "SeaweedFS",
        "uid": "seaweedfs",
        "panels": [
            {
                "type": "stat",
                "title": "Cluster Status",
                "targets": [
                    {
                        "expr": "sum(SeaweedFS_cluster_status)",
                        "legendFormat": "Status",
                    }
                ],
                "gridPos": {"h": 8, "w": 8, "x": 0, "y": 0},
            },
            {
                "type": "stat",
                "title": "Active Masters",
                "targets": [
                    {
                        "expr": "count(SeaweedFS_is_leader == 1)",
                        "legendFormat": "Masters",
                    }
                ],
                "gridPos": {"h": 8, "w": 8, "x": 8, "y": 0},
            },
            {
                "type": "stat",
                "title": "Total Volumes",
                "targets": [
                    {
                        "expr": "sum(SeaweedFS_Volumes)",
                        "legendFormat": "Volumes",
                    }
                ],
                "gridPos": {"h": 8, "w": 8, "x": 16, "y": 0},
            },
            {
                "type": "graph",
                "title": "Volume Count Over Time",
                "targets": [
                    {
                        "expr": "SeaweedFS_Volumes",
                        "legendFormat": "{{instance}}",
                    }
                ],
                "gridPos": {"h": 10, "w": 24, "x": 0, "y": 8},
            },
            {
                "type": "graph",
                "title": "Free Capacity",
                "targets": [
                    {
                        "expr": "SeaweedFS_total_free",
                        "legendFormat": "{{instance}}",
                    }
                ],
                "gridPos": {"h": 10, "w": 12, "x": 0, "y": 18},
            },
            {
                "type": "graph",
                "title": "Used Capacity",
                "targets": [
                    {
                        "expr": "SeaweedFS_total_size - SeaweedFS_total_free",
                        "legendFormat": "{{instance}}",
                    }
                ],
                "gridPos": {"h": 10, "w": 12, "x": 12, "y": 18},
            },
            {
                "type": "graph",
                "title": "Requests per Second",
                "targets": [
                    {
                        "expr": "rate(SeaweedFS_request_counts[1m])",
                        "legendFormat": "{{type}}",
                    }
                ],
                "gridPos": {"h": 10, "w": 24, "x": 0, "y": 28},
            },
        ],
        "schemaVersion": 27,
        "refresh": "30s",
    }
    return json.dumps(dashboard)
