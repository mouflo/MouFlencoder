#!/usr/bin/env python3
"""Traitement d'un fichier pour Tdarr : filtre, encodage GPU hevc_vaapi, remux (séries/films), marqueur, contrôles.
Usage : tdarr_media.py <fichier> <dossier_sortie>   |   tdarr_media.py --post <profil> <original> <encodé> <sortie>
Jamais de remplacement : la sortie est écrite dans <dossier_sortie> seulement si tous les contrôles passent.
Variables de test : TDARR_PROFIL (anime|series), TDARR_MIN_AGE (minutes), TDARR_CAP_MB."""
import json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

os.environ.update(LC_ALL="C.UTF-8", LANG="C.UTF-8")
MODE = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("--post", "--estimate") else ""
POST = MODE == "--post"
if MODE == "--estimate":   # --estimate <profil> <fichier> : estime le gain par 3 extraits de 40 s
    EST_PROFIL = sys.argv[2]; SRC = Path(sys.argv[3])
elif POST:   # --post <profil> <original> <fichier_encodé_par_Tdarr> <fichier_sortie>
    POST_PROFIL = sys.argv[2]; SRC = Path(sys.argv[3]); ENC_IN = Path(sys.argv[4]); OUTFILE = Path(sys.argv[5])
else:
    SRC = Path(sys.argv[1]); OUTDIR = Path(sys.argv[2])
REMUX = POST and str(ENC_IN) == str(SRC)   # remux seul : la vidéo n'est pas réencodée, l'original sert d'entrée
HERE = Path(__file__).resolve().parent
FONT = HERE / "fonts" / "trebucbd_0.ttf"
MARKER = HERE / "marker.txt"
MARKER_NAME = "mouflanimexer_processed.marker"
FR = {"fre", "fra", "fr"}; EN = {"eng", "en"}
STYLE = ("Style: Default,Trebuchet MS,18,&H00FFFFFF,&H0000FFFF,&H00000000,&H00000000,"
         "-1,0,0,0,100,100,0,0,1,0.53,0.8,2,20,20,20,1")
PROFILS = {"anime": dict(qp=24, cap=550), "series": dict(qp=26, cap=900), "films": dict(qp=26, cap=900)}
NICE = ["nice", "-n", "19", "ionice", "-c3"]


def log(m): print(m, flush=True)
def skip(m): log("SKIP : " + m); sys.exit(0)
def fail(m): log("ECHEC : " + m); sys.exit(1)


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return r


def js(cmd):
    r = run(cmd)
    if r.returncode not in (0, 1):
        fail(f"{cmd[0]} code {r.returncode} : {r.stderr[:300]}")
    return json.loads(r.stdout)


def probe(p):
    return js(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)])


def mkvinfo(p):
    return js(["mkvmerge", "-J", str(p)])


def has_marker(info):
    return any(a.get("file_name") == MARKER_NAME for a in info.get("attachments", []))


# ---------- estimation du gain (mode --estimate) ----------
if MODE == "--estimate":
    qp = PROFILS[EST_PROFIL]["qp"]; seg = 40
    dur = float(probe(SRC)["format"]["duration"]); tot = SRC.stat().st_size
    tmpd = Path(tempfile.mkdtemp(prefix="tdarr_est_", dir="/var/tmp"))
    try:
        cp_b = enc_b = 0
        for i, frac in enumerate((0.2, 0.5, 0.8)):
            ss = f"{dur * frac:.0f}"; a = tmpd / f"c{i}.mkv"; b = tmpd / f"e{i}.mkv"
            run(NICE + ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-ss", ss, "-t", str(seg), "-i", str(SRC), "-map", "0:v:0", "-an", "-sn", "-c", "copy", str(a)])
            run(NICE + ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-init_hw_device", "vaapi=va:/dev/dri/renderD128", "-filter_hw_device", "va",
                        "-ss", ss, "-t", str(seg), "-i", str(SRC), "-map", "0:v:0", "-an", "-sn", "-vf", "format=p010,hwupload", "-c:v", "hevc_vaapi",
                        "-low_power", "1", "-profile:v", "main10", "-rc_mode", "CQP", "-qp", str(qp), str(b)])
            if a.exists() and b.exists(): cp_b += a.stat().st_size; enc_b += b.stat().st_size
        if cp_b == 0 or enc_b == 0: fail("estimation impossible")
        ratio = enc_b / cp_b
        vid = min(cp_b / (3 * seg) * dur, tot)
        new = tot - vid + vid * ratio
        print(json.dumps({"gain": round(1 - new / tot, 3), "ratio_video": round(ratio, 3), "orig_mb": round(tot / 2**20), "est_mb": round(new / 2**20)}))
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    sys.exit(0)

# ---------- profil ----------
p = str(SRC)
profil = POST_PROFIL if POST else os.environ.get("TDARR_PROFIL") or ("anime" if "/Manga/" in p else "series" if re.search(r"/(Series|Films|Movies)/", p) else "anime")
cfg = dict(PROFILS[profil])
if os.environ.get("TDARR_CAP_MB"): cfg["cap"] = int(os.environ["TDARR_CAP_MB"])
min_age = float(os.environ.get("TDARR_MIN_AGE", "15"))
log(f"Fichier : {SRC.name}\nProfil : {profil} (qp {cfg['qp']}, plafond {cfg['cap']} Mo)")

# ---------- filtres ----------
st = SRC.stat()
pr = probe(SRC); mi = mkvinfo(SRC)
dur0 = float(pr["format"]["duration"])
if REMUX and has_marker(mi):
    log("DEJA : le marqueur est déjà posé, pistes déjà harmonisées"); sys.exit(3)
if not POST:
    if (time.time() - st.st_mtime) < min_age * 60:
        skip(f"modifié depuis moins de {min_age:g} min")
    if st.st_size <= cfg["cap"] * 1024 * 1024:
        skip(f"déjà sous {cfg['cap']} Mo ({st.st_size // 2**20} Mo)")
    vid = [s for s in pr["streams"] if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not vid: skip("aucune piste vidéo")
    v0 = vid[0]
    if v0.get("codec_name") == "hevc" and has_marker(mi): skip("déjà en H.265 avec le marqueur")
    if v0.get("color_transfer") in ("smpte2084", "arib-std-b67") or "side_data_list" in v0:
        skip("HDR / Dolby Vision non géré")
    if int(v0.get("height", 0)) > 1100: skip("définition supérieure à 1080p non gérée")
    if v0.get("field_order") not in (None, "progressive", "unknown"): skip("vidéo entrelacée")

work = Path(tempfile.mkdtemp(prefix="tdarr_media_", dir="/var/tmp"))
try:
    # ---------- encodage GPU ----------
    if POST:
        enc = ENC_IN
    else:
        enc = work / "enc.mkv"; t0 = time.time()
        cmd = NICE + ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-init_hw_device", "vaapi=va:/dev/dri/renderD128",
                      "-filter_hw_device", "va", "-i", str(SRC), "-map", "0", "-map", "-0:d", "-map_metadata", "0", "-map_chapters", "0",
                      "-c", "copy", "-c:v:0", "hevc_vaapi", "-filter:v:0", "format=p010,hwupload", "-low_power", "1",
                      "-profile:v:0", "main10", "-rc_mode", "CQP", "-qp", str(cfg["qp"]), str(enc)]
        r = run(cmd)
        if r.returncode != 0 or not enc.exists(): fail("encodage ffmpeg : " + r.stderr[-400:])
        log(f"Encodage GPU terminé en {time.time() - t0:.0f} s : {enc.stat().st_size // 2**20} Mo")

    final = enc
    # ---------- remux séries/films ----------
    if profil == "series":
        ei = mkvinfo(enc); tracks = ei["tracks"]
        audio = [t for t in tracks if t["type"] == "audio"]
        subs = [t for t in tracks if t["type"] == "subtitles"]
        lang = lambda t: t["properties"].get("language", "und")
        name = lambda t: t["properties"].get("track_name") or ""
        is_forced = lambda t: bool(t["properties"].get("forced_track")) or bool(re.search(r"forc", name(t), re.I))
        fr_audio = [t for t in audio if lang(t) in FR]
        conv = [] if has_marker(ei) else [t for t in subs if t["codec"].startswith("SubRip") and lang(t) in FR | EN]   # marqueur présent : les ASS existent déjà, pas de doublon
        # conversion SRT -> ASS
        ass_files = []
        for t in conv:
            ass = work / f"s{t['id']}.ass"
            if run(NICE + ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(enc), "-map", f"0:s:{subs.index(t)}",
                           "-c:s", "ass", "-f", "ass", str(ass)]).returncode != 0:
                fail(f"conversion ASS piste {t['id']}")
            out = []; info_sec = False; got = set()
            for l in ass.read_text(encoding="utf-8").splitlines():
                if l.startswith("[Script Info]"): info_sec = True
                elif l.startswith("[") and info_sec:
                    for k, v in (("PlayResX", "384"), ("PlayResY", "288"), ("ScaledBorderAndShadow", "yes")):
                        if k not in got: out.append(f"{k}: {v}")
                    info_sec = False
                for k, v in (("PlayResX", "384"), ("PlayResY", "288"), ("ScaledBorderAndShadow", "yes")):
                    if l.startswith(k + ":"): l = f"{k}: {v}"; got.add(k)
                if l.startswith("Style: Default,"): l = STYLE
                out.append(l)
            ass.write_text("\n".join(out) + "\n", encoding="utf-8")
            srt = work / f"s{t['id']}.srt"
            run(["mkvextract", str(enc), "tracks", f"{t['id']}:{srt}"])
            cues = srt.read_text(encoding="utf-8-sig", errors="replace").count("-->") if srt.exists() else 0
            ndlg = sum(1 for l in out if l.startswith("Dialogue:"))
            if cues > 0 and ndlg == 0:
                fail(f"ASS vide pour la piste {t['id']} ({cues} répliques dans le SRT)")
            log(f"  piste {t['id']} ({lang(t)}{' forcé' if is_forced(t) else ''}) : {cues} répliques SRT -> {ndlg} lignes ASS")
            ass_files.append((t, ass))
        # tri : forcé FR, FR complet, EN
        ass_files.sort(key=lambda x: (lang(x[0]) not in FR, not is_forced(x[0]), x[0]["id"]))
        forced_fr_ass = next((a for a in ass_files if lang(a[0]) in FR and is_forced(a[0])), None)
        mux = ["mkvmerge", "-o", str(work / "final.mkv")]
        if ass_files and FONT.exists() and not any(a.get("file_name", "").lower().startswith("trebucbd") for a in ei.get("attachments", [])):
            mux += ["--attachment-name", "trebucbd.ttf", "--attachment-mime-type", "font/ttf", "--attach-file", str(FONT)]
        if not has_marker(ei):
            mux += ["--attachment-name", MARKER_NAME, "--attachment-mime-type", "text/plain", "--attach-file", str(MARKER)]
        # drapeaux des pistes d'origine
        if fr_audio:
            for t in audio: mux += ["--default-track-flag", f"{t['id']}:{'yes' if t is fr_audio[0] else 'no'}"]
        if ass_files:
            for t in subs: mux += ["--default-track-flag", f"{t['id']}:no"]
        mux.append(str(enc))
        for t, ass in ass_files:
            isf = is_forced(t); dflt = forced_fr_ass is not None and t is forced_fr_ass[0]
            nm = (name(t) or ("Forcés" if isf else "Complets")) + " (ASS)"
            mux += ["--language", f"0:{lang(t)}", "--track-name", f"0:{nm}", "--default-track-flag", f"0:{'yes' if dflt else 'no'}",
                    "--forced-display-flag", f"0:{'yes' if isf else 'no'}", str(ass)]
        order = [f"0:{t['id']}" for t in tracks if t["type"] == "video"]
        aud = fr_audio + [t for t in audio if t not in fr_audio] if fr_audio else audio
        order += [f"0:{t['id']}" for t in aud]
        order += [f"{i + 1}:0" for i in range(len(ass_files))]
        order += [f"0:{t['id']}" for t in tracks if t["type"] not in ("video", "audio")]
        mux += ["--track-order", ",".join(order)]
        r = run(mux)
        if r.returncode not in (0, 1): fail("mkvmerge : " + (r.stdout + r.stderr)[-400:])
        final = work / "final.mkv"
        log(f"Remux : {len(ass_files)} piste(s) ASS ajoutée(s), audio FR par défaut : {'oui' if fr_audio else 'non (ordre inchangé)'}, "
            f"sous-titre forcé FR par défaut : {'oui' if forced_fr_ass else 'non'}")
    else:
        if not has_marker(mkvinfo(enc)):
            r = run(["mkvpropedit", str(enc), "--attachment-name", MARKER_NAME, "--attachment-mime-type", "text/plain",
                     "--add-attachment", str(MARKER)])
            if r.returncode != 0: fail("marqueur : " + r.stdout[-300:])

    # ---------- contrôles ----------
    pf = probe(final); fi = mkvinfo(final)
    cnt = lambda i, ty: sum(1 for t in i["tracks"] if t["type"] == ty)
    dur1 = float(pf["format"]["duration"])
    if abs(dur0 - dur1) > 1.5: fail(f"durée différente ({dur0:.1f} s -> {dur1:.1f} s)")
    if cnt(fi, "video") != cnt(mi, "video") or cnt(fi, "audio") != cnt(mi, "audio"): fail("nombre de pistes vidéo/audio différent")
    if cnt(fi, "subtitles") < cnt(mi, "subtitles"): fail("des sous-titres ont disparu")
    if not has_marker(fi): fail("marqueur absent")
    if REMUX:   # remux seul : même taille à quelques pour cent près (pistes ASS, police, marqueur) ; un TS, MP4 ou AVI change de conteneur
        ratio = final.stat().st_size / st.st_size
        lo, hi = (0.97, 1.03) if SRC.suffix.lower() == ".mkv" else (0.70, 1.08)
        if not lo <= ratio <= hi: fail(f"taille de sortie inattendue ({ratio * 100:.1f} % de l'original)")
    elif final.stat().st_size >= st.st_size: fail("fichier de sortie plus gros que l'original")
    if POST:
        OUTFILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(final), str(OUTFILE))
        dest = OUTFILE
    else:
        OUTDIR.mkdir(parents=True, exist_ok=True)
        dest = OUTDIR / SRC.name
        shutil.move(str(final), str(dest))
    log(f"OK : {st.st_size // 2**20} Mo -> {dest.stat().st_size // 2**20} Mo (-{100 - dest.stat().st_size * 100 // st.st_size} %) | pistes sous-titres {cnt(mi, 'subtitles')} -> {cnt(fi, 'subtitles')}")
    log(f"Sortie : {dest}")
finally:
    shutil.rmtree(work, ignore_errors=True)
