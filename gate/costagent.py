#!/usr/bin/env python3
"""costagent: Marko's cost guard, the gate every paid call asks first (13.11.2026).

Marko: "Please build a guard to ask me, are you sure you want to recognize images? This will cost you. Are you sure
anything which costs money, it needs to ask me and give me how much money it costs."

    costagent.py serve                 the gate (127.0.0.1 and the Tailscale address; port from the registry)
    costagent.py ask WHO WHAT PROVIDER ESTIMATE_USD [--items N] [--job J] [--timeout S]   exit 0 YES, 1 NO
    costagent.py spent WHO PROVIDER USD [--job J] [--what W] [--items N]                    record a real spend
    costagent.py status                the summary (today / week / month per provider and process)

HTTP (JSON). Local callers need nothing; callers from another machine (the VM over Tailscale) send X-Cost-Token.
    POST /ask    {who, what, provider, estimate_usd, items, job?, timeout?}  ->  {answer: yes|no, reason, id, ...}
    POST /spent  {who, provider, usd, items?, job?, what?}                     ->  {ok}
    GET  /summary  /pending  /history  /health        POST /answer {id, answer}      GET /q/<id>?t=TOKEN (phone page)

The rules (modules/cost-agent.md):
  * NO is the answer to silence: nothing is spent without a YES, a timeout is a NO.
  * One question per job. A YES is a grant for that job's estimate; calls inside the grant pass silently; a job that
    would pass its estimate by more than 20 % asks again. A call with no declared size is a grant of one chunk
    ($0.25 by default) and asks again for the next chunk.
  * A NO is remembered for 10 minutes for that job: the callers fail fast, Marko is not asked 400 times.
  * Free providers pass without a box. Speechify passes inside a daily allowance Marko can see (config.json).
Data (ledgers, answers, token) lives in ~/.mantra-os/cost-agent, never in the repository.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
DATA = os.environ.get("COSTAGENT_DATA") or os.path.join(HOME, ".mantra-os", "cost-agent")
LEDGER = os.path.join(DATA, "ledger.jsonl")
DECISIONS = os.path.join(DATA, "decisions.jsonl")
TOKEN_FILE = os.path.join(DATA, "token")
CONFIG = os.path.join(DATA, "config.json")
PORTS = os.path.join(HOME, ".config", "mantra-ports.json")
NAME, WANTED = "costagent", 8870
APP_BIN = "/Applications/Cost Agent (Mantra).app/Contents/MacOS/CostAgent"
VOICE_ROUTE = os.path.join(HOME, ".voice", "route")

DEFAULTS = {
    "chunk_usd": 0.25,            # a call with no declared size is granted this much, then asks again
    "overrun": 1.2,               # a job may pass its estimate by 20 % before it asks again
    "timeout_s": 120,             # no answer in this time = NO
    "deny_memory_s": 600,         # a NO is remembered this long for its job
    "grant_hours": 12,
    "free_providers": ["free", "edge", "whisper-local", "groq-free", "gemini-web", "github", "cloudflare", "local"],
    "daily_allowance_usd": {"speechify": 0.50},   # his voice replies: this much a day without a question
    "usd_per_unit": {"speechify_char": 0.00001, "groq_vision_image": 0.009, "groq_chat_call": 0.002},
}
LOCK = threading.RLock()
PENDING = {}      # id -> dict(question, event, answer)
GRANTS = {}       # job -> dict(approved, spent, at)
DENIED = {}       # job -> time of the NO


def cfg():
    c = dict(DEFAULTS)
    try:
        c.update(json.load(open(CONFIG)))
    except Exception:
        pass
    return c


def now():
    return datetime.datetime.now().astimezone()


def jappend(path, row):
    os.makedirs(DATA, exist_ok=True)
    row = dict(row)
    row.setdefault("ts", now().isoformat(timespec="seconds"))
    with LOCK, open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def jread(path):
    out = []
    try:
        for line in open(path):
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return out


def token():
    os.makedirs(DATA, exist_ok=True)
    if not os.path.exists(TOKEN_FILE):
        import secrets
        with open(TOKEN_FILE, "w") as f:
            f.write(secrets.token_urlsafe(24))
        os.chmod(TOKEN_FILE, 0o600)
    return open(TOKEN_FILE).read().strip()


def port():
    if os.environ.get("COSTAGENT_PORT"):
        return int(os.environ["COSTAGENT_PORT"])
    try:
        e = json.load(open(PORTS)).get(NAME)
        if e and e.get("port"):
            return int(e["port"])
    except Exception:
        pass
    return WANTED


def base_url():
    return f"http://127.0.0.1:{port()}"


def tailscale_ip():
    try:
        return subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()[0]
    except Exception:
        return ""


# ------------------------------------------------------------------ the question
def money(x):
    return f"${x:.2f}" if x >= 0.01 else f"${x:.4f}"


def today_spent(provider):
    d = now().date().isoformat()
    return sum(r.get("usd", 0) for r in jread(LEDGER) if r.get("provider") == provider and r.get("ts", "")[:10] == d)


def ask_text(q):
    n = f" ({q['items']} {q.get('unit') or 'items'})" if q.get("items") else ""
    extra = ""
    if q.get("reason_ask"):
        extra = " " + q["reason_ask"]
    return (f"Are you sure? This will cost about {money(q['estimate_usd'])}{n} with {q['provider']}. "
            f"{q['who']}: {q['what']}.{extra}")


def away_on_phone():
    try:
        p = open(VOICE_ROUTE).read().split()
        return p[1] if p[:1] == ["phone"] and len(p) > 1 else None
    except Exception:
        return None


def notify_phone(q, qid, adb_target):
    """Marko is away with the Pixel: say the question on its Voice Engine and open the YES / NO page on it."""
    ts = tailscale_ip()
    url = f"http://{ts}:{port()}/q/{qid}?t={token()}" if ts else ""
    say = f"Cost question. {ask_text(q)} Answer yes or no on the screen."
    cmds = [[os.path.join(HOME, ".local/bin/phonesay"), "--phone", adb_target, say, "en"]]
    if url:
        cmds.append(["adb", "-s", adb_target, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url])
    if os.environ.get("COSTAGENT_DRY_PHONE"):
        jappend(DECISIONS, {"event": "phone-dry-run", "id": qid, "cmds": [c[:2] + ["..."] for c in cmds]})
        return
    for c in cmds:
        try:
            subprocess.run(c, timeout=40, capture_output=True)
        except Exception:
            pass


def mac_box(q, qid, timeout):
    """The box on the Mac: the app's own panel (at the mouse, on top) when installed, else a system dialog.
    Returns 'yes' / 'no' / None (not answered)."""
    if os.environ.get("COSTAGENT_NO_UI"):
        return None
    text = ask_text(q)
    if os.path.exists(APP_BIN):
        cmd = [APP_BIN, "--ask", text, "--timeout", str(timeout)]
    else:
        esc = text.replace("\\", "\\\\").replace('"', '\\"')
        script = (f'tell application "System Events" to activate\n'
                  f'tell application "System Events" to display dialog "{esc}" with title "Cost Agent" '
                  f'buttons {{"NO", "YES"}} default button "NO" with icon caution giving up after {timeout}')
        cmd = ["osascript", "-e", script]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except Exception:
        return None
    PENDING[qid]["proc"] = proc
    try:
        out, _ = proc.communicate(timeout=timeout + 5)
    except Exception:
        proc.kill()
        return None
    if PENDING.get(qid, {}).get("answer"):      # answered elsewhere (phone, the app's list)
        return None
    if "YES" in out and "gave up:true" not in out:
        return "yes"
    if "NO" in out:
        return "no"
    return None


def decide(q, qid, timeout):
    ev = PENDING[qid]["event"]
    t = threading.Thread(target=lambda: _box_thread(q, qid, timeout), daemon=True)
    t.start()
    phone = away_on_phone()
    if phone:
        threading.Thread(target=notify_phone, args=(q, qid, phone), daemon=True).start()
    ev.wait(timeout)
    ans = PENDING[qid].get("answer") or "no"
    proc = PENDING[qid].get("proc")
    if proc and proc.poll() is None:
        proc.kill()
    return ans, ("answered" if PENDING[qid].get("answer") else "timeout")


def _box_thread(q, qid, timeout):
    a = mac_box(q, qid, timeout)
    if a:
        answer(qid, a, "mac")


def answer(qid, ans, where):
    with LOCK:
        p = PENDING.get(qid)
        if not p or p.get("answer"):
            return False
        p["answer"] = "yes" if str(ans).lower().startswith("y") else "no"
        p["where"] = where
        p["event"].set()
    return True


# ------------------------------------------------------------------ the decision
def ask(d):
    c = cfg()
    who, what = str(d.get("who", "?")), str(d.get("what", ""))
    provider = str(d.get("provider", "?")).lower()
    est = float(d.get("estimate_usd") or 0)
    items = d.get("items")
    job = str(d.get("job") or f"{who}|{provider}")
    q = {"who": who, "what": what, "provider": provider, "estimate_usd": est, "items": items,
         "unit": d.get("unit"), "job": job}

    def log(answer_, reason, qid=None):
        jappend(DECISIONS, {"event": "ask", "id": qid, "who": who, "what": what, "provider": provider,
                            "estimate_usd": est, "items": items, "job": job, "answer": answer_, "reason": reason})
        return {"answer": answer_, "reason": reason, "id": qid, "job": job}

    if provider in c["free_providers"] or est <= 0 and d.get("free"):
        return log("yes", "free")
    allow = c["daily_allowance_usd"].get(provider)
    if allow is not None and today_spent(provider) + est <= allow:
        return log("yes", f"inside the daily allowance of {money(allow)}")
    with LOCK:
        t = DENIED.get(job)
        if t and time.time() - t < c["deny_memory_s"]:
            return log("no", "Marko said no to this job a moment ago")
        g = GRANTS.get(job)
        if g and time.time() - g["at"] < c["grant_hours"] * 3600 and g["spent"] + est <= g["approved"] * c["overrun"]:
            return log("yes", f"inside the grant of {money(g['approved'])} ({money(g['spent'])} spent)")
        # one open question per job: a second caller of the same job waits for the same answer
        same = next((i for i, p in PENDING.items() if p["q"]["job"] == job and not p.get("answer")), None)
        if same:
            qid, fresh = same, False
        else:
            qid = f"q{int(time.time()*1000)}"
            if est <= 0 or not d.get("job"):
                q["estimate_usd"] = est = max(est, c["chunk_usd"])
                q["reason_ask"] = "(size not declared: one chunk, then it asks again)"
                q["items"] = None
            elif g:
                q["reason_ask"] = f"(this job passed its estimate: {money(g['spent'])} spent of {money(g['approved'])})"
            PENDING[qid] = {"q": q, "event": threading.Event(), "at": time.time()}
            fresh = True
    timeout = int(d.get("timeout") or c["timeout_s"])
    if fresh:
        ans, how = decide(q, qid, timeout)
        with LOCK:
            if ans == "yes":
                GRANTS[job] = {"approved": max(est, q["estimate_usd"]) + (GRANTS.get(job, {}).get("spent", 0) if g else 0),
                               "spent": GRANTS.get(job, {}).get("spent", 0), "at": time.time()}
                DENIED.pop(job, None)
            else:
                DENIED[job] = time.time()
        where = PENDING[qid].get("where", how)
        jappend(DECISIONS, {"event": "answer", "id": qid, "who": who, "what": what, "provider": provider,
                            "estimate_usd": q["estimate_usd"], "items": items, "job": job, "answer": ans, "where": where})
        res = {"answer": ans, "reason": f"Marko said {ans} ({where})" if how == "answered" else "no answer: NO",
               "id": qid, "job": job, "approved_usd": GRANTS.get(job, {}).get("approved")}
        PENDING.pop(qid, None)
        return res
    PENDING[qid]["event"].wait(timeout)
    a = PENDING.get(qid, {}).get("answer") or ("yes" if job in GRANTS and job not in DENIED else "no")
    return {"answer": a, "reason": "shared answer", "id": qid, "job": job}


def spent(d):
    usd = float(d.get("usd") or 0)
    row = {"who": d.get("who", "?"), "provider": str(d.get("provider", "?")).lower(), "usd": usd,
           "items": d.get("items"), "job": d.get("job"), "what": d.get("what", ""), "ref": d.get("ref"),
           "where": d.get("where", "mac")}
    jappend(LEDGER, row)
    job = str(d.get("job") or f"{row['who']}|{row['provider']}")
    with LOCK:
        if job in GRANTS:
            GRANTS[job]["spent"] += usd
    return {"ok": True}


# ------------------------------------------------------------------ the numbers
def queen_rows():
    """Queen's own ledger on this Mac and, when reachable, on the VM: the last 100 calls of each (usd, model, req)."""
    rows = []
    for where, path in (("mac", os.path.join(HOME, ".groq", "spend_ledger.json")),):
        try:
            for h in json.load(open(path)).get("history", []):
                rows.append({"ts": datetime.datetime.fromtimestamp(h["timestamp"]).astimezone().isoformat(timespec="seconds"),
                             "provider": "groq", "who": "queen", "usd": h["cost"], "ref": h.get("req_id"),
                             "what": h.get("model"), "where": where, "src": "queen"})
        except Exception:
            pass
    return rows


_vm_cache = {"t": 0, "rows": [], "total": None}


def vm_rows():
    if time.time() - _vm_cache["t"] < 90:
        return _vm_cache
    _vm_cache["t"] = time.time()
    try:
        out = subprocess.run(["ssh", "-o", "ConnectTimeout=4", "-o", "BatchMode=yes", "ubuntu@chromium-vm",
                              "cat ~/.groq/spend_ledger.json"], capture_output=True, text=True, timeout=10).stdout
        led = json.loads(out)
        _vm_cache["total"] = led.get("total_spent")
        _vm_cache["rows"] = [{"ts": datetime.datetime.fromtimestamp(h["timestamp"]).astimezone().isoformat(timespec="seconds"),
                              "provider": "groq", "who": "queen", "usd": h["cost"], "ref": h.get("req_id"),
                              "what": h.get("model"), "where": "vm", "src": "queen"} for h in led.get("history", [])]
    except Exception:
        pass
    return _vm_cache


def process_of(r):
    w = (r.get("who") or "").lower()
    p = r.get("provider")
    if p == "speechify" or "voice" in w:
        return "voice"
    if "vision" in w or "image" in w or "collect" in w:
        return "images"
    if r.get("where") == "vm":
        return "VM"
    if "queen" in w:
        return "Queen"
    return w or "other"


def all_rows(with_vm=True):
    rows = jread(LEDGER)
    seen = {r.get("ref") for r in rows if r.get("ref")}      # calls the gate already knows are not counted twice
    src = queen_rows() + (vm_rows()["rows"] if with_vm else [])
    rows += [r for r in src if r.get("ref") not in seen]
    return rows


def summary(with_vm=True):
    t = now()
    today = t.date().isoformat()
    week = (t - datetime.timedelta(days=t.weekday())).date().isoformat()
    month = t.strftime("%Y-%m")
    periods = {"today": lambda s: s[:10] == today, "week": lambda s: s[:10] >= week, "month": lambda s: s[:7] == month}
    out = {"now": t.isoformat(timespec="seconds"), "totals": {k: 0.0 for k in periods}, "by_provider": {}, "by_process": {},
           "by_where": {}}
    for r in all_rows(with_vm):
        ts = r.get("ts", "")
        for k, f in periods.items():
            if f(ts):
                u = float(r.get("usd") or 0)
                out["totals"][k] += u
                for key, name in ((r.get("provider", "?"), "by_provider"), (process_of(r), "by_process"),
                                  (r.get("where", "mac"), "by_where")):
                    out[name].setdefault(key, {p: 0.0 for p in periods})[k] += u
    for name in ("totals",):
        out[name] = {k: round(v, 4) for k, v in out[name].items()}
    for name in ("by_provider", "by_process", "by_where"):
        out[name] = {a: {k: round(v, 4) for k, v in b.items()} for a, b in out[name].items()}
    vm = _vm_cache.get("total")
    out["ledger_totals"] = {"queen_mac_total": _safe_total(os.path.join(HOME, ".groq", "spend_ledger.json")),
                            "queen_vm_total": vm}
    c = cfg()
    out["allowances"] = {p: {"daily_usd": a, "today_usd": round(today_spent(p), 4)} for p, a in c["daily_allowance_usd"].items()}
    out["pending"] = [{"id": i, "ask": ask_text(p["q"]), "who": p["q"]["who"], "provider": p["q"]["provider"],
                       "estimate_usd": p["q"]["estimate_usd"], "age_s": int(time.time() - p["at"])}
                      for i, p in PENDING.items() if not p.get("answer")]
    out["grants"] = {j: {"approved": round(g["approved"], 4), "spent": round(g["spent"], 4)} for j, g in GRANTS.items()}
    out["history"] = [r for r in jread(DECISIONS) if r.get("event") == "answer"][-30:][::-1]
    out["credits_ledger"] = "~/.mantra-os/spending/ledger.jsonl (spend, credits: imgtoimg, Suno), kept apart from dollars"
    return out


def _safe_total(path):
    try:
        return json.load(open(path)).get("total_spent")
    except Exception:
        return None


# ------------------------------------------------------------------ HTTP
PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><body style="background:#101010;color:#e8dcc0;
font-family:ui-monospace,monospace;text-align:center;padding:24px"><h2>Cost Agent</h2><p style="font-size:20px">%s</p>
<form method=post action="/answer?t=%s"><input type=hidden name=id value="%s">
<button name=answer value=yes style="font-size:34px;padding:22px 44px;margin:12px;background:#2b6b3a;color:#fff;border:0;border-radius:12px">YES</button>
<button name=answer value=no style="font-size:34px;padding:22px 44px;margin:12px;background:#8a2b22;color:#fff;border:0;border-radius:12px">NO</button></form>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _ok(self, qs=None):
        if self._local():
            return True
        t = self.headers.get("X-Cost-Token") or (qs or {}).get("t", [""])[0]
        return t == token()

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        if "json" in (self.headers.get("Content-Type") or "") or raw.startswith("{"):
            return json.loads(raw or "{}")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path == "/health":
            return self._send(200, json.dumps({"ok": True, "port": port()}))
        if not self._ok(qs):
            return self._send(403, '{"error":"token"}')
        if u.path == "/summary":
            return self._send(200, json.dumps(summary(qs.get("vm", ["1"])[0] != "0")))
        if u.path == "/pending":
            return self._send(200, json.dumps(summary(False)["pending"]))
        if u.path.startswith("/q/"):
            qid = u.path[3:]
            p = PENDING.get(qid)
            if not p or p.get("answer"):
                return self._send(200, "<body style='background:#101010;color:#e8dcc0'><h2>Answered already.</h2>", "text/html")
            return self._send(200, PAGE % (ask_text(p["q"]).replace("<", ""), token(), qid), "text/html")
        self._send(404, "{}")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if not self._ok(qs):
            return self._send(403, '{"error":"token"}')
        try:
            d = self._body()
            if u.path == "/ask":
                return self._send(200, json.dumps(ask(d)))
            if u.path == "/spent":
                return self._send(200, json.dumps(spent(d)))
            if u.path == "/answer":
                ok = answer(d.get("id"), d.get("answer", "no"), "phone" if not self._local() else "app")
                ctype = "text/html" if "form" in (self.headers.get("Content-Type") or "") else "application/json"
                return self._send(200, "<body style='background:#101010;color:#e8dcc0;font-size:30px;text-align:center'>Thank you, answered."
                                  if ctype == "text/html" else json.dumps({"ok": ok}), ctype)
        except Exception as exc:
            return self._send(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        self._send(404, "{}")


def serve():
    sys.path.insert(0, os.path.join(HOME, ".local", "bin"))
    p = port()
    try:
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader("mports", os.path.join(HOME, ".local/bin/mantra-ports"))
        spec = importlib.util.spec_from_loader("mports", loader)
        m = importlib.util.module_from_spec(spec)
        loader.exec_module(m)
        p = p if os.environ.get("COSTAGENT_PORT") else m.claim(NAME, WANTED)
    except Exception:
        pass
    token()
    srv = ThreadingHTTPServer(("0.0.0.0", p), H)
    print(f"costagent on :{p}", flush=True)
    srv.serve_forever()


# ------------------------------------------------------------------ CLI client
def call(path, data=None, timeout=700):
    req = urllib.request.Request(base_url() + path, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def main():
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    flags = {}
    pos = []
    i = 0
    while i < len(a):
        if a[i].startswith("--") and i + 1 < len(a):
            flags[a[i][2:]] = a[i + 1]
            i += 2
        else:
            pos.append(a[i])
            i += 1
    if pos[0] == "serve":
        serve()
    elif pos[0] == "ask" and len(pos) >= 5:
        r = call("/ask", {"who": pos[1], "what": pos[2], "provider": pos[3], "estimate_usd": float(pos[4]),
                          "items": int(flags["items"]) if "items" in flags else None, "job": flags.get("job"),
                          "timeout": int(flags["timeout"]) if "timeout" in flags else None})
        print(r["answer"].upper(), "-", r["reason"])
        sys.exit(0 if r["answer"] == "yes" else 1)
    elif pos[0] == "spent" and len(pos) >= 4:
        print(call("/spent", {"who": pos[1], "provider": pos[2], "usd": float(pos[3]), "job": flags.get("job"),
                              "what": flags.get("what", ""), "items": flags.get("items")}))
    elif pos[0] == "status":
        s = call("/summary")
        print(json.dumps(s, indent=1))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
