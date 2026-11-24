#!/usr/bin/env python3
"""MouFlencoder : serveur de démonstration.

Sert la vraie interface (static/index.html) avec une médiathèque FICTIVE générée à la volée :
aucun fichier vidéo, aucun Emby, rien n'est encodé ni modifié. Les actions renvoient « ok ».

    python3 demo/demo_server.py          puis ouvrir http://127.0.0.1:8301
    python3 demo/demo_server.py 9000     (autre port)
"""
import http.server, json, random, sys, time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8301
NOW = time.time()
R = random.Random(42)
LIB_ROOT = "/media/Bibliotheque"

# ------------------------------------------------------------------ médiathèque fictive
CATALOGUE = {
    "anime": [("Kaze no Tabibito", "KAZE", 3, 12, 1100), ("Ryūsei Academy", "NOVA", 2, 24, 900), ("Hoshizora Mechanics", "KAZE", 1, 13, 1300),
              ("Le Renard des Neiges", "LUMEN", 2, 12, 650), ("Tsuki to Kitsune", "NOVA", 1, 24, 480)],
    "series": [("Les Gardiens d'Orion", "LUMEN", 4, 10, 2400), ("Station Polaris", "NOVA", 3, 8, 3100), ("Brume Nord", "ORBIT", 2, 6, 2900),
               ("Le Dernier Phare", "LUMEN", 3, 10, 1700), ("Cité Néon", "ORBIT", 2, 8, 2600), ("Les Veilleurs", "NOVA", 5, 12, 1500),
               ("Hôtel Méridien", "ARCO", 2, 10, 1250), ("Ligne 7", "ARCO", 1, 8, 820)],
    "films": [("Horizon Rouge", "LUMEN", 1, 1, 9800), ("La Onzième Heure", "ORBIT", 1, 1, 7400), ("Sable et Cendre", "NOVA", 1, 1, 12100),
              ("Opération Albatros", "ARCO", 1, 1, 6300)],
}
DOSSIER = {"anime": "Animes", "series": "Series", "films": "Films"}
CAP = {"anime": 550, "series": 900, "films": 900}


def nom_fichier(cat, serie, s, e, team, hevc):
    codec = "x265" if hevc else "h264"
    if cat == "films":
        return f"{serie}.2023.MULTi.1080p.WEB.{codec}-{team}.mkv"
    return f"{serie}.S{s:02d}E{e:02d}.MULTi.1080p.WEB.{codec}-{team}.mkv"


LIB = {}
for cat, liste in CATALOGUE.items():
    series = []
    for (serie, team, ns, ne, moy) in liste:
        seasons = []
        for s in range(1, ns + 1):
            eps = []
            for e in range(1, ne + 1):
                sz = max(200, int(R.gauss(moy, moy * 0.12)))
                fait = s < ns - 1 or (s == ns - 1 and e <= ne // 2)
                hevc = fait or R.random() < 0.15
                tm = team + ("-MouFlo" if fait else "")
                why, st = "", ""
                if fait:
                    sz, why, st = int(sz * R.uniform(0.32, 0.5)), "Traité", "REMPLACE"
                elif sz < CAP[cat]:
                    why = "Sous le plafond"
                elif hevc:
                    why = "Peu de gain"
                p = f"{LIB_ROOT}/{DOSSIER[cat]}/{serie}/" + (f"s{s}/" if cat != "films" else "") + nom_fichier(cat, serie, s, e, tm, hevc)
                eps.append(dict(e=e, sz=sz, tm=tm, hev=hevc, p=p, r="1080p", dm="1920×1080", why=why, g=None, gx=False,
                                rx=why in ("Sous le plafond", "Peu de gain"), hx=why in ("Sous le plafond", "Peu de gain"), ha=fait, st=st))
            seasons.append(dict(s=s, eps=eps))
        series.append(dict(n=serie, c=cat, seasons=seasons, qpi=3, min=40, qps=[18, 21, 24, 27, 30] if cat == "anime" else [20, 23, 26, 29, 32]))
    LIB[cat] = series

# estimations « à la louche » sur quelques séries (curseur Qualité ↔ Gain)
for d in LIB["series"][:3] + LIB["anime"][:2]:
    for se in d["seasons"]:
        for ep in se["eps"]:
            if not ep["why"]:
                g = R.uniform(0.45, 0.7)
                ep["g"] = {str(q): round(min(0.9, g + (q - d["qps"][2]) * 0.04), 3) for q in d["qps"]}
                ep["gx"] = R.random() < 0.5

EPISODES = [(cat, d, se, ep) for cat in LIB for d in LIB[cat] for se in d["seasons"] for ep in se["eps"]]
A_FAIRE = [x for x in EPISODES if not x[3]["why"]]


def nom(p):
    return p.rsplit("/", 1)[-1]


# ------------------------------------------------------------------ file, registre, corbeille
QUEUE = []
for i, (cat, d, se, ep) in enumerate(A_FAIRE[:14]):
    QUEUE.append(dict(id=900 + i, kind="encode", path=ep["p"], cat=cat, series=d["n"], qp=d["qps"][2], min_gain=40,
                      status="running" if i == 0 else "queued", added=NOW - 3600, started=NOW - 140 if i == 0 else None,
                      finished=None, detail=None, prio=0, src=None, name=nom(ep["p"])))
QUEUE[4]["kind"], QUEUE[7]["kind"] = "estimate", "remux"

REG, TRASH = [], []
traites = [x for x in EPISODES if x[3]["st"] == "REMPLACE"]
R.shuffle(traites)
for i, (cat, d, se, ep) in enumerate(traites[:120]):
    avant = int(ep["sz"] / R.uniform(0.32, 0.5))
    orig = ep["p"].replace("-MouFlo", "").replace("x265", "h264")
    row = dict(id=5000 - i, ts=NOW - i * 410, statut="REMPLACE", profil=cat, avant_mo=avant, apres_mo=ep["sz"], gain=round(100 - ep["sz"] * 100 / avant),
               path=ep["p"], detail=f"qp {d['qps'][2] - R.choice([2, 3, 4])}, original en corbeille", corbeille=orig.replace(LIB_ROOT, LIB_ROOT + "/MouFlencoder/Corbeille"),
               purged=0, orig_path=orig, name=nom(ep["p"]))
    REG.append(row)
    if i < 40:
        TRASH.append(row)
for i, (statut, detail) in enumerate([("REMUXE", "remux seul : 2 piste(s) ASS ajoutée(s), audio FR par défaut : oui ; remplacé sur place, sans corbeille"),
                                      ("REJETE_GAIN", "économie insuffisante (−9 %, minimum 15 %) : original gardé"),
                                      ("IGNORE_ESTIMATION", "source x265 déjà légère (612 Mo / 22 min)"),
                                      ("ESTIMATION", "≈ −58 % à qp 24 (3 extraits de 40 s)")]):
    cat, d, se, ep = A_FAIRE[20 + i]
    REG.insert(2 + i * 3, dict(id=6000 + i, ts=NOW - 900 - i * 1300, statut=statut, profil=cat, avant_mo=ep["sz"], apres_mo=ep["sz"], gain=0, path=ep["p"],
                               detail=detail, corbeille="", purged=0, orig_path=None, name=nom(ep["p"])))

daily = {time.strftime("%Y-%m-%d", time.localtime(NOW - k * 86400)): int(R.uniform(90, 420) * 1000) for k in range(17, -1, -1)}
STATS = dict(saved_mo=sum(daily.values()), replaced=1840, avg_gain=57, daily=daily, trash_mo=sum(r["avant_mo"] for r in TRASH),
             by_cat={"anime": {"n": 412, "saved": 268000, "avant": 441000}, "series": {"n": 1394, "saved": 3290000, "avant": 5610000},
                     "films": {"n": 34, "saved": 196000, "avant": 351000}},
             counts={"REMPLACE": 1840, "REMUXE": 2210, "REJETE_GAIN": 212, "IGNORE_ESTIMATION": 380, "ESTIMATION": 64, "ERREUR": 9, "RESTAURE": 12})

job = QUEUE[0]
cur = next(x[3] for x in A_FAIRE if x[3]["p"] == job["path"])
STATE = dict(job=dict(id=job["id"], path=job["path"], cat=job["cat"], qp=job["qp"], kind="encode"), throttle=100, dur=2580, size=cur["sz"],
             tgt=int(cur["sz"] * 0.42), step="Encodage GPU", pct=47, fps=362, eta=96, blocked="", paused=False, frozen=False, test_mode=False,
             temp=71.0, nas_free=7.4e12, emby={"ok": True, "playing": 1, "transcoding": 0},
             tempo={"today": "bleu", "tomorrow": "blanc", "ts": NOW - 600, "src": "api-couleur-tempo.fr"},
             lib_refreshing=False, msg="", queue_len=len(QUEUE), version="1.0.0 · démo")
PAUSES = {"today_n": 3, "today_s": 410, "recent": [
    {"reason": "Emby transcode une vidéo", "start": NOW - 5400, "end": NOW - 5220, "ongoing": False},
    {"reason": "processeur trop chaud (92 °C, reprise sous 84 °C)", "start": NOW - 3100, "end": NOW - 3020, "ongoing": False}]}
SETTINGS = {"cap_anime": 550, "enable_4k": False, "rename_after": True, "target_4k_mbps": 8, "tg_on": True, "tg_done": True, "tg_daily": True,
            "tg_errors": True, "tg_pause": True, "tg_milestone": True, "tg_daily_hour": 9, "tg_pause_min": 30, "min_saved": 150, "min_pct": 15,
            "cap_series": 900, "cap_films": 900, "min_gain": 40, "est_min": 30, "test_mode": False, "paused": False, "target_anime": 375,
            "target_series": 375, "target_films": 375, "qp_max": 26, "skip_x265_mo22": 700, "skip_video_pct": 128, "pause_on_transcode": True,
            "min_age_min": 15, "temp_max": 92, "temp_cible": 85, "emby_host": "http://192.168.1.10:8096", "tempo_unknown_ok": False, "unify_remux": True}


def media():
    out = []
    for cat in LIB:
        for s in LIB[cat]:
            eps = [e for se in s["seasons"] for e in se["eps"]]
            tot, todo = sum(e["sz"] for e in eps), [e for e in eps if not e["why"]]
            out.append(dict(n=s["n"], c=cat, files=len(eps), total=tot, avg=round(tot / len(eps)), todo=sum(e["sz"] for e in todo), todo_n=len(todo),
                            rx_n=sum(e["rx"] for e in eps), hx_n=sum(e["hx"] for e in eps), ha_n=sum(e["ha"] for e in eps), res={"1080p": len(eps)},
                            done=sum(e["st"] == "REMPLACE" for e in eps)))
    return dict(series=out, ts=NOW - 1800)


def media_files(q):
    cat = q.get("cat", [""])[0]
    fs = sorted((x for x in EPISODES if not cat or x[0] == cat), key=lambda x: -x[3]["sz"])
    return dict(total=len(fs), files=[dict(p=ep["p"], n=d["n"], s=se["s"], e=ep["e"], sz=ep["sz"], c=c, hev=ep["hev"], tm=ep["tm"], why=ep["why"],
                                           r=ep["r"], dm=ep["dm"], ha=ep["ha"], st=ep["st"]) for c, d, se, ep in fs[:300]])


def route(path, q):
    if path == "/api/state": return STATE
    if path == "/api/stats": return STATS
    if path == "/api/queue": return QUEUE
    if path == "/api/pauses": return PAUSES
    if path == "/api/trash": return TRASH
    if path == "/api/settings": return SETTINGS
    if path == "/api/library": return {"series": LIB.get(q.get("cat", ["anime"])[0], [])}
    if path == "/api/media": return media()
    if path == "/api/media/files": return media_files(q)
    if path == "/api/media/series":
        d = next((s for s in LIB.get(q.get("cat", [""])[0], []) if s["n"] == q.get("name", [""])[0]), None)
        return {"seasons": d["seasons"] if d else []}
    if path == "/api/remux/saisons": return {"actif": True, "saisons": 0, "fichiers": 0, "mo": 0, "series": []}
    if path == "/api/registry":
        st, s = q.get("statut", [""])[0], q.get("q", [""])[0].lower()
        return [r for r in REG if (not st or r["statut"] == st) and (not s or s in r["name"].lower())][:int(q.get("limit", ["300"])[0])]
    if path == "/api/settings/emby": return {"host": SETTINGS["emby_host"], "configured": True, "hint": "…d3m0"}
    if path == "/api/settings/arr": return {n: {"url": f"http://192.168.1.10:{p}", "configured": True, "hint": "…d3m0"} for n, p in (("sonarr", 8989), ("radarr", 7878))}
    if path == "/api/settings/telegram": return {"configured": True, "hint": "…d3m0", "chat_id": "-1000000000000", "thread_id": ""}
    if path == "/api/settings/hook": return {"configured": False}
    return {"ok": True, "message": "Mode démo : rien n'a été modifié."}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send((ROOT / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
        for pre, ctype in (("/icons/", None), ("/ui/", "text/css")):
            if u.path.startswith(pre):
                f = (ROOT / pre.strip("/") / u.path[len(pre):]).resolve()
                if f.is_file() and ROOT in f.parents:
                    types = {".svg": "image/svg+xml", ".png": "image/png", ".webmanifest": "application/manifest+json", ".css": "text/css"}
                    return self._send(f.read_bytes(), ctype or types.get(f.suffix, "application/octet-stream"))
        self._send(json.dumps(route(u.path, parse_qs(u.query)), ensure_ascii=False).encode(), "application/json")

    do_POST = do_GET


if __name__ == "__main__":
    print(f"MouFlencoder (démo) : http://127.0.0.1:{PORT}  —  Ctrl+C pour arrêter")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
