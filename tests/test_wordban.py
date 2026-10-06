"""The package may name hosts, pins and the attribution text ONLY in vtcseamless/profile.py.
Nothing else — code, comments, docs, tests — mentions the tile host, the operator's private
surface, tokens, or real-world examples (named places, object ids)."""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")
BAN = re.compile("|".join(["cad" + "astre", "kat" + "aster", "/k/", "CAD" + "ASTRE_TOKEN", "CONTRIB" + "UTOR_TOKEN", "umf" + "eld", r"exe\.xyz",
                          "K[öo]e?fl" + "ach", "Kohl" + "schwarz", "Pib" + "er", "Naud" + "ers", r"\b\d{5}-\d+(?:/\d+)?\b"]), re.I)
EXEMPT_FILES = {"vtcseamless/profile.py", "tests/test_wordban.py"}
# frozen reference strings (part of the NE container header contract) — may not be edited
EXEMPT_LINES = {"ne_cells/pack.py": ("ATTRIBUTION = ", "NOTICE = (", '"GNR or EZ is contained')}


def _files():
    for d, dirs, files in os.walk(ROOT):
        dirs[:] = [x for x in dirs if x not in (".git", "__pycache__", "build", "dist", ".pytest_cache") and not x.endswith(".egg-info")]
        for f in files:
            if f.endswith((".py", ".md", ".toml", ".sh", ".txt", ".service", ".cfg", ".yml", ".yaml")):
                yield os.path.relpath(os.path.join(d, f), ROOT)


def test_word_ban():
    bad = []
    for rel in _files():
        if rel in EXEMPT_FILES:
            continue
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if rel in EXEMPT_LINES and any(line.lstrip().startswith(p) for p in EXEMPT_LINES[rel]):
                    continue
                m = BAN.search(line)
                if m:
                    bad.append(f"{rel}:{i}: {m.group(0)!r}")
    assert not bad, "\n".join(bad)


def test_profile_is_the_only_host_source():
    with open(os.path.join(ROOT, "vtcseamless/profile.py"), encoding="utf-8") as fh:
        p = fh.read()
    assert "PUBLIC_API" in p and "CONTRIB_PREFIX" in p and "def notice" in p
    assert "kataster.bev.gv.at" not in p, "the python side never fetches tiles; the host lives in the Go preset only"
