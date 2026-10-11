"""
Régression du chemin de remontée d'erreur du VDDK.

Ces tests chargent réellement la bibliothèque : le défaut corrigé ici ne se
manifeste pas comme une exception Python mais comme un abandon du processus
par la glibc (« munmap_chunk(): invalid pointer »). Seul un appel natif
répété peut le démontrer — un bouchon ne prouverait rien.

Ils sont ignorés si VDDK_DISTRIB n'est pas défini.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DISTRIB = os.environ.get("VDDK_DISTRIB")
pytestmark = pytest.mark.skipif(not DISTRIB, reason="VDDK_DISTRIB non défini")


def test_check_leve_sans_faire_avorter_le_processus():
    """
    Cent allers-retours GetErrorText/FreeErrorText dans un sous-processus.

    Avec l'ancienne déclaration (restype c_char_p), ctypes recopiait la
    chaîne et FreeErrorText libérait de la mémoire Python : la glibc tuait
    le processus, en général avant la centième itération. Le test vérifie
    donc un code de sortie, pas une exception.
    """
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from datamover import vddk
        lib = vddk.init({DISTRIB!r})
        messages = set()
        for code in range(1, 101):
            try:
                lib.check(code, "test")
            except vddk.VddkError as exc:
                assert exc.code == code & 0xFFFF
                messages.add(exc.text)
        assert messages, "aucun message d'erreur récupéré"
        print("OK", len(messages))
    """)
    env = dict(os.environ, LD_LIBRARY_PATH=f"{DISTRIB}/lib64")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env, timeout=120)
    assert proc.returncode == 0, (
        f"processus terminé avec {proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr[-2000:]}")
    assert "munmap" not in proc.stderr
    assert proc.stdout.startswith("OK")


def test_vix_ok_ne_leve_pas():
    from datamover import vddk
    lib = vddk.init(DISTRIB)
    lib.check(vddk.VIX_OK, "test")          # ne doit rien faire


def test_message_derreur_exploitable():
    from datamover import vddk
    lib = vddk.init(DISTRIB)
    with pytest.raises(vddk.VddkError) as exc:
        lib.check(1, "contexte-de-test")
    assert "contexte-de-test" in str(exc.value)
    assert exc.value.text and exc.value.text != "erreur inconnue"
