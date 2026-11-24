#!/usr/bin/env python3
"""Estimation à la louche du gain d'un fichier, à plusieurs niveaux de qualité.
Usage : estimate.py <fichier> [qp,qp,...]      Affiche une ligne JSON : {"gains": {"20": 0.41, ...}, "orig_mb": N}
Trois extraits de 40 s (à 20, 50 et 80 % de la durée) sont encodés par le GPU à chaque qp, puis comparés à la vidéo d'origine copiée.
Précision attendue : environ ±8 points."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

SRC = Path(sys.argv[1])
QPS = [int(x) for x in (sys.argv[2] if len(sys.argv) > 2 else "20,23,26,29,32").split(",")]
SEG = 40


def _hw():
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,pix_fmt", "-of", "csv=p=0", str(SRC)], capture_output=True, text=True)
    c = (r.stdout.strip().split(",") + ["", ""])[:2]
    return (c[0] == "h264" and c[1] == "yuv420p") or (c[0] == "hevc" and c[1] in ("yuv420p", "yuv420p10le"))


HW = _hw()
NICE = ["nice", "-n", "19", "ionice", "-c3"]


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def main():
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(SRC)])
    try:
        dur = float(r.stdout.strip())
    except ValueError:
        print(json.dumps({"error": "durée illisible"})); return 1
    tot = SRC.stat().st_size
    tmp = Path(tempfile.mkdtemp(prefix="mfe_est_", dir="/var/tmp"))
    try:
        starts = [f"{dur * f:.0f}" for f in (0.2, 0.5, 0.8)]
        cp = 0
        for i, ss in enumerate(starts):
            a = tmp / f"c{i}.mkv"
            run(NICE + ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-ss", ss, "-t", str(SEG), "-i", str(SRC), "-map", "0:v:0", "-an", "-sn", "-c", "copy", str(a)])
            if a.exists(): cp += a.stat().st_size
        if cp == 0:
            print(json.dumps({"error": "extraits illisibles"})); return 1
        vid = min(cp / (3 * SEG) * dur, tot)
        gains = {}
        for qp in QPS:
            enc = 0
            for i, ss in enumerate(starts):
                b = tmp / f"e{qp}_{i}.mkv"
                for hw in ((True, False) if HW else (False,)):
                    pre = (["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128", "-hwaccel_output_format", "vaapi"] if hw
                           else ["-init_hw_device", "vaapi=va:/dev/dri/renderD128", "-filter_hw_device", "va"])
                    vf = "scale_vaapi=format=p010" if hw else "format=p010,hwupload"
                    r = run(NICE + ["ffmpeg", "-hide_banner", "-v", "error", "-y"] + pre + ["-ss", ss, "-t", str(SEG), "-i", str(SRC), "-map", "0:v:0", "-an", "-sn",
                                    "-vf", vf, "-c:v", "hevc_vaapi", "-low_power", "1", "-profile:v", "main10", "-rc_mode", "CQP", "-qp", str(qp), str(b)])
                    if r.returncode == 0 and b.exists():
                        break
                if b.exists(): enc += b.stat().st_size
                b.unlink(missing_ok=True)
            if enc == 0:
                continue
            new = tot - vid + vid * (enc / cp)
            gains[str(qp)] = round(max(0.0, 1 - new / tot), 3)
        if not gains:
            print(json.dumps({"error": "encodage d'essai impossible"})); return 1
        print(json.dumps({"gains": gains, "orig_mb": round(tot / 2**20)}))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


sys.exit(main())
