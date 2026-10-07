"""Pure logic for the SeaweedFS peer mesh (capped master set, DNS naming).

Kept free of ``ops`` imports so it is trivial to unit test.
"""

from __future__ import annotations

from typing import Iterable, List

MASTER_PORT = 9333

#: SeaweedFS/Raft requires an odd quorum; the charm never runs more than this
#: many masters regardless of cluster size (see spec §2.2).
MAX_MASTERS = 3


def pod_dns_name(app_name: str, ordinal: int, model_name: str) -> str:
    """Return the stable pod DNS name for a given unit ordinal.

    Never pod IPs -- see spec §2.2.
    """
    return f"{app_name}-{ordinal}.{app_name}-endpoints.{model_name}.svc.cluster.local"


def ordinal_from_unit_name(unit_name: str) -> int:
    """Extract the ordinal (trailing integer) from a Juju unit name, e.g. 'app/2' -> 2."""
    return int(unit_name.rsplit("/", 1)[-1])


def master_ordinals(all_ordinals: Iterable[int]) -> List[int]:
    """Compute which unit ordinals should run a master, per spec §2.2.

    - 0 units: no masters.
    - 1-2 units: exactly 1 master (lowest ordinal). Never 2 masters.
    - >=3 units: the 3 lowest ordinals.
    """
    ordinals = sorted(set(all_ordinals))
    if not ordinals:
        return []
    if len(ordinals) <= 2:
        return [ordinals[0]]
    return ordinals[:MAX_MASTERS]


def is_master(ordinal: int, masters: Iterable[int]) -> bool:
    """Return True if the given ordinal is in the master set."""
    return ordinal in set(masters)


def default_replication(num_units: int) -> str:
    """Compute the SeaweedFS auto default replication value.

    Empty config means auto: ``000`` with 1 unit, ``001`` with >= 2 units
    (see spec §3.1).
    """
    return "000" if num_units <= 1 else "001"


def master_peer_addresses(
    master_ords: Iterable[int], app_name: str, model_name: str
) -> List[str]:
    """Return the ``host:port`` list for all master-eligible units."""
    return [
        f"{pod_dns_name(app_name, ordinal, model_name)}:{MASTER_PORT}"
        for ordinal in sorted(set(master_ords))
    ]
