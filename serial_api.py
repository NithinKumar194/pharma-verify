#!/usr/bin/env python3
"""
serial_api.py — pharma serial-verification service (REST API + SQLite).

This is the SOURCE OF TRUTH of the loop. The QR on a pack carries an identity
(GTIN + serial + batch + dates); this service stores the authoritative record
per serial and answers, in real time, "is this serial genuine?" with checks for
unknown (counterfeit), expired, not-yet-active, already-dispensed, and duplicate
(possible clone) — logging every scan for audit.

Endpoints:
  POST /commission   create N serials for a batch -> status COMMISSIONED
  POST /activate     mark serials ACTIVE (after print + inline verify pass)
  GET  /verify/{sn}  HOT PATH: verify a scanned serial -> verdict + record
  POST /dispense/{sn} mark a serial DISPENSED (point of sale)
  GET  /serial/{sn}  full admin record
  GET  /stats        batch/status counts
  GET  /health       liveness

Run (production-ish):
    pip install fastapi "uvicorn[standard]" pydantic
    uvicorn serial_api:app --host 0.0.0.0 --port 8077 --workers 1
    # docs at http://127.0.0.1:8077/docs

DB path via env PHARMA_DB (default ./pharma_serials.db). SQLite is fine for
moderate throughput; for high volume / many writers, point the same schema at
PostgreSQL.
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request, Header, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("PHARMA_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "pharma_serials.db"))
# Manufacturer API key. Write actions (commission/activate/dispense/recall) require it.
# Set PHARMA_ADMIN_KEY in the environment for production. If unset, writes are OPEN (dev only)
# and the server prints a clear warning at startup.
ADMIN_KEY = os.environ.get("PHARMA_ADMIN_KEY", "").strip()


def require_admin(x_api_key: str = Header(default="")):
    """Gate manufacturer-only endpoints. No key configured => open (dev), but warned."""
    if not ADMIN_KEY:
        return  # dev mode: no key set
    if x_api_key != ADMIN_KEY:
        raise HTTPException(401, "invalid or missing X-API-Key (manufacturer key required)")

# unambiguous alphabet (no 0/O/1/I) for human-safe, hard-to-guess serials
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_SERIAL_LEN = 16

_write_lock = threading.Lock()
_conn: sqlite3.Connection = None  # type: ignore


# ---------------------------------------------------------------- db setup
def _connect() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")      # concurrent reads, durable writes
    c.execute("PRAGMA synchronous=NORMAL;")
    c.execute("PRAGMA foreign_keys=ON;")
    return c


def _init_db(c: sqlite3.Connection):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS batches (
        batch_id     TEXT PRIMARY KEY,
        gtin         TEXT NOT NULL,
        batch_no     TEXT NOT NULL,
        mfg_date     TEXT,
        exp_date     TEXT,
        product_name TEXT,
        link         TEXT,
        qty          INTEGER NOT NULL,
        created_at   TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS serials (
        serial       TEXT PRIMARY KEY,
        gtin         TEXT NOT NULL,
        batch_no     TEXT NOT NULL,
        batch_id     TEXT NOT NULL,
        mfg_date     TEXT,
        exp_date     TEXT,
        status       TEXT NOT NULL,          -- COMMISSIONED|ACTIVE|DISPENSED|VOID
        created_at   TEXT NOT NULL,
        activated_at TEXT,
        dispensed_at TEXT,
        first_seen_at TEXT,
        scan_count   INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
    );
    CREATE TABLE IF NOT EXISTS scans (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        serial     TEXT,
        scanned_at TEXT NOT NULL,
        verdict    TEXT NOT NULL,
        client     TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_serials_batch ON serials(batch_id);
    CREATE INDEX IF NOT EXISTS idx_scans_serial ON scans(serial);
    """)
    c.commit()


def _gen_serial() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_SERIAL_LEN))


# ---------------------------------------------------------------- models
class CommissionReq(BaseModel):
    gtin: str = Field(..., min_length=8, max_length=14)
    batch_no: str
    qty: int = Field(..., gt=0, le=2_000_000)
    mfg_date: Optional[str] = None       # 'YYYY-MM'
    exp_date: Optional[str] = None       # 'YYYY-MM'
    product_name: Optional[str] = None
    link: Optional[str] = None


class CommissionResp(BaseModel):
    batch_id: str
    gtin: str
    batch_no: str
    qty: int
    serials: List[str]


class ActivateReq(BaseModel):
    serials: Optional[List[str]] = None
    batch_id: Optional[str] = None       # activate a whole batch


# ---------------------------------------------------------------- app
app = FastAPI(title="Pharma Serial Verification API", version="1.0")

# initialise the DB connection at import time (works for uvicorn AND tests)
_conn = _connect()
_init_db(_conn)
if not ADMIN_KEY:
    print("WARNING: PHARMA_ADMIN_KEY not set — write endpoints (commission/activate/dispense/recall) "
          "are OPEN. Set PHARMA_ADMIN_KEY in the environment for production.", file=sys.stderr)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expired(exp: Optional[str]) -> bool:
    if not exp:
        return False
    m = re.match(r"(\d{4})-(\d{1,2})", exp)
    if not m:
        return False
    y, mo = int(m.group(1)), int(m.group(2))
    t = date.today()
    return (y, mo) < (t.year, t.month)


# ---------------------------------------------------------------- commissioning
@app.post("/commission", response_model=CommissionResp)
def commission(req: CommissionReq, _=Depends(require_admin)):
    batch_id = f"{req.batch_no}-{secrets.token_hex(3)}"
    now = _now()
    serials: List[str] = []
    with _write_lock:
        with closing(_conn.cursor()) as cur:
            cur.execute("""INSERT INTO batches
                (batch_id,gtin,batch_no,mfg_date,exp_date,product_name,link,qty,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                        (batch_id, req.gtin, req.batch_no, req.mfg_date, req.exp_date,
                         req.product_name, req.link, req.qty, now))
            made = 0
            while made < req.qty:
                sn = _gen_serial()
                try:
                    cur.execute("""INSERT INTO serials
                        (serial,gtin,batch_no,batch_id,mfg_date,exp_date,status,created_at)
                        VALUES (?,?,?,?,?,?, 'COMMISSIONED', ?)""",
                                (sn, req.gtin, req.batch_no, batch_id,
                                 req.mfg_date, req.exp_date, now))
                    serials.append(sn)
                    made += 1
                except sqlite3.IntegrityError:
                    continue                      # extremely rare serial collision -> retry
        _conn.commit()
    return CommissionResp(batch_id=batch_id, gtin=req.gtin, batch_no=req.batch_no,
                          qty=req.qty, serials=serials)


@app.post("/activate")
def activate(req: ActivateReq, _=Depends(require_admin)):
    now = _now()
    with _write_lock:
        cur = _conn.cursor()
        if req.batch_id:
            cur.execute("""UPDATE serials SET status='ACTIVE', activated_at=?
                           WHERE batch_id=? AND status='COMMISSIONED'""", (now, req.batch_id))
        elif req.serials:
            cur.executemany("""UPDATE serials SET status='ACTIVE', activated_at=?
                               WHERE serial=? AND status='COMMISSIONED'""",
                            [(now, s) for s in req.serials])
        else:
            raise HTTPException(400, "provide serials[] or batch_id")
        n = cur.rowcount
        _conn.commit()
    return {"activated": n}


# ---------------------------------------------------------------- HOT PATH: verify
@app.get("/verify/{serial}")
def verify(serial: str, client: Optional[str] = None):
    """Real-time verification. One indexed read + one logged scan.
    scan_count (used for clone/duplicate detection) is bumped ONLY for genuine,
    active, in-date scans — not for unknown/expired/not-active checks."""
    row = _conn.execute("SELECT * FROM serials WHERE serial=?", (serial,)).fetchone()
    now = _now()

    if row is None:
        with _write_lock:
            _conn.execute("INSERT INTO scans(serial,scanned_at,verdict,client) VALUES(?,?,?,?)",
                          (serial, now, "UNKNOWN", client))
            _conn.commit()
        return {"serial": serial, "verdict": "UNKNOWN", "authentic": False,
                "reason": "serial not found — not a genuine code", "record": None}

    status = row["status"]
    prior = row["scan_count"]
    if status == "VOID":
        verdict, authentic, reason = "VOID", False, "serial voided/recalled"
    elif status == "COMMISSIONED":
        verdict, authentic, reason = "NOT_ACTIVE", False, "code not activated (should not be in market)"
    elif _expired(row["exp_date"]):
        verdict, authentic, reason = "EXPIRED", False, f"expired {row['exp_date']}"
    elif status == "DISPENSED":
        verdict, authentic, reason = "ALREADY_DISPENSED", False, \
            "already dispensed — re-scan in market suggests a clone"
    elif prior > 0:
        verdict, authentic, reason = "DUPLICATE", True, \
            f"genuine but already scanned {prior}x — verify it is not a copied code"
    else:
        verdict, authentic, reason = "VALID", True, "genuine, active, in date"

    genuine_active = verdict in ("VALID", "DUPLICATE")
    with _write_lock:
        if genuine_active:
            _conn.execute("""UPDATE serials
                             SET scan_count=scan_count+1,
                                 first_seen_at=COALESCE(first_seen_at,?)
                             WHERE serial=?""", (now, serial))
        _conn.execute("INSERT INTO scans(serial,scanned_at,verdict,client) VALUES(?,?,?,?)",
                      (serial, now, verdict, client))
        _conn.commit()

    record = {"gtin": row["gtin"], "batch_no": row["batch_no"],
              "mfg_date": row["mfg_date"], "exp_date": row["exp_date"],
              "status": status, "scan_count": prior + (1 if genuine_active else 0)}
    return {"serial": serial, "verdict": verdict, "authentic": authentic,
            "reason": reason, "duplicate": verdict == "DUPLICATE", "record": record}


@app.post("/dispense/{serial}")
def dispense(serial: str, _=Depends(require_admin)):
    with _write_lock:
        cur = _conn.cursor()
        cur.execute("""UPDATE serials SET status='DISPENSED', dispensed_at=?
                       WHERE serial=? AND status IN ('ACTIVE','COMMISSIONED')""",
                    (_now(), serial))
        n = cur.rowcount
        _conn.commit()
    if not n:
        raise HTTPException(404, "serial not found or not dispensable")
    return {"serial": serial, "status": "DISPENSED"}


@app.post("/recall/{batch_id}")
def recall(batch_id: str, _=Depends(require_admin)):
    """Recall/void an entire batch. Every serial becomes VOID; scans then read RECALLED/VOID."""
    with _write_lock:
        cur = _conn.cursor()
        cur.execute("UPDATE serials SET status='VOID' WHERE batch_id=?", (batch_id,))
        n = cur.rowcount
        _conn.commit()
    if not n:
        raise HTTPException(404, "batch_id not found")
    return {"batch_id": batch_id, "voided_serials": n, "status": "RECALLED"}


@app.get("/batches")
def list_batches(_=Depends(require_admin)):
    """Manufacturer view: every batch with pack counts and scan totals."""
    rows = _conn.execute("""
        SELECT b.batch_id, b.gtin, b.batch_no, b.product_name, b.mfg_date, b.exp_date,
               b.qty, b.created_at,
               (SELECT COUNT(*) FROM serials s WHERE s.batch_id=b.batch_id) AS packs,
               (SELECT COUNT(*) FROM serials s WHERE s.batch_id=b.batch_id AND s.status='ACTIVE') AS active,
               (SELECT COUNT(*) FROM serials s WHERE s.batch_id=b.batch_id AND s.status='VOID') AS voided,
               (SELECT COALESCE(SUM(s.scan_count),0) FROM serials s WHERE s.batch_id=b.batch_id) AS scans
        FROM batches b ORDER BY b.created_at DESC""").fetchall()
    return {"batches": [dict(r) for r in rows]}


@app.get("/serial/{serial}")
def get_serial(serial: str):
    row = _conn.execute("SELECT * FROM serials WHERE serial=?", (serial,)).fetchone()
    if row is None:
        raise HTTPException(404, "not found")
    return dict(row)


@app.get("/stats")
def stats():
    rows = _conn.execute("SELECT status, COUNT(*) n FROM serials GROUP BY status").fetchall()
    scans = _conn.execute("SELECT COUNT(*) n FROM scans").fetchone()["n"]
    batches = _conn.execute("SELECT COUNT(*) n FROM batches").fetchone()["n"]
    return {"by_status": {r["status"]: r["n"] for r in rows},
            "total_scans": scans, "batches": batches}


@app.get("/health")
def health():
    return {"ok": True, "db": os.path.basename(DB_PATH)}


# ---------------------------------------------------------------- phone details page
# The QR carries a GS1 Digital Link pointing at THIS server, e.g.
#   http://192.168.1.5:8077/p/01/<gtin>/10/<batch>/21/<serial>?11=260201&17=300131&mrp=32.12&lic=...&pn=...
# Any phone that scans the QR opens this page and sees the pack details + live verdict.
_MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _nice_ym(v: Optional[str]) -> str:
    if not v:
        return "-"
    d = re.sub(r"\D", "", str(v))
    if len(d) >= 4:                                     # YYMMDD or YYYY-MM digits
        if len(d) == 6 and v.count("-") == 0:           # 260201 (YYMMDD)
            yy, mm = int(d[:2]), int(d[2:4])
            if 1 <= mm <= 12:
                return f"{_MON[mm]}-{2000 + yy}"
        if "-" in str(v):                               # 2026-02
            try:
                y, m = str(v).split("-")[:2]
                return f"{_MON[int(m)]}-{y}"
            except (ValueError, IndexError):
                pass
    return str(v)


@app.get("/p/{path:path}", response_class=HTMLResponse)
def pack_page(path: str, request: Request = None):
    """Human details page for a scanned GS1 Digital Link QR."""
    segs = [s for s in path.split("/") if s]
    ais = {}
    for i in range(0, len(segs) - 1, 2):
        if re.fullmatch(r"\d{2,4}", segs[i]):
            ais[segs[i]] = segs[i + 1]
    q = dict(request.query_params) if request is not None else {}
    for k, v in q.items():
        if re.fullmatch(r"\d{2,4}", k):
            ais[k] = v
    gtin, batch, serial = ais.get("01"), ais.get("10"), ais.get("21")
    mrp = q.get("mrp"); lic = q.get("lic"); pn = q.get("pn")
    mfg = ais.get("11"); exp = ais.get("17")

    verdict, reason, rec = None, "", None
    if serial:
        row = _conn.execute("SELECT * FROM serials WHERE serial=?", (serial,)).fetchone()
        now = _now()
        if row is None:
            verdict, reason = "UNKNOWN", "serial not found — not a genuine code"
            with _write_lock:
                _conn.execute("INSERT INTO scans(serial,scanned_at,verdict,client) VALUES(?,?,?,?)",
                              (serial, now, "UNKNOWN", "phone")); _conn.commit()
        else:
            prior = row["scan_count"]; status = row["status"]
            if status == "VOID":
                verdict, reason = "VOID", "recalled"
            elif status == "COMMISSIONED":
                verdict, reason = "NOT_ACTIVE", "not yet released"
            elif _expired(row["exp_date"]):
                verdict, reason = "EXPIRED", f"expired {row['exp_date']}"
            elif prior > 0:
                verdict, reason = "DUPLICATE", f"already scanned {prior}x — possible copy"
            else:
                verdict, reason = "GENUINE", "genuine, active, in date"
            if verdict in ("GENUINE", "DUPLICATE"):
                with _write_lock:
                    _conn.execute("""UPDATE serials SET scan_count=scan_count+1,
                                     first_seen_at=COALESCE(first_seen_at,?) WHERE serial=?""",
                                  (now, serial))
                    _conn.execute("INSERT INTO scans(serial,scanned_at,verdict,client) VALUES(?,?,?,?)",
                                  (serial, now, verdict, "phone")); _conn.commit()
            rec = row
            mfg = mfg or row["mfg_date"]; exp = exp or row["exp_date"]
            batch = batch or row["batch_no"]; gtin = gtin or row["gtin"]
            b = _conn.execute("SELECT product_name FROM batches WHERE batch_id=?",
                              (row["batch_id"],)).fetchone()
            if b and b["product_name"] and not pn:
                pn = b["product_name"]

    ok = verdict in ("GENUINE", "DUPLICATE")
    col = "#16a34a" if verdict == "GENUINE" else ("#d97706" if verdict == "DUPLICATE" else "#dc2626")
    badge = verdict or "NO SERIAL"
    rows = ""
    for label, val in [("Product", pn), ("GTIN / UPIN", gtin), ("Batch no.", batch),
                       ("Mfg date", _nice_ym(mfg)), ("Expiry date", _nice_ym(exp)),
                       ("MRP", f"Rs. {mrp}" if mrp else None), ("Mfg licence no.", lic)]:
        if val and val != "-":
            rows += f'<div class="r"><span>{label}</span><b>{val}</b></div>'
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pack details</title><style>
body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#f1f5f9;color:#0f172a}}
.w{{max-width:440px;margin:0 auto;padding:20px}}
.card{{background:#fff;border-radius:16px;padding:22px;box-shadow:0 4px 14px rgba(0,0,0,.08)}}
.badge{{display:inline-block;font-weight:800;font-size:15px;padding:7px 16px;border-radius:24px;
color:#fff;background:{col};margin-bottom:6px}}
.reason{{color:#475569;font-size:13px;margin-bottom:14px}}
.r{{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #e2e8f0;font-size:14.5px}}
.r span{{color:#64748b}} .r b{{text-align:right}}
.foot{{color:#94a3b8;font-size:11px;margin-top:14px;text-align:center}}
h1{{font-size:17px;margin:0 0 12px}}</style></head><body><div class="w"><div class="card">
<h1>Medicine pack details</h1>
<div class="badge">{badge}</div>
<div class="reason">{reason}</div>
{rows if rows else '<div class="reason">No details in this code.</div>'}
</div><div class="foot">Scanned via local verification server · serial checked live</div></div></body></html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------- shareable QR page
# Copy-paste link:  http(s)://<host>/qr/<serial>
# Anyone who opens it (any phone/laptop, anywhere the host is reachable) sees the QR code
# for that pack + a tap-to-open details link. Scanning the QR opens the same /p details page,
# so both paths show identical data. The link/QR are built from the address the visitor used
# (request.base_url), so it automatically works with a LAN IP or a public tunnel URL.
try:
    import segno as _segno
    import io as _io, base64 as _b64
    _HAS_SEGNO = True
except ImportError:
    _HAS_SEGNO = False

import json as _json


def _yymmdd_q(ym: Optional[str]) -> Optional[str]:
    m = re.fullmatch(r"(\d{4})-(\d{2})", ym or "")
    return f"{m.group(1)[2:]}{m.group(2)}01" if m else None


@app.get("/qr/{serial}", response_class=HTMLResponse)
def qr_share(serial: str, request: Request):
    row = _conn.execute("SELECT * FROM serials WHERE serial=?", (serial,)).fetchone()
    if row is None:
        return HTMLResponse("<h3 style='font-family:sans-serif'>Unknown serial.</h3>", status_code=404)
    b = _conn.execute("SELECT product_name, link FROM batches WHERE batch_id=?",
                      (row["batch_id"],)).fetchone()
    extra = {}
    if b and b["link"]:
        try:
            extra = _json.loads(b["link"])
        except (ValueError, TypeError):
            extra = {}
    base = str(request.base_url).rstrip("/")             # exactly how the visitor reached us
    url = f"{base}/p/01/{row['gtin']}/10/{row['batch_no']}/21/{serial}"
    q = []
    if _yymmdd_q(row["mfg_date"]):
        q.append(f"11={_yymmdd_q(row['mfg_date'])}")
    if _yymmdd_q(row["exp_date"]):
        q.append(f"17={_yymmdd_q(row['exp_date'])}")
    if extra.get("mrp"):
        q.append(f"mrp={extra['mrp']}")
    if extra.get("lic"):
        q.append(f"lic={extra['lic']}")
    pn = (b["product_name"] if b else None) or extra.get("pn")
    if pn:
        from urllib.parse import quote as _quote
        q.append(f"pn={_quote(str(pn))}")
    if q:
        url += "?" + "&".join(q)

    if _HAS_SEGNO:
        buf = _io.BytesIO()
        _segno.make_qr(url, error="m").save(buf, kind="png", scale=8, border=3)
        qr_img = f'<img src="data:image/png;base64,{_b64.b64encode(buf.getvalue()).decode()}" ' \
                 f'style="width:260px;height:260px;image-rendering:pixelated" alt="QR">'
    else:
        qr_img = "<div style='color:#b91c1c'>segno not installed on server: pip install segno</div>"

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pack QR</title><style>
body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#f1f5f9;color:#0f172a}}
.w{{max-width:440px;margin:0 auto;padding:22px;text-align:center}}
.card{{background:#fff;border-radius:16px;padding:24px;box-shadow:0 4px 14px rgba(0,0,0,.08)}}
h1{{font-size:17px;margin:0 0 4px}} .sub{{color:#64748b;font-size:13px;margin-bottom:14px}}
a.btn{{display:inline-block;margin-top:14px;background:#2563eb;color:#fff;text-decoration:none;
padding:11px 20px;border-radius:10px;font-size:14.5px}}
.foot{{color:#94a3b8;font-size:11px;margin-top:12px}}</style></head><body><div class="w">
<div class="card"><h1>{pn or 'Medicine pack'}</h1>
<div class="sub">Batch {row['batch_no']} · scan the QR or tap the button — both show the same details</div>
{qr_img}<br><a class="btn" href="{url}">Open pack details</a></div>
<div class="foot">Share this page's link — anyone who can reach this server sees this same QR.</div>
</div></body></html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------- manufacturer dashboard
# Served WITHOUT auth (it is just an empty shell); the page asks for the manufacturer key and
# uses it for the data calls (/batches, /recall), which ARE gated. Open /admin in a browser.
@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Manufacturer Dashboard</title>
<style>
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}
.wrap{max-width:1000px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 4px} .sub{color:#94a3b8;font-size:13px;margin-bottom:18px}
.key{display:flex;gap:8px;margin-bottom:18px}
input{flex:1;padding:10px 12px;border-radius:8px;border:1px solid #334155;background:#1e293b;color:#fff;font-size:14px}
button{padding:10px 16px;border:0;border-radius:8px;background:#2563eb;color:#fff;font-size:14px;cursor:pointer}
button:hover{background:#1d4ed8} .danger{background:#dc2626} .danger:hover{background:#b91c1c}
.cards{display:flex;gap:12px;margin-bottom:18px;flex-wrap:wrap}
.card{background:#1e293b;border-radius:12px;padding:14px 18px;min-width:130px}
.card .n{font-size:24px;font-weight:800} .card .l{color:#94a3b8;font-size:12px}
table{width:100%;border-collapse:collapse;background:#1e293b;border-radius:12px;overflow:hidden}
th,td{padding:10px 12px;text-align:left;font-size:13px;border-bottom:1px solid #334155}
th{background:#0b1220;color:#94a3b8} tr:last-child td{border-bottom:0}
.msg{color:#f87171;font-size:13px;margin:8px 0} .ok{color:#4ade80}
.badge{padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700}
.b-active{background:#166534;color:#dcfce7} .b-void{background:#7f1d1d;color:#fee2e2}
</style></head><body><div class="wrap">
<h1>Manufacturer Dashboard</h1>
<div class="sub">Enter your manufacturer key to view batches, scan counts, and issue recalls.</div>
<div class="key">
  <input id="k" type="password" placeholder="Manufacturer key (PHARMA_ADMIN_KEY)"
         onkeydown="if(event.key==='Enter')load()">
  <button onclick="load()">Load</button>
</div>
<div id="msg" class="msg"></div>
<div class="cards" id="cards" style="display:none">
  <div class="card"><div class="n" id="c-batches">0</div><div class="l">Batches</div></div>
  <div class="card"><div class="n" id="c-packs">0</div><div class="l">Total packs</div></div>
  <div class="card"><div class="n" id="c-scans">0</div><div class="l">Total scans</div></div>
  <div class="card"><div class="n" id="c-void">0</div><div class="l">Voided (recalled)</div></div>
</div>
<table id="tbl" style="display:none"><thead><tr>
<th>Product</th><th>Batch</th><th>Packs</th><th>Active</th><th>Scans</th><th>Mfg</th><th>Exp</th><th>Action</th>
</tr></thead><tbody id="rows"></tbody></table>
<script>
let KEY="";
async function load(){
  KEY=document.getElementById('k').value.trim();
  const msg=document.getElementById('msg'); msg.textContent="Loading...";
  try{
    const r=await fetch('/batches',{headers:{'X-API-Key':KEY}});
    if(r.status===401){msg.textContent="Wrong key — access denied.";hideAll();return;}
    if(!r.ok){msg.textContent="Error "+r.status;hideAll();return;}
    const data=await r.json(); render(data.batches||[]); msg.textContent="";
  }catch(e){msg.textContent="Could not reach server: "+e;}
}
function hideAll(){document.getElementById('cards').style.display='none';document.getElementById('tbl').style.display='none';}
function render(batches){
  let packs=0,scans=0,voided=0;
  const rows=document.getElementById('rows'); rows.innerHTML="";
  for(const b of batches){
    packs+=b.packs||0; scans+=b.scans||0; voided+=b.voided||0;
    const recalled=(b.voided||0)>0 && (b.voided===b.packs);
    const tr=document.createElement('tr');
    tr.innerHTML=`<td>${esc(b.product_name||'-')}</td>
      <td>${esc(b.batch_no||'-')}<br><span style="color:#64748b;font-size:11px">${esc(b.batch_id||'')}</span></td>
      <td>${b.packs||0}</td>
      <td>${recalled?'<span class="badge b-void">RECALLED</span>':'<span class="badge b-active">'+(b.active||0)+'</span>'}</td>
      <td>${b.scans||0}</td>
      <td>${esc(b.mfg_date||'-')}</td><td>${esc(b.exp_date||'-')}</td>
      <td>${recalled?'-':'<button class="danger" onclick="recall(\\''+b.batch_id+'\\')">Recall</button>'}</td>`;
    rows.appendChild(tr);
  }
  document.getElementById('c-batches').textContent=batches.length;
  document.getElementById('c-packs').textContent=packs;
  document.getElementById('c-scans').textContent=scans;
  document.getElementById('c-void').textContent=voided;
  document.getElementById('cards').style.display='flex';
  document.getElementById('tbl').style.display=batches.length?'table':'none';
}
async function recall(batchId){
  if(!confirm('Recall (void) ALL packs in batch '+batchId+'? This cannot be undone.'))return;
  const r=await fetch('/recall/'+encodeURIComponent(batchId),{method:'POST',headers:{'X-API-Key':KEY}});
  if(r.ok){alert('Batch recalled. Packs are now VOID.');load();}
  else alert('Recall failed: '+r.status);
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
</script>
</div></body></html>""")
