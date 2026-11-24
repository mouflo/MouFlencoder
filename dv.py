#!/usr/bin/env python3
"""Chaîne 4K Dolby Vision / HDR10 de MouFlencoder (préparée, désactivée par défaut : réglage enable_4k).

Principe : le GPU réencode seulement l'image de base (HEVC 10 bits). Le Dolby Vision vit dans des paquets à part (RPU) :
on les extrait de l'original avec dovi_tool, puis on les réinjecte dans la nouvelle vidéo, image pour image
(dovi_tool refuse si le nombre d'images ne correspond pas). Les métadonnées HDR10 (mastering, MaxCLL) sont reposées dans
le conteneur. Audio, sous-titres, chapitres et polices sont repris tels quels de l'original.

  dv.py test <fichier> [qp,qp,...]   -> extrait de 60 s à plusieurs qualités, avec audio, pour juger sur la Shield
  dv.py run  <fichier> <sortie.mkv> <qp> [dossier_de_travail]   -> film entier
"""
import json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

BIN = Path(__file__).resolve().parent / "bin"
DOVI, HDR10P = str(BIN / "dovi_tool"), str(BIN / "hdr10plus_tool")
NICE = ["nice", "-n", "19"]


def sh(cmd, **k):
    return subprocess.run(cmd, capture_output=True, text=True, **k)


def info(path):
    """Ce qu'il faut savoir d'un fichier 4K : résolution, cadence, profil Dolby Vision, métadonnées HDR10 / HDR10+."""
    j = json.loads(sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1", "-show_frames", "-show_streams", "-show_format", "-print_format", "json", str(path)]).stdout)
    st = j["streams"][0]
    fr = (j.get("frames") or [{}])[0]
    sd = (fr.get("side_data_list") or []) + (st.get("side_data_list") or [])
    t = lambda name: next((s for s in sd if name in s.get("side_data_type", "")), None)
    dv = t("DOVI configuration")
    return {"w": st["width"], "h": st["height"], "fps": st["r_frame_rate"], "dur": float(j["format"]["duration"]), "pix": st.get("pix_fmt"),
            "dv_profile": dv.get("dv_profile") if dv else None, "dv_compat": dv.get("dv_bl_signal_compatibility_id") if dv else None,
            "mastering": t("Mastering display"), "cll": t("Content light level"), "hdr10plus": t("SMPTE2094-40") is not None,
            "transfer": st.get("color_transfer"), "primaries": st.get("color_primaries"), "matrix": st.get("color_space")}


def _frac(v):
    return float(eval(v)) if "/" in str(v) else float(v)


def mkv_hdr_args(i):
    a = ["--colour-matrix", "0:9", "--colour-range", "0:1", "--colour-transfer-characteristics", "0:16", "--colour-primaries", "0:9"]
    m, c = i.get("mastering"), i.get("cll")
    if m:
        f = lambda k: _frac(m[k])
        a += ["--chromaticity-coordinates", "0:%.5f,%.5f,%.5f,%.5f,%.5f,%.5f" % (f("red_x"), f("red_y"), f("green_x"), f("green_y"), f("blue_x"), f("blue_y")),
              "--white-colour-coordinates", "0:%.5f,%.5f" % (f("white_point_x"), f("white_point_y")), "--max-luminance", "0:%.4f" % f("max_luminance"), "--min-luminance", "0:%.5f" % f("min_luminance")]
    if c:
        a += ["--max-content-light", "0:%d" % int(c["max_content"]), "--max-frame-light", "0:%d" % int(c["max_average"])]
    return a


def gpu_encode(src, out, qp, ss=None, t=None, hw_dec=True):
    """Image de base : décodage et encodage par le GPU Intel, 10 bits, B-frames, qualité constante."""
    pre = ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128", "-hwaccel_output_format", "vaapi"] if hw_dec else ["-init_hw_device", "vaapi=va:/dev/dri/renderD128", "-filter_hw_device", "va"]
    vf = "scale_vaapi=format=p010" if hw_dec else "format=p010,hwupload"
    cmd = NICE + ["ffmpeg", "-hide_banner", "-v", "error", "-y"] + pre + (["-ss", str(ss)] if ss else []) + (["-t", str(t)] if t else []) + ["-i", str(src), "-map", "0:v:0", "-vf", vf,
           "-c:v", "hevc_vaapi", "-low_power", "1", "-profile:v", "main10", "-rc_mode", "CQP", "-qp", str(qp), "-bf", "3", "-b_depth", "3",
           "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-f", "hevc", str(out)]
    r = sh(cmd)
    if r.returncode or not Path(out).exists():
        raise RuntimeError("encodage GPU : " + r.stderr[-200:])


def make_dv(i, base_hevc, rpu_src_hevc, work, out_hevc):
    """Réinjecte le Dolby Vision (et le HDR10+ s'il y en a) dans la nouvelle vidéo."""
    cur = Path(base_hevc)
    if i["hdr10plus"]:
        js = Path(work) / "hdr10plus.json"
        r = sh([HDR10P, "extract", "-i", str(rpu_src_hevc), "-o", str(js)])
        if r.returncode == 0 and js.exists():
            o = Path(work) / "hdr10p.hevc"
            r = sh([HDR10P, "inject", "-i", str(cur), "-j", str(js), "-o", str(o)])
            if r.returncode == 0 and o.exists():
                cur = o
    if i["dv_profile"]:
        rpu = Path(work) / "rpu.bin"
        mode = ["-m", "2"] if i["dv_profile"] == 7 else []   # profil 7 (Blu-ray double couche) converti en 8.1 : compatible partout
        r = sh([DOVI] + mode + ["extract-rpu", "-i", str(rpu_src_hevc), "-o", str(rpu)])
        if r.returncode or not rpu.exists():
            raise RuntimeError("extraction du Dolby Vision : " + (r.stdout + r.stderr)[-200:])
        r = sh([DOVI, "inject-rpu", "-i", str(cur), "--rpu-in", str(rpu), "-o", str(out_hevc)])
        if r.returncode or not Path(out_hevc).exists():
            raise RuntimeError("injection du Dolby Vision (nombre d'images différent ?) : " + (r.stdout + r.stderr)[-200:])
    else:
        shutil.copy(cur, out_hevc)


def mux(i, video_hevc, rest_from, out_mkv, ss=None, t=None):
    """Conteneur final : nouvelle vidéo + tout le reste de l'original."""
    cmd = ["mkvmerge", "-o", str(out_mkv)] + mkv_hdr_args(i) + ["--default-duration", "0:" + i["fps"] + "fps", str(video_hevc), "--no-video", str(rest_from)]
    r = sh(cmd, env=dict(os.environ, LC_ALL="C.UTF-8"))
    if r.returncode > 1:
        raise RuntimeError("mkvmerge : " + (r.stdout + r.stderr)[-200:])


def verify(i, out_mkv):
    o = info(out_mkv)
    probs = []
    if i["dv_profile"] and not o["dv_profile"]:
        probs.append("Dolby Vision absent du résultat")
    if i["mastering"] and not o["mastering"]:
        probs.append("métadonnées de mastering perdues")
    if (o["w"], o["h"]) != (i["w"], i["h"]):
        probs.append("résolution changée")
    if abs(o["dur"] - i["dur"]) > 1.5:
        probs.append("durée changée")
    return probs, o


def process(src, out_mkv, qp, work, ss=None, t=None):
    """Film entier (ss/t : pour un extrait de test). Le dossier de travail doit pouvoir contenir environ la taille du film."""
    work = Path(work); work.mkdir(parents=True, exist_ok=True)
    i = info(src)
    base = work / "base.hevc"
    raw = work / "src.hevc"
    if i["dv_profile"] or i["hdr10plus"]:
        cmd = ["ffmpeg", "-v", "error", "-y"] + (["-ss", str(ss)] if ss else []) + (["-t", str(t)] if t else []) + ["-i", str(src), "-map", "0:v:0", "-c", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(raw)]
        sh(NICE + cmd)
    gpu_encode(src, base, qp, ss, t)
    dv = work / "final.hevc"
    make_dv(i, base, raw, work, dv)
    rest = src
    if ss is not None or t is not None:   # extrait de test : audio de la même portion
        rest = work / "rest.mkv"
        sh(NICE + ["ffmpeg", "-v", "error", "-y", "-ss", str(ss or 0), "-t", str(t or 60), "-i", str(src), "-map", "0:a:0", "-c", "copy", str(rest)])
    mux(i, dv, rest, out_mkv)
    probs, o = verify(i, out_mkv) if ss is None else ([], info(out_mkv))
    return {"in": i, "out": o, "problems": probs, "mbps": Path(out_mkv).stat().st_size * 8 / (t or i["dur"]) / 1e6}


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "test":
        src = sys.argv[2]; qps = [int(x) for x in (sys.argv[3] if len(sys.argv) > 3 else "28,31").split(",")]
        i = info(src)
        print(f"{Path(src).name[:60]} | {i['w']}x{i['h']} | Dolby Vision: {('profil %s' % i['dv_profile']) if i['dv_profile'] else 'non'} | HDR10+: {i['hdr10plus']} | mastering: {bool(i['mastering'])}")
        for q in qps:
            w = Path(tempfile.mkdtemp(prefix="dvt_", dir="/var/tmp"))
            try:
                out = Path("/var/tmp/dv_out") / f"{Path(src).stem[:40]}_qp{q}.mkv"; out.parent.mkdir(exist_ok=True)
                t0 = time.time(); r = process(src, out, q, w, ss=int(i["dur"] / 2), t=60)
                print(f"  qp{q}: {r['mbps']:.1f} Mb/s | {60 / (time.time() - t0):.1f}x | Dolby Vision en sortie: {('profil %s' % r['out']['dv_profile']) if r['out']['dv_profile'] else 'non'} | mastering: {bool(r['out']['mastering'])}")
            except Exception as e:
                print(f"  qp{q}: ÉCHEC {e}")
            finally:
                shutil.rmtree(w, ignore_errors=True)
    elif len(sys.argv) >= 5 and sys.argv[1] == "run":
        r = process(sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5] if len(sys.argv) > 5 else "/var/tmp/dv_work")
        print(json.dumps({"mbps": round(r["mbps"], 2), "problemes": r["problems"]}, ensure_ascii=False))
        shutil.rmtree(sys.argv[5] if len(sys.argv) > 5 else "/var/tmp/dv_work", ignore_errors=True)
    else:
        print(__doc__)
