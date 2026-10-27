#!/usr/bin/env python3
"""
Runner V2FC — exécute les rounds de migration sans intervention (ajouté le
26/10/2026, à la demande de l'utilisateur : « ça doit être en automatique »).

Tourne dans le service `runner` de docker-compose.yml, sur la VM relais
elle-même (accès à /dev pour les volumes FusionCompute attachés) avec le
VDDK monté. En boucle :

  0. s'enregistre comme relais au démarrage puis toutes les 10 min : l'API
     retrouve cette VM dans FusionCompute par son UUID matériel ;
  1. demande à l'API ce qu'il y a à faire (`GET /api/v1/runner/work`) :
     seed après provisioning, delta périodique, delta final après bascule ;
  2. pour chaque round, récupère les identifiants source et lance
     `v2fc_source.py job-run` en sous-processus — exactement la commande
     que la page Migration affichait jusqu'ici, plus rien à copier.

Un job déjà en cours n'est jamais relancé en double ; `job-run` rapporte
lui-même résultat et logs à l'API.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

from relay_metrics import RelayMetrics

log = logging.getLogger("v2fc.runner")

API_URL = os.environ.get("V2FC_API_URL", "http://api:8000").rstrip("/")
TOKEN = os.environ.get("V2FC_RUNNER_TOKEN", "")
STATE_ROOT = Path(os.environ.get("V2FC_RUNNER_STATE", "/var/lib/v2fc/jobs"))
POLL_S = float(os.environ.get("V2FC_RUNNER_POLL_S", "15"))
MAX_PARALLEL = int(os.environ.get("V2FC_RUNNER_MAX_PARALLEL", "4"))
TRANSPORT = os.environ.get("V2FC_RUNNER_TRANSPORT", "nbdssl")
# Pause minimale avant de relancer un round du même job et du même type :
# garde-fou si l'API n'enregistre pas le résultat (le job resterait dans le
# même état et serait relancé en boucle — vu le 26/10/2026, 4 seeds de
# suite). Un round d'un autre type, ou un delta, part sans attendre
# (27/10/2026 : un delta demandé juste après le seed restait 5 min sur
# « démarrage sous 15 s »).
COOLDOWN_S = float(os.environ.get("V2FC_RUNNER_COOLDOWN_S", "300"))

REGISTER_EVERY_S = 600
SYSTEM_UUID_PATH = Path("/sys/class/dmi/id/product_uuid")

SOURCE_CLI = Path(__file__).with_name("v2fc_source.py")


def _get(path: str) -> requests.Response:
    resp = requests.get(f"{API_URL}{path}", timeout=30,
                        headers={"Authorization": f"Bearer {TOKEN}"})
    resp.raise_for_status()
    return resp


def _register() -> bool:
    """False si l'API n'a pas répondu (démarrage) : nouvel essai au tour suivant."""
    try:
        system_uuid = SYSTEM_UUID_PATH.read_text().strip()
    except OSError as exc:
        log.warning("UUID matériel illisible (%s) : relais non enregistré", exc)
        return True
    try:
        resp = requests.post(f"{API_URL}/api/v1/runner/register", timeout=120,
                             json={"system_uuid": system_uuid},
                             headers={"Authorization": f"Bearer {TOKEN}"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("enregistrement du relais impossible : %s", exc)
        return False
    relay = resp.json()
    if relay:
        log.info("relais enregistré : %s", relay["fc_vm_uri"])
    else:
        log.warning("VM %s introuvable dans les endpoints FusionCompute — "
                    "nouvel essai dans %d min", system_uuid, REGISTER_EVERY_S // 60)
    return True


def _heartbeat(running: dict, metrics: RelayMetrics) -> None:
    """Pouls pour l'interface (état du runner, rounds en cours, charge de la
    VM relais) — best-effort."""
    try:
        sample = metrics.sample()
    except (OSError, ValueError, IndexError) as exc:
        log.warning("mesures de la VM relais illisibles : %s", exc)
        sample = None
    try:
        requests.post(f"{API_URL}/api/v1/runner/heartbeat", timeout=10,
                      json={"running": [{"job_id": j, "round": r} for j, (r, _) in running.items()],
                            "metrics": sample},
                      headers={"Authorization": f"Bearer {TOKEN}"})
    except requests.RequestException:
        pass


def _start_round(job_id: str, round_kind: str) -> subprocess.Popen:
    cred = _get(f"/api/v1/runner/jobs/{job_id}/source-credentials").json()
    env = {**os.environ, "V2FC_API_TOKEN": TOKEN,
           "V2FC_VC_USER": cred["user"], "V2FC_VC_PASSWORD": cred["password"]}
    cmd = [sys.executable, str(SOURCE_CLI), "job-run", "--api-url", API_URL,
           "--job-id", job_id, "--round", round_kind,
           "--target-dir", str(STATE_ROOT / job_id),
           "--transport", TRANSPORT, "--require", TRANSPORT]
    log.info("job %s : round %s lancé", job_id, round_kind)
    return subprocess.Popen(cmd, env=env)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if not TOKEN:
        sys.exit("V2FC_RUNNER_TOKEN non défini — voir .env.example")
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    running: dict[str, tuple[str, subprocess.Popen]] = {}
    finished_at: dict[str, tuple[str, float]] = {}   # job -> (type de round, fin)
    registered_at = -REGISTER_EVERY_S
    metrics = RelayMetrics()

    log.info("runner démarré — API %s, %d round(s) en parallèle au plus",
             API_URL, MAX_PARALLEL)
    while True:
        if time.monotonic() - registered_at >= REGISTER_EVERY_S:
            if _register():
                registered_at = time.monotonic()

        for job_id, (round_kind, proc) in list(running.items()):
            rc = proc.poll()
            if rc is not None:
                log.info("job %s : round %s terminé (code %s)", job_id, round_kind, rc)
                del running[job_id]
                finished_at[job_id] = (round_kind, time.monotonic())

        _heartbeat(running, metrics)
        try:
            work = _get("/api/v1/runner/work").json()
        except requests.RequestException as exc:
            log.warning("API injoignable : %s", exc)
            work = []

        for item in work:
            if item["job_id"] in running or len(running) >= MAX_PARALLEL:
                continue
            # Un delta ne part que sur demande, consommée au démarrage du
            # round (api/routers/jobs.py) : pas de boucle possible, pas d'attente.
            last_kind, last_end = finished_at.get(item["job_id"], ("", -COOLDOWN_S))
            if item["round"] != "delta" and last_kind == item["round"] \
                    and time.monotonic() - last_end < COOLDOWN_S:
                continue
            try:
                running[item["job_id"]] = (item["round"],
                                           _start_round(item["job_id"], item["round"]))
            except requests.RequestException as exc:
                log.warning("job %s : lancement impossible : %s", item["job_id"], exc)

        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
