"""Renommage après remplacement, et demande de rescan à Sonarr / Radarr / Emby.

Séries et animes : on laisse Sonarr renommer lui-même selon son propre format (comme Mouflanimexer) :
  1. RescanSeries : Sonarr relit le fichier (H.264 devenu H.265, audio...) ;
  2. le groupe de release passe de « Team » à « Team-MouFlo » ;
  3. RenameFiles sur ce seul fichier : Sonarr applique le format {Series Title}.S{season:00}E{episode:00}... avec ces informations.
Films : seul le nom de la team change (« -[Team] » -> « -[Team-MouFlo] » ou « -Team » -> « -Team-MouFlo »), le reste du nom n'est pas touché ;
  Radarr est ensuite prévenu par un rescan du film.
Dans les deux cas Emby est prévenu du changement de chemin."""
import json, os, re, time, urllib.error, urllib.request
from pathlib import Path

MARQUE = "MouFlo"


# ---------------------------------------------------------------- noms
_RG_CROCHETS = re.compile(r"-\[([^\]]+)\](\.[A-Za-z0-9]{2,4})$")
_RG_SIMPLE = re.compile(r"-([A-Za-z0-9][A-Za-z0-9_.]*)(\.[A-Za-z0-9]{2,4})$")


def groupe_actuel(nom):
    """Team lue dans un nom de fichier (-[Team].mkv ou -Team.mkv), ou None."""
    m = _RG_CROCHETS.search(nom) or _RG_SIMPLE.search(nom)
    return m.group(1) if m else None


def groupe_marque(g):
    return g if g.endswith("-" + MARQUE) else g + "-" + MARQUE


def nom_film(nom):
    """Films : on ne touche qu'à la team. Renvoie le nouveau nom, ou le même s'il n'y a rien à faire."""
    if ("-" + MARQUE) in nom:
        return nom
    m = _RG_CROCHETS.search(nom)
    if m:
        return nom[:m.start()] + f"-[{m.group(1)}-{MARQUE}]" + m.group(2)
    m = _RG_SIMPLE.search(nom)
    if m and not re.fullmatch(r"\d{3,4}p|x26[45]|h26[45]|HEVC|AVC", m.group(1), re.I):
        return nom[:m.start()] + f"-{m.group(1)}-{MARQUE}" + m.group(2)
    return nom


# ---------------------------------------------------------------- client HTTP minimal
def _appel(base, cle, methode, chemin, corps=None, timeout=30, _redir=3):
    req = urllib.request.Request(base + chemin, data=json.dumps(corps).encode() if corps is not None else None, method=methode,
                                 headers={"X-Api-Key": cle, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            t = r.read()
            return json.loads(t) if t else None
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 307, 308) and _redir and e.headers.get("Location"):   # derrière un reverse proxy : on suit la redirection nous-mêmes
            loc = e.headers["Location"]
            from urllib.parse import urlsplit
            u = urlsplit(base)
            return _appel(f"{u.scheme}://{u.netloc}", cle, methode, loc if loc.startswith("/") else urlsplit(loc).path, corps, timeout, _redir - 1)
        raise


def acces(nom):
    return os.getenv(nom.upper() + "_URL", "").rstrip("/"), os.getenv(nom.upper() + "_KEY", "")


def etat(nom):
    """Test de connexion pour la page Réglages : (réussi, message)."""
    b, k = acces(nom)
    if not (b and k):
        return False, "adresse ou clé manquante"
    try:
        s = _appel(b, k, "GET", "/api/v3/system/status", timeout=15)
        n = len(_appel(b, k, "GET", "/api/v3/series" if nom == "sonarr" else "/api/v3/movie", timeout=60) or [])
        return True, f"{nom.capitalize()} {s.get('version', '?')} : {n} {'séries' if nom == 'sonarr' else 'films'}"
    except urllib.error.HTTPError as e:
        return False, "refusé (clé invalide ?)" if e.code in (401, 403) else f"erreur HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, "connexion impossible : " + type(e).__name__


def _attendre(b, k, cid, max_s=180):
    t0 = time.time()
    while time.time() - t0 < max_s:
        try:
            s = _appel(b, k, "GET", f"/api/v3/command/{cid}", timeout=15)
            if s and s.get("status") in ("completed", "failed"):
                return s.get("status")
        except Exception:  # noqa: BLE001
            return None
        time.sleep(2)
    return None


def _plus_long(items, chemin, cle="path"):
    chemin = os.path.normpath(chemin)
    best = None
    for it in items:
        p = os.path.normpath(it.get(cle) or "")
        if p and (chemin == p or chemin.startswith(p + os.sep)) and (best is None or len(p) > len(best[0])):
            best = (p, it)
    return best[1] if best else None


# ---------------------------------------------------------------- Sonarr
def sonarr_apres(chemin):
    """Rescan, groupe « Team-MouFlo », renommage par Sonarr. Renvoie le chemin final (Path) ou None si rien n'a pu être suivi."""
    b, k = acces("sonarr")
    if not (b and k):
        return None
    chemin = str(chemin)
    serie = _plus_long(_appel(b, k, "GET", "/api/v3/series"), chemin)
    if not serie:
        raise RuntimeError("série introuvable dans Sonarr pour " + os.path.basename(chemin))
    sid = serie["id"]

    def fichier():
        for f in _appel(b, k, "GET", f"/api/v3/episodefile?seriesId={sid}") or []:
            if os.path.normpath(f.get("path") or "") == os.path.normpath(chemin):
                return f
    f = fichier()
    c = _appel(b, k, "POST", "/api/v3/command", {"name": "RescanSeries", "seriesId": sid})
    if c and c.get("id") is not None:
        _attendre(b, k, c["id"])
    f = fichier() or f
    if not f:
        raise RuntimeError("fichier introuvable dans Sonarr après le rescan")
    g = groupe_actuel(os.path.basename(chemin)) or f.get("releaseGroup")
    if g:
        _appel(b, k, "PUT", "/api/v3/episodefile/editor", {"episodeFileIds": [f["id"]], "releaseGroup": groupe_marque(g)})
    c = _appel(b, k, "POST", "/api/v3/command", {"name": "RenameFiles", "seriesId": sid, "files": [f["id"]]})
    if c and c.get("id") is not None:
        _attendre(b, k, c["id"])
    nf = _appel(b, k, "GET", f"/api/v3/episodefile/{f['id']}")
    return Path(nf["path"]) if nf and nf.get("path") else None


# ---------------------------------------------------------------- Radarr (films)
def radarr_apres(chemin):
    """Films : seule la team change dans le nom, puis rescan du film par Radarr (s'il est branché)."""
    p = Path(chemin)
    nouveau = p.with_name(nom_film(p.name))
    if nouveau != p:
        if nouveau.exists():
            raise RuntimeError("un fichier porte déjà le nouveau nom : " + nouveau.name)
        os.rename(p, nouveau)
    b, k = acces("radarr")
    if b and k:
        try:
            m = _plus_long(_appel(b, k, "GET", "/api/v3/movie", timeout=60), str(nouveau))
            if m:
                _appel(b, k, "POST", "/api/v3/command", {"name": "RescanMovie", "movieId": m["id"]})
        except Exception:  # noqa: BLE001  (le rescan est facultatif : le fichier est déjà renommé)
            pass
    return nouveau


# ---------------------------------------------------------------- Emby
def emby_prevenir(emby_host, ancien, nouveau):
    cle = os.getenv("EMBY_API_KEY", "")
    if not (emby_host and cle):
        return
    ups = [{"Path": str(nouveau), "UpdateType": "Created"}]
    if str(ancien) != str(nouveau):
        ups.insert(0, {"Path": str(ancien), "UpdateType": "Deleted"})
    req = urllib.request.Request(emby_host.rstrip("/") + "/Library/Media/Updated", data=json.dumps({"Updates": ups}).encode(), method="POST",
                                 headers={"X-Emby-Token": cle, "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def apres_remplacement(chemin, cat, emby_host):
    """Point d'entrée : renvoie le chemin final du fichier (identique si rien n'a changé)."""
    final = sonarr_apres(chemin) if cat in ("anime", "series") else radarr_apres(chemin)
    final = final or Path(chemin)
    try:
        emby_prevenir(emby_host, chemin, final)
    except Exception:  # noqa: BLE001
        pass
    return final


def apres_lot(items, emby_host):
    """items : [(chemin, cat)]. Même résultat que apres_remplacement pour chaque fichier, mais Sonarr ne relit et ne renomme qu'UNE fois par série (au lieu d'une fois
    par fichier) et Emby reçoit UNE requête pour tous les fichiers. Renvoie {chemin: (chemin final, "")} ou {chemin: (None, message d'erreur)}."""
    res = {}
    sonarr = [c for c, cat in items if cat in ("anime", "series")]
    if sonarr:
        b, k = acces("sonarr")
        if not (b and k):
            for c in sonarr:
                res[c] = (Path(c), "")   # Sonarr non branché : le fichier garde son nom (comme sonarr_apres)
        else:
            try:
                series = _appel(b, k, "GET", "/api/v3/series")
            except Exception as e:  # noqa: BLE001
                series = None
                for c in sonarr:
                    res[c] = (None, "Sonarr injoignable : " + type(e).__name__)
            if series is not None:
                par = {}
                for c in sonarr:
                    s = _plus_long(series, c)
                    if s:
                        par.setdefault(s["id"], (s, []))[1].append(c)
                    else:
                        res[c] = (None, "série introuvable dans Sonarr pour " + os.path.basename(c))
                for s, chemins in par.values():
                    try:
                        out = renommer_sans_emby(s, [{"path": c} for c in chemins], log=lambda *_a: None)
                    except Exception as e:  # noqa: BLE001
                        for c in chemins:
                            res[c] = (None, type(e).__name__ + " : " + str(e)[:150])
                        continue
                    for c in chemins:
                        n = out.get(c)
                        res[c] = (Path(n), "") if n else (None, "fichier introuvable dans Sonarr après le rescan")
    for c, cat in items:
        if cat not in ("anime", "series"):
            try:
                res[c] = (radarr_apres(c), "")
            except Exception as e:  # noqa: BLE001
                res[c] = (None, str(e)[:200])
    cle = os.getenv("EMBY_API_KEY", "")
    if emby_host and cle and res:
        ups = []
        for c, (f, _err) in res.items():
            if f is None:
                ups.append({"Path": str(c), "UpdateType": "Modified"})   # non renommé : Emby doit quand même relire les pistes
            else:
                if str(c) != str(f):
                    ups.append({"Path": str(c), "UpdateType": "Deleted"})
                ups.append({"Path": str(f), "UpdateType": "Created"})
        for j in range(0, len(ups), 200):   # une requête par paquet de 200 changements
            try:
                req = urllib.request.Request(emby_host.rstrip("/") + "/Library/Media/Updated", data=json.dumps({"Updates": ups[j:j + 200]}).encode(), method="POST",
                                             headers={"X-Emby-Token": cle, "Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=60).read()
            except Exception:  # noqa: BLE001
                pass
    return res


# ---------------------------------------------------------------- Emby : état de lecture conservé
_UD = ("Played", "PlayCount", "PlaybackPositionTicks", "IsFavorite", "LastPlayedDate")


def _emby(host, cle, methode, chemin, corps=None, timeout=120):
    req = urllib.request.Request(host.rstrip("/") + chemin, method=methode, data=json.dumps(corps).encode() if corps is not None else None,
                                 headers={"X-Emby-Token": cle, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        t = r.read()
        return json.loads(t) if t else None


def emby_serie(host, cle, dossier_serie):
    """Identifiant Emby de la série dont le dossier est dossier_serie."""
    for s in _emby(host, cle, "GET", "/Items?Recursive=true&IncludeItemTypes=Series&Fields=Path&Limit=5000")["Items"]:
        if os.path.normpath(s.get("Path") or "") == os.path.normpath(dossier_serie):
            return s["Id"]


def emby_etats(host, cle, serie_id):
    """{chemin du fichier: {utilisateur: état de lecture}} pour tous les épisodes de la série."""
    out = {}
    for u in _emby(host, cle, "GET", "/Users"):
        for i in _emby(host, cle, "GET", f"/Users/{u['Id']}/Items?Recursive=true&ParentId={serie_id}&IncludeItemTypes=Episode&Fields=Path&Limit=5000")["Items"]:
            ud = i.get("UserData") or {}
            if ud.get("Played") or ud.get("PlaybackPositionTicks") or ud.get("IsFavorite") or ud.get("PlayCount"):
                out.setdefault(i["Path"], {})[u["Id"]] = {k: ud.get(k) for k in _UD if ud.get(k) is not None}
    return out


def emby_rafraichir(host, cle, serie_id):
    _emby(host, cle, "POST", f"/Items/{serie_id}/Refresh?Recursive=true&ImageRefreshMode=ValidationOnly&MetadataRefreshMode=ValidationOnly&ReplaceAllMetadata=false")


def emby_restaurer(host, cle, serie_id, etats, correspondance, attente=240):
    """Remet l'état de lecture sur les nouveaux éléments. correspondance : {ancien chemin: nouveau chemin}.
    Renvoie (remis, manquants, déjà conservés)."""
    attendus = {correspondance[p]: etats[p] for p in etats if p in correspondance}
    if not attendus:
        return 0, 0, 0
    t0 = time.time(); trouves = {}
    users = [u["Id"] for u in _emby(host, cle, "GET", "/Users")]
    while time.time() - t0 < attente and len(trouves) < len(attendus):
        for i in _emby(host, cle, "GET", f"/Items?Recursive=true&ParentId={serie_id}&IncludeItemTypes=Episode&Fields=Path&Limit=5000")["Items"]:
            if i["Path"] in attendus:
                trouves[i["Path"]] = i["Id"]
        if len(trouves) < len(attendus):
            time.sleep(10)
    remis = deja = 0
    for p, par_user in attendus.items():
        if p not in trouves:
            continue
        for uid, ud in par_user.items():
            cur = (_emby(host, cle, "GET", f"/Users/{uid}/Items/{trouves[p]}") or {}).get("UserData") or {}
            if bool(cur.get("Played")) == bool(ud.get("Played")) and (cur.get("PlaybackPositionTicks") or 0) == (ud.get("PlaybackPositionTicks") or 0) and bool(cur.get("IsFavorite")) == bool(ud.get("IsFavorite")):
                deja += 1; continue
            _emby(host, cle, "POST", f"/Users/{uid}/Items/{trouves[p]}/UserData", {k: v for k, v in ud.items()})
            remis += 1
    return remis, len(attendus) - len(trouves), deja


# ---------------------------------------------------------------- renommage en masse, série par série
_TAG = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,4})")


def resoudre_chemin(chemin):
    """Si Sonarr a déjà renommé le fichier, retrouve son nom actuel (même dossier, même SxxEyy)."""
    p = Path(chemin)
    if p.exists():
        return str(p)
    m = _TAG.search(p.name)
    if not m or not p.parent.exists():
        return None
    cle = (int(m.group(1)), int(m.group(2)))
    c = [f for f in p.parent.glob("*.mkv") if (t := _TAG.search(f.name)) and (int(t.group(1)), int(t.group(2))) == cle]
    return str(c[0]) if len(c) == 1 else None


def renommer_une_serie(emby_host, serie, fichiers, log=print):
    """fichiers : [{'path': ...}]. Renvoie ({ancien chemin: nouveau chemin}, (remis, manquants, conservés))."""
    b, k = acces("sonarr")
    ekey = os.getenv("EMBY_API_KEY", "")
    eid, etats = None, {}
    try:
        eid = emby_serie(emby_host, ekey, serie["path"])
        etats = emby_etats(emby_host, ekey, eid) if eid else {}
    except Exception as e:  # noqa: BLE001
        log(f"   Emby : état de lecture non relevé ({type(e).__name__})")
    c = _appel(b, k, "POST", "/api/v3/command", {"name": "RescanSeries", "seriesId": serie["id"]})
    if c and c.get("id") is not None:
        _attendre(b, k, c["id"], 600)
    files = {os.path.normpath(x["path"]): x for x in _appel(b, k, "GET", f"/api/v3/episodefile?seriesId={serie['id']}") or []}
    todo, groupes = [], {}
    for f in fichiers:
        x = files.get(os.path.normpath(f["path"]))
        if not x:
            log(f"   introuvable dans Sonarr : {os.path.basename(f['path'])[:70]}")
            continue
        g = groupe_actuel(os.path.basename(f["path"])) or x.get("releaseGroup")
        if g and not g.endswith("-" + MARQUE):
            groupes.setdefault(groupe_marque(g), []).append(x["id"])
        todo.append((f, x["id"]))
    for g, ids in groupes.items():
        _appel(b, k, "PUT", "/api/v3/episodefile/editor", {"episodeFileIds": ids, "releaseGroup": g})
    ids = [i for _, i in todo]
    for j in range(0, len(ids), 80):
        c = _appel(b, k, "POST", "/api/v3/command", {"name": "RenameFiles", "seriesId": serie["id"], "files": ids[j:j + 80]})
        if c and c.get("id") is not None:
            _attendre(b, k, c["id"], 600)
    apres = {x["id"]: x for x in _appel(b, k, "GET", f"/api/v3/episodefile?seriesId={serie['id']}") or []}
    res = {f["path"]: (apres.get(i) or {}).get("path") for f, i in todo}
    bilan = (0, 0, 0)
    if eid and etats:
        try:
            emby_rafraichir(emby_host, ekey, eid)
            bilan = emby_restaurer(emby_host, ekey, eid, etats, {o: n for o, n in res.items() if n and n != o}, attente=300)
        except Exception as e:  # noqa: BLE001
            log(f"   Emby : restauration de l'état de lecture en échec ({type(e).__name__}: {str(e)[:80]})")
    return res, bilan


def renommer_sans_emby(serie, fichiers, log=print):
    """Comme renommer_une_serie mais sans toucher à Emby (il est rafraîchi une seule fois à la fin, pour toutes les séries)."""
    b, k = acces("sonarr")
    c = _appel(b, k, "POST", "/api/v3/command", {"name": "RescanSeries", "seriesId": serie["id"]})
    if c and c.get("id") is not None:
        _attendre(b, k, c["id"], 600)
    files = {os.path.normpath(x["path"]): x for x in _appel(b, k, "GET", f"/api/v3/episodefile?seriesId={serie['id']}") or []}
    todo, groupes = [], {}
    for f in fichiers:
        x = files.get(os.path.normpath(f["path"]))
        if not x:
            log(f"   introuvable dans Sonarr : {os.path.basename(f['path'])[:70]}")
            continue
        g = groupe_actuel(os.path.basename(f["path"])) or x.get("releaseGroup")
        if g and not g.endswith("-" + MARQUE):
            groupes.setdefault(groupe_marque(g), []).append(x["id"])
        todo.append((f, x["id"]))
    for g, ids in groupes.items():
        _appel(b, k, "PUT", "/api/v3/episodefile/editor", {"episodeFileIds": ids, "releaseGroup": g})
    ids = [i for _, i in todo]
    for j in range(0, len(ids), 80):
        c = _appel(b, k, "POST", "/api/v3/command", {"name": "RenameFiles", "seriesId": serie["id"], "files": ids[j:j + 80]})
        if c and c.get("id") is not None:
            _attendre(b, k, c["id"], 600)
    apres = {x["id"]: x for x in _appel(b, k, "GET", f"/api/v3/episodefile?seriesId={serie['id']}") or []}
    return {f["path"]: (apres.get(i) or {}).get("path") for f, i in todo}
