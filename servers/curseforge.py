"""Installation d'un modpack CurseForge sans l'endpoint de recherche.

Certaines clés API n'ont pas accès à /v1/mods/search : l'image itzg ne peut alors pas
résoudre un lien ou un slug. Ici on résout l'id nous-mêmes, on télécharge l'archive du
pack par son id, on en extrait manifest.json et overrides/, puis l'image installe les
mods depuis le manifest (CF_MODPACK_MANIFEST), qui ne contient que des ids.
"""

import json
import logging
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import configs
from .models import cf_key_file

log = logging.getLogger("servers")

API = "https://api.curseforge.com/v1"
CFWIDGET = "https://api.cfwidget.com/minecraft/modpacks/"
MODPACK_CLASS_ID = 4471
WORK_DIR = ".mcmanager"
MANIFEST_IN_CONTAINER = f"/data/{WORK_DIR}/manifest.json"
UA = "mcmanager (serveur Minecraft perso)"


class CurseForgeError(Exception):
    pass


def _request(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise CurseForgeError(f"{url.split('?')[0]} : HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise CurseForgeError(f"{url.split('?')[0]} : {exc.reason}") from exc


def _api(path, body=None):
    key = cf_key_file().read_text().strip() if cf_key_file().is_file() else ""
    if not key:
        raise CurseForgeError("Aucune clé CurseForge : ajoute-la dans Paramètres.")
    headers = {"x-api-key": key, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, headers={"User-Agent": UA, **headers})
    try:
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)["data"]
        except urllib.error.HTTPError as exc:
            raise CurseForgeError(f"{path.split('?')[0]} : HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise CurseForgeError(f"{path.split('?')[0]} : {exc.reason}") from exc
    except CurseForgeError as exc:
        if "HTTP 403" in str(exc):
            raise CurseForgeError(f"Clé CurseForge refusée pour {path.split('?')[0]} (HTTP 403).") from exc
        raise


def resolve_project_id(ref):
    """Project ID, lien de la page ou slug → id numérique."""
    ref = ref.strip().rstrip("/")
    if ref.isdigit():
        return int(ref)
    slug = urllib.parse.urlparse(ref).path.rstrip("/").split("/")[-1] if "/" in ref else ref
    if not re.fullmatch(r"[a-z0-9-]+", slug or ""):
        raise CurseForgeError(f"Modpack introuvable : « {ref} ». Donne son Project ID.")
    # Service public sans clé ; la recherche officielle est souvent refusée aux clés perso.
    try:
        with _request(CFWIDGET + slug, timeout=20) as r:
            data = json.load(r)
        return int(data["id"])
    except (CurseForgeError, KeyError, ValueError) as exc:
        raise CurseForgeError(
            f"Impossible de trouver l'id de « {slug} ». Mets le Project ID (panneau « About Project » de la page)."
        ) from exc


def _image_exclusion_rules(image):
    """Liste des mods « client uniquement » fournie par l'image (par slug)."""
    proc = subprocess.run(
        ["podman", "run", "--rm", "--entrypoint", "cat", image, "/image/cf-exclude-include.json"],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise CurseForgeError(f"Liste d'exclusions de l'image illisible : {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)


def _exclusion_ids(server, manifest, pack_slug):
    """Traduit les exclusions par slug de l'image en ids : l'image ferait sinon des recherches."""
    rules = _image_exclusion_rules(server.image)
    pack = (rules.get("modpacks") or {}).get(pack_slug) or {}
    excludes = set(rules.get("globalExcludes") or []) | set(pack.get("excludes") or [])
    excludes -= set(rules.get("globalForceIncludes") or []) | set(pack.get("forceIncludes") or [])
    ids = sorted({f["projectID"] for f in manifest.get("files", [])})
    found = []
    for i in range(0, len(ids), 500):
        for m in _api("/mods", {"modIds": ids[i:i + 500]}):
            if m.get("slug") in excludes:
                found.append((m["id"], m["slug"]))
    return sorted(found)


def prepare_modpack(server):
    """Télécharge le pack si besoin et renvoie (nom, id du fichier). Idempotent."""
    project_id = resolve_project_id(server.modpack_ref)
    mod = _api(f"/mods/{project_id}")
    if mod.get("classId") != MODPACK_CLASS_ID:
        raise CurseForgeError(f"« {mod.get('name')} » ({project_id}) n'est pas un modpack.")
    file_id = mod.get("mainFileId")
    if not file_id:
        raise CurseForgeError(f"« {mod['name']} » n'a pas de fichier publié.")

    work = server.data_dir / WORK_DIR
    work.mkdir(parents=True, exist_ok=True)
    # L'image exige un slug même avec un manifest ; elle ne s'en sert que comme nom.
    (work / "modpack-slug").write_text(mod["slug"])
    marker = work / "modpack-file-id"
    if marker.is_file() and marker.read_text().strip() == str(file_id) and (work / "manifest.json").is_file():
        if not (work / "exclude-ids").is_file():
            _write_exclusions(server, work, mod["slug"])
        return mod["name"], file_id

    url = _api(f"/mods/{project_id}/files/{file_id}/download-url")
    if not url:
        raise CurseForgeError(
            f"L'auteur de « {mod['name']} » interdit le téléchargement automatique du pack."
        )
    archive = work / f"modpack-{file_id}.zip"
    tmp = archive.with_suffix(".part")
    with _request(url, timeout=300) as r, open(tmp, "wb") as out:
        shutil.copyfileobj(r, out)
    tmp.rename(archive)

    with zipfile.ZipFile(archive) as z:
        try:
            manifest = json.loads(z.read("manifest.json"))
        except KeyError as exc:
            raise CurseForgeError("Archive du modpack sans manifest.json.") from exc
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2))
        overrides = manifest.get("overrides", "overrides").strip("/") + "/"
        root = server.data_dir.resolve()
        # Une config modifiée dans l'éditeur garde la version de l'utilisateur à chaque mise à jour du pack.
        edited = configs.edited_files(server)
        kept = []
        for info in z.infolist():
            if info.is_dir() or not info.filename.startswith(overrides):
                continue
            rel = info.filename[len(overrides):]
            target = (root / rel).resolve()
            if root not in target.parents:
                continue  # chemin hors du dossier du serveur : ignoré
            if rel in edited and target.is_file():
                kept.append(rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        (work / "overrides-kept.txt").write_text("".join(f"{r}\n" for r in kept))
        if kept:
            log.info("%s : mise à jour du pack, configs modifiées conservées : %s", server.slug, ", ".join(kept))

    for old in work.glob("modpack-*.zip"):
        if old != archive:
            old.unlink()
    _write_exclusions(server, work, mod["slug"])
    marker.write_text(str(file_id))
    return mod["name"], file_id


def _write_exclusions(server, work, pack_slug):
    manifest = json.loads((work / "manifest.json").read_text())
    excluded = _exclusion_ids(server, manifest, pack_slug)
    (work / "exclude-ids").write_text(",".join(str(i) for i, _ in excluded))
    (work / "excluded.txt").write_text("\n".join(f"{i} {s}" for i, s in excluded) + "\n")
