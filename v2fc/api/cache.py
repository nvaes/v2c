"""
Redis — mémorise le dernier inventaire connu (vSphere) et les dernières
listes de datastores/réseaux (FusionCompute) obtenues d'un endpoint, pour
éviter de rescanner toutes les VM à chaque ouverture de page : un inventaire
complet avec pré-vérification par VM est loin d'être instantané, et Nicolas
n'a pas besoin qu'il soit rejoué à chaque connexion (17/09/2026).

Pas de TTL : la dernière valeur connue reste servie indéfiniment tant que
personne ne clique sur « rafraîchir » (routes `get_inventory` /
`get_fc_datastores` / `get_fc_networks`, paramètre `refresh=true`), seul
déclencheur d'un nouveau scan. C'est un choix délibéré, différent d'un cache
TTL classique : la donnée n'est jamais automatiquement invalidée par le
temps, uniquement remplacée par la valeur suivante quand l'utilisateur
demande explicitement un rafraîchissement (`set_cached_*` ci-dessous, appelé
uniquement après un scan réussi). Ce n'est toujours pas la source de vérité
— un simple raccourci pour ne pas revalider ce qui n'a pas changé — donc pas
grave si Redis perd son contenu (redémarrage sans volume, `docker compose
down`) : la page relance alors un scan complet au prochain chargement,
exactement comme avant ce changement.

Sert aussi, depuis le 17/09/2026, à publier une progression fine pendant un
inventaire vCenter (ci-dessous, section « Progression ») : Nicolas a demandé
un statut d'avancement pendant le scan, qui est justement l'opération lente
que le cache ci-dessus cherche à éviter de rejouer inutilement. Contrairement
à l'inventaire, cette progression EST transitoire par nature (elle ne décrit
qu'un scan en cours) : elle porte un TTL court, pour ne jamais laisser
croire qu'un scan tourne encore après un crash ou un redémarrage de l'API.

Amorce aussi du futur bus d'événements de progression (ARCHITECTURE.md §3 :
« Event bus (Redis) »), qui arrivera avec L2. On n'écrit pas de code pour ce
qui n'existe pas encore : ce module ne fait que ce qui est décrit ici.
"""
from __future__ import annotations

import json
from functools import lru_cache

import redis.asyncio as redis
import redis as redis_sync   # client bloquant : seul utilisable depuis un
                              # thread non-async (voir set_inventory_progress_sync)

from .config import get_settings


@lru_cache
def get_redis() -> "redis.Redis":
    return redis.from_url(get_settings().redis_url, decode_responses=True)


async def get_cached_inventory(endpoint_id) -> dict | None:
    raw = await get_redis().get(f"inventory:{endpoint_id}")
    return json.loads(raw) if raw else None


async def set_cached_inventory(endpoint_id, data: dict) -> None:
    await get_redis().set(f"inventory:{endpoint_id}", json.dumps(data))


# Datastores/réseaux FusionCompute (§7.5) : même motif que l'inventaire
# vSphere ci-dessus — dernière valeur connue conservée sans expiration,
# remplacée uniquement par un rafraîchissement explicite.
async def get_cached_fc_datastores(endpoint_id) -> list | None:
    raw = await get_redis().get(f"fc_datastores:{endpoint_id}")
    return json.loads(raw) if raw else None


async def set_cached_fc_datastores(endpoint_id, data: list) -> None:
    await get_redis().set(f"fc_datastores:{endpoint_id}", json.dumps(data))


async def get_cached_fc_networks(endpoint_id) -> list | None:
    raw = await get_redis().get(f"fc_networks:{endpoint_id}")
    return json.loads(raw) if raw else None


async def set_cached_fc_networks(endpoint_id, data: list) -> None:
    await get_redis().set(f"fc_networks:{endpoint_id}", json.dumps(data))


# --------------------------------------------------------------------------
# Progression d'un inventaire vCenter en cours (17/09/2026)
# --------------------------------------------------------------------------
# TTL court et volontaire (contrairement au cache ci-dessus) : c'est un état
# « un scan tourne, en voici l'avancement », pas une valeur qu'on veut
# revoir indéfiniment. S'il expire pendant un scan anormalement long, l'UI
# retombe simplement sur l'état par défaut (« idle ») plutôt que d'afficher
# une progression figée pour toujours.
PROGRESS_TTL_SECONDS = 120


@lru_cache
def get_sync_redis() -> "redis_sync.Redis":
    """
    Client Redis synchrone. `get_redis()` ci-dessus (redis.asyncio) ne peut
    pas être appelé depuis `vsphere_bridge.inventory()` : cette fonction
    tourne dans un thread bloquant (starlette.concurrency.run_in_threadpool,
    pyVmomi n'est pas async), sans boucle asyncio à qui parler. D'où ce
    second client, réservé à la publication de progression depuis ce thread.
    """
    return redis_sync.Redis.from_url(get_settings().redis_url, decode_responses=True)


def set_inventory_progress_sync(endpoint_id, phase: str, done: int, total: int) -> None:
    """
    À appeler UNIQUEMENT depuis le thread bloquant de `vsphere_bridge.inventory()`
    (jamais depuis une route FastAPI async — utiliser un client asyncio y
    ferait un blocage de la boucle événementielle, pas d'ici).
    """
    get_sync_redis().set(
        f"inventory_progress:{endpoint_id}",
        json.dumps({"phase": phase, "done": done, "total": total}),
        ex=PROGRESS_TTL_SECONDS,
    )


async def get_inventory_progress(endpoint_id) -> dict | None:
    """Lu par la route de poll (GET .../inventory/progress) — jamais par le scan lui-même."""
    raw = await get_redis().get(f"inventory_progress:{endpoint_id}")
    return json.loads(raw) if raw else None


# --------------------------------------------------------------------------
# Pouls du runner (26/10/2026) : envoyé à chaque tour de boucle (15 s), lu
# par GET /runner/status pour afficher son état dans l'interface.
# --------------------------------------------------------------------------
async def set_runner_heartbeat(data: dict) -> None:
    await get_redis().set("runner:heartbeat", json.dumps(data))


async def get_runner_heartbeat() -> dict | None:
    raw = await get_redis().get("runner:heartbeat")
    return json.loads(raw) if raw else None
