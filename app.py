"""MouFlencoder : serveur web (même connexion que Mouflanimexer) et API."""
import hashlib, hmac, os, re, secrets, time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import emby_settings

BASE = Path(__file__).resolve().parent
emby_settings.load_env(BASE / "data" / "secrets.env")   # identifiant, clés : fichier en droits 600, comme MouFlip

import auth
import core

app = Flask(__name__, static_folder=None)
VERSION = (BASE / "VERSION").read_text().strip() if (BASE / "VERSION").exists() else ""
auth.init_app(app, VERSION)
emby_settings.init_app(app, BASE, lambda: core.get("emby_host"), lambda h: core.put("emby_host", h), on_change=core.refresh_library_async)


def hook_ok(given):
    h = os.getenv("HOOK_SECRET_HASH", "")
    return bool(h and given and hmac.compare_digest(hashlib.sha256(given.encode()).hexdigest(), h))


@app.get("/api/settings/hook")
def hook_state():
    return jsonify(configured=bool(os.getenv("HOOK_SECRET_HASH")))


@app.post("/api/settings/hook/regenerate")
def hook_regenerate():
    """Génère un nouveau secret pour Home Assistant, affiché UNE SEULE fois ; seul son hachage est conservé."""
    sec = secrets.token_urlsafe(24)
    h = hashlib.sha256(sec.encode()).hexdigest()
    emby_settings.write_secret(BASE / "data" / "secrets.env", "HOOK_SECRET_HASH", h)
    os.environ["HOOK_SECRET_HASH"] = h
    return jsonify(secret=sec)


@app.route("/")
def index():
    return send_from_directory(BASE / "static", "index.html", max_age=0)


@app.route("/static/<path:name>")
def static_files(name):
    return send_from_directory(BASE / "static", name, max_age=60)


@app.get("/api/state")
def api_state():
    return jsonify(dict(core.system_state(), version=VERSION))


@app.get("/api/stats")
def api_stats():
    return jsonify(core.stats())


@app.get("/api/library")
def api_library():
    cat = request.args.get("cat", "anime")
    if cat not in ("anime", "series", "films"):
        return jsonify(error="catégorie inconnue"), 400
    lib = core.load_library()
    if not lib["items"] and not core.STATE["lib_refreshing"]:
        core.refresh_library_async()
    return jsonify(core.library_for(cat))


@app.post("/api/library/refresh")
def api_library_refresh():
    core.refresh_library_async()
    return jsonify(ok=True)


@app.get("/api/pauses")
def api_pauses():
    return jsonify(core.pauses_info())


@app.get("/api/media")
def api_media():
    """Médiathèque classée par taille réelle des fichiers (aucune estimation) : un résumé par série."""
    out = []
    for cat in ("anime", "series", "films"):
        for s in core.library_for(cat)["series"]:
            eps = [e for se in s["seasons"] for e in se["eps"]]
            if not eps:
                continue
            tot = sum(e["sz"] for e in eps)
            todo = [e for e in eps if not e["why"]]
            res = {}
            for e in eps:
                if e["r"]:
                    res[e["r"]] = res.get(e["r"], 0) + 1
            out.append(dict(n=s["n"], c=cat, files=len(eps), total=tot, avg=round(tot / len(eps)), todo=sum(e["sz"] for e in todo), todo_n=len(todo), rx_n=sum(1 for e in eps if e.get("rx")),
                            hx_n=sum(1 for e in eps if e.get("hx")), ha_n=sum(1 for e in eps if e.get("ha")),
                            res=res, done=sum(1 for e in eps if e["why"] == "Traité" and e.get("st") != "REMUXE")))   # done : encodés ; l'harmonisation seule se lit dans ha_n
    return jsonify(series=out, ts=core.load_library()["ts"])


@app.get("/api/media/series")
def api_media_series():
    cat, name = request.args.get("cat", ""), request.args.get("name", "")
    for s in core.library_for(cat)["series"]:
        if s["n"] == name:
            return jsonify(seasons=[dict(s=se["s"], eps=[dict(e=e["e"], sz=e["sz"], p=e["p"], why=e["why"], hev=e["hev"], tm=e["tm"], r=e["r"], dm=e["dm"], rx=e.get("rx", False), hx=e.get("hx", False), ha=e.get("ha", False), st=e.get("st", "")) for e in se["eps"]]) for se in s["seasons"]])
    return jsonify(seasons=[])


@app.get("/api/remux/saisons")
def api_remux_saisons():
    """Épisodes non harmonisés dans les saisons où d'autres épisodes sont déjà traités (résumé, rien n'est mis en file)."""
    d = core.remux_saisons_traitees()
    d.pop("paths")
    d["actif"] = bool(core.get("unify_remux"))
    return jsonify(d)


@app.post("/api/remux/saisons")
def api_remux_saisons_go():
    d = request.get_json(force=True)
    if d.get("confirm") is not True:
        return jsonify(error="confirmation requise"), 400
    if not core.get("unify_remux"):
        return jsonify(error="l'option « Harmoniser les pistes » est désactivée dans Réglages"), 400
    serie = str(d.get("serie") or "")[:200]
    r = core.remux_saisons_traitees(serie or None)
    return jsonify(added=core.enqueue_remux(r["paths"], series=serie or "saisons déjà traitées"))


@app.get("/api/media/files")
def api_media_files():
    """Tous les fichiers, du plus gros au plus petit."""
    cat, q = request.args.get("cat", ""), request.args.get("q", "").lower()
    lim = max(1, min(int(request.args.get("limit", 300)), 1000))
    items = [f for f in core.load_library()["items"] if (not cat or f["c"] == cat) and (not q or q in f["n"].lower() or q in os.path.basename(f["p"]).lower())]
    items.sort(key=lambda f: -f["sz"])
    shown = items[:lim]
    info = {}   # état et définition de chaque fichier affiché, tels que la médiathèque les calcule
    for c in {f["c"] for f in shown}:
        for s in core.library_for(c)["series"]:
            for se in s["seasons"]:
                for e in se["eps"]:
                    info[e["p"]] = (e["why"], e["r"], e["dm"], e.get("ha", False), e.get("st", ""))
    return jsonify(total=len(items), files=[dict(p=f["p"], n=f["n"], s=f["s"], e=f["e"], sz=f["sz"], c=f["c"], hev=f["hev"], tm=f["tm"],
                                                 why=info.get(f["p"], ("", "", "", False, ""))[0], r=info.get(f["p"], ("", "", "", False, ""))[1], dm=info.get(f["p"], ("", "", "", False, ""))[2],
                                                 ha=info.get(f["p"], ("", "", "", False, ""))[3], st=info.get(f["p"], ("", "", "", False, ""))[4]) for f in shown])


@app.get("/api/settings/telegram")
def api_telegram_get():
    import notify
    return jsonify(configured=notify.configured(), hint=notify.hint(), chat_id=notify.chat(), thread_id=notify.thread())


@app.post("/api/settings/telegram")
def api_telegram_set():
    import notify
    d = request.get_json(force=True)
    tok, chat, th = str(d.get("token", "")).strip(), str(d.get("chat_id", "")).strip(), str(d.get("thread_id", "")).strip()
    if tok and not notify.token_ok(tok):
        return jsonify(ok=False, error="Le jeton ressemble à 123456789:ABCdef… (donné par @BotFather)."), 400
    if chat and not notify.chat_ok(chat):
        return jsonify(ok=False, error="L'identifiant de conversation est un nombre (ex. 123456789, ou -100… pour un groupe)."), 400
    if th and not notify.thread_ok(th):
        return jsonify(ok=False, error="Le numéro du sujet est un nombre. Laissez vide si vous n'utilisez pas de sujets."), 400
    if d.get("action") == "test":
        ok, msg = notify.send("🔔 <b>MouFlencoder</b> : les notifications Telegram fonctionnent.", tok or None, chat or None, th)
        return jsonify(ok=ok, message="Message de test envoyé : regardez votre Telegram." if ok else "", error="" if ok else msg), (200 if ok else 400)
    f = BASE / "data" / "secrets.env"
    for name, val in (("TELEGRAM_BOT_TOKEN", tok), ("TELEGRAM_CHAT_ID", chat)):
        if val:
            emby_settings.write_secret(f, name, val)
            os.environ[name] = val
    emby_settings.write_secret(f, "TELEGRAM_THREAD_ID", th)
    os.environ["TELEGRAM_THREAD_ID"] = th
    return jsonify(ok=True, message="Enregistré. Les notifications sont actives tout de suite.")


@app.get("/api/settings/arr")
def api_arr_get():
    import renamer
    out = {}
    for n in ("sonarr", "radarr"):
        b, k = renamer.acces(n)
        out[n] = {"url": b, "configured": bool(b and k), "hint": ("…" + k[-4:]) if len(k) >= 8 else ""}
    out["rename_after"] = core.get("rename_after")
    return jsonify(out)


@app.post("/api/settings/arr")
def api_arr_set():
    import renamer
    d = request.get_json(force=True)
    n = d.get("which")
    if n not in ("sonarr", "radarr"):
        return jsonify(ok=False, error="service inconnu"), 400
    url, key = str(d.get("url", "")).strip().rstrip("/"), str(d.get("key", "")).strip()
    if url and not re.match(r"^https?://[\w\.\-:]+(/[\w\-]*)?$", url):
        return jsonify(ok=False, error="L'adresse doit ressembler à http://192.168.1.10:8989"), 400
    if key and not re.match(r"^[A-Za-z0-9]{16,64}$", key):
        return jsonify(ok=False, error="La clé API est une suite de 32 lettres et chiffres (Sonarr/Radarr : Réglages > Général)."), 400
    f = BASE / "data" / "secrets.env"
    if d.get("action") == "test":
        old = (os.environ.get(n.upper() + "_URL", ""), os.environ.get(n.upper() + "_KEY", ""))
        if url: os.environ[n.upper() + "_URL"] = url
        if key: os.environ[n.upper() + "_KEY"] = key
        try:
            ok, msg = renamer.etat(n)
        finally:
            os.environ[n.upper() + "_URL"], os.environ[n.upper() + "_KEY"] = old
        return jsonify(ok=ok, message=msg if ok else "", error="" if ok else msg), (200 if ok else 400)
    if url:
        emby_settings.write_secret(f, n.upper() + "_URL", url); os.environ[n.upper() + "_URL"] = url
    if key:
        emby_settings.write_secret(f, n.upper() + "_KEY", key); os.environ[n.upper() + "_KEY"] = key
    return jsonify(ok=True, message="Enregistré.")


@app.get("/api/queue")
def api_queue():
    return jsonify(core.queue_list())


@app.post("/api/queue/add")
def api_queue_add():
    d = request.get_json(force=True)
    paths = [p for p in d.get("paths", []) if isinstance(p, str) and p.startswith(core.NAS_ROOT)]
    n = core.enqueue(paths, kind=d.get("kind", "encode") if d.get("kind") in ("encode", "estimate") else "encode",
                     series=str(d.get("series", ""))[:200], cat=d.get("cat", ""), qp=d.get("qp"), min_gain=d.get("min_gain"), front=bool(d.get("front")))
    nrx = 0
    if d.get("kind", "encode") != "estimate" and core.get("unify_remux"):   # saison ajoutée : ses épisodes non réductibles passent par le remux seul
        demande = [p for p in d.get("remux", []) if isinstance(p, str) and p.startswith(core.NAS_ROOT)]
        ser = str(d.get("series", ""))[:200]
        nrx = core.enqueue_remux(list(dict.fromkeys(demande)), series=ser, tout=True)   # demande explicite (bouton Harmoniser) : épisodes encodables compris
        nrx += core.enqueue_remux(list(dict.fromkeys(core.remux_pour_saisons(paths))), series=ser)
    return jsonify(added=n, remux=nrx)


@app.post("/api/queue/remove")
def api_queue_remove():
    d = request.get_json(force=True)
    for rid in ([int(x) for x in d["ids"]] if d.get("ids") else [int(d.get("id", 0))]):
        core.sql("DELETE FROM queue WHERE id=? AND status='queued'", (rid,))
    return jsonify(ok=True)


@app.post("/api/queue/move")
def api_queue_move():
    d = request.get_json(force=True)
    rid = int(d.get("id", 0))
    rows = [r["id"] for r in core.sql("SELECT id FROM queue WHERE status='queued' ORDER BY prio DESC, id")]
    if d.get("dir") in ("top", "bottom"):
        ids = {int(x) for x in d.get("ids", [rid])}
        sel = [r for r in rows if r in ids]
        rest = [r for r in rows if r not in ids]
        rows = sel + rest if d["dir"] == "top" else rest + sel
        for pos, x in enumerate(rows):
            core.sql("UPDATE queue SET prio=? WHERE id=?", (len(rows) - pos, x))
        return jsonify(ok=True, moved=len(sel))
    if rid in rows:
        i = rows.index(rid)
        j = i + (-1 if d.get("dir") == "up" else 1)
        if 0 <= j < len(rows):
            rows[i], rows[j] = rows[j], rows[i]
            for pos, x in enumerate(rows):
                core.sql("UPDATE queue SET prio=? WHERE id=?", (len(rows) - pos, x))
    return jsonify(ok=True)


@app.post("/api/queue/kind")
def api_queue_kind():
    d = request.get_json(force=True)
    kind = d.get("kind")
    if kind not in ("encode", "estimate"):
        return jsonify(error="type inconnu"), 400
    ids = [int(x) for x in d.get("ids", [])] if d.get("ids") else [r["id"] for r in core.sql("SELECT id FROM queue WHERE status='queued'")]
    n = 0
    for rid in ids:
        r = core.sql("SELECT * FROM queue WHERE id=? AND status='queued'", (rid,), one=True)
        if not r or r["kind"] == kind:
            continue
        if core.sql("SELECT 1 FROM queue WHERE path=? AND kind=? AND status IN ('queued','running')", (r["path"], kind), one=True):
            core.sql("DELETE FROM queue WHERE id=?", (rid,))
        else:
            core.sql("UPDATE queue SET kind=? WHERE id=?", (kind, rid))
        n += 1
    return jsonify(changed=n)


@app.post("/api/queue/cancel")
def api_queue_cancel():
    core.STATE["cancel"] = True
    return jsonify(ok=True)


@app.post("/api/pause")
def api_pause():
    d = request.get_json(force=True)
    core.put("paused", bool(d.get("paused")))
    return jsonify(paused=core.get("paused"))


@app.get("/api/registry")
def api_registry():
    return jsonify(core.registry_list(request.args.get("statut", ""), request.args.get("q", ""), int(request.args.get("limit", 300))))


@app.get("/api/trash")
def api_trash():
    return jsonify(core.trash_list())


@app.post("/api/trash/restore")
def api_trash_restore():
    try:
        core.trash_restore(int(request.get_json(force=True).get("id", 0)))
        return jsonify(ok=True)
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)), 400


@app.post("/api/trash/delete")
def api_trash_delete():
    d = request.get_json(force=True)
    if d.get("confirm") is not True:
        return jsonify(error="confirmation requise"), 400
    try:
        core.trash_delete(int(d.get("id", 0)))
        return jsonify(ok=True)
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)), 400


@app.post("/api/trash/empty")
def api_trash_empty():
    d = request.get_json(force=True)
    if d.get("confirm") is not True:
        return jsonify(error="confirmation requise"), 400
    done, errs = 0, []
    for r in core.trash_list():
        try:
            core.trash_delete(r["id"]); done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(str(e)[:120])
    return jsonify(deleted=done, errors=errs[:5])


@app.get("/api/settings")
def api_settings_get():
    return jsonify({k: core.get(k) for k in core.DEFAULTS})


@app.post("/api/settings")
def api_settings_set():
    d = request.get_json(force=True)
    LIM = {"cap_anime": (100, 5000), "cap_series": (100, 20000), "cap_films": (100, 50000), "min_saved": (20, 2000), "min_pct": (0, 90), "est_min": (0, 90),
           "target_anime": (0, 3000), "target_series": (0, 5000), "target_films": (0, 30000), "temp_max": (60, 100), "temp_cible": (0, 95), "qp_max": (18, 30), "skip_x265_mo22": (0, 1500), "skip_video_pct": (0, 200),
           "min_age_min": (0, 1440), "tg_daily_hour": (0, 23), "tg_pause_min": (5, 1440)}
    for k, v in d.items():
        if k in core.DEFAULTS and type(v) in (int, bool, float, type(core.DEFAULTS[k])):
            if k in LIM and not isinstance(v, bool):
                v = max(LIM[k][0], min(LIM[k][1], v))   # valeurs hors limites ramenées dans la plage
            core.put(k, v)
    return jsonify({k: core.get(k) for k in core.DEFAULTS})


@app.post("/api/prefs")
def api_prefs():
    d = request.get_json(force=True)
    name = str(d.get("series", ""))[:200]
    qpi = max(1, min(5, int(d.get("qpi", 3))))
    mg = max(0, min(90, int(d.get("min_gain", core.get("min_gain")))))
    core.sql("INSERT INTO prefs(series,qpi,min_gain) VALUES(?,?,?) ON CONFLICT(series) DO UPDATE SET qpi=excluded.qpi,min_gain=excluded.min_gain", (name, qpi, mg))
    return jsonify(ok=True)


@app.post("/hook/tempo")
def hook_tempo():
    """Home Assistant envoie la couleur du jour et du lendemain. Authentification par secret partagé (en-tête X-MFE-Secret)."""
    if not hook_ok(request.headers.get("X-MFE-Secret", "")):
        return jsonify(error="non autorisé"), 401
    d = request.get_json(silent=True) or {}
    core.set_tempo(d.get("today"), d.get("tomorrow"))
    return jsonify(ok=True, tempo=core.tempo_now())


if __name__ == "__main__":
    core.start_worker()
    lib = core.load_library()
    if not lib["items"] or time.time() - lib["ts"] > 6 * 3600:
        core.refresh_library_async()
    app.run(host="0.0.0.0", port=8300, threaded=True)
