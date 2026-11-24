"""Réglage de la clé API Emby depuis la page web, comme dans MouFlip (sans dépendance externe).

- La clé est enregistrée dans data/secrets.env (jamais sur GitHub), droits 600
- « Tester » vérifie la clé auprès d'Emby avant de l'enregistrer
- La clé n'est jamais renvoyée à la page (seulement ses 4 derniers caractères)
- Les routes /api/... sont protégées par la connexion de l'appli
"""
import json, os, re, urllib.error, urllib.request
from pathlib import Path

from flask import jsonify, request

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_HOST_RE = re.compile(r"^https?://[A-Za-z0-9._-]+(:\d{2,5})?$")


def write_secret(file, name, value):
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    lines = file.read_text(encoding="utf-8").splitlines() if file.exists() else []
    lines = [l for l in lines if not l.startswith(name + "=")]
    lines.append(f'{name}="{value}"')
    tmp = file.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, file)


def load_env(file):
    """Charge data/secrets.env dans l'environnement (lignes NOM='valeur' ou NOM="valeur")."""
    try:
        for line in Path(file).read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(['\"])(.*)\2$", line.strip())
            if m:
                os.environ.setdefault(m.group(1), m.group(3))
    except OSError:
        pass


def _check(host, key):
    try:
        req = urllib.request.Request(host.rstrip("/") + "/System/Info", headers={"X-Emby-Token": key})
        with urllib.request.urlopen(req, timeout=8) as r:
            try:
                name = json.loads(r.read().decode()).get("ServerName") or "Emby"
            except ValueError:
                name = "Emby"
        return True, f"Clé acceptée par « {name} »"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "Emby refuse cette clé"
        return False, f"Emby a répondu {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, f"Emby injoignable ({host})"


def init_app(app, base_dir, get_host, set_host, on_change=None):
    secrets_file = Path(base_dir) / "data" / "secrets.env"

    @app.route("/api/settings/emby")
    def emby_state():
        key = os.getenv("EMBY_API_KEY", "").strip()
        return jsonify({"host": get_host(), "configured": bool(key), "hint": ("…" + key[-4:]) if len(key) >= 8 else ""})

    @app.route("/api/settings/emby", methods=["POST"])
    def emby_save():
        body = request.get_json(silent=True) or {}
        host = str(body.get("host") or get_host()).strip().rstrip("/")
        if not _HOST_RE.match(host):
            return jsonify({"ok": False, "error": "Adresse invalide : par exemple http://192.168.1.10:8096"}), 400
        key = str(body.get("api_key", "")).strip() or os.getenv("EMBY_API_KEY", "").strip()
        if not _KEY_RE.match(key):
            return jsonify({"ok": False, "error": "Clé invalide : lettres et chiffres uniquement (colle-la sans espace ni guillemets)."}), 400
        ok, msg = _check(host, key)
        if body.get("test"):
            return jsonify({"ok": ok, "message": msg}), 200
        if not ok and not body.get("force"):
            return jsonify({"ok": False, "error": msg + ". Rien n'a été enregistré."}), 400
        if body.get("api_key"):
            write_secret(secrets_file, "EMBY_API_KEY", key)
            os.environ["EMBY_API_KEY"] = key
        set_host(host)
        if on_change:
            on_change()
        return jsonify({"ok": True, "message": "Enregistré. " + msg})
