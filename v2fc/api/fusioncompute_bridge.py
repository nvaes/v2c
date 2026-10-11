"""
Pont synchrone vers `datamover.fusioncompute` (requests, donc bloquant,
même raison que api/vsphere_bridge.py pour pyVmomi) : les routeurs FastAPI
l'appellent via `starlette.concurrency.run_in_threadpool`.

Rien ici n'invente de comportement FusionCompute : fine couche au-dessus du
driver cible documenté en ARCHITECTURE.md §7.5 (D2 résolu le 17/09/2026).
"""
from __future__ import annotations

from datamover.fusioncompute import FusionComputeClient, FusionComputeError


def probe(host: str, user: str, password: str, port: int, verify_tls: bool) -> dict:
    """Connexion + repli immédiat. Ne lève pas : renvoie ok=False avec le message."""
    try:
        with FusionComputeClient(host, user, password, port=port, verify=verify_tls) as client:
            return {
                "ok": True,
                "message": f"site {client.site.name or client.site.id}",
                "platform": f"FusionCompute — site {client.site.name or client.site.id}",
                "tls_fingerprint_sha1": None,  # cf. HANDOFF.md : pas encore relevé côté FC
            }
    except FusionComputeError as exc:
        return {"ok": False, "message": str(exc), "platform": None,
                "tls_fingerprint_sha1": None}
    except Exception as exc:                            # noqa: BLE001
        # une erreur réseau/TLS brute (hors FusionComputeError) n'est pas à
        # laisser remonter en 500 nue, même logique que vsphere_bridge.probe.
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}",
                "platform": None, "tls_fingerprint_sha1": None}


def list_datastores(host: str, user: str, password: str, port: int,
                    verify_tls: bool) -> list[dict]:
    """
    Datastores du site (agrégés depuis tous les hôtes, voir
    FusionComputeClient.list_datastores), reformatés vers api.schemas.FcDatastore.
    """
    with FusionComputeClient(host, user, password, port=port, verify=verify_tls) as client:
        out = []
        for ds in client.list_datastores():
            out.append({
                "urn": ds["urn"],
                "name": ds.get("name", ""),
                "status": ds.get("status"),
                "storage_type": ds.get("storageType"),
                "actual_capacity_gb": ds.get("actualCapacityGB"),
                "actual_free_gb": ds.get("actualFreeSizeGB"),
            })
        return out


def list_networks(host: str, user: str, password: str, port: int,
                  verify_tls: bool) -> list[dict]:
    """Port groups de tous les DVS du site, reformatés vers api.schemas.FcNetwork."""
    with FusionComputeClient(host, user, password, port=port, verify=verify_tls) as client:
        out = []
        for pg in client.list_networks():
            out.append({
                "urn": pg["urn"],
                "name": pg.get("name", ""),
                "dvswitch_urn": pg["dvswitch_urn"],
                "dvswitch_name": pg.get("dvswitch_name", ""),
                "port_type": pg.get("portType"),
                "vlan_id": pg.get("vlanId"),
                "vlan_range": pg.get("vlanRange"),
            })
        return out
