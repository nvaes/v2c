# Data Mover V2FC — image de développement et de banc d'essai.
#
# Le VDDK n'est PAS embarqué : il est sous licence VMware et sa
# redistribution est interdite. Il est monté au lancement depuis le poste.
#
# Le VDDK n'existe qu'en x86-64. Sur un Mac Apple Silicon, construire et
# lancer avec --platform linux/amd64 : Docker Desktop passe alors par
# Rosetta. (VMware Fusion sur Apple Silicon ne sait pas exécuter de VM
# x86-64, ce n'est donc pas une alternative.)
#
# Décompresser l'archive une fois :
#   tar xzf VMware-vix-disklib-8.0.3-*.tar.gz   -> vmware-vix-disklib-distrib/
#
#   docker build --platform linux/amd64 -t v2fc-datamover .
#   docker run --rm --platform linux/amd64 \
#       -v "$PWD/../vmware-vix-disklib-distrib:/opt/vddk:ro" \
#       v2fc-datamover

# Pas de --platform en dur ici : c'est le drapeau --platform de `docker build`
# qui fixe l'architecture. Le coder dans le FROM déclenche l'avertissement
# FromPlatformFlagConstDisallowed et casse les builds multi-architecture.
FROM debian:12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt /tmp/
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements-dev.txt \
 && rm /tmp/requirements*.txt

# Le VDDK ne pose aucun RPATH : sans cette variable, le plugin de transport
# avancé (san, hotadd) ne se charge pas et InitEx renvoie quand même VIX_OK.
# Elle doit être posée AVANT le démarrage du processus — la définir depuis
# Python est sans effet. Voir ARCHITECTURE.md §2.2.
ENV VDDK_DISTRIB=/opt/vddk \
    LD_LIBRARY_PATH=/opt/vddk/lib64 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/work

WORKDIR /work
COPY datamover/ /work/datamover/
COPY relay/     /work/relay/
COPY tools/     /work/tools/
COPY lab/       /work/lab/
COPY tests/     /work/tests/

# Contrôle d'environnement au démarrage : refuse de tourner en mode dégradé.
ENTRYPOINT ["/bin/bash", "-c", "\
    python3 /work/tools/vddk_probe.py \"$VDDK_DISTRIB\" --require nbdssl || exit 1; \
    exec \"$@\"", "--"]
CMD ["bash", "-c", "python3 -m pytest /work/tests -q \
 && cd /work/lab && python3 make_disk.py && python3 test_e2e.py"]
