"""MouFlencoder : cœur (base de données, bibliothèque Emby, file de traitement, remplacement sûr, registre, règles Tempo)."""
import re
import json, os, re, shutil, signal, sqlite3, subprocess, threading, time, urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DB_PATH = DATA / "mouflencoder.db"
NAS_ROOT = os.path.join(os.environ.get("MFE_NAS_ROOT", "/mnt/media"), "")
MFE_DIR = Path(NAS_ROOT) / "MouFlencoder"
CORBEILLE = MFE_DIR / "Corbeille"
REG_TSV = MFE_DIR / "Registre" / "registre.tsv"
WORK = Path(os.environ.get("MFE_WORK", "/var/tmp/mouflencoder"))
NICE = ["nice", "-n", "19", "ionice", "-c3"]
QP_BASE = {"anime": 24, "series": 26, "films": 26}
DEFAULTS = {"cap_anime": 550, "enable_4k": False, "rename_after": False, "target_4k_mbps": 8, "tg_on": True, "tg_done": True, "tg_daily": True, "tg_errors": True, "tg_pause": True, "tg_milestone": True, "tg_daily_hour": 9, "tg_pause_min": 30, "min_saved": 150, "min_pct": 15, "cap_series": 900, "cap_films": 900, "min_gain": 40, "est_min": 30, "test_mode": True,
            "paused": False, "target_anime": 375, "target_series": 375, "target_films": 375, "qp_max": 26, "skip_x265_mo22": 700, "skip_video_pct": 128, "pause_on_transcode": True, "min_age_min": 15, "temp_max": 92, "temp_cible": 85,
            "emby_host": os.environ.get("MFE_EMBY_HOST", "http://localhost:8096"), "tempo_unknown_ok": False, "unify_remux": True}
_lock = threading.RLock()
_db = None
STATE = {"job": None, "step": "", "pct": 0, "fps": 0, "eta": None, "msg": "", "blocked": "", "cancel": False, "lib_refreshing": False}


def qps_for(cat):
    b = QP_BASE.get(cat, 26)
    return [b - 6, b - 3, b, b + 3, b + 6]


# ------------------------------------------------------------------ base de données
def db():
    global _db
    with _lock:
        if _db is None:
            DATA.mkdir(exist_ok=True)
            _db = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
            _db.row_factory = sqlite3.Row
            _db.executescript("""
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS queue(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, path TEXT, cat TEXT, series TEXT, qp INTEGER, min_gain INTEGER,
  status TEXT, added REAL, started REAL, finished REAL, detail TEXT, prio INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS registry(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, statut TEXT, profil TEXT, avant_mo INTEGER, apres_mo INTEGER,
  gain INTEGER, path TEXT, detail TEXT, corbeille TEXT, purged INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS estimates(path TEXT PRIMARY KEY, size INTEGER, gains TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS prefs(series TEXT PRIMARY KEY, qpi INTEGER, min_gain INTEGER);
CREATE TABLE IF NOT EXISTS pauses(id INTEGER PRIMARY KEY AUTOINCREMENT, start REAL, end REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS audio_cache(path TEXT PRIMARY KEY, size INTEGER, aud REAL);
CREATE TABLE IF NOT EXISTS notif_pending(path TEXT PRIMARY KEY, cat TEXT, actuel TEXT, discret INTEGER, ts REAL);
""")
            try:
                _db.execute("ALTER TABLE registry ADD COLUMN orig_path TEXT")  # nom d'origine avant le renommage Sonarr/Radarr
            except sqlite3.OperationalError:
                pass
            try:
                _db.execute("ALTER TABLE queue ADD COLUMN src TEXT")  # source alternative (mode « refaire depuis la Corbeille »)
            except sqlite3.OperationalError:
                pass
            _db.commit()
        return _db


def sql(query, args=(), one=False):
    with _lock:
        cur = db().execute(query, args)
        db().commit()
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows


def get(k):
    r = sql("SELECT v FROM settings WHERE k=?", (k,), one=True)
    return json.loads(r["v"]) if r else DEFAULTS.get(k)


def put(k, v):
    sql("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


def pause_open(reason):
    """Journal des pauses automatiques (chaleur, Emby, jour rouge...) : mesure le temps réellement perdu."""
    if STATE.get("pause_id"):
        return
    with _lock:
        cur = db().execute("INSERT INTO pauses(start,reason) VALUES(?,?)", (time.time(), reason))
        db().commit()
        STATE["pause_id"] = cur.lastrowid


def pause_close():
    pid = STATE.get("pause_id")
    if pid:
        sql("UPDATE pauses SET end=? WHERE id=?", (time.time(), pid))
        STATE["pause_id"] = None


def pauses_info():
    now = time.time()
    day0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = sql("SELECT * FROM pauses ORDER BY id DESC LIMIT 15")
    today = sql("SELECT start,end FROM pauses WHERE start>=?", (day0,))
    return {"recent": [{"start": r["start"], "end": r["end"], "reason": r["reason"], "ongoing": r["end"] is None} for r in rows],
            "today_n": len(today), "today_s": int(sum((r["end"] or now) - r["start"] for r in today))}


def reg(statut, profil, avant, apres, gain, path, detail="", corbeille=""):
    if statut == "REMPLACE":  # un fichier refait remplace l'ancienne ligne : les statistiques ne comptent jamais deux fois
        sql("UPDATE registry SET statut='REMPLACE_ANCIEN' WHERE path=? AND statut='REMPLACE'", (path,))
    sql("INSERT INTO registry(ts,statut,profil,avant_mo,apres_mo,gain,path,detail,corbeille) VALUES(?,?,?,?,?,?,?,?,?)",
        (time.time(), statut, profil, avant, apres, gain, path, detail, corbeille))
    try:
        REG_TSV.parent.mkdir(parents=True, exist_ok=True)
        new = not REG_TSV.exists()
        with open(REG_TSV, "a", encoding="utf-8") as f:
            if new:
                f.write("date\tstatut\tprofil\tavant_Mo\tapres_Mo\tgain_pct\tchemin\tdetail\n")
            f.write("\t".join([datetime.now().isoformat(timespec="seconds"), statut, profil, str(avant), str(apres), str(gain), path, detail]) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ Emby
def emby_cfg():
    key = os.getenv("EMBY_API_KEY", "").strip()
    if not key:
        return None
    return str(get("emby_host")).rstrip("/"), key


def emby_get(path, timeout=90):
    c = emby_cfg()
    if not c:
        raise RuntimeError("Emby non configuré (emby_config.json)")
    req = urllib.request.Request(c[0] + path, headers={"X-Emby-Token": c[1]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def emby_status():
    try:
        playing = trans = 0
        for s in emby_get("/Sessions", 8):
            if s.get("NowPlayingItem"):
                playing += 1
                t = s.get("TranscodingInfo")
                if t and not t.get("IsVideoDirect", False):
                    trans += 1
        return {"ok": True, "playing": playing, "transcoding": trans}
    except Exception:
        return {"ok": False, "playing": 0, "transcoding": 0}


TEAM_RE = re.compile(r"\[([^\]]+)\](?:\.[A-Za-z0-9]{2,4})$")


def cat_of(p):
    if "/Manga/" in p:
        return "anime"
    if "/Series/" in p:
        return "series"
    if "/Films" in p:
        return "films"
    return ""


def build_library():
    STATE["lib_refreshing"] = True
    try:
        out, start, total = [], 0, None
        while total is None or start < total:
            d = emby_get(f"/Items?Recursive=true&IncludeItemTypes=Episode,Movie&Fields=Path,MediaSources,RunTimeTicks&StartIndex={start}&Limit=4000", 240)
            total = d.get("TotalRecordCount", 0)
            if not d.get("Items"):
                break
            for it in d["Items"]:
                p = it.get("Path") or ""
                c = cat_of(p)
                if not c or not it.get("MediaSources"):
                    continue
                ms = it["MediaSources"][0]
                v = next((s for s in ms.get("MediaStreams", []) if s.get("Type") == "Video"), {})
                m = TEAM_RE.search(os.path.basename(p))
                out.append({"p": p, "c": c, "n": (it.get("SeriesName") if it.get("Type") == "Episode" else it.get("Name")) or it.get("Name") or "?",
                            "s": it.get("ParentIndexNumber") or 1, "e": it.get("IndexNumber") or 1, "sz": round(ms.get("Size", 0) / 1048576),
                            "dur": round((it.get("RunTimeTicks") or 0) / 6e8, 1), "hev": (v.get("Codec") or "").lower() in ("hevc", "h265"), "h": v.get("Height") or 0, "w": v.get("Width") or 0,
                            "vr": v.get("VideoRangeType") or v.get("VideoRange") or "SDR", "tm": m.group(1) if m else ""})
            start += len(d["Items"])
        (DATA / "library.json").write_text(json.dumps({"ts": time.time(), "items": out}), encoding="utf-8")
    finally:
        STATE["lib_refreshing"] = False


def load_library():
    try:
        return json.loads((DATA / "library.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ts": 0, "items": []}


def refresh_library_async():
    if STATE["lib_refreshing"]:
        return
    threading.Thread(target=lambda: _safe(build_library), daemon=True).start()


def _safe(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        STATE["msg"] = f"erreur bibliothèque : {e}"
        STATE["lib_refreshing"] = False


def res_label(w, h):
    """Définition lisible (720p, 1080p, 4K) : on retient la plus haute des deux mesures, car la hauteur seule
    est trompeuse (un film 4K en format scope fait 1600 px de haut, un 1080p recadré 800 px)."""
    w, h = int(w or 0), int(h or 0)
    if not w and not h:
        return ""
    rk = lambda x, tiers: sum(1 for t in tiers if x >= t)   # noqa: E731
    r = max(rk(w, (1100, 1700, 2300, 3000)), rk(h, (700, 1000, 1300, 2000)))
    return ("SD", "720p", "1080p", "1440p", "4K")[r]


def why_not(f, procs, queued, ests):
    """Raison pour laquelle un fichier n'est pas cochable (chaîne vide = traitable)."""
    p = f["p"]
    if p in queued:
        return "Dans la file"
    pr = procs.get(p)
    if pr:
        if pr["statut"] in ("REMPLACE", "REMUXE") and abs(pr["apres_mo"] - f["sz"]) <= 2:
            return "Traité"
        if pr["statut"] in ("REJETE_GAIN", "IGNORE_ESTIMATION") and pr["avant_mo"] == f["sz"]:
            return "Déjà évalué"
    vr = (f["vr"] or "SDR").upper()
    if vr.startswith("DOVI"):
        return "Dolby Vision"
    if vr not in ("SDR", ""):
        return "HDR10 · à tester" if vr.startswith("HDR10") else "HDR"
    if f["h"] > 1100:
        return "Définition > 1080p"
    cap = get("cap_series" if f["c"] == "films" else "cap_" + f["c"])   # un seul plafond pour séries et films (le curseur de la page)
    if f["c"] == "anime":  # le plafond des animes vaut pour 22 min : proportionnel à la durée
        cap *= max(0.3, (f.get("dur") or 22) / 22)
    if f["sz"] <= cap:
        return "Sous le plafond"
    e = ests.get(p)
    if e and e["size_mo"] == f["sz"]:
        g = e["gains"].get(str(QP_BASE[f["c"]]))
        if g is not None and g < get("est_min") / 100:
            return "Peu de gain"
    return ""


def library_for(cat):
    lib = load_library()
    procs = {}
    for r in sql("SELECT path,statut,avant_mo,apres_mo FROM registry WHERE statut IN ('REMPLACE','REMUXE','REJETE_GAIN','IGNORE_ESTIMATION') ORDER BY id"):
        procs[r["path"]] = dict(r)
    queued = {r["path"] for r in sql("SELECT path FROM queue WHERE status IN ('queued','running')")}
    ests = {}
    for r in sql("SELECT path,size,gains FROM estimates"):
        ests[r["path"]] = {"size_mo": round(r["size"] / 1048576), "gains": json.loads(r["gains"])}
    prefs = {r["series"]: dict(r) for r in sql("SELECT * FROM prefs")}
    series = {}
    suivis = set(procs) | set(ests)
    unify = bool(get("unify_remux"))
    for f in lib["items"]:
        if f["c"] != cat:
            continue
        if f["p"] in suivis:  # taille réelle sur le disque : Emby garde l'ancienne tant qu'il n'a pas rescanné
            try:
                f = dict(f, sz=round(os.path.getsize(f["p"]) / 1048576))
            except OSError:
                pass
        s = series.setdefault(f["n"], {"n": f["n"], "c": cat, "seasons": {}})
        e = ests.get(f["p"])
        g = e["gains"] if e and e["size_mo"] == f["sz"] else None
        s["seasons"].setdefault(f["s"], []).append({"e": f["e"], "sz": f["sz"], "tm": f["tm"], "hev": f["hev"], "p": f["p"], "r": res_label(f.get("w"), f["h"]),
                                                    "dm": f"{f['w']}×{f['h']}" if f.get("w") else f"{f['h']} px de haut",
                                                    "why": why_not(f, procs, queued, ests), "g": g})
    out = []
    for n, s in series.items():
        acc = {}
        for eps in s["seasons"].values():
            for x in eps:
                if x["g"]:
                    for qk, gv in x["g"].items():
                        a = acc.setdefault(x["tm"], {}).setdefault(qk, [])
                        a.append(gv)
        for eps in s["seasons"].values():
            for x in eps:
                x["gx"] = False
                x["rx"] = unify and cat != "anime" and x["why"] in REMUX_OK and "MouFlo" not in os.path.basename(x["p"])
                # hx : bouton « Harmoniser » de la médiathèque, aussi pour les épisodes encore encodables (on peut vouloir les harmoniser sans les réencoder)
                x["hx"] = unify and cat != "anime" and x["why"] in (*REMUX_OK, "") and "MouFlo" not in os.path.basename(x["p"])
                # ha : pistes déjà harmonisées (encodé ou remuxé par MouFlencoder, ou marqueur MouFlo dans le nom) ; st : REMPLACE (encodé) ou REMUXE (pistes seules)
                x["ha"] = cat != "anime" and (x["why"] == "Traité" or "MouFlo" in os.path.basename(x["p"]))
                x["st"] = procs[x["p"]]["statut"] if x["why"] == "Traité" and x["p"] in procs else ""
                if x["g"] is None and not x["why"] and x["tm"] in acc:
                    x["g"] = {qk: round(sum(v) / len(v), 3) for qk, v in acc[x["tm"]].items()}
                    x["gx"] = True
        seasons = [{"s": k, "eps": sorted(v, key=lambda x: x["e"])} for k, v in sorted(s["seasons"].items())]
        pf = prefs.get(n)
        out.append({"n": n, "c": cat, "seasons": seasons, "qpi": pf["qpi"] if pf else 3, "min": pf["min_gain"] if pf else get("min_gain"),
                    "qps": qps_for(cat)})
    return {"ts": lib["ts"], "refreshing": STATE["lib_refreshing"], "series": out}


# ------------------------------------------------------------------ état du système
def read_temp():
    """Température du processeur (sonde coretemp, 'Package id 0' sinon maximum des cœurs)."""
    best, pkg = None, None
    try:
        for hw in Path("/sys/class/hwmon").glob("hwmon*"):
            if (hw / "name").read_text().strip() != "coretemp":
                continue
            for t in hw.glob("temp*_input"):
                v = int(t.read_text().strip()) / 1000
                lab = (t.with_name(t.name.replace("_input", "_label")))
                if lab.exists() and lab.read_text().strip().startswith("Package"):
                    pkg = v
                best = v if best is None or v > best else best
    except (OSError, ValueError):
        pass
    return pkg if pkg is not None else best


_HOT = {"v": False}
_THR = {"d": 1.0}  # fraction du temps où ffmpeg travaille (1 = pleine vitesse)


def hot():
    """Arrêt à temp_max, reprise 8 °C plus bas (évite les arrêts et reprises en rafale)."""
    t, lim = read_temp(), get("temp_max")
    if t is None:
        _HOT["v"] = False
    elif _HOT["v"]:
        if t <= lim - 8:
            _HOT["v"] = False
    elif t >= lim:
        _HOT["v"] = True
    return f"processeur trop chaud ({t:.0f} °C, reprise sous {lim - 8} °C)" if _HOT["v"] else ""


def tempo_day(now=None):
    now = now or datetime.now()
    return (now - timedelta(hours=6)).date().isoformat()


def tempo_now():
    t = get("tempo") or {}
    d = tempo_day()
    tm = (datetime.now() - timedelta(hours=6) + timedelta(days=1)).date().isoformat()
    return t.get(d), t.get(tm)


def put_tempo(date, color):
    t = get("tempo") or {}
    t[date] = color
    keep = {k: v for k, v in t.items() if k >= (datetime.now() - timedelta(days=5)).date().isoformat()}
    put("tempo", keep)
    put("tempo_ts", time.time())


def tempo_poll_once():
    """Relève la couleur d'aujourd'hui et de demain sur une source publique (api-couleur-tempo.fr, relais de RTE). Aucune configuration."""
    cols = {1: "bleu", 2: "blanc", 3: "rouge"}
    got = 0
    for ep in ("today", "tomorrow"):
        try:
            req = urllib.request.Request("https://www.api-couleur-tempo.fr/api/jourTempo/" + ep, headers={"User-Agent": "MouFlencoder"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())
            c = cols.get(int(d.get("codeJour", 0)))
            if c and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d.get("dateJour", ""))):
                put_tempo(d["dateJour"], c)
                got += 1
        except Exception:  # noqa: BLE001
            pass
    if got:
        put("tempo_src", "api-couleur-tempo.fr")
    return got


def tempo_loop():
    while True:
        try:
            tempo_poll_once()
        except Exception:  # noqa: BLE001
            pass
        h = datetime.now().hour
        time.sleep(300 if 8 <= h < 11 else 1800)   # RTE annonce la couleur du lendemain à 10 h 30


def set_tempo(today, tomorrow):
    norm = {"blue": "bleu", "bleu": "bleu", "white": "blanc", "blanc": "blanc", "red": "rouge", "rouge": "rouge"}
    t = get("tempo") or {}
    d = datetime.now() - timedelta(hours=6)
    for off, val in ((0, today), (1, tomorrow)):
        c = norm.get(str(val or "").strip().lower())
        if c:
            t[(d + timedelta(days=off)).date().isoformat()] = c
    keep = {k: v for k, v in t.items() if k >= (d - timedelta(days=3)).date().isoformat()}
    put("tempo", keep)
    put("tempo_ts", time.time())


def hard_block():
    """Raisons qui arrêtent même un fichier en cours (jamais l'arrêt manuel : lui laisse finir le fichier)."""
    col, _ = tempo_now()
    if col == "rouge":
        return "jour Tempo rouge"
    if col is None and not get("tempo_unknown_ok"):
        return "couleur Tempo inconnue (source injoignable ou pas encore annoncée)"
    if get("pause_on_transcode") and emby_status()["transcoding"]:
        return "Emby transcode la vidéo"
    return hot()


def allowed_now():
    if get("paused"):
        return False, "en pause (manuel)"
    hb = hard_block()
    if hb:
        return False, hb
    if get("test_mode"):
        return True, ""
    col, _ = tempo_now()
    h = datetime.now().hour
    night = h >= 22 or h < 6
    pending = sql("SELECT COUNT(*) c FROM queue WHERE status='queued' AND kind='encode'", one=True)["c"]
    if col == "blanc" and not night:
        return False, "jour blanc : de nuit seulement"
    if col == "bleu" and not night and pending < 10:
        return False, "jour bleu : de nuit en priorité"
    return True, ""


# ------------------------------------------------------------------ file
def enqueue(paths, kind="encode", series="", cat="", qp=None, min_gain=None, front=False, src=None):
    n = 0
    for p in paths:
        if sql("SELECT 1 FROM queue WHERE path=? AND kind=? AND status IN ('queued','running')", (p, kind), one=True):
            continue
        c = cat or cat_of(p)
        sql("INSERT INTO queue(kind,path,cat,series,qp,min_gain,status,added,src) VALUES(?,?,?,?,?,?,?,?,?)",
            (kind, p, c, series, qp or QP_BASE.get(c, 26), min_gain if min_gain is not None else get("min_gain"), "queued", time.time(), src))
        if front:
            sql("UPDATE queue SET prio=(SELECT COALESCE(MAX(prio),0)+1 FROM queue) WHERE id=(SELECT MAX(id) FROM queue)")
        n += 1
    return n


def queue_list():
    rows = sql("SELECT * FROM queue WHERE status IN ('queued','running') ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, prio DESC, id")
    return [dict(r, name=os.path.basename(r["path"])) for r in rows]


# ------------------------------------------------------------------ traitement d'un fichier
def ffprobe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], capture_output=True, text=True)
    return json.loads(r.stdout or "{}")


def check_file(path, cat):
    p = Path(path)
    if not p.exists():
        return "fichier introuvable"
    age = (time.time() - p.stat().st_mtime) / 60
    if age < get("min_age_min"):
        return f"modifié depuis moins de {get('min_age_min')} min"
    pr = ffprobe(p)
    v = next((s for s in pr.get("streams", []) if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    if not v:
        return "aucune piste vidéo"
    if v.get("color_transfer") in ("smpte2084", "arib-std-b67") or "side_data_list" in v:
        return "HDR / Dolby Vision non géré"
    if int(v.get("height", 0)) > 1100:
        return "définition supérieure à 1080p non gérée"
    if v.get("field_order") not in (None, "progressive", "unknown"):
        return "vidéo entrelacée"
    return ""


def run_estimate(job, qps=None):
    STATE.update(step="Estimation", pct=0, msg="")
    qps = qps or qps_for(job["cat"])
    r = subprocess.run([str(BASE / "estimate.py"), job["path"], ",".join(str(q) for q in qps)], capture_output=True, text=True)
    d = json.loads((r.stdout.strip().splitlines() or ["{}"])[-1])
    if "gains" not in d:
        raise RuntimeError(d.get("error", "estimation impossible"))
    size = Path(job["path"]).stat().st_size
    old = sql("SELECT size,gains FROM estimates WHERE path=?", (job["path"],), one=True)
    gains = dict(json.loads(old["gains"])) if old and old["size"] == size else {}
    gains.update(d["gains"])
    sql("INSERT INTO estimates(path,size,gains,ts) VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size,gains=excluded.gains,ts=excluded.ts",
        (job["path"], size, json.dumps(gains), time.time()))
    return gains


def hw_decodable(codec, pix_fmt):
    """Le GPU Intel sait décoder ces formats (H.264 8 bits, HEVC 8 et 10 bits) : le processeur chauffe 12 à 18 fois moins."""
    return (codec == "h264" and pix_fmt == "yuv420p") or (codec == "hevc" and pix_fmt in ("yuv420p", "yuv420p10le"))


def encode(job, src, out, dur, hw=False, fps=24.0, reinit=True, mixte=False):
    """Encode la vidéo avec le GPU. hw=True : le GPU décode aussi (sinon le processeur décode).
    mixte=True : le GPU décode mais les images repassent par la mémoire (comme avec le décodage processeur), ce qui permet -reinit_filter 0."""
    if hw:
        pre = ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128", "-hwaccel_output_format", "vaapi"]
        vf = "scale_vaapi=format=p010"
    elif mixte:
        pre = ["-init_hw_device", "vaapi=va:/dev/dri/renderD128", "-filter_hw_device", "va", "-hwaccel", "vaapi", "-hwaccel_device", "va"]
        vf = "format=p010,hwupload"
    else:
        pre = ["-init_hw_device", "vaapi=va:/dev/dri/renderD128", "-filter_hw_device", "va"]
        vf = "format=p010,hwupload"
    if not reinit:  # source dont la colorimétrie change en cours de flux : ffmpeg ne doit pas reconstruire sa chaîne de filtres
        pre = pre + ["-reinit_filter", "0"]
    cmd = NICE + ["ffmpeg", "-hide_banner", "-v", "error", "-nostats", "-progress", "pipe:1", "-y"] + pre + [
                  "-i", str(src), "-map", "0", "-map", "-0:d"] + [x for i in job.get("skip", []) for x in ("-map", "-0:%d" % i)] + ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy",
                  "-c:v:0", "hevc_vaapi", "-filter:v:0", vf, "-low_power", "1", "-profile:v:0", "main10", "-rc_mode", "CQP",
                  "-bf", "3", "-b_depth", "3", "-qp", str(job["qp"]), str(out)]
    errf = open(str(out) + ".err", "w")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True)
    stop = threading.Event()
    held = {"v": False, "gel": 0.0, "t": 0.0}

    def geler(raison):  # SIGSTOP : l'encodage s'arrête sur place et repartira exactement là où il s'est arrêté
        os.kill(p.pid, signal.SIGSTOP)
        held.update(v=True, t=time.time())
        STATE.update(blocked=raison, frozen=True, fps=0, eta=None)

    def degeler():
        os.kill(p.pid, signal.SIGCONT)
        held["gel"] += time.time() - held["t"]
        held["v"] = False
        STATE.update(blocked="", frozen=False)

    def guard():  # gel immédiat (SIGSTOP) si pause manuelle, Emby transcode, jour rouge ou trop chaud ; reprise automatique
        hb, n = "", 0
        while not stop.is_set():
            if STATE["cancel"]:  # annulation : ffmpeg doit tourner pour que la boucle de lecture la voie et l'arrête
                try:
                    if held["v"]:
                        degeler(); pause_close()
                except OSError:
                    pass
                break
            if n % 10 == 0:
                hb = hard_block()  # Emby, Tempo, température : toutes les 10 s ; la pause manuelle, elle, est lue chaque seconde
            n += 1
            raison = hb or ("en pause (manuel)" if get("paused") else "")
            try:
                if raison and not held["v"]:
                    geler(raison)
                    if hb:
                        pause_open(hb)
                elif raison:
                    STATE["blocked"] = raison
                    if hb:
                        pause_open(hb)
                    else:
                        pause_close()  # la pause manuelle n'est pas du temps perdu à mesurer
                elif held["v"]:
                    degeler(); pause_close()
            except OSError:  # ffmpeg vient de se terminer
                break
            stop.wait(1)
    threading.Thread(target=guard, daemon=True).start()

    def lisse():  # ralentissement : ffmpeg est suspendu dès que le processeur dépasse temp_cible et repart 3 °C plus bas (contrôle toutes les 0,5 s : la puce réagit en quelques secondes)
        arret, hist = False, []
        while not stop.is_set():
            t, cible = read_temp(), get("temp_cible")
            if held["v"]:
                arret = False
            elif not cible or t is None:
                if arret:
                    arret = False
                    try: os.kill(p.pid, signal.SIGCONT)
                    except OSError: break
            elif t > cible and not arret:
                try: os.kill(p.pid, signal.SIGSTOP)
                except OSError: break
                arret = True
            elif t <= cible - 3 and arret:
                try: os.kill(p.pid, signal.SIGCONT)
                except OSError: break
                arret = False
            hist = (hist + [0 if arret else 1])[-60:]
            STATE["throttle"] = round(100 * sum(hist) / len(hist))
            stop.wait(0.5)
        if not held["v"]:
            try: os.kill(p.pid, signal.SIGCONT)
            except OSError: pass
    threading.Thread(target=lisse, daemon=True).start()
    t0 = time.time()
    try:
        for line in p.stdout:
            if STATE["cancel"]:
                p.kill(); raise RuntimeError("annulé par l'utilisateur")
            k, _, v = line.strip().partition("=")
            if k == "frame" and v.isdigit() and dur and not STATE.get("_ot"):
                # avec le décodage GPU, ffmpeg renvoie N/A pour le temps écoulé : l'avancement se calcule sur les images
                n, total = int(v), max(1.0, dur * fps)
                STATE["pct"] = min(99, round(n / total * 100, 1))
                el = time.time() - t0 - held["gel"]  # sans le temps passé gelé
                STATE["eta"] = int(el / max(n, 1) * (total - n)) if n > 50 else None
            elif k == "out_time_us" and v.isdigit() and dur:
                STATE["_ot"] = True
                t = int(v) / 1e6
                STATE["pct"] = min(99, round(t / dur * 100, 1))
                el = time.time() - t0 - held["gel"]
                STATE["eta"] = int(el / max(t, 1) * (dur - t)) if t > 1 else None
            elif k == "fps":
                STATE["fps"] = float(v or 0)
        p.wait()
    finally:
        stop.set()
        if held["v"]:
            try: os.kill(p.pid, signal.SIGCONT)
            except OSError: pass
            pause_close()
        STATE["frozen"] = False
        errf.close()
    if p.returncode != 0 or not Path(out).exists():
        err = Path(str(out) + ".err").read_text(errors="replace")[-300:]
        raise RuntimeError("encodage ffmpeg : " + err)


def encode_cpu(job, src, out, dur, fps=24.0):
    """Décodage processeur. Si ffmpeg échoue en reconstruisant sa chaîne de filtres (colorimétrie qui change en cours de flux), un dernier essai la fige."""
    try:
        encode(job, src, out, dur, hw=False, fps=fps)
    except RuntimeError as e:
        if "annulé" in str(e) or "reinitializing filters" not in str(e):
            raise
        STATE["msg"] = "colorimétrie changeante dans la source : nouvel essai sans reconstruction des filtres"
        try: Path(out).unlink()
        except OSError: pass
        STATE.update(pct=0, eta=None)
        encode(job, src, out, dur, hw=False, fps=fps, reinit=False)


def fin_problematique(src):
    """Essai de contrôle sur les 40 dernières secondes : une colorimétrie qui change tout à la fin du fichier fait échouer le décodage GPU complet
    après 99 % du travail (Fire Country, 12 dernières secondes sans étiquette de couleur). Quelques secondes de GPU suffisent à le voir."""
    cmd = NICE + ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-sseof", "-40", "-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128",
                  "-hwaccel_output_format", "vaapi", "-i", str(src), "-map", "0:v:0", "-vf", "scale_vaapi=format=p010", "-c:v", "hevc_vaapi",
                  "-low_power", "1", "-profile:v", "main10", "-rc_mode", "CQP", "-qp", "30", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return "reinitializing filters" in (r.stderr or "")


def encode_auto(job, src, out, dur, fps, mode):
    """Essais successifs : « gpu » (GPU décode et encode), « mixte » (GPU, sans reconstruction des filtres : colorimétrie changeante), « cpu » (le processeur décode).
    Renvoie le mode qui a réussi : un deuxième encodage du même fichier (qp plus bas) repart de celui-là."""
    def propre():
        try: Path(out).unlink()
        except OSError: pass
        STATE.update(pct=0, eta=None)
    if mode == "gpu":
        try:
            encode(job, src, out, dur, hw=True, fps=fps)
            return "gpu"
        except RuntimeError as e:
            if "annulé" in str(e):
                raise
            propre()
            if "reinitializing filters" in str(e):
                mode = "mixte"
                STATE["msg"] = "colorimétrie changeante dans la source : décodage GPU sans reconstruction des filtres"
            else:
                mode = "cpu"
                STATE["msg"] = "décodage GPU refusé, reprise avec le décodage normal : " + str(e)[:120]
    if mode == "mixte":
        try:
            encode(job, src, out, dur, hw=False, fps=fps, reinit=False, mixte=True)
            return "mixte"
        except RuntimeError as e:
            if "annulé" in str(e):
                raise
            propre()
            STATE["msg"] = "décodage GPU refusé, reprise avec le décodage normal : " + str(e)[:120]
    encode_cpu(job, src, out, dur, fps=fps)
    return "cpu"


def replace_from_corbeille(orig, newfile):
    """Mode « refaire » : l'original reste dans la Corbeille. La version actuelle est mise de côté dans Remplaces, puis le nouveau fichier (créé par cp) la remplace d'un renommage."""
    dest = Path(orig).with_suffix(".mkv")
    if not str(dest).startswith(NAS_ROOT):
        raise RuntimeError("chemin hors du partage : " + str(dest))
    tmp = Path(str(dest) + ".mfe.tmp")
    side = MFE_DIR / "Remplaces" / str(dest)[len(NAS_ROOT):]
    side.parent.mkdir(parents=True, exist_ok=True)
    if side.exists():
        side = side.with_name(side.stem + ".%d.mkv" % time.time())
    size = Path(newfile).stat().st_size
    try:
        subprocess.run(["cp", str(newfile), str(tmp)], check=True)
        if tmp.stat().st_size != size:
            raise RuntimeError("copie incomplète à côté de la version actuelle")
        os.link(dest, side)
    except Exception:
        try: tmp.unlink()
        except OSError: pass
        raise
    try:
        os.rename(tmp, dest)
    except OSError as e:
        for f in (side, tmp):
            try: f.unlink()
            except OSError: pass
        raise RuntimeError("remplacement refusé par le NAS, version actuelle intacte : " + str(e))


def replace_original(orig, newfile):
    """Lien en dur de l'original dans la corbeille, puis le nouveau fichier (créé par cp) écrase l'original d'un renommage."""
    orig = Path(orig)
    if not str(orig).startswith(NAS_ROOT):
        raise RuntimeError("chemin hors du partage : " + str(orig))
    dest = orig.with_suffix(".mkv")
    tmp = Path(str(dest) + ".mfe.tmp")
    corb = CORBEILLE / str(orig)[len(NAS_ROOT):]
    corb.parent.mkdir(parents=True, exist_ok=True)
    garde = False  # True : la corbeille contient déjà une copie de cet original, on la garde telle quelle
    if corb.exists():
        if abs(corb.stat().st_size - orig.stat().st_size) <= orig.stat().st_size * 0.01:
            garde = True
        else:
            corb = corb.with_name(corb.stem + ".%d.mkv" % time.time())
    size = Path(newfile).stat().st_size
    try:
        subprocess.run(["cp", str(newfile), str(tmp)], check=True)
        if tmp.stat().st_size != size:
            raise RuntimeError("copie incomplète à côté de l'original")
        if not garde:
            os.link(orig, corb)
    except Exception:
        for f in (tmp,):
            try: f.unlink()
            except OSError: pass
        raise
    try:
        os.rename(tmp, dest)
    except OSError as e:
        for f in ((tmp,) if garde else (corb, tmp)):
            try: f.unlink()
            except OSError: pass
        raise RuntimeError("remplacement refusé par le NAS, original intact : " + str(e))
    if dest != orig:
        try: orig.unlink()
        except OSError: pass
    return str(corb)


AUDIO_REF = 50  # Mo d'audio et sous-titres pour 22 min déjà compris dans la taille visée (AAC/AC3 courants)


def nonvideo_mb(path):
    """Poids en Mo de tout ce qui n'est pas la vidéo (audio, sous-titres, polices) : fichier moins paquets vidéo, lus en 1 à 2 s."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=size", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    v = sum(int(x) for x in r.stdout.split() if x.isdigit()) / 1048576
    return max(0.0, Path(path).stat().st_size / 1048576 - v) if v > 0 else 0.0


def skip_reason(pr, avant, tgt, cat):
    """Raison de ne pas encoder du tout (sinon chaîne vide). Deux cas mesurés sur 1 312 résultats réels : source déjà en x265 et légère
    (taille pour 22 min sous skip_x265_mo22), ou vidéo seule déjà proche de la taille visée (audio lourd, ou source déjà très compressée).
    Le poids de l'audio vient des en-têtes du fichier (octets des pistes, sinon débit x durée) : instantané, aucune relecture du NAS."""
    try:
        dur = float(pr["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return ""
    if dur < 60 or not avant:
        return ""
    v = next((x for x in pr.get("streams", []) if x.get("codec_type") == "video" and not x.get("disposition", {}).get("attached_pic")), {})
    aud = 0.0
    for x in pr.get("streams", []):
        if x.get("codec_type") == "video":
            continue
        t = x.get("tags") or {}
        if str(t.get("NUMBER_OF_BYTES", "")).isdigit():
            aud += int(t["NUMBER_OF_BYTES"])
        elif str(t.get("BPS", "")).isdigit():
            aud += int(t["BPS"]) * dur / 8
        elif str(x.get("bit_rate", "")).isdigit():
            aud += int(x["bit_rate"]) * dur / 8
    aud /= 1048576
    k = 1320.0 / dur   # tout est ramené à 22 min
    tot22, vid22, aud22 = avant * k, max(0.0, avant - aud) * k, aud * k
    seuil = int(get("skip_x265_mo22") or 0)
    if seuil and v.get("codec_name") in ("hevc", "h265") and tot22 < seuil:
        return f"déjà en x265 et léger ({round(tot22)} Mo pour 22 min, seuil {seuil} Mo) : fichier non encodé"
    pct = int(get("skip_video_pct") or 0)
    cible_v = (tgt or 0) - (AUDIO_REF if cat == "anime" else 0)
    if pct and cible_v > 0 and vid22 < cible_v * pct / 100:
        return f"vidéo déjà proche de la taille visée ({round(vid22)} Mo de vidéo pour 22 min, audio et sous-titres {round(aud22)} Mo) : fichier non encodé"
    return ""


PENTE = 0.1465  # le logarithme de la taille varie de 0,1465 par point de qp (mesuré sur un Muhyo : qp16 = 691 Mo, qp24 = 214 Mo)


DUREE_LONGUE = 25 * 60  # au-delà de 25 min : pas de taille cible, qp 24 fixe
_DURS = {"mt": 0, "m": {}}
ALPHA = {"anime": 0.27, "series": 0.11, "films": 0.15}  # dépendance de la taille encodée à la taille de la source (mesurée sur le registre : 0,11 séries, 0,27 animes ; 1 = proportionnelle)


def _dur_min(*paths):
    f = DATA / "library.json"
    try:
        mt = f.stat().st_mtime
        if _DURS["mt"] != mt:
            _DURS["m"] = {i["p"]: i.get("dur") for i in json.loads(f.read_text(encoding="utf-8"))["items"]}
            _DURS["mt"] = mt
    except (OSError, ValueError, KeyError):
        pass
    return next((_DURS["m"][p] for p in paths if p and _DURS["m"].get(p)), None)


DUREE_REF = 22 * 60  # la taille cible des réglages vaut pour un épisode de 22 minutes ; elle est proportionnelle à la durée réelle


def _cle_ep(p):
    """Identité d'un épisode indépendante du nom de fichier (la team et le renommage changent le nom) : dossier + SxxEyy."""
    m = re.search(r"S\d{1,3}E\d{1,3}", os.path.basename(p), re.I)
    return (os.path.dirname(p), m.group(0).upper() if m else "")


def _team(nom):
    m = TEAM_RE.search(nom)
    return m.group(1).removesuffix("-MouFlo") if m else ""   # le renommage ajoute -MouFlo à la team d'origine


def _index_biblio():
    idx = {"p": {}, "k": {}}
    for f in load_library()["items"]:
        c = {"p": f["p"], "sz": f["sz"], "dur": f.get("dur")}
        idx["p"][f["p"]] = c
        idx["k"].setdefault(_cle_ep(f["p"]), []).append(c)
    return idx


def _fichier_du_registre(idx, attendu, *paths):
    """Fichier d'aujourd'hui qui correspond à une ligne du registre : le nom a pu changer (renommage Sonarr après un remux), donc même dossier et même
    épisode, à condition que la taille soit celle de la ligne (à 3 % près) : sinon c'est une autre version du fichier, on ne s'en sert pas."""
    tol = max(3.0, 0.03 * attendu)
    for p in paths:
        c = idx["p"].get(p) if p else None
        if c and abs(c["sz"] - attendu) <= tol:
            return c
    for p in paths:
        for c in (idx["k"].get(_cle_ep(p), ()) if p else ()):
            if abs(c["sz"] - attendu) <= tol:
                return c
    return None


def _audio_entetes(pr, dur):
    """Audio + sous-titres en Mo d'après les en-têtes du fichier (octets des pistes, sinon débit x durée) ; None si les en-têtes ne le disent pas."""
    aud, ok = 0.0, False
    for x in pr.get("streams", []):
        if x.get("codec_type") in ("video", "attachment"):
            continue
        t = x.get("tags") or {}
        if str(t.get("NUMBER_OF_BYTES", "")).isdigit():
            aud += int(t["NUMBER_OF_BYTES"]); ok = True
        elif str(t.get("BPS", "")).isdigit():
            aud += int(t["BPS"]) * dur / 8; ok = True
        elif str(x.get("bit_rate", "")).isdigit():
            aud += int(x["bit_rate"]) * dur / 8; ok = True
    return aud / 1048576 if ok else None


def audio_du_fichier(p, lire=True, sz_mo=None):
    """Poids en Mo de l'audio et des sous-titres de CE fichier (jamais une moyenne de série : la team, donc l'audio, change d'un épisode à l'autre).
    En cache tant que la taille du fichier ne change pas (sz_mo : taille déjà connue, évite d'interroger le NAS) ; lire=False : cache seul."""
    c = sql("SELECT size,aud FROM audio_cache WHERE path=?", (p,), one=True)
    if c and sz_mo is not None and abs(c["size"] / 1048576 - sz_mo) <= 4:   # un remux de pistes change la taille de moins de 2 Mo, jamais l'audio
        return c["aud"]
    try:
        sz = os.path.getsize(p)
    except OSError:
        return None
    if c and abs(c["size"] - sz) <= 4 * 1048576:
        return c["aud"]
    if not lire:
        return None
    pr = ffprobe(p)
    try:
        dur = float(pr["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return None
    aud = _audio_entetes(pr, dur)
    if aud is None:
        aud = nonvideo_mb(p)   # en-têtes muets : relecture complète du fichier, seulement pour ceux-là
    sql("INSERT INTO audio_cache(path,size,aud) VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size,aud=excluded.aud", (p, sz, aud))
    return aud


def qp_plafond():
    return max(18, min(28, int(get("qp_max") or 28)))   # plafond de qualité : jamais plus compressé que ce qp


def qp_plus_haut(qp, vid, tv, qmax):
    """Vidéo encodée nettement au-dessus de la taille visée : nouveau qp (plus haut) tiré de la taille réellement obtenue ; None = pas de nouvel essai."""
    import math
    if tv <= 10 or vid <= 1.25 * tv or qp >= qmax:
        return None
    nq = min(qmax, max(qp + 1, round(qp + math.log(vid / tv) / PENTE)))
    return nq if nq > qp else None


def choose_qp(job, path, tgt, dur=0, src=None):
    """Taille cible : part de ce que le même épisode, la même saison ou la même série a réellement donné au registre (vidéo seule, ramenée à qp24, avec l'audio
    propre à chaque fichier), puis règle le qp pour viser la cible."""
    import math
    lec = src or path  # fichier réellement lu (la copie de la Corbeille en mode « refaire »)
    avant = Path(lec).stat().st_size / 1048576
    if dur > 0:
        tgt = tgt * max(0.3, dur / DUREE_REF)
    STATE["tgt"] = round(tgt)
    sea = os.path.dirname(path) + "/"                 # dossier de la saison
    ser = os.path.dirname(os.path.dirname(path)) + "/"  # dossier de la série
    team = _team(os.path.basename(path))
    cle = _cle_ep(path)
    idx = _index_biblio()
    niv = {k: [] for k in ("fichier", "saison_team", "serie_team", "saison", "serie", "cat")}
    qrej = {r["path"]: r["qp"] for r in sql("SELECT path,qp FROM queue WHERE status='rejected' ORDER BY id")}  # qp essayé sur les fichiers refusés
    al = ALPHA.get(job["cat"], 0.15)
    dmin = dur / 60 if dur > 0 else 22.0
    for r in sql("SELECT path,orig_path,avant_mo,apres_mo,detail,profil,statut FROM registry WHERE statut IN ('REMPLACE','REJETE_GAIN','REMPLACE_ANCIEN') AND avant_mo>0 AND apres_mo>0"):
        # un refus garde l'original (taille avant) ; un remplacement donne le fichier d'aujourd'hui (taille après)
        cur = _fichier_du_registre(idx, r["avant_mo"] if r["statut"] == "REJETE_GAIN" else r["apres_mo"], r["path"], r["orig_path"])
        if not cur:
            continue   # fichier disparu, remplacé par une autre version ou introuvable : cette ligne ne dit plus rien du fichier
        dh = cur["dur"] or _dur_min(r["path"], r["orig_path"])
        if not dh or dh < 3:
            continue
        proche = cur["p"].startswith(sea) or cur["p"].startswith(ser)
        aud_h = audio_du_fichier(cur["p"], lire=proche, sz_mo=cur["sz"])   # l'audio de CE fichier ; hors de la série, seulement s'il est déjà en cache
        if aud_h is None:
            continue
        vs, vo = r["avant_mo"] - aud_h, r["apres_mo"] - aud_h   # vidéo seule avant et après
        if vs < 20 or vo < 5:
            continue
        m = re.search(r"qp (\d+)", r["detail"] or "")
        qp = int(m.group(1)) if m else (qrej.get(r["path"], 24) if r["statut"] == "REJETE_GAIN" else 24)
        v = (vo / dh) / math.exp(PENTE * (24 - qp)) / ((vs / dh) ** al)   # Mo de vidéo par minute à qp24, corrigé de la taille de la vidéo source
        rt = _team(os.path.basename(cur["p"]))
        mm = bool(team) and bool(rt) and rt == team
        if _cle_ep(cur["p"]) == cle:
            niv["fichier"].append(v)
        if cur["p"].startswith(sea):
            niv["saison"].append(v)
            if mm: niv["saison_team"].append(v)
        if cur["p"].startswith(ser):
            niv["serie"].append(v)
            if mm: niv["serie_team"].append(v)
        if r["profil"] == job["cat"]:
            niv["cat"].append(v)
    # du plus précis au plus général : même saison et même team, même série et même team, même saison, même série, catégorie
    l = next((sorted(niv[k]) for k in ("fichier", "saison_team", "serie_team", "saison", "serie") if len(niv[k]) >= (1 if k == "fichier" else 3)), sorted(niv["cat"]))
    med = l[len(l) // 2] if l else None
    # la taille visée vaut pour la vidéo : un audio lourd (DTS-HD...) s'ajoute à la cible au lieu de manger la qualité vidéo
    echelle = max(0.3, dur / DUREE_REF) if dur > 0 else 1
    aud = nonvideo_mb(lec)
    ref_aud = AUDIO_REF if job.get("cat") == "anime" else 0   # séries et films : la taille visée est celle de la vidéo seule, tout l'audio s'ajoute
    cible = tgt + max(0.0, aud - ref_aud * echelle)
    STATE["tgt"] = round(cible)
    hist = (aud + med * (max(avant - aud, 20.0) / dmin) ** al * dmin) if med else None   # audio de ce fichier + vidéo prévue à qp24
    repli = max(avant * 0.3, aud + 0.15 * (avant - aud))   # règle sans historique ; un audio très lourd n'écrase plus la vidéo prévue
    if hist is None or (hist - aud) < 0.5 * (repli - aud):
        # l'historique vient de fichiers à l'audio plus léger : une fois l'audio de celui-ci retiré, il ne reste presque rien pour la vidéo, d'où un qp 14 absurde (sortie plus grosse que l'original)
        s24 = repli
        job["_repli"] = True
    else:
        s24 = hist
    s24 = max(s24, aud + 5)  # taille totale prévue à qp 24
    qmax = qp_plafond()   # plafond de qualité : jamais plus compressé que ce qp, même si la taille visée n'est pas atteinte
    if cible - aud <= 5:
        brut = 99
    else:
        brut = round(24 - math.log((cible - aud) / (s24 - aud)) / PENTE)
    qp = max(14, min(qmax, brut))
    job["_plafond"] = brut > qmax
    if qp != job["qp"]:
        job["qp"] = qp
        sql("UPDATE queue SET qp=? WHERE id=?", (qp, job["id"]))
    return aud + (s24 - aud) * math.exp(PENTE * (24 - qp))  # taille totale prévue en Mo


def video_packets(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(p)], capture_output=True, text=True, timeout=900)
    try:
        return int(r.stdout.split()[0])
    except (ValueError, IndexError):
        return None


def emby_modifie(path):
    """Prévient Emby qu'un fichier a été modifié sur place : il ne voit pas seul les changements faits sur le NAS et garderait les anciennes pistes en mémoire."""
    import urllib.request
    host, cle = get("emby_host"), os.getenv("EMBY_API_KEY", "")
    if not (host and cle):
        return
    req = urllib.request.Request(host.rstrip("/") + "/Library/Media/Updated", data=json.dumps({"Updates": [{"Path": str(path), "UpdateType": "Modified"}]}).encode(), method="POST",
                                 headers={"X-Emby-Token": cle, "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def renommer_apres(path, cat, actuel=None, discret=False):
    """Le fichier est déjà remplacé et en sécurité. Le renommage (Sonarr/Radarr) et la notification à Emby ne partent PAS tout de suite : ils sont mis de côté puis envoyés
    GROUPÉS par flush_notifs (une relecture par série, une seule requête Emby), au plus toutes les 10 minutes ou quand la file est vide. Une harmonisation en lot ne fait
    que quelques secondes par fichier : un envoi par fichier saturait Sonarr (relecture de toute la série) et Emby (analyse de chaque fichier)."""
    sql("INSERT INTO notif_pending(path,cat,actuel,discret,ts) VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET cat=excluded.cat,actuel=excluded.actuel,discret=excluded.discret,ts=excluded.ts",
        (path, cat, actuel, 1 if discret else 0, time.time()))


_NOTIF = {"t": 0.0, "busy": False}


def flush_notifs(force=False):
    """Lance (en arrière-plan, sans bloquer le GPU) l'envoi groupé de ce qui a été mis de côté. Sans force : au plus une fois toutes les `notif_espace` secondes (600)."""
    if _NOTIF["busy"] or not sql("SELECT 1 FROM notif_pending LIMIT 1", one=True):
        return
    if not force and time.time() - _NOTIF["t"] < int(get("notif_espace") or 600):
        return
    _NOTIF["busy"] = True
    _NOTIF["t"] = time.time()
    threading.Thread(target=_flush_notifs, daemon=True).start()


def _flush_notifs():
    try:
        rows = [dict(r) for r in sql("SELECT path,cat,actuel,discret FROM notif_pending ORDER BY ts")]
        if not rows:
            return
        res = renamer.apres_lot([(r["actuel"] or r["path"], r["cat"]) for r in rows], get("emby_host"))
        change = False
        for r in rows:
            final, err = res.get(r["actuel"] or r["path"], (None, "pas de résultat"))
            if final is not None:
                if str(final) != str(r["path"]):
                    sql("UPDATE registry SET orig_path=COALESCE(orig_path,path), path=? WHERE path=? AND statut IN ('REMPLACE','REMUXE')", (str(final), r["path"]))
                    sql("UPDATE estimates SET path=? WHERE path=?", (str(final), r["path"]))
                    change = True
            elif not (r["discret"] and "introuvable dans Sonarr" in err):   # remux en série : une série hors Sonarr n'est pas une anomalie
                reg("ANOMALIE", r["cat"], 0, 0, 0, r["path"], "renommage non fait (le fichier est bien remplacé) : " + err[:200])
            sql("DELETE FROM notif_pending WHERE path=?", (r["path"],))
        if change and time.time() - STATE.get("_lib_ts", 0) > 600:   # la liste des médias est relue au plus toutes les 10 minutes
            STATE["_lib_ts"] = time.time()
            refresh_library_async()
    except Exception as e:  # noqa: BLE001
        STATE["msg"] = "envoi groupé à Sonarr/Emby en échec : " + str(e)[:120]   # les lignes restent en attente : nouvel essai au prochain envoi
    finally:
        _NOTIF["busy"] = False


REMUX_OK = ("Sous le plafond", "Déjà évalué", "Peu de gain", "HDR", "HDR10 · à tester", "Dolby Vision", "Définition > 1080p")   # états non cochables que le remux seul peut harmoniser (HDR, Dolby Vision et 4K compris : la vidéo n'est pas touchée)


def replace_in_place(orig, newfile):
    """Remux seul d'un fichier déjà en MKV : copie complète du nouveau fichier à côté, vérifiée en taille, puis renommée sur l'original (renommage atomique :
    l'original reste intact jusqu'au bout). Pas de Corbeille : le remux ne retire aucune piste, les sous-titres SRT d'origine restent dans le fichier."""
    orig = Path(orig)
    if not str(orig).startswith(NAS_ROOT):
        raise RuntimeError("chemin hors du partage : " + str(orig))
    tmp = Path(str(orig) + ".mfe.tmp")
    size = Path(newfile).stat().st_size
    try:
        subprocess.run(["cp", str(newfile), str(tmp)], check=True)
        if tmp.stat().st_size != size:
            raise RuntimeError("copie incomplète à côté de l'original")
        os.rename(tmp, orig)
    except Exception as e:  # noqa: BLE001
        try: tmp.unlink()
        except OSError: pass
        raise RuntimeError("remplacement refusé par le NAS, original intact : " + str(e))


def check_remux(path):
    """Garde-fous du remux seul : le fichier existe, n'est pas en cours d'écriture et a une vidéo. HDR, Dolby Vision, 4K et entrelacé sont acceptés : la vidéo n'est jamais réencodée."""
    p = Path(path)
    if not p.exists():
        return "fichier introuvable"
    if (time.time() - p.stat().st_mtime) / 60 < get("min_age_min"):
        return f"modifié depuis moins de {get('min_age_min')} min"
    pr = ffprobe(p)
    if not any(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic") for s in pr.get("streams", [])):
        return "aucune piste vidéo"
    return ""


def meta_video(path):
    """Étiquettes de couleur et données associées de la vidéo (HDR10, lumière, configuration Dolby Vision) : strictement les mêmes après un remux."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,profile,pix_fmt,width,height,color_range,color_space,color_transfer,color_primaries:stream_side_data", "-of", "json", str(path)],
                       capture_output=True, text=True)
    try:
        s = json.loads(r.stdout)["streams"][0]
    except (ValueError, KeyError, IndexError):
        return None
    d = {k: s.get(k) for k in ("codec_name", "profile", "pix_fmt", "width", "height", "color_range", "color_space", "color_transfer", "color_primaries")}
    d["side_data"] = s.get("side_data_list", [])
    return json.dumps(d, sort_keys=True)


def run_remux(job, motif=""):
    """Remux seul : la vidéo n'est pas réencodée. Audio français en premier, sous-titres normalisés, marqueur (media.py --post, comme après un encodage), puis remplacement.
    Fichier en MKV : remplacé sur place, sans Corbeille. Autre conteneur (TS, MP4, M4V, AVI) : converti en MKV, original gardé en Corbeille.
    Renvoie True si le fichier est traité ou déjà harmonisé."""
    path, cat = job["path"], job["cat"]
    if cat == "anime":
        raise RuntimeError("remux seul non prévu pour les animes (Mouflanimexer gère leurs pistes)")
    prof = cat if cat in ("series", "films") else "series"
    src = Path(path)
    why = check_remux(str(src))
    if why:
        if job.get("kind") == "remux":
            sql("UPDATE queue SET status='skipped', detail=?, finished=? WHERE id=?", (why, time.time(), job["id"]))
        return False
    avant = round(src.stat().st_size / 1048576)
    nonmkv = src.suffix.lower() != ".mkv"
    if src.stat().st_size * (2.2 if nonmkv else 1.15) + 3 * 2**30 > shutil.disk_usage(WORK.parent).free:
        raise RuntimeError("remux : pas assez de place dans /var/tmp pour ce fichier, laissé tel quel")
    wd = WORK / f"rx{job['id']}"
    shutil.rmtree(wd, ignore_errors=True)
    wd.mkdir(parents=True, exist_ok=True)
    try:
        STATE.update(step="Pistes, sous-titres et contrôles", pct=5, eta=None, fps=0, msg="remux seul, sans réencodage" + (f" ({motif})" if motif else ""))
        out = wd / "final.mkv"
        if nonmkv:   # changement de conteneur : une copie de flux ne perd rien, le nombre d'images vidéo doit rester strictement le même
            if src.stat().st_size > 8 * 2**30:
                raise RuntimeError("remux : fichier hors MKV trop gros pour être contrôlé, laissé tel quel")
            na = video_packets(src)
            if not na:
                raise RuntimeError("remux : images vidéo illisibles, laissé tel quel")
            # MP4/M4V : mkvmerge peut couper la fin de la vidéo (2 s sur un M4V de Lucifer) ; ffmpeg copie alors les flux vers un MKV intermédiaire, mkvmerge ne fait que le remux ensuite
            essais = ["ffmpeg"] if src.suffix.lower() in (".m4v", ".mp4", ".mov") else ["direct", "ffmpeg"]
        else:
            na, essais = 0, ["direct"]
        derniere = "échec"
        for mode in essais:
            entree = src
            if mode == "ffmpeg":
                entree = wd / "pre.mkv"
                rc = subprocess.run(NICE + ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(src), "-map", "0", "-map", "-0:d", "-map_metadata", "0", "-map_chapters", "0",
                                            "-c", "copy", "-c:s", "srt", str(entree)], capture_output=True, text=True)
                if rc.returncode != 0 or not entree.exists():
                    derniere = "copie ffmpeg vers MKV impossible : " + rc.stderr.strip()[-160:]
                    continue
            r = subprocess.run(NICE + [str(BASE / "media.py"), "--post", "series", str(entree), str(entree), str(out)], capture_output=True, text=True)
            txt = (r.stdout + r.stderr).strip()
            if r.returncode == 3:   # le marqueur est déjà posé : rien à faire
                reg("REMUXE", prof, avant, avant, 0, path, "pistes déjà harmonisées (marqueur présent), rien à faire")
                sql("UPDATE queue SET status='done', detail=?, finished=? WHERE id=?", ("déjà harmonisé (marqueur présent)", time.time(), job["id"]))
                return True
            if r.returncode != 0 or not out.exists():
                derniere = (txt.splitlines() or ["échec"])[-1][:200]
                out.unlink(missing_ok=True)
                continue
            if nonmkv:
                nb = video_packets(out)
                if nb != na:
                    derniere = f"nombre d'images vidéo différent ({na} -> {nb})"
                    out.unlink(missing_ok=True)
                    continue
                pa, pb = ffprobe(src), ffprobe(out)
                nn = lambda p, t: sum(1 for x in p.get("streams", []) if x.get("codec_type") == t)   # noqa: E731
                if nn(pb, "video") != nn(pa, "video") or nn(pb, "audio") != nn(pa, "audio") or nn(pb, "subtitle") < nn(pa, "subtitle"):
                    derniere = "pistes manquantes par rapport à l'original"
                    out.unlink(missing_ok=True)
                    continue
            ma = meta_video(src)   # HDR10, Dolby Vision, couleurs : strictement conservés, sinon rien n'est remplacé
            if ma is None or ma != meta_video(out):
                derniere = "métadonnées vidéo (HDR, Dolby Vision, couleurs) différentes de l'original"
                out.unlink(missing_ok=True)
                continue
            break
        else:
            raise RuntimeError("remux : " + derniere + ", original conservé")
        apres = round(out.stat().st_size / 1048576)
        resume = next((l for l in txt.splitlines() if l.startswith("Remux :")), "Remux :")[7:].strip()
        STATE.update(step="Remplacement", pct=99)
        if nonmkv:
            corb = replace_original(path, out)
        else:
            replace_in_place(path, out)
            corb = ""
        det = "remux seul : " + resume + (" ; converti en MKV, original en corbeille" if nonmkv else " ; remplacé sur place, sans corbeille") + (f" ; {motif}" if motif else "")
        reg("REMUXE", prof, avant, apres, round((1 - apres / avant) * 100) if avant else 0, path, det, corb)
        final = str(src.with_suffix(".mkv"))
        if nonmkv:   # le registre suit le fichier : après conversion son nom se termine par .mkv (l'ancien nom reste dans orig_path pour une restauration)
            sql("UPDATE registry SET orig_path=COALESCE(orig_path,path), path=? WHERE path=? AND statut='REMUXE'", (final, path))
        sql("UPDATE queue SET status='done', detail=?, finished=? WHERE id=?",
            (f"remux seul : pistes harmonisées ({avant} Mo → {apres} Mo)" + (f" · {motif}" if motif else ""), time.time(), job["id"]))
        if get("rename_after"):
            renommer_apres(final, prof, discret=True)
        return True
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def remux_apres_saut(job, motif):
    """Option « harmoniser » : le fichier n'est pas réduit (sauté ou refusé), mais ses pistes sont tout de même mises au standard de la saison.
    Jamais bloquant : en cas d'échec le fichier reste tel quel, avec son statut de saut ou de refus."""
    if not get("unify_remux") or job.get("src") or job.get("cat") == "anime" or job.get("kind") in ("estimate", "force"):
        return False
    try:
        return run_remux(job, motif)
    except Exception as e:  # noqa: BLE001
        STATE["msg"] = "remux non fait : " + str(e)[:120]
        return False


def remux_cibles(paths=None, tout=False):
    """Fichiers que le remux seul peut harmoniser aujourd'hui (même calcul que la médiathèque) : {chemin: catégorie}.
    tout=True : demande explicite de l'utilisateur, y compris les épisodes encore encodables (hx) ; sinon seulement les non réductibles (rx)."""
    out = {}
    for cat in ("series", "films"):
        for s in library_for(cat)["series"]:
            for se in s["seasons"]:
                for e in se["eps"]:
                    if e["hx" if tout else "rx"] and (paths is None or e["p"] in paths):
                        out[e["p"]] = cat
    return out


def remux_pour_saisons(paths):
    """Quand on ajoute une saison (tous ses épisodes encodables), ses autres épisodes, non réductibles, passent par le remux seul : la saison reste homogène."""
    demandes, extra = set(paths), []
    for cat in {cat_of(p) for p in paths} & {"series", "films"}:
        for s in library_for(cat)["series"]:
            for se in s["seasons"]:
                todo = {e["p"] for e in se["eps"] if not e["why"]}
                if todo and todo <= demandes:
                    extra += [e["p"] for e in se["eps"] if e["rx"]]
    return extra


def remux_saisons_traitees(serie=None):
    """Saisons de séries dont au moins un épisode est déjà traité (« Traité » ou marque MouFlo dans le nom) : leurs épisodes harmonisables restants.
    Lecture seule ; serie : limiter à une série."""
    paths, par, saisons = [], {}, 0
    for s in library_for("series")["series"]:
        if serie and s["n"] != serie:
            continue
        for se in s["seasons"]:
            if not any(e["why"] == "Traité" or "MouFlo" in os.path.basename(e["p"]) for e in se["eps"]):
                continue
            rx = [e for e in se["eps"] if e["rx"]]
            if not rx:
                continue
            saisons += 1
            paths += [e["p"] for e in rx]
            p = par.setdefault(s["n"], {"n": s["n"], "fichiers": 0, "mo": 0, "saisons": 0})
            p["fichiers"] += len(rx); p["mo"] += sum(e["sz"] for e in rx); p["saisons"] += 1
    liste = sorted(par.values(), key=lambda x: -x["fichiers"])
    return {"paths": paths, "saisons": saisons, "series": len(par), "fichiers": len(paths), "mo": sum(p["mo"] for p in liste), "liste": liste}


def enqueue_remux(paths, series="", tout=False):
    if not get("unify_remux") or not paths:
        return 0
    ok = remux_cibles(set(paths), tout)
    return sum(enqueue([p], kind="remux", series=series, cat=ok[p]) for p in dict.fromkeys(paths) if p in ok)


def run_encode(job):
    path, cat = job["path"], job["cat"]
    prof = cat if cat in ("anime", "series", "films") else "series"
    mprof = "anime" if prof == "anime" else "series"
    src = Path(job.get("src") or path)  # mode « refaire » : on encode la copie de la Corbeille, jamais un original remis dans la médiathèque
    STATE.update(step="Vérifications", pct=0, eta=None, fps=0, msg="")
    why = check_file(str(src), cat)
    if why:
        sql("UPDATE queue SET status='skipped', detail=?, finished=? WHERE id=?", (why, time.time(), job["id"]))
        if why.startswith(("vidéo entrelacée", "HDR", "définition supérieure")):
            remux_apres_saut(job, why)
        return
    avant = round(src.stat().st_size / 1048576)
    tgt = get("target_" + prof)
    use_tgt = bool(tgt) and job.get("kind") != "force"
    dur0 = 0
    pr0 = {}
    if use_tgt:
        try:
            pr0 = ffprobe(src)
            dur0 = float(pr0["format"]["duration"])
        except (KeyError, ValueError):
            dur0 = 0
    if use_tgt and not job.get("src"):   # pas en mode « refaire » : là, l'utilisateur a demandé explicitement
        why_skip = skip_reason(pr0, avant, tgt, cat)
        if why_skip:
            reg("IGNORE_ESTIMATION", prof, avant, avant, 0, path, why_skip)
            sql("UPDATE queue SET status='skipped', detail=?, finished=? WHERE id=?", (why_skip.split(" (")[0], time.time(), job["id"]))
            remux_apres_saut(job, why_skip.split(" (")[0])
            return
    if use_tgt:
        prevu = choose_qp(job, path, tgt, dur0, str(src))
        gp = round((1 - prevu / avant) * 100)
        besoin = get("min_saved") * max(0.3, dur0 / 1320)
        if (avant - prevu) < besoin * 0.6 or gp < get("min_pct") - 5:
            reg("IGNORE_ESTIMATION", prof, avant, round(prevu), gp, path, f"économie prévue {round(avant - prevu)} Mo (qp {job['qp']}{', plafond de qualité' if job.get('_plafond') else ''}) sous les {round(besoin)} Mo requis, fichier non encodé")
            sql("UPDATE queue SET status='skipped', detail=?, finished=? WHERE id=?", (f"économie prévue trop faible ({round(avant - prevu)} Mo)", time.time(), job["id"]))
            remux_apres_saut(job, f"économie prévue trop faible ({round(avant - prevu)} Mo)")
            return
    else:
        est = sql("SELECT size,gains FROM estimates WHERE path=?", (path,), one=True)
        if not est or est["size"] != src.stat().st_size or str(job["qp"]) not in json.loads(est["gains"]):
            run_estimate(job, [job["qp"]])
            est = sql("SELECT size,gains FROM estimates WHERE path=?", (path,), one=True)
        g_est = json.loads(est["gains"]).get(str(job["qp"]))
        if g_est is not None and g_est < get("est_min") / 100 and job.get("kind") != "force":
            reg("IGNORE_ESTIMATION", prof, avant, 0, round(g_est * 100), path, f"gain estimé {round(g_est * 100)} % < {get('est_min')} %")
            sql("UPDATE queue SET status='skipped', detail=?, finished=? WHERE id=?", (f"gain estimé trop faible ({round(g_est * 100)} %)", time.time(), job["id"]))
            remux_apres_saut(job, f"gain estimé trop faible ({round(g_est * 100)} %)")
            return
    wd = WORK / f"job{job['id']}"
    wd.mkdir(parents=True, exist_ok=True)
    try:
        pr = ffprobe(src)
        dur = float(pr["format"]["duration"])
        v0 = next((x for x in pr.get("streams", []) if x.get("codec_type") == "video" and not x.get("disposition", {}).get("attached_pic")), {})
        try:
            a_, b_ = str(v0.get("avg_frame_rate", "0/1")).split("/")
            fps = float(a_) / float(b_)
        except (ValueError, ZeroDivisionError):
            fps = 0.0
        fps = fps if fps > 1 else 24.0
        STATE.update(dur=dur, size=avant)
        job["skip"] = [x["index"] for x in pr.get("streams", []) if x.get("codec_type") == "video" and x.get("disposition", {}).get("attached_pic")]
        STATE.update(step="Encodage GPU", pct=0, eta=None, _ot=False)
        enc = wd / "enc.mkv"
        mode = "gpu" if hw_decodable(v0.get("codec_name"), v0.get("pix_fmt")) else "cpu"
        if mode == "gpu" and fin_problematique(src):
            mode = "mixte"
            STATE["msg"] = "fin de fichier à colorimétrie changeante : décodage GPU sans reconstruction des filtres"
        mode = encode_auto(job, src, enc, dur, fps, mode)
        if use_tgt and not job.get("_retry"):   # filet de qualité : sortie très loin de la taille visée → un nouvel essai, qp corrigé d'après la taille réellement obtenue
            import math
            tv = (STATE.get("tgt") or 0) - nonvideo_mb(src)
            vid = enc.stat().st_size / 1048576
            nq, qual = None, ""
            if tv > 10 and 0 < vid < 0.7 * tv and job["qp"] > 14:
                nq, qual = max(14, min(job["qp"] - 1, round(job["qp"] - math.log(tv / vid) / PENTE))), "trop petite"
            else:
                nq = qp_plus_haut(job["qp"], vid, tv, qp_plafond())
                qual = "trop grosse"
            if nq:
                STATE.update(msg=f"sortie {qual} ({round(vid)} Mo de vidéo pour {round(tv)} visés) : nouvel encodage à qp {nq}", pct=0, eta=None, fps=0, step="Encodage GPU (2e essai)")
                job["_retry"] = True
                job["qp"] = nq
                sql("UPDATE queue SET qp=? WHERE id=?", (nq, job["id"]))
                try: enc.unlink()
                except OSError: pass
                encode_auto(job, src, enc, dur, fps, mode)
        STATE.update(step="Pistes, sous-titres et contrôles", pct=99, eta=None)
        if src.stat().st_size < 3 * 2**30:  # contrôle : aucune image perdue ni ajoutée (les gros fichiers sont trop longs à relire)
            na, nb = video_packets(src), video_packets(enc)
            if na and nb and abs(na - nb) > max(3, 0.001 * na):
                raise RuntimeError(f"contrôles : ECHEC : images perdues ou ajoutées ({na} -> {nb}), original conservé")
        gros = enc.stat().st_size / 1048576 + nonvideo_mb(src)
        if gros >= avant:
            apres = round(gros)
            reg("REJETE_GAIN", prof, avant, apres, round((1 - apres / avant) * 100), path, "sortie plus grosse que l'original (qp %s), original conservé" % job["qp"])
            sql("UPDATE queue SET status='rejected', detail=?, finished=? WHERE id=?", ("sortie plus grosse que l'original", time.time(), job["id"]))
            remux_apres_saut(job, "sortie plus grosse que l'original")
            return
        out = wd / "final.mkv"
        r = subprocess.run([str(BASE / "media.py"), "--post", mprof, str(src), str(enc), str(out)], capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError("contrôles : " + (r.stdout + r.stderr).strip().splitlines()[-1][:300])
        apres = round(out.stat().st_size / 1048576)
        gain = round((1 - out.stat().st_size / src.stat().st_size) * 100)
        besoin = get("min_saved") * max(0.3, dur / 1320)
        if (avant - apres) < besoin or gain < get("min_pct"):
            motif = f"économie réelle {avant - apres} Mo < {round(besoin)} Mo requis (pour {round(dur / 60)} min)" if (avant - apres) < besoin else f"gain réel {gain} % < {get('min_pct')} %"
            reg("REJETE_GAIN", prof, avant, apres, gain, path, motif + ", original conservé")
            sql("UPDATE queue SET status='rejected', detail=?, finished=? WHERE id=?", (motif, time.time(), job["id"]))
            remux_apres_saut(job, motif)
            return
        STATE.update(step="Remplacement", pct=99)
        if job.get("src"):
            replace_from_corbeille(path, out)
            old = sql("SELECT id,apres_mo FROM registry WHERE path=? AND statut='REMPLACE' AND purged=0 ORDER BY id DESC LIMIT 1", (path,), one=True)
            det = "qp %s, refait depuis la Corbeille (version précédente de %s Mo dans Remplaces)" % (job["qp"], old["apres_mo"] if old else "?")
            if old:
                sql("UPDATE registry SET ts=?, avant_mo=?, apres_mo=?, gain=?, detail=? WHERE id=?", (time.time(), avant, apres, gain, det, old["id"]))
            else:
                reg("REMPLACE", prof, avant, apres, gain, path, det, str(job["src"]))
        else:
            corb = replace_original(path, out)
            reg("REMPLACE", prof, avant, apres, gain, path, "qp %s%s, original en corbeille" % (job["qp"], " (plafond de qualité : taille visée non atteinte)" if job.get("_plafond") else ""), corb)
        sql("UPDATE queue SET status='done', detail=?, finished=? WHERE id=?", (f"{avant} Mo → {apres} Mo (−{gain} %)", time.time(), job["id"]))
        if get("rename_after"):
            renommer_apres(path, prof, str(Path(path).with_suffix(".mkv")))
    finally:
        shutil.rmtree(wd, ignore_errors=True)


import notify
import renamer


def tg(kind, text, key=None, gap=0, silent=False):
    """Alerte Telegram si elle est activée dans les réglages (interrupteur général + un par type)."""
    if get("tg_on") and get("tg_" + kind):
        notify.alert(text, key=key, gap=gap, silent=silent)


_SESS = {"n": 0, "t0": 0.0, "errs": 0}


def _go(mo): return ("%.1f Go" % (mo / 1024)) if mo >= 1024 else ("%d Mo" % mo)


def tg_fin_de_file():
    """File vide après un travail : un message de bilan pour la session."""
    t0 = _SESS["t0"]
    rx = sql("SELECT COUNT(*) c FROM registry WHERE statut='REMUXE' AND ts>?", (t0,), one=True)["c"]
    rem = sql("SELECT COUNT(*) c, COALESCE(SUM(avant_mo-apres_mo),0) g FROM registry WHERE statut='REMPLACE' AND ts>?", (t0,), one=True)
    rej = sql("SELECT COUNT(*) c FROM registry WHERE statut IN ('REJETE_GAIN','IGNORE_ESTIMATION') AND ts>?", (t0,), one=True)["c"]
    err = sql("SELECT COUNT(*) c FROM registry WHERE statut='ERREUR' AND ts>?", (t0,), one=True)["c"]
    d = int(time.time() - t0)
    tg("done", f"✅ <b>MouFlencoder : file terminée</b>\n{rem['c']} fichier(s) remplacé(s), <b>{_go(rem['g'])}</b> gagnés\n{rej} refusé(s) ou ignoré(s), {rx} remuxé(s), {err} erreur(s)\nDurée : {d // 3600} h {d % 3600 // 60:02d}", key="done", gap=600)
    _SESS.update(n=0, t0=0.0, errs=0)


def tg_jalon():
    s = sql("SELECT COALESCE(SUM(avant_mo-apres_mo),0) g FROM registry WHERE statut='REMPLACE'", one=True)["g"]
    pas = 100 * 1024
    cur = int(s // pas)
    prev = int(get("tg_jalon_v") or 0)
    if cur > prev:
        put("tg_jalon_v", cur)
        if prev:  # le premier passage ne fait que mémoriser le palier
            tg("milestone", f"🎉 <b>{cur * 100} Go récupérés</b> depuis le début ({sql('SELECT COUNT(*) c FROM registry WHERE statut=?', ('REMPLACE',), one=True)['c']} fichiers)", key="jalon", gap=60, silent=True)


def tg_quotidien():
    """Résumé de la veille, une fois par jour à l'heure choisie."""
    now = datetime.now()
    if not (get("tg_on") and get("tg_daily")) or now.hour != int(get("tg_daily_hour")):
        return
    day = now.date().isoformat()
    if (get("tg_daily_last") or "") == day:
        return
    put("tg_daily_last", day)
    t0 = time.time() - 86400
    c = lambda q, a=(): sql(q, a, one=True)
    rem = c("SELECT COUNT(*) c, COALESCE(SUM(avant_mo-apres_mo),0) g FROM registry WHERE statut='REMPLACE' AND ts>?", (t0,))
    rej = c("SELECT COUNT(*) c FROM registry WHERE statut IN ('REJETE_GAIN','IGNORE_ESTIMATION') AND ts>?", (t0,))["c"]
    err = c("SELECT COUNT(*) c FROM registry WHERE statut='ERREUR' AND ts>?", (t0,))["c"]
    tot = c("SELECT COUNT(*) c, COALESCE(SUM(avant_mo-apres_mo),0) g FROM registry WHERE statut='REMPLACE'")
    st = stats()
    try: free = shutil.disk_usage(NAS_ROOT).free / 2**40
    except OSError: free = 0
    en_att = c("SELECT COUNT(*) c FROM queue WHERE status='queued'")["c"]
    tg("daily", f"📊 <b>MouFlencoder : bilan des dernières 24 h</b>\n{rem['c']} remplacé(s) : <b>{_go(rem['g'])}</b> gagnés\n{rej} refusé(s)/ignoré(s), {err} erreur(s)\nEn attente : {en_att} fichier(s)\n\nTotal depuis le début : {tot['c']} fichiers, {_go(tot['g'])}\nCorbeille : {_go(st['trash_mo'])} à vider pour libérer l'espace réel\nNAS libre : {free:.1f} To", key="daily", gap=3600)


def tg_surveillance(ok, why, queued):
    """Appelée à chaque tour du moteur : pause longue et reprise."""
    if not queued or ok:
        if _SESS.get("pause_notif"):
            tg("pause", "▶️ <b>MouFlencoder : reprise</b> après une pause de %d min" % ((time.time() - _SESS["pause_t"]) / 60), key="reprise", gap=120, silent=True)
        _SESS.update(pause_t=0, pause_notif=False)
        return
    if why.startswith("en pause (manuel)"):
        return
    if not _SESS.get("pause_t"):
        _SESS["pause_t"] = time.time()
    elif not _SESS.get("pause_notif") and time.time() - _SESS["pause_t"] > get("tg_pause_min") * 60:
        _SESS["pause_notif"] = True
        tg("pause", f"⏸ <b>MouFlencoder : file en pause depuis {get('tg_pause_min')} min</b>\nRaison : {notify.esc(why)}\n{queued} fichier(s) en attente", key="pause", gap=3600)


def run_job(job):
    STATE.update(job=job, cancel=False, blocked="", frozen=False, pct=0, fps=0, eta=None, step="Démarrage", dur=None, size=None, tgt=None, throttle=100)
    sql("UPDATE queue SET status='running', started=? WHERE id=?", (time.time(), job["id"]))
    try:
        if job["kind"] == "estimate":
            g = run_estimate(job)
            sql("UPDATE queue SET status='done', detail=?, finished=? WHERE id=?", ("gain estimé à qp %s : −%d %%" % (job["qp"], round(g.get(str(job["qp"]), 0) * 100)), time.time(), job["id"]))
            gq = g.get(str(job["qp"]), 0)
            av = round(Path(job["path"]).stat().st_size / 2**20)
            reg("ESTIMATION", job["cat"], av, round(av * (1 - gq)), round(gq * 100), job["path"], "estimé à qp %s, à ±8 points près" % job["qp"])
        elif job["kind"] == "remux":
            run_remux(job)
        else:
            run_encode(job)
        _SESS["errs"] = 0
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:300]
        st = "cancelled" if "annulé" in msg else "error"
        sql("UPDATE queue SET status=?, detail=?, finished=? WHERE id=?", (st, msg, time.time(), job["id"]))
        if st == "error" and job["kind"] != "estimate":
            reg("ERREUR", job["cat"], 0, 0, 0, job["path"], msg)
            _SESS["errs"] += 1
            if _SESS["errs"] >= 5:
                tg("errors", f"⚠️ <b>MouFlencoder : {_SESS['errs']} erreurs de suite</b>\nDernière : {notify.esc(os.path.basename(job['path']))[:80]}\n{notify.esc(msg)[:200]}", key="erreurs", gap=3600)
    finally:
        STATE.update(job=None, frozen=False, step="", pct=0, fps=0, eta=None, dur=None, size=None, tgt=None, throttle=100)


def worker_loop():
    while True:
        try:
            ok, why = allowed_now()
            STATE["blocked"] = "" if ok else why
            if ok or why.startswith("en pause (manuel)"):
                pause_close()
            elif sql("SELECT 1 FROM queue WHERE status='queued' LIMIT 1", one=True):
                pause_open(why)
            job = None
            if ok:
                r = sql("SELECT * FROM queue WHERE status='queued' ORDER BY prio DESC, id LIMIT 1", one=True)
                job = dict(r) if r else None
            nq = sql("SELECT COUNT(*) c FROM queue WHERE status='queued'", one=True)["c"]
            tg_surveillance(ok, why, nq)
            tg_quotidien()
            if not job:
                if ok and not nq and _SESS["n"] > 0:
                    tg_fin_de_file()
                flush_notifs(force=True)   # plus rien à traiter : on envoie tout de suite ce qui reste
                time.sleep(5)
                continue
            if not _SESS["t0"]:
                _SESS["t0"] = time.time()
            run_job(job)
            flush_notifs()   # espacé : au plus toutes les 10 minutes
            _SESS["n"] += 1
            tg_jalon()
        except Exception as e:  # noqa: BLE001
            STATE["msg"] = f"erreur moteur : {e}"
            time.sleep(10)


def start_worker():
    # un redémarrage pendant un traitement remet le fichier en attente (les fichiers temporaires sont dans /var/tmp)
    sql("UPDATE queue SET status='queued' WHERE status='running'")
    sql("UPDATE pauses SET end=start WHERE end IS NULL")
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=tempo_loop, daemon=True).start()


# ------------------------------------------------------------------ registre, corbeille, statistiques
def registry_list(statut="", q="", limit=300):
    rows = sql("SELECT * FROM registry ORDER BY id DESC LIMIT 5000")
    out = []
    for r in rows:
        if statut and r["statut"] != statut:
            continue
        if q and q.lower() not in r["path"].lower():
            continue
        out.append(dict(r, name=os.path.basename(r["path"])))
        if len(out) >= limit:
            break
    return out


def trash_list():
    out = []
    for r in sql("SELECT * FROM registry WHERE statut IN ('REMPLACE','REMUXE') AND purged=0 ORDER BY id DESC"):
        if r["corbeille"] and os.path.exists(r["corbeille"]):
            out.append(dict(r, name=os.path.basename(r["path"])))
    return out


def trash_delete(rid):
    r = sql("SELECT * FROM registry WHERE id=?", (rid,), one=True)
    if not r or r["purged"]:
        raise RuntimeError("entrée introuvable")
    try:
        os.unlink(r["corbeille"])
    except FileNotFoundError:
        pass
    except OSError as e:
        raise RuntimeError("le NAS refuse la suppression (à faire depuis le NAS) : " + str(e))
    sql("UPDATE registry SET purged=1 WHERE id=?", (rid,))
    reg("CORBEILLE_SUPPR", r["profil"], r["avant_mo"], 0, 0, r["path"], "original supprimé définitivement par l'utilisateur")


def trash_restore(rid):
    r = sql("SELECT * FROM registry WHERE id=?", (rid,), one=True)
    if not r or r["purged"] or not os.path.exists(r["corbeille"]):
        raise RuntimeError("original introuvable en corbeille")
    cur = Path(r["path"]).with_suffix(".mkv")
    side = MFE_DIR / "Remplaces" / str(cur)[len(NAS_ROOT):]
    side.parent.mkdir(parents=True, exist_ok=True)
    if cur.exists():
        if side.exists():
            side = side.with_name(side.stem + ".%d.mkv" % time.time())
        os.link(cur, side)
    dest = r["orig_path"] or r["path"]   # après un renommage Sonarr, l'original retrouve son nom d'origine
    os.rename(r["corbeille"], dest)
    if str(cur) != dest and cur.exists():
        try: cur.unlink()
        except OSError: pass
    sql("UPDATE registry SET purged=1, statut='REMPLACE_ANCIEN' WHERE id=?", (rid,))
    reg("RESTAURE", r["profil"], r["avant_mo"], r["apres_mo"], 0, r["path"], "original restauré, version encodée mise de côté dans Remplaces")


def stats():
    rows = sql("SELECT ts,profil,avant_mo,apres_mo,gain FROM registry WHERE statut='REMPLACE' ORDER BY ts")
    saved = sum(r["avant_mo"] - r["apres_mo"] for r in rows)
    byc, daily = {}, {}
    for r in rows:
        c = byc.setdefault(r["profil"], {"n": 0, "saved": 0, "avant": 0})
        c["n"] += 1; c["saved"] += r["avant_mo"] - r["apres_mo"]; c["avant"] += r["avant_mo"]
        d = datetime.fromtimestamp(r["ts"]).date().isoformat()
        daily[d] = daily.get(d, 0) + (r["avant_mo"] - r["apres_mo"])
    cnt = {r["statut"]: r["c"] for r in sql("SELECT statut, COUNT(*) c FROM registry GROUP BY statut")}
    trash_mo = sum(r["avant_mo"] for r in trash_list())
    return {"saved_mo": saved, "replaced": len(rows), "avg_gain": round(sum(r["gain"] for r in rows) / len(rows)) if rows else 0,
            "by_cat": byc, "daily": daily, "counts": cnt, "trash_mo": trash_mo}


def system_state():
    st = emby_status()
    ts = get("tempo_ts")
    today, tomorrow = tempo_now()
    try:
        free = shutil.disk_usage(NAS_ROOT).free
    except OSError:
        free = None
    ok, why = allowed_now()
    return {"job": STATE["job"] and {k: STATE["job"].get(k) for k in ("id", "path", "cat", "qp", "kind")} or None,
            "throttle": STATE.get("throttle", 100), "dur": STATE.get("dur"), "size": STATE.get("size"), "tgt": STATE.get("tgt"), "step": STATE["step"], "pct": STATE["pct"], "fps": STATE["fps"], "eta": STATE["eta"], "blocked": STATE["blocked"] or ("" if ok else why),
            "paused": get("paused"), "frozen": bool(STATE.get("frozen")), "test_mode": get("test_mode"), "temp": read_temp(), "nas_free": free, "emby": st,
            "tempo": {"today": today, "tomorrow": tomorrow, "ts": ts, "src": get("tempo_src")}, "lib_refreshing": STATE["lib_refreshing"], "msg": STATE["msg"],
            "queue_len": sql("SELECT COUNT(*) c FROM queue WHERE status='queued'", one=True)["c"]}
