"""Alertes Telegram de MouFlencoder.

Même mécanisme que MouFlossover : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID et (facultatif) TELEGRAM_THREAD_ID
sont enregistrés dans data/secrets.env (droits 600) depuis la page Réglages. Le jeton n'apparaît jamais dans
les réponses de l'appli ni dans les journaux. Les envois se font dans un fil à part : ils ne ralentissent jamais l'encodage."""
import html, json, os, re, threading, time, urllib.error, urllib.request

API = "https://api.telegram.org"
_TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,50}$")
_CHAT_RE = re.compile(r"^-?\d{3,20}$")
_THREAD_RE = re.compile(r"^\d{1,12}$")
_last = {}
_lock = threading.Lock()


def token(): return os.getenv("TELEGRAM_BOT_TOKEN", "")
def chat(): return os.getenv("TELEGRAM_CHAT_ID", "")
def thread(): return os.getenv("TELEGRAM_THREAD_ID", "")
def configured(): return bool(_TOKEN_RE.match(token())) and bool(_CHAT_RE.match(chat()))
def hint(): return ("…" + token()[-4:]) if len(token()) >= 8 else ""
def token_ok(v): return bool(_TOKEN_RE.match(v or ""))
def chat_ok(v): return bool(_CHAT_RE.match(v or ""))
def thread_ok(v): return bool(_THREAD_RE.match(v or ""))


def send(text, tok=None, chat_id=None, thread_id=None, silent=False, timeout=10):
    """Envoie un message. Renvoie (réussi, message d'erreur sans jamais citer le jeton)."""
    tok, chat_id = tok or token(), chat_id or chat()
    thread_id = thread() if thread_id is None else thread_id
    if not (token_ok(tok) and chat_ok(chat_id)):
        return False, "jeton ou identifiant de conversation manquant ou invalide"
    body = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True, "disable_notification": bool(silent)}
    if thread_id and thread_ok(thread_id) and int(thread_id) != 1:
        body["message_thread_id"] = int(thread_id)
    req = urllib.request.Request(f"{API}/bot{tok}/sendMessage", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r).get("ok", False), ""
    except urllib.error.HTTPError as e:
        try: d = json.load(e).get("description", "")
        except Exception: d = ""
        return False, f"Telegram a refusé ({e.code}) : {d}"[:200]
    except Exception as e:  # noqa: BLE001
        return False, ("connexion à Telegram impossible : " + type(e).__name__)


def alert(text, key=None, gap=0, silent=False):
    """Envoi en arrière-plan. key + gap (secondes) : n'envoie pas deux fois la même alerte trop vite."""
    if not configured():
        return False
    if key:
        with _lock:
            if time.time() - _last.get(key, 0) < gap:
                return False
            _last[key] = time.time()
    threading.Thread(target=lambda: send(text, silent=silent), daemon=True).start()
    return True


def esc(s): return html.escape(str(s))
