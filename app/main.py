# -*- coding: utf-8 -*-
"""martin_stock — 리바이프로덕트 재고 관리 웹앱 (FastAPI + SQLite).

실행:  python app/main.py   →  http://127.0.0.1:8600
"""
import os
import re
import sys
import json
import time
import hmac
import base64
import sqlite3
import hashlib
import secrets
import threading
import contextvars
import datetime as dt
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import (connect, init_db, chat_connect, init_chat_db, purge_old_chat,
                CHAT_RETENTION_DAYS, BASE as DATA_BASE)

# 정적 파일: exe로 묶이면 임시 해제 폴더(_MEIPASS)에서 서빙
if getattr(sys, "frozen", False):
    BASE = Path(sys._MEIPASS)
else:
    BASE = Path(__file__).resolve().parent
# 제품 이미지: exe 옆(소스 실행이면 프로젝트 루트) Image/ 폴더 — 사용자가 추가하는 데이터
IMAGE_DIR = DATA_BASE / "Image"
IMAGE_DIR.mkdir(exist_ok=True)
PHOTO_DIR = DATA_BASE / "DayPhoto"      # 일일 생산 현장 사진
PHOTO_DIR.mkdir(exist_ok=True)
CHAT_DIR = DATA_BASE / "ChatFile"       # 채팅 첨부 (사진·파일)
CHAT_DIR.mkdir(exist_ok=True)
BACKUP_DIR = DATA_BASE / "백업"          # DB 자동/수동 백업

# ── 앱 버전 & 자동 업데이트 ────────────────────────────
APP_VERSION = "1.73.1"    # 새 버전 배포 시 이 값을 올리고 version.json의 version과 맞춘다
# 새 버전 정보(version.json)를 읽어올 주소.
#   1순위: exe 옆 update_url.txt 파일 (재빌드 없이 호스트 변경 가능)
#   2순위: 아래 기본값 (배포 전 GitHub Releases 등의 raw 주소로 교체)
UPDATE_MANIFEST_URL = "https://github.com/peach44400-oss/REBYPRODUCT/releases/latest/download/version.json"  # 내장 기본값 (exe 옆 update_url.txt가 있으면 그게 우선)


def manifest_url():
    f = DATA_BASE / "update_url.txt"
    if f.exists():
        try:
            u = f.read_text(encoding="utf-8").strip()
            if u:
                return u
        except OSError:
            pass
    return UPDATE_MANIFEST_URL


app = FastAPI(title="martin_stock")

# 요청 처리 중인 로그인 사용자 (audit_log에 '누가'를 남기기 위한 컨텍스트)
CURRENT_USER = contextvars.ContextVar("current_user", default="")
SESSION_TTL = 24 * 3600   # 유휴 세션 만료(초) — 마지막 활동 후 이 시간 지나면 자동 로그아웃

# 기준정보 변경 버전 — presence 폴링에 실어 다른 접속자 브라우저의 캐시를 자동 갱신
MASTERS_VER = {"v": 1}


def bump_masters():
    MASTERS_VER["v"] += 1


# 감사로그 → 채팅 시스템 메시지로 흘릴 액션 (업무 흐름에 의미 있는 것만 — update_* 등 잦은 건 제외)
SYS_CHAT = {
    "save_day":      "📝 일일 기록 저장 — {d}",
    "disposal":      "🗑 폐기 — {d}",
    "disposal_undo": "↩️ 폐기 취소 — {d}",
    "lot_expiry":    "📅 소비기한 변경 — {d}",
    "backup":        "💾 백업 — {d}",
    "restore":       "♻️ 복원 — {d}",
    "user_role":     "👤 권한 변경 — {d}",
    "bulk_import":   "📥 일괄 반영 — {d}",
    "pack_set":      "📦 포장 세트 — {d}",
    "integrity_fix": "🔧 자재 체인 재계산 — {d}",
    "save_bom":      "📐 배합비 저장 — 제품#{d}",
}
MTYPE_KO = {"product": "제품", "material": "자재", "partner": "거래처",
            "staff": "인원", "line": "라인", "users": "사용자"}


def sys_chat_text(action, detail):
    """감사로그 한 건을 사람이 읽을 채팅 문구로. 대상 아니면 None."""
    if action in SYS_CHAT:
        return SYS_CHAT[action].format(d=detail[:120])
    for pre, icon in (("create_", "➕"), ("delete_", "➖")):
        if action.startswith(pre):
            mtype = action[len(pre):].split("#")[0]
            name = detail
            if detail.startswith("{"):          # create_*는 detail이 JSON — 이름만 뽑아 표시
                try:
                    name = json.loads(detail).get("name") or detail
                except (ValueError, AttributeError):
                    pass
            verb = "등록" if pre == "create_" else "삭제"
            return f"{icon} {MTYPE_KO.get(mtype, mtype)} {verb} — {str(name)[:80]}"
    return None


def chat_system(text):
    """시스템 메시지를 채팅에 남긴다 — 실패해도 업무 저장을 막지 않는다."""
    try:
        con = chat_connect()
        try:
            con.execute("INSERT INTO chat(day, username, text, kind) VALUES(?,?,?,'system')",
                        (dt.date.today().isoformat(), CURRENT_USER.get() or "system", text))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def audit(con, action, detail):
    con.execute("INSERT INTO audit_log(action, detail, username) VALUES(?,?,?)",
                (action, str(detail), CURRENT_USER.get() or ""))
    msg = sys_chat_text(action, str(detail))
    if msg:
        chat_system(msg)

MASTER_TABLES = {
    "product": ("product", ["name", "category", "spec", "pack_sizes", "line_id", "unit_price",
                            "shelf_days", "safety_stock", "batch_yield", "status", "note", "is_semi", "fin_split"]),
    "material": ("material", ["kind", "name", "spec", "unit", "pack_count", "pack_set", "unit_price", "partner_id",
                              "safety_stock", "prod_mult", "prod_per", "shelf_days", "status", "note",
                              "is_semi", "batch_yield"]),
    "partner": ("partner", ["name", "type", "phone", "contact", "note", "status", "biz_no", "ceo", "mobile", "email"]),
    "staff": ("staff", ["name", "kind", "position", "process", "wage", "join_date", "phone", "status", "note"]),
    "line": ("line", ["name", "process", "std_hours", "parent_id", "note", "status"]),
}


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def ripple_material(con, mid, from_date):
    """자재 일일 기록의 전일재고 체인 재계산 (from_date 이후 전체).

    과거 날짜의 실사·입고·사용을 고치면 이후 날짜 기록이 낡은 전일재고를 물고 있게 됨.
    - 실사(manual) 행: 실재고 = 세어본 값(진실) 유지, 전일재고·사용량만 재계산
    - 자동(auto) 행: 사용량(기록된 사용 합) 유지, 전일재고·실재고를 다시 계산
    """
    prev_row = con.execute("""SELECT real_qty FROM material_daily
        WHERE material_id=? AND date<=? ORDER BY date DESC LIMIT 1""",
                           (mid, from_date)).fetchone()
    prev = float(prev_row["real_qty"]) if prev_row else 0.0
    for r in con.execute("""SELECT id, date, in_qty, real_qty, used_qty, src
            FROM material_daily WHERE material_id=? AND date>? ORDER BY date""",
                         (mid, from_date)).fetchall():
        if r["src"] == "auto":
            real = prev + float(r["in_qty"]) - float(r["used_qty"])
            con.execute("UPDATE material_daily SET prev_qty=?, real_qty=? WHERE id=?",
                        (prev, real, r["id"]))
            prev = real
        else:
            used = prev + float(r["in_qty"]) - float(r["real_qty"])
            con.execute("UPDATE material_daily SET prev_qty=?, used_qty=? WHERE id=?",
                        (prev, used, r["id"]))
            prev = float(r["real_qty"])


# ── 인증/권한 (admin=전체 / op=시급 제외 / guest=보기 전용) ──
SESSIONS = {}
# 강제 로그인으로 끊긴 세션 — 옛 브라우저에 사유를 알리기 위한 표시 (sid -> 메시지)
KICKED = {}
# 같은 아이디가 '활동 중'으로 볼 시간(초) — presence(75초)와 동일하게, 브라우저를 그냥 닫은 세션은 만료 후 재로그인 허용
ONLINE_WINDOW = 75


def hashpw(pw: str) -> str:
    """(구) 단순 SHA-256 — 기존 해시 검증·자동 업그레이드용으로만 남긴다."""
    return hashlib.sha256(("rebyproduct:" + pw).encode()).hexdigest()


PBKDF2_ITER = 200_000   # 비밀번호 해싱 반복 — DB 유출 시 크래킹을 어렵게

def make_password(pw: str) -> str:
    """개인 salt + PBKDF2(sha256) 해시 — 저장 형식: pbkdf2$반복$salt$해시."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), salt, PBKDF2_ITER)
    return f"pbkdf2${PBKDF2_ITER}${salt.hex()}${dk.hex()}"

def verify_password(stored: str, pw: str) -> bool:
    """새 방식(pbkdf2$…)과 구 방식(SHA-256) 둘 다 검증. 타이밍 공격 방지로 compare_digest."""
    if not stored:
        return False
    if stored.startswith("pbkdf2$"):
        try:
            _, iter_s, salt_hex, hash_hex = stored.split("$")
            dk = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), bytes.fromhex(salt_hex), int(iter_s))
            return hmac.compare_digest(dk.hex(), hash_hex)
        except Exception:
            return False
    return hmac.compare_digest(stored, hashpw(pw))

# 약한/기본 비밀번호 — 로그인 시 경고를 띄운다 (인터넷 노출 계정 보호)
WEAK_PWS = {"1", "0", "12", "123", "1234", "12345", "123456", "1234567", "12345678",
            "0000", "1111", "0930", "admin", "password", "passwd", "qwerty", "reby",
            "rebyproduct", "1q2w3e", "aaaa", "1212", "1004"}

def is_weak_password(pw: str) -> bool:
    return len(pw or "") < 6 or (pw or "").lower() in WEAK_PWS


def ensure_admin():
    con = connect()
    try:
        n = con.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        if n == 0:
            con.execute("INSERT OR IGNORE INTO users(username, pw_hash, role) VALUES(?,?,?)",
                        ("admin", make_password("1"), "admin"))   # 첫 실행용 — 로그인 시 '약한 비밀번호' 경고가 뜬다
            con.commit()
    finally:
        con.close()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    sid = request.cookies.get("sid")
    if path.startswith("/api/") and path != "/api/login":
        user = SESSIONS.get(sid)
        if not user:
            msg = KICKED.pop(sid, None)   # 강제 로그인으로 끊긴 세션이면 사유를 알린다
            return JSONResponse({"detail": msg or "로그인이 필요합니다", "kicked": bool(msg)},
                                status_code=401)
        # 유휴 세션 만료 — 마지막 활동 후 SESSION_TTL 지나면 자동 로그아웃 (브라우저가 열려 있으면 폴링이 갱신)
        if time.time() - user.get("seen", 0) > SESSION_TTL:
            SESSIONS.pop(sid, None)
            return JSONResponse({"detail": "오래 사용하지 않아 로그아웃되었습니다 — 다시 로그인해주세요"},
                                status_code=401)
        if (user["role"] == "guest" and request.method in ("POST", "PUT", "DELETE")
                and path not in ("/api/logout", "/api/password", "/api/chat")):
            return JSONResponse({"detail": "보기 전용(guest) 계정입니다 — 입력·수정 권한이 없습니다"},
                                status_code=403)
        user["seen"] = time.time()   # 접속 표시(presence)용 마지막 활동 시각
        request.state.user = user
        CURRENT_USER.set(user.get("username", ""))   # audit_log '누가' 기록용
    response = await call_next(request)
    # 화면 파일은 항상 재검증 — exe 업데이트 후 브라우저가 옛 app.js를 캐시로 쓰는 문제 방지
    if path == "/" or path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache"
    # 보안 헤더 — 클릭재킹(iframe 삽입)·MIME 스니핑 방지, 외부로 주소 유출 최소화
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


# 로그인 무차별 대입 방어 — IP(외부 접속 시 Cloudflare가 실제 IP를 CF-Connecting-IP로 전달)별 실패 누적
LOGIN_FAILS = {}          # ip -> [실패횟수, 잠금해제시각]
LOGIN_MAX = 8             # 이 횟수 이상 실패하면
LOGIN_LOCK_SEC = 600      # 10분 잠금


def client_ip(request: Request):
    return (request.headers.get("cf-connecting-ip")
            or (request.client.host if request.client else "?"))


@app.post("/api/login")
def login(body: dict, response: Response, request: Request):
    ip = client_ip(request)
    now = time.time()
    rec = LOGIN_FAILS.get(ip)
    if rec and rec[1] > now:
        raise HTTPException(429, "로그인 시도가 너무 많습니다 — 잠시 후(약 10분) 다시 시도해주세요")
    pw = body.get("password") or ""
    con = connect()
    try:
        u = con.execute("SELECT * FROM users WHERE username=?",
                        ((body.get("username") or "").strip(),)).fetchone()
        if not u or not verify_password(u["pw_hash"], pw):
            r = LOGIN_FAILS.get(ip) or [0, 0]
            r[0] += 1
            r[1] = now + LOGIN_LOCK_SEC if r[0] >= LOGIN_MAX else 0
            LOGIN_FAILS[ip] = r
            raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다")
        LOGIN_FAILS.pop(ip, None)   # 성공하면 실패 기록 초기화
        # 같은 아이디가 이미 다른 곳에서 '활동 중'이면 기본은 차단, force면 그 세션을 끊고 이 접속을 허용
        now2 = time.time()
        active = [tok for tok, s in list(SESSIONS.items())
                  if s.get("username") == u["username"] and now2 - s.get("seen", 0) < ONLINE_WINDOW]
        if active and not body.get("force"):
            raise HTTPException(409, {"code": "already_online", "username": u["username"]})
        for tok in active:   # 강제 접속: 기존 세션 종료 (옛 브라우저는 다음 요청에서 안내와 함께 로그아웃)
            KICKED[tok] = "다른 기기에서 이 아이디로 로그인해 연결이 끊어졌습니다."
            SESSIONS.pop(tok, None)
        # 구 방식(SHA-256) 해시면 이번 로그인에서 PBKDF2로 자동 업그레이드
        if not str(u["pw_hash"]).startswith("pbkdf2$"):
            con.execute("UPDATE users SET pw_hash=? WHERE id=?", (make_password(pw), u["id"]))
        audit(con, "login", f"로그인 — {ip}")
        con.commit()
        token = secrets.token_hex(16)
        duty = (u["duty"] if "duty" in u.keys() else "all") or "all"
        mp = (u["money_perms"] if "money_perms" in u.keys() else "") or ""
        SESSIONS[token] = {"id": u["id"], "username": u["username"], "role": u["role"],
                           "duty": duty, "money_perms": mp, "seen": time.time(),
                           "weak_pw": is_weak_password(pw)}
        https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie("sid", token, httponly=True, samesite="lax", secure=https)
        return {"username": u["username"], "role": u["role"], "duty": duty,
                "money_perms": sorted(money_set(SESSIONS[token])),
                "weak_pw": is_weak_password(pw)}
    finally:
        con.close()


@app.post("/api/logout")
def logout(request: Request, response: Response):
    SESSIONS.pop(request.cookies.get("sid"), None)
    response.delete_cookie("sid")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    u = request.state.user
    return {"username": u["username"], "role": u["role"], "duty": u.get("duty", "all"),
            "money_perms": sorted(money_set(u)), "weak_pw": bool(u.get("weak_pw"))}


@app.post("/api/password")
def change_password(request: Request, body: dict):
    u = request.state.user
    con = connect()
    try:
        row = con.execute("SELECT pw_hash FROM users WHERE id=?", (u["id"],)).fetchone()
        if not verify_password(row["pw_hash"], body.get("old") or ""):
            raise HTTPException(400, "기존 비밀번호가 올바르지 않습니다")
        new = (body.get("new") or "").strip()
        if len(new) < 6:
            raise HTTPException(400, "새 비밀번호는 6자 이상이어야 합니다")
        if is_weak_password(new):
            raise HTTPException(400, "너무 쉬운 비밀번호입니다 — 다른 비밀번호를 사용해주세요")
        con.execute("UPDATE users SET pw_hash=? WHERE id=?", (make_password(new), u["id"]))
        con.commit()
        # 세션의 약한 비밀번호 경고 해제
        u["weak_pw"] = False
        return {"ok": True}
    finally:
        con.close()


def require_admin(request: Request):
    if request.state.user["role"] != "admin":
        raise HTTPException(403, "관리자(admin)만 가능합니다")


# ── 앱 전역 설정(app_setting) 헬퍼 ──────────────
def get_app_setting(key, default=""):
    con = connect()
    try:
        r = con.execute("SELECT value FROM app_setting WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default
    finally:
        con.close()


def set_app_setting(key, value):
    con = connect()
    try:
        con.execute("INSERT OR REPLACE INTO app_setting(key, value) VALUES(?,?)", (key, str(value)))
        con.commit()
    finally:
        con.close()


# ── 외부 접속 터널(Cloudflare quick tunnel) 관리 ──────────────
# cloudflared.exe가 프로그램 폴더에 있으면, 실행 시 자동으로 외부 접속 주소(HTTPS)를 만든다.
# 도메인 없이 쓰는 빠른 터널이라 PC를 재시작하면 주소가 바뀐다 → 새 주소를 채팅에 자동 게시.
TUNNEL = {"proc": None, "url": "", "starting": False, "err": ""}
SERVE_PORT = {"v": 8600}


def cloudflared_path():
    p = DATA_BASE / "cloudflared.exe"
    return p if p.exists() else None


def tunnel_enabled():
    v = get_app_setting("tunnel_enabled", "")
    if v == "":
        return cloudflared_path() is not None   # 설정 없으면: cloudflared가 있으면 기본 켬
    return v == "1"


def start_tunnel():
    import subprocess
    if TUNNEL["proc"] and TUNNEL["proc"].poll() is None:
        return   # 이미 실행 중
    cf = cloudflared_path()
    if not cf:
        TUNNEL["err"] = "cloudflared.exe가 프로그램 폴더에 없습니다"
        return
    TUNNEL["err"] = ""
    TUNNEL["url"] = ""
    TUNNEL["starting"] = True
    port = SERVE_PORT["v"]
    flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW — 검은 창이 안 뜨게
    try:
        p = subprocess.Popen(
            [str(cf), "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", creationflags=flags)
    except Exception as e:
        TUNNEL["err"] = f"실행 실패: {e}"
        TUNNEL["starting"] = False
        return
    TUNNEL["proc"] = p

    def reader():
        rx = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
        try:
            for line in p.stdout:
                if not TUNNEL["url"]:
                    m = rx.search(line)
                    if m:
                        TUNNEL["url"] = m.group(0)
                        TUNNEL["starting"] = False
                        chat_system(f"🌐 외부 접속 주소가 준비됐습니다 — {TUNNEL['url']}\n"
                                    "(로그인 필요 · PC를 재시작하면 주소가 바뀝니다)")
        except Exception:
            pass
        TUNNEL["starting"] = False
    threading.Thread(target=reader, daemon=True).start()


def stop_tunnel():
    p = TUNNEL.get("proc")
    if p and p.poll() is None:
        try:
            p.terminate()
        except Exception:
            pass
    TUNNEL["proc"] = None
    TUNNEL["url"] = ""
    TUNNEL["starting"] = False


@app.get("/api/tunnel")
def tunnel_status(request: Request):
    require_admin(request)
    running = bool(TUNNEL["proc"] and TUNNEL["proc"].poll() is None)
    return {"available": cloudflared_path() is not None, "enabled": tunnel_enabled(),
            "running": running, "url": TUNNEL["url"], "starting": TUNNEL["starting"],
            "error": TUNNEL["err"]}


@app.post("/api/tunnel")
def tunnel_toggle(request: Request, body: dict):
    require_admin(request)
    if not cloudflared_path():
        raise HTTPException(400, "cloudflared.exe가 프로그램 폴더에 없습니다 — 먼저 내려받아 넣어주세요")
    on = bool(body.get("on"))
    set_app_setting("tunnel_enabled", "1" if on else "0")
    if on:
        start_tunnel()
    else:
        stop_tunnel()
    con = connect()
    try:
        audit(con, "tunnel", "외부 접속 " + ("켜기" if on else "끄기"))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "on": on}


# ── 금액 열람 권한 ──
# mat = 자재 단가·사용금액 / prod = 제품 단가·생산/출고/재고 금액 / labor = 시급·노무비 / cost = 원가·수익성
# 담당(duty) — 일일 입력 저장 범위. 사용자마다 여러 개 지정 가능(콤마 구분).
# 'all' = 전체(앞으로 담당이 늘어나도 자동 포함), 'none' = 담당 없음(저장 불가).
# ※ 옛 값 'prod'(생산 담당)는 여기 없음 — db.py가 세부 담당으로 1회 확장 (충돌 없어 재실행 안전)
DUTY_KEYS = ("production", "shipment", "usage", "staffing", "stock", "lot")
DUTY_KO = {"production": "생산실적", "shipment": "완제품 출고", "usage": "자재 사용",
           "staffing": "인원·가동", "stock": "재고·입고", "lot": "LOT 관리"}
# 일일 입력 body 섹션 → 저장에 필요한 담당 (memo/특이사항은 담당이 하나라도 있으면 허용)
DUTY_SECTION = {"production": "production", "shipment": "shipment", "usage": "usage",
                "staffing": "staffing", "materials": "stock", "mat_in": "stock"}


def duty_set(user) -> set:
    if user.get("role") == "admin":
        return set(DUTY_KEYS)
    d = (user.get("duty") or "").strip()
    if d == "all":
        return set(DUTY_KEYS)
    if d in ("", "none"):
        return set()
    return {k for k in d.split(",") if k in DUTY_KEYS}


def norm_duty(v) -> str:
    """입력(리스트 또는 문자열) → 저장 문자열. 전부 고르면 'all', 하나도 없으면 'none'."""
    if isinstance(v, str):
        if v == "all":
            return "all"
        if v in ("none", ""):
            return "none"
        v = v.split(",")
    ks = {k for k in (v or []) if k in DUTY_KEYS}
    if not ks:
        return "none"
    return "all" if ks == set(DUTY_KEYS) else ",".join(k for k in DUTY_KEYS if k in ks)


MONEY_KEYS = ("mat", "prod", "labor", "cost")


def money_set(user) -> set:
    if user["role"] == "admin":
        return set(MONEY_KEYS)
    return {k for k in (user.get("money_perms") or "").split(",") if k in MONEY_KEYS}


def mcan(request: Request, key: str) -> bool:
    return key in money_set(request.state.user)


@app.get("/api/users")
def users_list(request: Request):
    require_admin(request)
    con = connect()
    try:
        return rows(con.execute("SELECT id, username, role, duty, money_perms, created_at FROM users ORDER BY id"))
    finally:
        con.close()


@app.post("/api/users")
def users_create(request: Request, body: dict):
    require_admin(request)
    name = (body.get("username") or "").strip()
    pw = body.get("password") or ""
    role = body.get("role") or "guest"
    duty = norm_duty(body.get("duty") if body.get("duty") is not None else "all")
    if not name or not pw:
        raise HTTPException(400, "아이디와 비밀번호를 입력하세요")
    if len(pw) < 6:
        raise HTTPException(400, "비밀번호는 6자 이상이어야 합니다")
    if role not in ("admin", "op", "guest"):
        raise HTTPException(400, "권한은 admin/op/guest 중 하나여야 합니다")
    if role == "admin":
        duty = "all"   # 관리자는 항상 전체
    con = connect()
    try:
        try:
            con.execute("INSERT INTO users(username, pw_hash, role, duty) VALUES(?,?,?,?)",
                        (name, make_password(pw), role, duty))
        except Exception:
            raise HTTPException(400, "이미 존재하는 아이디입니다")
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.put("/api/users/{uid}")
def users_update(request: Request, uid: int, body: dict):
    """권한/담당 변경 (admin 전용). admin 계정은 못 바꾸며, 변경 즉시 접속 중인 세션에도 반영."""
    require_admin(request)
    role = body.get("role")
    duty = body.get("duty")            # 리스트(복수 담당) 또는 "production,stock" / "all" / "none"
    mperms = body.get("money_perms")   # 리스트 또는 "mat,labor" 문자열
    if role is not None and role not in ("op", "guest"):
        raise HTTPException(400, "권한은 op/guest 중 하나여야 합니다 (admin 승격은 새 계정으로)")
    if duty is not None:
        duty = norm_duty(duty)
    if mperms is not None:
        if isinstance(mperms, str):
            mperms = mperms.split(",")
        mperms = ",".join(k for k in mperms if k in MONEY_KEYS)
    if role is None and duty is None and mperms is None:
        raise HTTPException(400, "변경할 항목이 없습니다")
    con = connect()
    try:
        target = con.execute("SELECT id, username, role FROM users WHERE id=?", (uid,)).fetchone()
        if not target:
            raise HTTPException(404, "사용자 없음")
        if target["role"] == "admin":
            raise HTTPException(400, "admin 계정은 변경할 수 없습니다")
        if role is not None:
            con.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        if duty is not None:
            con.execute("UPDATE users SET duty=? WHERE id=?", (duty, uid))
        if mperms is not None:
            con.execute("UPDATE users SET money_perms=? WHERE id=?", (mperms, uid))
        # 실시간 반영: 접속 중인 세션도 즉시 교체 → 다음 요청부터 새 권한/담당으로 차단
        for u in SESSIONS.values():
            if u["id"] == uid:
                if role is not None:
                    u["role"] = role
                if duty is not None:
                    u["duty"] = duty
                if mperms is not None:
                    u["money_perms"] = mperms
        audit(con, "user_role", f"{target['username']} -> {role or ''}{(' 담당:' + duty) if duty else ''}"
              + (f" 금액:[{mperms}]" if mperms is not None else ""))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.post("/api/users/{uid}/password")
def users_set_password(request: Request, uid: int, body: dict):
    """관리자가 일반 사용자의 비밀번호를 재설정 (비밀번호를 잊었을 때). admin 계정은 대상 아님.
    재설정하면 그 사용자의 접속은 끊겨 새 비밀번호로 다시 로그인하게 된다."""
    require_admin(request)
    new = (body.get("password") or "").strip()
    if not new:
        raise HTTPException(400, "새 비밀번호를 입력하세요")
    con = connect()
    try:
        target = con.execute("SELECT id, username, role FROM users WHERE id=?", (uid,)).fetchone()
        if not target:
            raise HTTPException(404, "사용자 없음")
        if target["role"] == "admin":
            raise HTTPException(400, "admin 계정 비밀번호는 [내 설정]에서만 바꿀 수 있습니다")
        con.execute("UPDATE users SET pw_hash=? WHERE id=?", (make_password(new), uid))
        audit(con, "user_pw_reset", f"{target['username']} 비밀번호 관리자 재설정")
        con.commit()
        # 그 사용자가 접속 중이면 끊어 새 비밀번호로 다시 로그인하게 한다
        for tok, s in list(SESSIONS.items()):
            if s.get("id") == uid:
                KICKED[tok] = "관리자가 비밀번호를 재설정했습니다 — 새 비밀번호로 다시 로그인해주세요."
                SESSIONS.pop(tok, None)
        return {"ok": True, "username": target["username"], "weak": is_weak_password(new)}
    finally:
        con.close()


@app.delete("/api/users/{uid}")
def users_delete(request: Request, uid: int):
    require_admin(request)
    if uid == request.state.user["id"]:
        raise HTTPException(400, "본인 계정은 삭제할 수 없습니다")
    con = connect()
    try:
        con.execute("DELETE FROM users WHERE id=?", (uid,))
        for tok, u in list(SESSIONS.items()):
            if u["id"] == uid:
                SESSIONS.pop(tok, None)
        con.commit()
        return {"ok": True}
    finally:
        con.close()


# ── 완제품 LOT (생산일자별 재고) ───────────────
def current_lots(con, pid, upto, exclude_ship_on_date=False, exclude_ship_date=None):
    """생산일자별 재고 LOT 추정.

    최신 lot_snapshot(엑셀 수불부 우측 블록, ≤upto)을 기준으로 이후의 생산(LOT 추가)과
    출고(생산일자 지정분은 해당 LOT, 미지정분은 FIFO)를 반영한다. 합계가 계산 재고와
    어긋나면 '생산일 미상' LOT으로 보정해 총량을 맞춘다.
    exclude_ship_on_date=True면 upto 당일 출고는 차감하지 않음 (그날 출고 편집용).
    exclude_ship_date=날짜면 그 날짜의 출고만 차감 제외(다른 날 출고는 모두 반영) —
    과거 날짜로 돌아가 출고를 편집할 때, 이미 다른 날 나간 LOT은 안 뜨게 하기 위함.
    """
    prow = con.execute("SELECT shelf_days FROM product WHERE id=?", (pid,)).fetchone()
    if not prow:
        raise HTTPException(404, "제품 없음")
    shelf = int(prow["shelf_days"] or 0)
    # LOT별 직접 지정 소비기한 (제품 소비일보다 우선)
    exp_map = {r["made"]: r["expiry"] for r in con.execute(
        "SELECT made, expiry FROM lot_expiry WHERE product_id=? AND expiry!=''", (pid,))}
    # 생산 LOT 소비기한 분할 (한 생산분을 수량별로 여러 소비기한으로 — lot_expiry보다 우선)
    plan_map = {}
    for r in con.execute("""SELECT made, qty, expiry, partner_id, pack_mid, pack_set FROM lot_plan
        WHERE product_id=? AND qty>0 ORDER BY made, seq, id""", (pid,)):
        plan_map.setdefault(r["made"], []).append(
            (float(r["qty"]), r["expiry"] or "", r["partner_id"], r["pack_mid"], r["pack_set"] or ""))
    # 거래처 분배(prod_split) — LOT 구간 partner가 비어 있으면 같은 생산일·같은 수량의 분배 거래처를 자동 매칭
    dist_by_date = {}
    for r in con.execute("""SELECT date, qty, partner_id FROM prod_split
        WHERE product_id=? AND partner_id IS NOT NULL AND qty>0 ORDER BY date, id""", (pid,)):
        dist_by_date.setdefault(r["date"], []).append([float(r["qty"]), r["partner_id"]])

    def derive_partner(made, qty):
        """lot_plan에 거래처 미지정 시 그 생산일 분배에서 수량이 일치하는 거래처를 1회 소진 매칭."""
        for e in dist_by_date.get(made, []):
            if e[1] is not None and abs(e[0] - qty) < 0.5:
                p = e[1]; e[1] = None   # 한 번 쓰면 소진
                return p
        return None

    def fallback_exp(made):
        """분할 없는 LOT의 소비기한: 지정값 > 생산일 + 제품 소비일."""
        e = exp_map.get(made)
        if e:
            return e
        if made and shelf:
            try:
                return (dt.date.fromisoformat(made) + dt.timedelta(days=shelf)).isoformat()
            except ValueError:
                pass
        return ""

    snap = con.execute("""SELECT MAX(date) d FROM lot_snapshot
        WHERE product_id=? AND date<=? AND kind='stock'""", (pid, upto)).fetchone()["d"]
    base = snap or ""
    lots = []
    if snap:
        for r in con.execute("""SELECT made_date, SUM(qty) q, MAX(expiry) e FROM lot_snapshot
            WHERE product_id=? AND date=? AND kind='stock' AND qty>0
            GROUP BY made_date ORDER BY made_date""", (pid, snap)):
            made = r["made_date"] or ""
            lots.append({"made": made, "qty": float(r["q"]),
                         "expiry": exp_map.get(made) or r["e"] or ""})
    else:
        # 스냅샷이 없으면 기초재고 = '생산일 미상 (이월)' LOT으로 시작
        opening = con.execute("""SELECT COALESCE(SUM(qty),0) q FROM opening_stock
            WHERE kind='product' AND ref_id=?""", (pid,)).fetchone()["q"]
        if float(opening) > 0:
            lots.append({"made": "", "qty": float(opening), "expiry": fallback_exp("")})
    ship_cmp = "<" if exclude_ship_on_date else "<="
    # 특정 날짜 출고만 차감 제외 (그 날짜를 편집 중일 때) — 나머지 날짜 출고는 전부 반영
    ship_skip = " AND date!=?" if exclude_ship_date else ""

    def add_made_lot(made, qty):
        """생산 LOT 추가: 분할 계획이 있으면 수량별 여러 소비기한 LOT으로, 없으면 단일."""
        plan = plan_map.get(made)
        if plan:
            assigned = 0.0
            for pq, pexp, ppartner, ppmid, ppset in plan:
                take = min(pq, qty - assigned)
                if take <= 1e-9:
                    break
                lots.append({"made": made, "qty": take, "expiry": pexp, "planned": True,
                             "partner_id": ppartner or derive_partner(made, take),
                             "pack_mid": ppmid, "pack_set": ppset})
                assigned += take
            if qty - assigned > 1e-9:   # 분할 합보다 생산이 많으면 나머지는 폴백 기한
                lots.append({"made": made, "qty": qty - assigned, "expiry": fallback_exp(made),
                             "planned": True, "partner_id": derive_partner(made, qty - assigned),
                             "pack_mid": None, "pack_set": ""})
        else:
            exp = fallback_exp(made)
            ex = next((l for l in lots if l["made"] == made and l["expiry"] == exp), None)
            if ex:
                ex["qty"] += qty
            else:
                lots.append({"made": made, "qty": qty, "expiry": exp, "planned": False,
                             "partner_id": None, "pack_mid": None, "pack_set": ""})

    def fifo_take(amount, protect_made=None):
        # 선입선출 = ①이월(생산일 미상) LOT 먼저 — 가장 오래된 재고 ②그다음 소비기한 임박(이른) 순.
        # (기한미상을 뒤로 미루면 이월분 출고가 당일 분할 LOT을 깎아 기한 정보가 사라진다)
        # protect_made: 그 생산일 LOT은 차감하지 않음 (재고 부족 보정이 당일 생산분을 지우지 않게)
        for l in sorted(lots, key=lambda x: (x["made"] != "", x["expiry"] == "", x["expiry"], x["made"])):
            if amount <= 1e-9:
                break
            if l["qty"] <= 0 or (protect_made and l["made"] == protect_made):
                continue
            take = min(l["qty"], amount)
            l["qty"] -= take
            amount -= take
        return amount

    # 생산(추가)·출고·폐기(차감)를 **시간순**으로 처리 — 나중 생산분이 이전 출고에 소진되지 않도록.
    # 같은 날짜는 생산이 먼저 (그날 만든 걸 그날 출고 가능)
    events = []
    for r in con.execute("""SELECT date, SUM(prod_qty) q FROM production
        WHERE product_id=? AND date>? AND date<=? GROUP BY date""", (pid, base, upto)):
        if float(r["q"] or 0) > 0:
            events.append((r["date"], 0, float(r["q"]), r["date"], "", None))
    ship_params = [pid, base, upto] + ([exclude_ship_date] if exclude_ship_date else [])
    for s in con.execute(f"""SELECT date, qty, pd, pex, spid, id FROM (
          SELECT date, qty, COALESCE(prod_date,'') pd, COALESCE(expiry,'') pex, partner_id spid, id FROM shipment
            WHERE product_id=? AND date>? AND date{ship_cmp}?{ship_skip}
          UNION ALL
          SELECT date, qty, COALESCE(prod_date,'') pd, '' pex, NULL spid, id FROM disposal
            WHERE product_id=? AND date>? AND date<=?)
        ORDER BY date, id""", (*ship_params, pid, base, upto)):
        if float(s["qty"] or 0) > 0:
            events.append((s["date"], 1, float(s["qty"]), s["pd"], s["pex"], s["spid"]))
    events.sort(key=lambda e: (e[0], e[1]))
    for date_, kind_, qty_, pd_, pex_, spid_ in events:
        if kind_ == 0:   # 생산 → LOT 추가 (분할 계획 반영)
            add_made_lot(date_, qty_)
        else:            # 출고/폐기 → 지정 LOT(생산일+소비기한) 우선, 나머지 FIFO
            remain = qty_
            if pd_:
                cands = [l for l in lots if l["made"] == pd_ and (not pex_ or l["expiry"] == pex_)]
                if pex_ and not cands:   # 지정 소비기한 LOT이 없으면 생산일만 매칭
                    cands = [l for l in lots if l["made"] == pd_]
                # 같은 (생산일, 소비기한) 구간이 여럿이면 출고 거래처와 일치하는 구간부터 차감
                # (안 그러면 앞 구간부터 먹어 다른 거래처 몫이 남는 잘못된 결과)
                cands.sort(key=lambda x: (
                    x["expiry"] == "", x["expiry"],
                    0 if (spid_ is not None and x.get("partner_id") == spid_) else 1))
                for tgt in cands:
                    if remain <= 1e-9:
                        break
                    take = min(tgt["qty"], remain)
                    tgt["qty"] -= take
                    remain -= take
            fifo_take(remain)
    stock = con.execute(f"""SELECT
        COALESCE((SELECT SUM(qty) FROM opening_stock WHERE kind='product' AND ref_id=?),0)
        + COALESCE((SELECT SUM(prod_qty) FROM production WHERE product_id=? AND date<=?),0)
        - COALESCE((SELECT SUM(qty) FROM shipment WHERE product_id=? AND date{ship_cmp}?{ship_skip}),0)
        - COALESCE((SELECT SUM(qty) FROM disposal WHERE product_id=? AND date<=?),0) v""",
        (pid, pid, upto, *( [pid, upto, exclude_ship_date] if exclude_ship_date else [pid, upto] ), pid, upto)).fetchone()["v"]
    diff = float(stock) - sum(l["qty"] for l in lots)
    if diff > 0.5:
        ex0 = next((l for l in lots if l["made"] == ""), None)
        if ex0:
            ex0["qty"] += diff
        else:
            lots.insert(0, {"made": "", "qty": diff, "expiry": fallback_exp("")})
    elif diff < -0.5:
        # 계산 재고 < LOT 합 (과거 출고 기록만 있고 생산·기초재고 미정비 등) —
        # 부족분은 과거 LOT에서만 흡수하고 그날 생산 LOT은 남겨 출고 선택이 가능하게 한다
        fifo_take(-diff, protect_made=upto)
    lots = [l for l in lots if l["qty"] > 0.0005]
    lots.sort(key=lambda x: (x["made"] != "", x["made"], x["expiry"]))
    # 같은 (생산일, 소비기한) LOT이 여럿이면 구분용 순번(no) 부여 — 출고 LOT 선택 식별자로 사용.
    # 키가 유일하면 no=0(번호 없음), 중복이면 1,2,3…
    by_key = {}
    for l in lots:
        by_key.setdefault((l["made"], l["expiry"]), []).append(l)
    out = []
    for grp in by_key.values():
        multi = len(grp) > 1
        for i, l in enumerate(grp, 1):
            out.append({"made": l["made"], "qty": round(l["qty"], 3), "expiry": l["expiry"],
                        "planned": l.get("planned", False), "no": i if multi else 0,
                        "partner_id": l.get("partner_id"),
                        "pack_mid": l.get("pack_mid"), "pack_set": l.get("pack_set") or ""})
    out.sort(key=lambda x: (x["made"] != "", x["made"], x["expiry"], x["no"]))
    return {"lots": out, "stock": round(float(stock), 3), "base": snap}


@app.get("/api/lots/{pid}")
def lots_get(pid: int, date: str = ""):
    """출고용: 출고 가능한 생산일자별 재고.
    현재(오늘 또는 편집일 중 늦은 날) 시점의 실재고를 기준으로 하되, 편집 중인 그 날짜의
    출고만 되돌려(차감 제외) 재선택·수정이 가능하게 한다. 다른 날짜에 이미 나간 LOT은
    반영되어 안 뜨거나 남은 수량만 표시된다 (과거로 돌아가도 이중 출고 방지)."""
    con = connect()
    try:
        today = dt.date.today().isoformat()
        d = date or today
        upto = d if d > today else today
        return current_lots(con, pid, upto, exclude_ship_date=d)
    finally:
        con.close()


@app.get("/api/prodhistory/{pid}")
def prod_history(pid: int, limit: int = 40):
    """기준정보 제품명 클릭 팝업: 생산일자별 현재고 LOT + 최근 생산/출고 이력."""
    con = connect()
    try:
        p = con.execute("SELECT * FROM product WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "제품 없음")
        cl = current_lots(con, pid, dt.date.today().isoformat())
        recent = rows(con.execute("""
            SELECT date, SUM(p) prod, SUM(s) ship,
                   (SELECT GROUP_CONCAT(DISTINCT COALESCE(pa.name,'거래처 미상'))
                      FROM shipment s2 LEFT JOIN partner pa ON pa.id=s2.partner_id
                      WHERE s2.product_id=? AND s2.date=x.date AND s2.qty>0) partners
            FROM (
              SELECT date, prod_qty p, 0 s FROM production WHERE product_id=?
              UNION ALL
              SELECT date, 0, qty FROM shipment WHERE product_id=?) x
            GROUP BY date ORDER BY date DESC LIMIT ?""", (pid, pid, pid, limit)))
        agg = con.execute("""SELECT COALESCE(SUM(prod_qty),0) tp,
            MIN(CASE WHEN prod_qty>0 THEN date END) fp,
            MAX(CASE WHEN prod_qty>0 THEN date END) lp
            FROM production WHERE product_id=?""", (pid,)).fetchone()
        sh = con.execute("""SELECT COALESCE(SUM(qty),0) ts,
            MAX(CASE WHEN qty>0 THEN date END) ls
            FROM shipment WHERE product_id=?""", (pid,)).fetchone()
        return {"name": p["name"], "category": p["category"], "spec": p["spec"],
                "unit_price": p["unit_price"], "shelf_days": p["shelf_days"],
                "safety_stock": p["safety_stock"], "status": p["status"],
                "batch_yield": p["batch_yield"], "image": p["image"] if "image" in p.keys() else "",
                "stock": cl["stock"], "lots": cl["lots"], "lot_base": cl["base"],
                "recent": recent,
                "total_prod": agg["tp"], "first_prod": agg["fp"], "last_prod": agg["lp"],
                "total_ship": sh["ts"], "last_ship": sh["ls"]}
    finally:
        con.close()


# ── 제품 이미지 ──────────────────────────────
def _safe_name(name):
    n = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(name)).strip().strip(".")
    return n or "product"


@app.post("/api/product/{pid}/image")
def product_image_set(pid: int, body: dict):
    """제품 이미지 저장 — data:image/…;base64,… → Image/{제품명}.{ext}, product.image 갱신."""
    con = connect()
    try:
        p = con.execute("SELECT name, image FROM product WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "제품 없음")
        m = re.match(r"data:image/([\w.+-]+);base64,(.+)$", body.get("data") or "", re.S)
        if not m:
            raise HTTPException(400, "이미지 데이터가 올바르지 않습니다")
        ext = m.group(1).lower()
        ext = {"jpeg": "jpg", "svg+xml": "svg"}.get(ext, ext)
        if ext not in ("png", "jpg", "jpeg", "webp", "gif"):
            raise HTTPException(400, "지원 형식: png · jpg · webp · gif")
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except Exception:
            raise HTTPException(400, "이미지 디코딩 실패")
        if len(raw) > 8 * 1024 * 1024:
            raise HTTPException(400, "이미지는 8MB 이하만 가능합니다")
        IMAGE_DIR.mkdir(exist_ok=True)
        fname = f"{_safe_name(p['name'])}.{ext}"
        old = p["image"]
        if old and old != fname:   # 확장자 바뀌면 옛 파일 제거
            try:
                (IMAGE_DIR / old).unlink(missing_ok=True)
            except OSError:
                pass
        (IMAGE_DIR / fname).write_bytes(raw)
        con.execute("UPDATE product SET image=? WHERE id=?", (fname, pid))
        audit(con, "product_image", f"{p['name']} -> {fname} ({len(raw)}B)")
        bump_masters()
        con.commit()
        return {"image": fname}
    finally:
        con.close()


@app.delete("/api/product/{pid}/image")
def product_image_del(pid: int):
    con = connect()
    try:
        p = con.execute("SELECT image FROM product WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "제품 없음")
        if p["image"]:
            try:
                (IMAGE_DIR / p["image"]).unlink(missing_ok=True)
            except OSError:
                pass
        con.execute("UPDATE product SET image='' WHERE id=?", (pid,))
        bump_masters()
        con.commit()
        return {"ok": True}
    finally:
        con.close()


# ── 일일 생산 현장 사진 ───────────────────────
@app.post("/api/day/{date}/photo")
def day_photo_add(date: str, body: dict):
    """생산 현장 사진 저장 — data:image/…;base64,… → DayPhoto/{date}_{seq}.{ext}."""
    m = re.match(r"data:image/([\w.+-]+);base64,(.+)$", body.get("data") or "", re.S)
    if not m:
        raise HTTPException(400, "이미지 데이터가 올바르지 않습니다")
    ext = m.group(1).lower()
    ext = {"jpeg": "jpg", "svg+xml": "svg"}.get(ext, ext)
    if ext not in ("png", "jpg", "jpeg", "webp", "gif"):
        raise HTTPException(400, "지원 형식: png · jpg · webp · gif")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except Exception:
        raise HTTPException(400, "이미지 디코딩 실패")
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(400, "이미지는 8MB 이하만 가능합니다")
    con = connect()
    try:
        PHOTO_DIR.mkdir(exist_ok=True)
        n = con.execute("SELECT COUNT(*) c FROM day_photo WHERE date=?", (date,)).fetchone()["c"]
        # 파일명 충돌 방지: 이미 있으면 seq 증가
        seq = n + 1
        while (PHOTO_DIR / f"{date}_{seq}.{ext}").exists():
            seq += 1
        fname = f"{date}_{seq}.{ext}"
        (PHOTO_DIR / fname).write_bytes(raw)
        cur = con.execute("INSERT INTO day_photo(date, file, note) VALUES(?,?,?)",
                          (date, fname, (body.get("note") or "")[:200]))
        con.commit()
        return {"id": cur.lastrowid, "file": fname}
    finally:
        con.close()


@app.delete("/api/day/photo/{pid}")
def day_photo_del(pid: int):
    con = connect()
    try:
        row = con.execute("SELECT file FROM day_photo WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "사진 없음")
        try:
            (PHOTO_DIR / row["file"]).unlink(missing_ok=True)
        except OSError:
            pass
        con.execute("DELETE FROM day_photo WHERE id=?", (pid,))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


# ── 백업 / 복원 / 데이터 점검 / 변경 이력 (관리 도구) ──────────
def backup_dir():
    """백업 저장 폴더 — 관리에서 지정한 경로(구글/네이버 드라이브 동기화 폴더 등)가 있으면 그걸, 없으면 기본 백업 폴더.
    지정 경로를 만들 수 없으면 안전하게 기본 폴더로 되돌린다."""
    d = get_app_setting("backup_dir", "")
    if d:
        try:
            p = Path(d)
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            pass
    BACKUP_DIR.mkdir(exist_ok=True)
    return BACKUP_DIR


def _cleanup_backups(bdir):
    """보관 기간(일)이 지난 '자동백업'만 삭제 — 수동백업·복원전·업데이트전 스냅샷은 유지.
    보관 기간 0이면 삭제하지 않음(무제한 보관)."""
    try:
        keep_days = int(float(get_app_setting("backup_keep_days", "30") or 30))
    except ValueError:
        keep_days = 30
    if keep_days <= 0:
        return
    cutoff = dt.datetime.now() - dt.timedelta(days=keep_days)
    for p in bdir.glob("자동백업_*.db"):
        try:
            if dt.datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
                p.unlink()
        except OSError:
            pass


def do_backup(tag="자동백업"):
    """sqlite3 온라인 백업 API — 사용 중(WAL)에도 안전하게 스냅샷. 지정 폴더에 저장."""
    bdir = backup_dir()
    name = f"{tag}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    src = connect()
    try:
        dest = sqlite3.connect(str(bdir / name))
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    _cleanup_backups(bdir)
    return name


def _backup_scheduler():
    """설정한 주기(시간)마다 자동백업 — 가장 최근 자동백업이 주기보다 오래됐으면 새로 생성.
    주기 0이면 자동백업 끔. 보관 기간은 do_backup 안에서 적용."""
    while True:
        try:
            try:
                interval = float(get_app_setting("backup_interval_hours", "24") or 24)
            except ValueError:
                interval = 24.0
            if interval > 0:
                bdir = backup_dir()
                autos = sorted(bdir.glob("자동백업_*.db"), key=lambda x: x.stat().st_mtime)
                due = True
                if autos:
                    last = dt.datetime.fromtimestamp(autos[-1].stat().st_mtime)
                    due = (dt.datetime.now() - last).total_seconds() >= interval * 3600
                if due:
                    do_backup()
                _cleanup_backups(backup_dir())   # 주기 도래 전에도 오래된 건 정리
        except Exception:
            pass
        time.sleep(600)   # 10분마다 확인


def _backfill_matin_po():
    """v1.24 이전 발주 입고분의 material_in에 거래처·단가 소급 기록.
    거래처가 빈 발주 유래 행만 대상 — 발주서에서 거래처명·그 품목 단가를 찾아 채운다 (멱등)."""
    con = connect()
    try:
        targets = con.execute("""SELECT id, material_id, note FROM material_in
            WHERE note LIKE '발주 #%' AND COALESCE(partner,'')=''""").fetchall()
        for r in targets:
            m = re.match(r"발주 #(\d+)", r["note"] or "")
            if not m:
                continue
            po = con.execute("""SELECT po.items, COALESCE(pa.name, NULLIF(po.partner_name,''), '') pname
                FROM purchase_order po LEFT JOIN partner pa ON pa.id=po.partner_id
                WHERE po.id=?""", (int(m.group(1)),)).fetchone()
            if not po:
                continue
            price = 0
            try:
                for it in json.loads(po["items"] or "[]"):
                    if it.get("material_id") == r["material_id"]:
                        price = it.get("price") or 0
            except ValueError:
                pass
            if po["pname"] or price:
                con.execute("UPDATE material_in SET partner=?, price=? WHERE id=?",
                            (po["pname"], float(price), r["id"]))
        con.commit()
    except Exception:
        pass
    finally:
        con.close()


def _expiry_alert_once():
    """소비기한 임박(7일 이내)·만료 LOT + 안전재고 미달 자재를 채팅 시스템 메시지로 — 해당 건이 있을 때만."""
    con = connect()
    try:
        today = dt.date.today()
        upto = today.isoformat()
        found = []   # (dleft, 제품명, 수량, 기한)
        for p in con.execute("SELECT id, name FROM product WHERE status!='단종'"):
            for l in current_lots(con, p["id"], upto)["lots"]:
                if not l["expiry"]:
                    continue
                try:
                    dleft = (dt.date.fromisoformat(l["expiry"]) - today).days
                except ValueError:
                    continue
                if dleft <= 7:
                    found.append((dleft, p["name"], l["qty"], l["expiry"]))
        if found:
            n_exp = sum(1 for f in found if f[0] < 0)
            lines = [f"⏰ 소비기한 아침 알림 — 만료 {n_exp}건 · 임박(7일 이내) {len(found) - n_exp}건"]
            for dleft, name, qty, exp in sorted(found)[:8]:
                tag = f"D+{-dleft} 만료" if dleft < 0 else ("오늘 만료" if dleft == 0 else f"D-{dleft}")
                lines.append(f"· {name} {tag} ({qty:g}개, 기한 {exp})")
            if len(found) > 8:
                lines.append(f"· 외 {len(found) - 8}건 — LOT 관리에서 확인")
            chat_system("\n".join(lines))
        # 안전재고 미달 자재 (미발주분만) — lowstock와 동일 기준
        low = rows(con.execute("""
            SELECT m.name, m.unit, m.safety_stock safety, md.real_qty stock, md.order_qty, md.order_date
            FROM material m
            JOIN (SELECT material_id, real_qty, order_qty, order_date,
                         ROW_NUMBER() OVER (PARTITION BY material_id ORDER BY date DESC) rn
                  FROM material_daily) md ON md.material_id=m.id AND md.rn=1
            WHERE m.status!='중단' AND m.safety_stock>0 AND md.real_qty < m.safety_stock
            ORDER BY (md.real_qty - m.safety_stock)"""))
        todo = [r for r in low if not ((r["order_qty"] or 0) > 0 or (r["order_date"] or ""))]
        if todo:
            lines = [f"📦 안전재고 미달 아침 알림 — {len(todo)}종 발주 필요"]
            for r in todo[:8]:
                short = round((r["safety"] or 0) - (r["stock"] or 0), 1)
                lines.append(f"· {r['name']} 재고 {r['stock']:g}{r['unit'] or ''} (부족 {short:g})")
            if len(todo) > 8:
                lines.append(f"· 외 {len(todo) - 8}종 — 발주 관리에서 확인")
            chat_system("\n".join(lines))
    finally:
        con.close()


def _alert_scheduler():
    """30분마다 확인 — 아침 7시 이후 하루 1회 소비기한 알림 (게시 여부와 무관하게 하루 1번만 검사)."""
    while True:
        try:
            now = dt.datetime.now()
            if now.hour >= 7:
                run = False
                con = connect()
                try:
                    r = con.execute("SELECT value FROM app_setting WHERE key='expiry_alert_date'").fetchone()
                    if (r["value"] if r else "") != now.date().isoformat():
                        con.execute("INSERT OR REPLACE INTO app_setting(key,value) VALUES('expiry_alert_date',?)",
                                    (now.date().isoformat(),))
                        con.commit()
                        run = True
                finally:
                    con.close()
                if run:
                    _expiry_alert_once()
        except Exception:
            pass
        time.sleep(1800)


@app.get("/api/backups")
def backups_list(request: Request):
    require_admin(request)
    bdir = backup_dir()
    out = []
    for p in sorted(bdir.glob("*.db"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size,
                    "at": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    return out


@app.get("/api/backupsettings")
def backup_settings_get(request: Request):
    require_admin(request)
    try:
        interval = float(get_app_setting("backup_interval_hours", "24") or 24)
    except ValueError:
        interval = 24.0
    try:
        keep = int(float(get_app_setting("backup_keep_days", "30") or 30))
    except ValueError:
        keep = 30
    return {"dir": get_app_setting("backup_dir", "") or str(BACKUP_DIR),
            "default_dir": str(BACKUP_DIR), "custom": bool(get_app_setting("backup_dir", "")),
            "interval_hours": interval, "keep_days": keep}


@app.post("/api/backupsettings")
def backup_settings_save(request: Request, body: dict):
    require_admin(request)
    d = (body.get("dir") or "").strip()
    # 기본 폴더와 같으면 '지정 안 함'으로 저장 (기본 폴더 사용)
    if d and d != str(BACKUP_DIR):
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(400, f"백업 폴더를 만들 수 없습니다: {e}")
        set_app_setting("backup_dir", d)
    else:
        set_app_setting("backup_dir", "")
    try:
        interval = max(0.0, float(body.get("interval_hours") or 0))
    except (TypeError, ValueError):
        interval = 24.0
    try:
        keep = max(0, int(float(body.get("keep_days") or 0)))
    except (TypeError, ValueError):
        keep = 30
    set_app_setting("backup_interval_hours", str(interval))
    set_app_setting("backup_keep_days", str(keep))
    return {"ok": True, "dir": str(backup_dir())}


@app.post("/api/pickfolder")
def pick_folder(request: Request):
    """서버(=같은 PC)에서 윈도우 탐색기 '폴더 선택' 창을 띄워 경로를 고른다.
    브라우저는 보안상 서버 경로를 직접 못 고르므로 로컬 앱에서만 쓰는 방식."""
    require_admin(request)
    import subprocess
    ps = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
          "Add-Type -AssemblyName System.Windows.Forms;"
          "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
          "$f.Description='백업 폴더를 선택하세요';$f.ShowNewFolderButton=$true;"
          "if($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){[Console]::Out.Write($f.SelectedPath)}")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                             capture_output=True, timeout=300,
                             creationflags=0x08000000)   # CREATE_NO_WINDOW (콘솔 숨김, 선택창은 표시)
        return {"path": out.stdout.decode("utf-8", "replace").strip()}
    except Exception as e:
        raise HTTPException(500, f"폴더 선택 창을 열 수 없습니다: {e}")


@app.post("/api/backup")
def backup_now(request: Request):
    require_admin(request)
    name = do_backup("수동백업")
    con = connect()
    try:
        audit(con, "backup", name)
        con.commit()
    finally:
        con.close()
    return {"name": name}


@app.post("/api/backup/restore")
def backup_restore(request: Request, body: dict):
    require_admin(request)
    name = body.get("name") or ""
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".db"):
        raise HTTPException(400, "잘못된 백업 파일명입니다")
    path = backup_dir() / name
    if not path.exists():
        raise HTTPException(404, "백업 파일이 없습니다")
    safety = do_backup("복원전")   # 복원 직전 상태도 남김 — 복원 자체를 되돌릴 수 있게
    src = sqlite3.connect(str(path))
    live = connect()
    try:
        src.backup(live)           # 백업본 → 라이브 DB (온라인 복원)
        audit(live, "restore", f"{name} 복원 (직전 상태: {safety})")
        bump_masters()
        live.commit()
    finally:
        src.close()
        live.close()
    return {"ok": True, "safety": safety}


@app.get("/api/integrity")
def integrity_check(request: Request):
    """데이터 무결성 점검 — 자재 체인/자동차감/음수재고/완제품 음수/고아 레코드."""
    require_admin(request)
    con = connect()
    try:
        usage = {}
        for r in con.execute("SELECT material_id m, date d, SUM(qty) q FROM material_usage GROUP BY m, d"):
            usage[(r["m"], r["d"])] = r["q"]
        chain, auto_bad, neg = [], [], []
        mids = [r["material_id"] for r in con.execute(
            "SELECT DISTINCT material_id FROM material_daily").fetchall()]
        for mid in mids:
            rows_ = con.execute("SELECT * FROM material_daily WHERE material_id=? ORDER BY date",
                                (mid,)).fetchall()
            nm_r = con.execute("SELECT name FROM material WHERE id=?", (mid,)).fetchone()
            nm = nm_r["name"] if nm_r else str(mid)
            run = rows_[0]["prev_qty"]
            for r in rows_:
                if abs(r["prev_qty"] - run) > 0.005:
                    chain.append(f"{nm} · {r['date']}")
                if r["src"] == "auto":
                    us = usage.get((mid, r["date"]), 0)
                    if abs(r["used_qty"] - us) > 0.005:
                        auto_bad.append(f"{nm} · {r['date']}")
                    run = run + r["in_qty"] - r["used_qty"]
                else:
                    run = r["real_qty"]
            if run < -0.005:
                neg.append(f"{nm} ({round(run, 3)})")
        pneg = [f"{r['name']} ({r['stock']:g})" for r in con.execute("""
            SELECT p.name,
                   COALESCE(os.qty,0)+COALESCE(pb.q,0)-COALESCE(sb.q,0)-COALESCE(dp.q,0) stock
            FROM product p
            LEFT JOIN opening_stock os ON os.kind='product' AND os.ref_id=p.id
            LEFT JOIN (SELECT product_id, SUM(prod_qty) q FROM production GROUP BY product_id) pb ON pb.product_id=p.id
            LEFT JOIN (SELECT product_id, SUM(qty) q FROM shipment GROUP BY product_id) sb ON sb.product_id=p.id
            LEFT JOIN (SELECT product_id, SUM(qty) q FROM disposal GROUP BY product_id) dp ON dp.product_id=p.id
            WHERE COALESCE(os.qty,0)+COALESCE(pb.q,0)-COALESCE(sb.q,0)-COALESCE(dp.q,0) < -0.5""")]
        orphans = con.execute("""SELECT
            (SELECT COUNT(*) FROM staffing_agency sa
              WHERE NOT EXISTS(SELECT 1 FROM staffing st WHERE st.id=sa.staffing_id))
          + (SELECT COUNT(*) FROM staffing_member sm
              WHERE NOT EXISTS(SELECT 1 FROM staffing st WHERE st.id=sm.staffing_id)) c""").fetchone()["c"]
        return {"materials": len(mids), "chain": chain[:30], "auto_bad": auto_bad[:30],
                "negative": neg[:30], "product_negative": pneg[:30], "orphans": orphans,
                "ok": not (chain or auto_bad or neg or pneg or orphans)}
    finally:
        con.close()


@app.post("/api/integrity/fix")
def integrity_fix(request: Request):
    """체인 자동 복구 — 자재별 첫 기록일 기준으로 이후 전일재고 체인 전체 재계산."""
    require_admin(request)
    con = connect()
    try:
        n = 0
        for r in con.execute(
                "SELECT material_id, MIN(date) d FROM material_daily GROUP BY material_id").fetchall():
            ripple_material(con, r["material_id"], r["d"])
            n += 1
        audit(con, "integrity_fix", f"자재 {n}종 체인 재계산")
        bump_masters()
        con.commit()
        return {"fixed": n}
    finally:
        con.close()


@app.post("/api/masters/{mtype}/bulkset")
def master_bulkset(request: Request, mtype: str, body: dict):
    """CSV 일괄 가져오기 — 이름 매칭으로 단가/소비일/안전재고/시급만 갱신 (admin)."""
    require_admin(request)
    allowed = {"product": {"unit_price", "shelf_days", "safety_stock"},
               "raw": {"unit_price", "safety_stock", "pack_count", "shelf_days"},
               "sub": {"unit_price", "safety_stock", "pack_count", "shelf_days"},
               "staff": {"wage"}}
    if mtype not in allowed:
        raise HTTPException(400, "이 탭은 일괄 가져오기를 지원하지 않습니다")
    table = "material" if mtype in ("raw", "sub", "semi") else mtype
    fields = allowed[mtype]
    con = connect()
    try:
        applied, missed = 0, []
        for r in (body.get("rows") or []):
            name = (r.get("name") or "").strip()
            if not name:
                continue
            sets = {}
            for f in fields:
                v = r.get(f)
                if v is None or str(v).strip() == "":
                    continue
                fv = float(str(v).replace(",", ""))
                if fv < 0:
                    raise HTTPException(400, f"'{name}' 음수 값은 적용할 수 없습니다")
                sets[f] = fv
            if not sets:
                continue
            where = "name=?" + (" AND kind=?" if table == "material" else "")
            params = list(sets.values()) + [name] + ([mtype] if table == "material" else [])
            cur = con.execute(f"UPDATE {table} SET {','.join(f + '=?' for f in sets)} WHERE {where}",
                              params)
            if cur.rowcount:
                applied += 1
            else:
                missed.append(name)
        audit(con, "bulk_import", f"{mtype}: {applied}건 적용, 미매칭 {len(missed)}건")
        bump_masters()
        con.commit()
        return {"applied": applied, "missed": missed[:50], "missed_total": len(missed)}
    finally:
        con.close()


def sync_pack_set_col(con):
    """표시 호환용 material.pack_set 갱신 — 여러 세트면 콤마로 (정본은 pack_set_member)."""
    con.execute("UPDATE material SET pack_set=''")
    con.execute("""UPDATE material SET pack_set=(
        SELECT GROUP_CONCAT(set_name, ', ') FROM (
          SELECT set_name FROM pack_set_member WHERE material_id=material.id ORDER BY set_name))
        WHERE EXISTS(SELECT 1 FROM pack_set_member WHERE material_id=material.id)""")


@app.get("/api/packsets")
def list_packsets(request: Request):
    """포장 세트 목록 + 구성원 (관리 팝업의 '세트 목록' 탭)."""
    con = connect()
    try:
        out = {}
        for r in con.execute("""SELECT s.set_name, m.id, m.name, m.pack_count
            FROM pack_set_member s JOIN material m ON m.id=s.material_id
            ORDER BY s.set_name, m.name"""):
            out.setdefault(r["set_name"], []).append(
                {"id": r["id"], "name": r["name"], "pack_count": r["pack_count"]})
        return [{"name": k, "members": v} for k, v in out.items()]
    finally:
        con.close()


@app.post("/api/packset")
def save_packset(request: Request, body: dict):
    """포장 세트 저장 — 구성원 교체. 한 자재가 여러 세트에 동시에 속할 수 있다(다대다).
    rename이 오면 세트 이름 변경."""
    require_admin(request)
    name = (body.get("name") or "").strip()
    rename = (body.get("rename") or "").strip()   # 기존 이름(수정 시)
    mids = [int(x) for x in (body.get("mids") or [])]
    if not name:
        raise HTTPException(400, "세트 이름을 입력하세요")
    con = connect()
    try:
        if rename and rename != name:
            if con.execute("SELECT 1 FROM pack_set_member WHERE set_name=?", (name,)).fetchone():
                raise HTTPException(400, f"'{name}' 세트가 이미 있습니다 — 다른 이름을 쓰세요")
            con.execute("UPDATE lot_plan SET pack_set=? WHERE pack_set=?", (name, rename))
            con.execute("DELETE FROM pack_set_member WHERE set_name=?", (rename,))
        # 이 세트의 구성원만 교체 — 다른 세트 소속은 건드리지 않는다 (중복 소속 허용)
        con.execute("DELETE FROM pack_set_member WHERE set_name=?", (name,))
        for mid in mids:
            con.execute("INSERT OR IGNORE INTO pack_set_member(set_name, material_id) VALUES(?,?)",
                        (name, mid))
        sync_pack_set_col(con)
        audit(con, "pack_set", f"{name}: {len(mids)}종" + (f" (이름변경: {rename})" if rename and rename != name else ""))
        bump_masters()
        con.commit()
        return {"ok": True, "count": len(mids)}
    finally:
        con.close()


@app.delete("/api/packset/{name}")
def delete_packset(request: Request, name: str):
    """포장 세트 삭제 — 자재 자체는 그대로 두고 묶음만 해제."""
    require_admin(request)
    con = connect()
    try:
        n = con.execute("DELETE FROM pack_set_member WHERE set_name=?", (name,)).rowcount
        if not n:
            raise HTTPException(404, "세트를 찾을 수 없습니다")
        # 이 세트로 지정된 LOT 구간은 포장 미지정으로 (자재 소모 계산에서 빠짐)
        used = con.execute("SELECT COUNT(*) c FROM lot_plan WHERE pack_set=?", (name,)).fetchone()["c"]
        con.execute("UPDATE lot_plan SET pack_set='' WHERE pack_set=?", (name,))
        sync_pack_set_col(con)
        audit(con, "pack_set", f"{name} 삭제 ({n}종 해제, LOT 구간 {used}건 포장 해제)")
        bump_masters()
        con.commit()
        return {"ok": True, "released": n, "lots": used}
    finally:
        con.close()


# ── 자동 업데이트 ─────────────────────────────────────
def _vtuple(v):
    """'1.2.10' → (1,2,10). 비교용 — 자리수 달라도 안전."""
    out = []
    for part in str(v or "").strip().split("."):
        n = "".join(ch for ch in part if ch.isdigit())
        out.append(int(n) if n else 0)
    return tuple(out)


def version_newer(latest, current):
    a, b = _vtuple(latest), _vtuple(current)
    n = max(len(a), len(b))
    a += (0,) * (n - len(a)); b += (0,) * (n - len(b))
    return a > b


def _nocache_request(url):
    """GitHub 릴리스 자산 CDN(Fastly) 캐시 우회 — 새 릴리스 직후 엣지 노드가
    같은 파일명(version.json·RebyStock.exe)의 이전 자산을 최대 수 분 캐싱한다.
    쿼리 캐시버스터로 캐시 키를 바꾸고 no-cache 헤더를 함께 보낸다."""
    import urllib.request
    sep = "&" if "?" in url else "?"
    busted = f"{url}{sep}_cb={int(time.time())}"
    return urllib.request.Request(busted, headers={
        "User-Agent": "martin_stock-updater",
        "Cache-Control": "no-cache", "Pragma": "no-cache"})


def fetch_manifest():
    """version.json 읽기 — {version, url, notes, sha256?}. 실패 시 예외."""
    import urllib.request
    url = manifest_url()
    if not url:
        raise RuntimeError("업데이트 주소가 설정되지 않았습니다 (update_url.txt)")
    if not url.lower().startswith("https://"):
        raise RuntimeError("업데이트 주소는 https 여야 합니다")
    req = _nocache_request(url)
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data.get("version") or not data.get("url"):
        raise RuntimeError("버전 정보 형식이 올바르지 않습니다 (version/url 필요)")
    if not str(data["url"]).lower().startswith("https://"):
        raise RuntimeError("다운로드 주소는 https 여야 합니다")
    return data


@app.get("/api/update/check")
def update_check(request: Request):
    require_admin(request)
    info = {"current": APP_VERSION, "frozen": bool(getattr(sys, "frozen", False)),
            "configured": bool(manifest_url())}
    if not manifest_url():
        info["error"] = "업데이트 주소가 설정되지 않았습니다"
        return info
    try:
        m = fetch_manifest()
    except Exception as e:
        info["error"] = str(e)
        return info
    info.update(latest=m["version"], notes=m.get("notes", ""), url=m["url"],
                newer=version_newer(m["version"], APP_VERSION))
    return info


@app.post("/api/update/apply")
def update_apply(request: Request):
    """새 exe 다운로드 → 검증 → DB 백업 → 교체 배치 실행 → 서버 종료(자동 재시작).
    exe(frozen) 실행일 때만 동작. 실패해도 현재 실행본은 그대로 유지된다."""
    require_admin(request)
    if not getattr(sys, "frozen", False):
        raise HTTPException(400, "개발 모드에서는 자동 업데이트를 쓸 수 없습니다 (exe 실행 시에만)")
    try:
        m = fetch_manifest()
    except Exception as e:
        raise HTTPException(502, f"버전 정보를 읽지 못했습니다: {e}")
    if not version_newer(m["version"], APP_VERSION):
        raise HTTPException(400, "이미 최신 버전입니다")

    exe = Path(sys.executable)                       # 현재 실행 중인 exe (…/재고관리.exe)
    newexe = exe.with_name(exe.stem + "_업데이트" + exe.suffix)
    import urllib.request
    try:
        req = _nocache_request(m["url"])   # exe도 CDN 캐시 우회 (새 릴리스 직후 이전 exe 방지)
        with urllib.request.urlopen(req, timeout=120) as r, open(newexe, "wb") as f:
            raw = r.read()
            f.write(raw)
    except Exception as e:
        try: newexe.unlink(missing_ok=True)
        except OSError: pass
        raise HTTPException(502, f"다운로드 실패: {e}")
    # 검증: 최소 크기 + (있으면) sha256
    if len(raw) < 1_000_000:
        newexe.unlink(missing_ok=True)
        raise HTTPException(502, "받은 파일이 너무 작습니다 — 다운로드가 온전치 않습니다")
    want = (m.get("sha256") or "").lower().strip()
    if want:
        got = hashlib.sha256(raw).hexdigest()
        if got != want:
            newexe.unlink(missing_ok=True)
            raise HTTPException(502, "체크섬이 일치하지 않습니다 — 교체를 중단했습니다")
    # 교체 전 DB 백업 (혹시 새 버전 마이그레이션 문제 대비)
    try:
        do_backup("업데이트전백업")
    except Exception:
        pass
    # 교체 배치: 이 exe가 종료되길 기다렸다 새 파일로 바꾸고 재실행 후 자기 삭제.
    #  교체 후 백신 스캔이 끝나도록 잠깐 대기 → 재실행. 실패해도 새 파일을 직접 실행하고 로그를 남긴다.
    bat = exe.with_name("_자동업데이트.bat")
    bat.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "title 재고관리 업데이트\r\n"
        'cd /d "%~dp0"\r\n'
        'echo [%date% %time%] 업데이트 적용 시작 (%~dp0) > "_업데이트로그.txt"\r\n'
        "echo  업데이트 적용 중입니다. 잠시만 기다려 주세요...\r\n"
        "timeout /t 2 /nobreak >nul\r\n"       # 실행 중이던 exe가 완전히 종료되도록 대기
        "set N=0\r\n"
        ":wait\r\n"
        f'move /y "{newexe.name}" "{exe.name}" >> "_업데이트로그.txt" 2>&1\r\n'
        "if not errorlevel 1 goto done\r\n"
        "set /a N+=1\r\n"
        "if %N% GEQ 60 goto fallback\r\n"      # 60초까지 재시도 (파일 잠김 대비)
        "timeout /t 1 /nobreak >nul\r\n"
        "goto wait\r\n"
        ":done\r\n"
        f'echo [%date% %time%] 파일 교체 완료 >> "_업데이트로그.txt"\r\n'
        "timeout /t 2 /nobreak >nul\r\n"       # 교체 직후 백신 스캔 대기
        f'echo [%date% %time%] 재실행(탐색기) >> "_업데이트로그.txt"\r\n'
        # 탐색기로 실행 = 사용자가 더블클릭한 것과 같은 방식 (start가 막히는 환경 대비)
        f'explorer.exe "%~dp0{exe.name}"\r\n'
        "echo.\r\n"
        "echo ==================================================\r\n"
        "echo   업데이트가 완료되었습니다. 프로그램을 다시 실행했습니다.\r\n"
        "echo   잠시 뒤에도 프로그램(브라우저) 창이 안 뜨면, 이 폴더의\r\n"
        f"echo      {exe.name}\r\n"
        "echo   파일을 직접 두 번 눌러 실행해 주세요. (이 창은 곧 닫힙니다)\r\n"
        "echo ==================================================\r\n"
        "timeout /t 12 /nobreak\r\n"
        'del "%~f0" >nul 2>&1\r\n'
        "exit\r\n"
        ":fallback\r\n"                          # 교체 실패 시: 새 버전 파일을 직접 실행 안내
        f'echo [%date% %time%] 파일 교체 실패 >> "_업데이트로그.txt"\r\n'
        "echo.\r\n"
        f"echo  업데이트 파일 교체에 실패했습니다. 이 폴더의  {newexe.name}  파일을\r\n"
        "echo  직접 실행해 주세요. (아무 키나 누르면 창이 닫힙니다)\r\n"
        "pause >nul\r\n"
        "exit\r\n", encoding="utf-8")

    def _relaunch():
        import subprocess
        time.sleep(0.6)
        # 새 콘솔에서 배치 실행 → 이 프로세스 종료 후 교체·재시작
        subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(exe.parent),
                         creationflags=0x00000010)   # CREATE_NEW_CONSOLE
        time.sleep(0.4)
        os._exit(0)
    threading.Thread(target=_relaunch, daemon=True).start()
    return {"ok": True, "version": m["version"]}


@app.get("/api/lowstock")
def lowstock(request: Request):
    """사이드바 '발주 필요' 알림용.
    안전재고가 설정된 자재만 실제 알림 대상 — 미설정은 판단 기준이 없어 건수만 안내한다
    (전부 알리면 안 쓰는 자재까지 매일 떠서 알림이 무시된다).
    이미 발주한 건(발주량/발주일 기록)은 ordered로 표시해 중복 발주를 막는다."""
    con = connect()
    try:
        items = rows(con.execute("""
            SELECT m.id, m.kind, m.name, m.unit, m.safety_stock safety, md.real_qty stock,
                   md.order_qty, md.order_date
            FROM material m
            JOIN (SELECT material_id, real_qty, order_qty, order_date, date,
                         ROW_NUMBER() OVER (PARTITION BY material_id ORDER BY date DESC) rn
                  FROM material_daily) md ON md.material_id=m.id AND md.rn=1
            WHERE m.status!='중단' AND m.safety_stock>0 AND md.real_qty < m.safety_stock
            ORDER BY (md.real_qty - m.safety_stock) LIMIT 40"""))
        for r in items:
            r["shortfall"] = round((r["safety"] or 0) - (r["stock"] or 0), 3)
            r["ordered"] = bool((r["order_qty"] or 0) > 0 or (r["order_date"] or ""))
        # 안전재고를 설정한 자재만 대상 — 미설정 자재는 판단 기준이 없어 어디에도 세지 않는다 (대시보드와 동일 기준)
        return {"items": items}
    finally:
        con.close()


def latest_material_prices(con, upto=None):
    """자재별 최신 실입고 단가 {mid: price}.
    발주 입고 단가(purchase_order.items) + 일일 입고 직접 단가(material_in, 발주 유래 행 제외)를
    날짜순으로 합쳐 각 자재의 가장 최근 값을 남긴다. upto(YYYY-MM-DD) 지정 시 그 날짜까지만."""
    priced = []   # (날짜, material_id, price)
    q1 = "SELECT received_at, items FROM purchase_order WHERE received_at!=''"
    p1 = []
    if upto:
        q1 += " AND substr(received_at,1,10)<=?"
        p1.append(upto)
    q1 += " ORDER BY received_at"
    for r in con.execute(q1, p1):
        try:
            its = json.loads(r["items"] or "[]")
        except ValueError:
            continue
        for it in its:
            if it.get("material_id") and (it.get("price") or 0) > 0:
                priced.append((r["received_at"][:10], it["material_id"], it["price"]))
    q2 = "SELECT date, material_id, price FROM material_in WHERE price>0 AND note NOT LIKE '발주 #%'"
    p2 = []
    if upto:
        q2 += " AND date<=?"
        p2.append(upto)
    q2 += " ORDER BY date"
    for r in con.execute(q2, p2):
        priced.append((r["date"], r["material_id"], r["price"]))
    # 관리자가 '적용 시작일'과 함께 지정한 단가 변경 이력도 날짜 포인트로 합친다 (같은 날짜면 수동 지정이 뒤 → 우선)
    q3 = "SELECT from_date, material_id, price FROM material_price WHERE price>0"
    p3 = []
    if upto:
        q3 += " AND from_date<=?"
        p3.append(upto)
    q3 += " ORDER BY from_date, id"
    for r in con.execute(q3, p3):
        priced.append((r["from_date"], r["material_id"], r["price"]))
    priced.sort(key=lambda x: x[0])   # 날짜 오름차순 — dict 갱신으로 각 자재의 '가장 최근' 값이 남음
    return {mid: price for _, mid, price in priced}


@app.get("/api/matprice/{mid}")
def matprice(request: Request, mid: int):
    """자재 단가 추이 — 발주 입고 처리 때 입력한 실제 단가 이력 (입고 완료 발주서의 items 스냅샷)."""
    if not mcan(request, "mat"):
        raise HTTPException(403, "단가 열람 권한이 없습니다")
    con = connect()
    try:
        m = con.execute("SELECT name, unit, unit_price FROM material WHERE id=?", (mid,)).fetchone()
        if not m:
            raise HTTPException(404, "자재를 찾을 수 없습니다")
        pts = []
        for r in con.execute("""
                SELECT po.id, po.date, po.received_at, po.items,
                       COALESCE(pa.name, po.partner_name, '') pname
                FROM purchase_order po LEFT JOIN partner pa ON pa.id=po.partner_id
                WHERE po.received_at!='' ORDER BY po.received_at"""):
            try:
                its = json.loads(r["items"] or "[]")
            except ValueError:
                continue
            for it in its:
                if it.get("material_id") == mid and (it.get("price") or 0) > 0:
                    pts.append({"date": (r["received_at"] or r["date"])[:10], "po_id": r["id"],
                                "price": it["price"], "partner": r["pname"],
                                "qty": it.get("recv") or it.get("qty") or 0})
        # 일일 입력에서 직접 입력한 입고 단가 — 발주 유래 행(note '발주 #')은 위에서 이미 집계되므로 제외
        for r in con.execute("""SELECT date, qty, price, COALESCE(partner,'') partner FROM material_in
                WHERE material_id=? AND price>0 AND note NOT LIKE '발주 #%' ORDER BY date""", (mid,)):
            pts.append({"date": r["date"], "po_id": None, "price": r["price"],
                        "partner": r["partner"], "qty": r["qty"]})
        # 관리자가 '적용 시작일'과 함께 지정한 단가 변경 이력
        manual = []
        for r in con.execute("""SELECT from_date, price FROM material_price
                WHERE material_id=? ORDER BY from_date, id""", (mid,)):
            pts.append({"date": r["from_date"], "po_id": None, "price": r["price"],
                        "partner": "단가 지정", "qty": 0, "manual": True})
            manual.append({"from_date": r["from_date"], "price": r["price"]})
        pts.sort(key=lambda x: x["date"])
        # 기간별 단가(periods) — 모든 단가 포인트를 날짜순으로 훑어 값이 바뀔 때마다 한 구간
        periods = []
        for p in pts:
            if not periods or abs(periods[-1]["price"] - p["price"]) > 1e-9:
                periods.append({"from": p["date"], "price": p["price"],
                                "source": "지정" if p.get("manual") else "입고"})
        for i in range(len(periods)):   # 각 구간의 종료일 = 다음 구간 시작 전날 표시용
            periods[i]["to"] = periods[i + 1]["from"] if i + 1 < len(periods) else ""
        return {"name": m["name"], "unit": m["unit"] or "",
                "base_price": m["unit_price"] or 0, "points": pts,
                "manual": manual, "periods": periods}
    finally:
        con.close()


@app.put("/api/matin/expiry")
def matin_expiry_set(request: Request, body: dict):
    """자재 이력에서 소비기한 직접 입력·수정. 그날 입고가 있으면 그 입고분에,
    없으면(전일·초기재고 등 입고 없는 재고) material_expiry에 저장한다."""
    require_stock_duty(request)
    mid = body.get("material_id")
    date = (body.get("date") or "").strip()
    expiry = (body.get("expiry") or "").strip()
    if not mid or not date:
        raise HTTPException(400, "자재와 날짜가 필요합니다")
    con = connect()
    try:
        has_in = con.execute("SELECT 1 FROM material_in WHERE material_id=? AND date=?",
                             (mid, date)).fetchone()
        if has_in:
            con.execute("UPDATE material_in SET expiry=? WHERE material_id=? AND date=?",
                        (expiry, mid, date))
        elif expiry:
            con.execute("""INSERT INTO material_expiry(material_id, date, expiry) VALUES(?,?,?)
                ON CONFLICT(material_id, date) DO UPDATE SET expiry=excluded.expiry""",
                        (mid, date, expiry))
        else:   # 빈 값으로 지우기
            con.execute("DELETE FROM material_expiry WHERE material_id=? AND date=?", (mid, date))
        audit(con, "matin_expiry", f"자재#{mid} {date} 소비기한 → {expiry or '(제거)'}")
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.put("/api/matin/made")
def matin_made_set(request: Request, body: dict):
    """자재 이력에서 제조일자 직접 입력·수정. 그날 입고가 있으면 그 입고분에,
    없으면(전일·초기재고 등) material_expiry.made에 저장한다. (소비기한과 동일한 방식)"""
    require_stock_duty(request)
    mid = body.get("material_id")
    date = (body.get("date") or "").strip()
    made = (body.get("made") or "").strip()
    if not mid or not date:
        raise HTTPException(400, "자재와 날짜가 필요합니다")
    con = connect()
    try:
        has_in = con.execute("SELECT 1 FROM material_in WHERE material_id=? AND date=?",
                             (mid, date)).fetchone()
        if has_in:
            con.execute("UPDATE material_in SET made_date=? WHERE material_id=? AND date=?",
                        (made, mid, date))
        else:
            con.execute("""INSERT INTO material_expiry(material_id, date, made) VALUES(?,?,?)
                ON CONFLICT(material_id, date) DO UPDATE SET made=excluded.made""",
                        (mid, date, made))
            # 소비기한·제조일이 모두 비면 행 정리
            con.execute("DELETE FROM material_expiry WHERE material_id=? AND date=?"
                        " AND COALESCE(expiry,'')='' AND COALESCE(made,'')=''", (mid, date))
        audit(con, "matin_made", f"자재#{mid} {date} 제조일자 → {made or '(제거)'}")
        con.commit()
        return {"ok": True}
    finally:
        con.close()


def _ledgerprint_key(date: str, src: str) -> str:
    """수불부 종류별 저장 키 — raw는 날짜 그대로(하위호환), fin/staff는 '날짜#종류'."""
    src = (src or "").strip().lower()
    return date if src in ("", "raw") else f"{date}#{src}"


@app.get("/api/ledgerprint")
def ledgerprint_get(date: str, src: str = ""):
    """수불부 '출력용' 저장본 조회 (없으면 saved=False). src=raw/fin/staff."""
    con = connect()
    try:
        r = con.execute("SELECT html, saved_at, saved_by FROM ledger_print WHERE date=?",
                        (_ledgerprint_key(date, src),)).fetchone()
        if not r or not (r["html"] or "").strip():
            return {"saved": False}
        return {"saved": True, "html": r["html"], "saved_at": r["saved_at"], "saved_by": r["saved_by"]}
    finally:
        con.close()


@app.post("/api/ledgerprint")
def ledgerprint_save(request: Request, body: dict):
    """원료 수불부 '출력용' 저장 — 사용자가 임시 수정·행선택한 화면 그대로 스냅샷 저장.
    원본 재고 데이터는 건드리지 않는다."""
    require_stock_duty(request)
    date = (body.get("date") or "").strip()
    html = body.get("html") or ""
    if not date:
        raise HTTPException(400, "날짜가 필요합니다")
    key = _ledgerprint_key(date, body.get("src"))
    con = connect()
    try:
        con.execute("""INSERT INTO ledger_print(date, html, saved_at, saved_by)
            VALUES(?,?,datetime('now','localtime'),?)
            ON CONFLICT(date) DO UPDATE SET
                html=excluded.html, saved_at=excluded.saved_at, saved_by=excluded.saved_by""",
                    (key, html, CURRENT_USER.get() or ""))
        audit(con, "ledger_print", f"{key} 출력용 저장")
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.get("/api/semibom/{semi_id}")
def semibom_get(semi_id: int):
    """반제품 레시피(원재료 구성) 조회."""
    con = connect()
    try:
        return {"rows": rows(con.execute(
            "SELECT material_id, qty_per_unit, unit FROM semi_bom WHERE semi_id=? ORDER BY id",
            (semi_id,)))}
    finally:
        con.close()


@app.post("/api/semibom/{semi_id}")
def semibom_save(semi_id: int, request: Request, body: dict):
    """반제품 레시피 저장 — 전량 교체 (원재료만)."""
    require_stock_duty(request)
    con = connect()
    try:
        con.execute("DELETE FROM semi_bom WHERE semi_id=?", (semi_id,))
        for it in body.get("items", []):
            mid = it.get("material_id")
            if not mid:
                continue
            con.execute("INSERT INTO semi_bom(semi_id, material_id, qty_per_unit, unit) VALUES(?,?,?,?)",
                        (semi_id, mid, float(it.get("qty_per_unit") or 0), it.get("unit") or ""))
        audit(con, "semi_recipe", f"반제품#{semi_id} 레시피 {len(body.get('items', []))}종")
        bump_masters()
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.get("/api/prodprice/{pid}")
def get_prodprice(request: Request, pid: int):
    """제품의 거래처별 판매 단가 — 미설정 거래처는 기본 단가(product.unit_price)를 쓴다."""
    if not mcan(request, "prod"):
        raise HTTPException(403, "단가 열람 권한이 없습니다")
    con = connect()
    try:
        p = con.execute("SELECT id, name, unit_price FROM product WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "제품 없음")
        prices = {r["partner_id"]: r["price"] for r in con.execute(
            "SELECT partner_id, price FROM product_price WHERE product_id=?", (pid,))}
        partners = rows(con.execute("""SELECT id, name FROM partner
            WHERE type!='자재공급처' AND status!='중지' ORDER BY name"""))
        for r in partners:
            r["price"] = prices.get(r["id"])
        return {"id": p["id"], "name": p["name"], "unit_price": p["unit_price"], "partners": partners}
    finally:
        con.close()


@app.post("/api/prodprice/{pid}")
def save_prodprice(request: Request, pid: int, body: dict):
    """거래처별 판매 단가 저장 — {partner_id: 단가}. 빈값·0이면 그 거래처는 기본 단가 사용."""
    require_admin(request)
    items = body.get("prices") or {}
    con = connect()
    try:
        con.execute("DELETE FROM product_price WHERE product_id=?", (pid,))
        n = 0
        for k, v in items.items():
            try:
                price = float(str(v).replace(",", "")) if str(v).strip() != "" else 0
            except ValueError:
                continue
            if price > 0:
                con.execute("INSERT INTO product_price(product_id, partner_id, price) VALUES(?,?,?)",
                            (pid, int(k), price))
                n += 1
        nm = con.execute("SELECT name FROM product WHERE id=?", (pid,)).fetchone()
        audit(con, "prod_price", f"{nm['name'] if nm else pid}: 거래처별 단가 {n}건")
        bump_masters()
        con.commit()
        return {"ok": True, "count": n}
    finally:
        con.close()


@app.get("/api/audit")
def audit_list(request: Request, q: str = "", limit: int = 300):
    require_admin(request)
    limit = min(max(int(limit or 300), 1), 1000)
    con = connect()
    try:
        if q:
            like = f"%{q}%"
            return rows(con.execute("""SELECT id, at, username, action, detail FROM audit_log
                WHERE action LIKE ? OR detail LIKE ? OR username LIKE ?
                ORDER BY id DESC LIMIT ?""", (like, like, like, limit)))
        return rows(con.execute(
            "SELECT id, at, username, action, detail FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)))
    finally:
        con.close()


# ── 접속 인원(presence) + 사내 채팅 ──────────
# 답장 대상(rt.*)까지 한 번에 — reply_to가 가리키는 원 메시지의 작성자·본문 미리보기
CHAT_SEL = """SELECT c.id, c.username, c.text, c.kind, c.mentions, c.file, c.fname, c.fkind, c.at,
  c.reply_to, c.pinned, c.edited, c.deleted,
  rt.username r_user, rt.text r_text, rt.deleted r_deleted
  FROM chat c LEFT JOIN chat rt ON rt.id=c.reply_to"""
CHAT_REACT_EMOJIS = {"👍", "✅", "❤️", "😂", "👀", "🙏"}   # 확인·공감용 (자유 입력 금지 — 목록 통제)
_PURGED = {"day": ""}      # 하루 1회만 보관주기 정리
CHAT_VER = {"v": 0}        # 수정·삭제·반응·고정 등 '기존 메시지 변경'마다 +1 → 프론트가 그날 대화를 다시 그림
DAY_SAVED_BY = {}          # 날짜 → 마지막 저장자 (동시 편집 알림 문구용 · 재시작 시 비어도 무방)


def bump_chat():
    CHAT_VER["v"] += 1


def chat_extras(con, day):
    """그날 메시지의 이모지 반응 맵과 고정(공지) 목록 — 프론트 렌더용.
    reactions = {msg_id: {emoji: [사용자…]}}, pinned = 고정 메시지 목록(최신 먼저)."""
    reactions = {}
    for r in con.execute("""SELECT cr.msg_id, cr.emoji, cr.username FROM chat_reaction cr
            JOIN chat c ON c.id=cr.msg_id WHERE c.day=?""", (day,)):
        reactions.setdefault(r["msg_id"], {}).setdefault(r["emoji"], []).append(r["username"])
    pinned = rows(con.execute(f"{CHAT_SEL} WHERE c.day=? AND c.pinned=1 AND c.deleted=0"
                              " ORDER BY c.id DESC", (day,)))
    return reactions, pinned


def chat_usernames():
    con = connect()
    try:
        return [r["username"] for r in con.execute("SELECT username FROM users ORDER BY username")]
    finally:
        con.close()


def parse_mentions(text):
    """텍스트에서 @사용자명을 찾아 ',a,b,' 형태로. (한글 이름 때문에 \\b 대신 부분일치 사용)"""
    hit = [n for n in chat_usernames() if ("@" + n) in text]
    return ("," + ",".join(hit) + ",") if hit else ""


def chat_purge_daily():
    """하루에 한 번 보관주기 지난 대화 정리 (기동 후 첫 폴링/전송 때)."""
    today = dt.date.today().isoformat()
    if _PURGED["day"] == today:
        return
    _PURGED["day"] = today
    try:
        purge_old_chat(CHAT_DIR)
    except Exception:
        pass


@app.get("/api/presence")
def presence(request: Request, after: int = 0, read: int = 0, edit: str = ""):
    """접속 인원 + 오늘자 채팅(after 이후) — 프론트가 8초마다 폴링.
    채팅창은 하루 단위 — 날짜가 바뀌면 day가 달라져 새 창처럼 비워진다 (기록은 DB에 남음).
    read=N이면 '나는 N번까지 읽음'으로 기록 (읽음 표시용).
    edit=날짜면 '내가 그 날짜를 편집 중'으로 갱신 — 같은 날짜를 보는 사람(viewers)과
    그 날짜의 현재 저장본(day_ver)·마지막 저장자(day_by)를 함께 돌려준다 (동시 편집 경고·갱신 알림용)."""
    chat_purge_daily()
    now = time.time()
    sess = request.state.user
    # 편집 중 표시 — 일일 입력 화면을 벗어나면 edit=""로 와서 목록에서 빠진다
    if edit:
        sess["editing"] = {"date": edit, "t": now}
    else:
        sess.pop("editing", None)
    online = sorted({s["username"] for s in SESSIONS.values() if now - s.get("seen", 0) < 75})
    today = dt.date.today().isoformat()
    me = request.state.user["username"]
    # 같은 날짜를 지금 보고 있는 다른 사용자 (폴링이 8초라 30초 이내면 '지금 보는 중')
    viewers = sorted({s["username"] for s in SESSIONS.values()
                      if s.get("editing") and s["editing"]["date"] == edit
                      and now - s["editing"]["t"] < 30 and s["username"] != me}) if edit else []
    day_ver = None
    if edit:
        c = connect()
        try:
            r = c.execute("SELECT updated_at FROM day_record WHERE date=?", (edit,)).fetchone()
            day_ver = r["updated_at"] if r else None
        finally:
            c.close()
    con = chat_connect()
    try:
        if read > 0:
            con.execute("""INSERT INTO chat_read(username, last_id) VALUES(?,?)
                ON CONFLICT(username) DO UPDATE SET last_id=MAX(last_id, excluded.last_id),
                                                    at=datetime('now','localtime')""", (me, read))
            con.commit()
        msgs = rows(con.execute(
            f"{CHAT_SEL} WHERE c.day=? AND c.id>? ORDER BY c.id LIMIT 200",
            (today, after)))
        last = con.execute("SELECT COALESCE(MAX(id),0) m FROM chat WHERE day=?",
                           (today,)).fetchone()["m"]
        reads = {r["username"]: r["last_id"] for r in con.execute(
            "SELECT username, last_id FROM chat_read")}
        # 나를 부른 안 읽은 메시지 수 (배지 강조용)
        myread = reads.get(me, 0)
        mention = con.execute("""SELECT COUNT(*) c FROM chat
            WHERE day=? AND id>? AND username!=? AND mentions LIKE ?""",
            (today, myread, me, f"%,{me},%")).fetchone()["c"]
        reactions, pinned = chat_extras(con, today)
    finally:
        con.close()
    return {"online": online, "count": len(online), "me": me, "messages": msgs,
            "last_id": last, "day": today, "reads": reads, "mention_unread": mention,
            "users": chat_usernames(), "mver": MASTERS_VER["v"],
            "viewers": viewers, "day_ver": day_ver, "day_by": DAY_SAVED_BY.get(edit),
            "chat_ver": CHAT_VER["v"], "reactions": reactions, "pinned": pinned}


@app.get("/api/chat/day")
def chat_day(d: str = ""):
    """지난 대화 보기 — 그날 메시지 + 대화가 있는 이전/다음 날짜 (◀ ▶ 이동용)."""
    day = d or dt.date.today().isoformat()
    con = chat_connect()
    try:
        msgs = rows(con.execute(
            f"{CHAT_SEL} WHERE c.day=? ORDER BY c.id LIMIT 500", (day,)))
        prev = con.execute("SELECT MAX(day) v FROM chat WHERE day<?", (day,)).fetchone()["v"]
        nxt = con.execute("SELECT MIN(day) v FROM chat WHERE day>?", (day,)).fetchone()["v"]
        reads = {r["username"]: r["last_id"] for r in con.execute(
            "SELECT username, last_id FROM chat_read")}
        reactions, pinned = chat_extras(con, day)
        return {"day": day, "messages": msgs, "prev": prev, "next": nxt, "reads": reads,
                "today": dt.date.today().isoformat(), "retention": CHAT_RETENTION_DAYS,
                "chat_ver": CHAT_VER["v"], "reactions": reactions, "pinned": pinned}
    finally:
        con.close()


@app.post("/api/chat")
def chat_send(request: Request, body: dict):
    text = (body.get("text") or "").strip()[:1000]
    f = body.get("file") or None
    if not text and not f:
        raise HTTPException(400, "메시지를 입력하세요")
    chat_purge_daily()
    fname = stored = fkind = ""
    if f:
        m = re.match(r"data:([\w./+-]+);base64,(.+)$", f.get("data") or "", re.S)
        if not m:
            raise HTTPException(400, "첨부 데이터가 올바르지 않습니다")
        mime = m.group(1).lower()
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except Exception:
            raise HTTPException(400, "첨부 디코딩 실패")
        if len(raw) > 8 * 1024 * 1024:
            raise HTTPException(400, "첨부는 8MB 이하만 가능합니다")
        orig = (f.get("name") or "file").replace("\\", "/").split("/")[-1][:80] or "file"
        safe = re.sub(r"[^\w.가-힣-]", "_", orig)
        fkind = "image" if mime.startswith("image/") else "file"
        CHAT_DIR.mkdir(exist_ok=True)
        seq, today = 1, dt.date.today().isoformat()
        while (CHAT_DIR / f"{today}_{seq}_{safe}").exists():
            seq += 1
        stored = f"{today}_{seq}_{safe}"
        (CHAT_DIR / stored).write_bytes(raw)
        fname = orig
    reply_to = int(body.get("reply_to") or 0)
    con = chat_connect()
    try:
        if reply_to:   # 답장 대상이 실제로 존재하는 메시지인지 확인 (엉뚱한 id 방지)
            if not con.execute("SELECT 1 FROM chat WHERE id=?", (reply_to,)).fetchone():
                reply_to = 0
        cur = con.execute(
            """INSERT INTO chat(day, username, text, mentions, file, fname, fkind, reply_to)
               VALUES(?,?,?,?,?,?,?,?)""",
            (dt.date.today().isoformat(), request.state.user["username"], text,
             parse_mentions(text), stored, fname, fkind, reply_to))
        con.commit()
        return {"id": cur.lastrowid}
    finally:
        con.close()


@app.post("/api/chat/{mid}/react")
def chat_react(request: Request, mid: int, body: dict):
    """이모지 반응 토글 — 있으면 제거, 없으면 추가 (작업 지시 확인 등)."""
    emoji = (body.get("emoji") or "").strip()
    if emoji not in CHAT_REACT_EMOJIS:
        raise HTTPException(400, "허용되지 않은 이모지입니다")
    me = request.state.user["username"]
    con = chat_connect()
    try:
        if not con.execute("SELECT 1 FROM chat WHERE id=? AND deleted=0", (mid,)).fetchone():
            raise HTTPException(404, "메시지가 없습니다")
        ex = con.execute("SELECT 1 FROM chat_reaction WHERE msg_id=? AND username=? AND emoji=?",
                         (mid, me, emoji)).fetchone()
        if ex:
            con.execute("DELETE FROM chat_reaction WHERE msg_id=? AND username=? AND emoji=?",
                        (mid, me, emoji))
        else:
            con.execute("INSERT INTO chat_reaction(msg_id, username, emoji) VALUES(?,?,?)",
                        (mid, me, emoji))
        con.commit()
        bump_chat()
        return {"ok": True, "on": not ex}
    finally:
        con.close()


@app.put("/api/chat/{mid}")
def chat_edit(request: Request, mid: int, body: dict):
    """내 메시지 본문 수정 (작성자 본인만). 첨부·시스템 메시지·삭제된 메시지는 수정 불가."""
    me = request.state.user["username"]
    text = (body.get("text") or "").strip()[:1000]
    if not text:
        raise HTTPException(400, "메시지를 입력하세요")
    con = chat_connect()
    try:
        m = con.execute("SELECT username, kind, deleted FROM chat WHERE id=?", (mid,)).fetchone()
        if not m:
            raise HTTPException(404, "메시지가 없습니다")
        if m["username"] != me or m["kind"] != "user" or m["deleted"]:
            raise HTTPException(403, "본인이 보낸 메시지만 수정할 수 있습니다")
        con.execute("UPDATE chat SET text=?, mentions=?, edited=1 WHERE id=?",
                    (text, parse_mentions(text), mid))
        con.commit()
        bump_chat()
        return {"ok": True}
    finally:
        con.close()


@app.delete("/api/chat/{mid}")
def chat_delete(request: Request, mid: int):
    """메시지 삭제 — 본인 또는 관리자. 자리는 '삭제된 메시지'로 남기고 본문·첨부만 지운다."""
    user = request.state.user
    me = user["username"]
    con = chat_connect()
    try:
        m = con.execute("SELECT username, kind, file, deleted FROM chat WHERE id=?", (mid,)).fetchone()
        if not m:
            raise HTTPException(404, "메시지가 없습니다")
        if m["deleted"]:
            return {"ok": True}
        if m["username"] != me and user["role"] != "admin":
            raise HTTPException(403, "본인이 보낸 메시지만 삭제할 수 있습니다 (관리자는 모두 가능)")
        if m["file"]:
            try:
                (CHAT_DIR / m["file"]).unlink(missing_ok=True)
            except OSError:
                pass
        con.execute("UPDATE chat SET deleted=1, text='', file='', fname='', fkind='', pinned=0 WHERE id=?",
                    (mid,))
        con.execute("DELETE FROM chat_reaction WHERE msg_id=?", (mid,))
        con.commit()
        bump_chat()
        return {"ok": True}
    finally:
        con.close()


@app.post("/api/chat/{mid}/pin")
def chat_pin(request: Request, mid: int, body: dict):
    """공지 고정/해제 — 로그인 사용자(게스트 제외, 미들웨어가 차단). 상단 배너로 표시된다."""
    pin = 1 if body.get("pin") else 0
    con = chat_connect()
    try:
        m = con.execute("SELECT kind, deleted FROM chat WHERE id=?", (mid,)).fetchone()
        if not m or m["deleted"]:
            raise HTTPException(404, "메시지가 없습니다")
        con.execute("UPDATE chat SET pinned=? WHERE id=?", (pin, mid))
        con.commit()
        bump_chat()
        return {"ok": True, "pinned": bool(pin)}
    finally:
        con.close()


@app.get("/api/chat/search")
def chat_search(request: Request, q: str = "", limit: int = 40):
    """전체 기간 대화 검색 — 본문 부분일치(삭제분 제외), 최신 먼저."""
    q = (q or "").strip()
    if len(q) < 1:
        return {"results": []}
    con = chat_connect()
    try:
        res = rows(con.execute(
            f"{CHAT_SEL} WHERE c.deleted=0 AND c.kind='user' AND c.text LIKE ? ESCAPE '\\'"
            " ORDER BY c.id DESC LIMIT ?",
            ("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", max(1, min(limit, 100)))))
        for r in res:
            r["day"] = con.execute("SELECT day FROM chat WHERE id=?", (r["id"],)).fetchone()["day"]
        return {"results": res, "q": q}
    finally:
        con.close()


# ── LOT 관리 ─────────────────────────────────
def require_prod_duty(request: Request):
    """LOT 관리(폐기·소비기한 지정)는 'LOT 관리' 담당만 (admin·전체 담당 포함)."""
    if "lot" not in duty_set(request.state.user):
        raise HTTPException(403, "LOT 관리 담당 계정만 가능합니다 — 관리자에게 담당 지정을 요청하세요")


@app.put("/api/lotexpiry")
def lot_expiry_set(request: Request, body: dict):
    """LOT(제품 × 생산일)별 소비기한 지정 — 비우면 제거(제품 소비일 폴백)."""
    require_prod_duty(request)
    pid = body.get("product_id")
    if not pid:
        raise HTTPException(400, "product_id required")
    made = body.get("made") or ""
    expiry = (body.get("expiry") or "").strip()
    con = connect()
    try:
        if expiry:
            con.execute("INSERT OR REPLACE INTO lot_expiry(product_id, made, expiry) VALUES(?,?,?)",
                        (pid, made, expiry))
        else:
            con.execute("DELETE FROM lot_expiry WHERE product_id=? AND made=?", (pid, made))
        audit(con, "lot_expiry", f"제품#{pid} LOT {made or '미상'} → {expiry or '(제거)'}")
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.get("/api/lotboard")
def lotboard(request: Request):
    """LOT 관리 화면: 전 제품의 생산일자별 재고 LOT + 요약 + 최근 폐기 이력."""
    admin = mcan(request, "prod")   # 제품 단가·재고금액 열람 권한
    con = connect()
    try:
        today = dt.date.today()
        upto = today.isoformat()
        lots, no_shelf = [], 0
        for p in con.execute("""SELECT id, name, shelf_days, image, unit_price FROM product
                WHERE status!='단종' ORDER BY sort, id"""):
            cl = current_lots(con, p["id"], upto)
            if not cl["lots"]:
                continue
            if not (p["shelf_days"] or 0):
                no_shelf += 1
            for l in cl["lots"]:
                dleft = None
                if l["expiry"]:
                    try:
                        dleft = (dt.date.fromisoformat(l["expiry"]) - today).days
                    except ValueError:
                        pass
                kept = None
                if l["made"]:
                    try:
                        kept = (today - dt.date.fromisoformat(l["made"])).days
                    except ValueError:
                        pass
                status = ("expired" if dleft is not None and dleft < 0
                          else "soon" if dleft is not None and dleft <= 7
                          else "ok" if dleft is not None else "unknown")
                lots.append({"product_id": p["id"], "name": p["name"], "image": p["image"],
                             "unit_price": (p["unit_price"] if admin else None),
                             "shelf_days": p["shelf_days"], "made": l["made"], "qty": l["qty"],
                             "expiry": l["expiry"], "days_kept": kept, "days_left": dleft,
                             "status": status, "planned": l.get("planned", False)})
        # 소비기한 임박 순 (기한 미상은 뒤로)
        lots.sort(key=lambda x: (x["expiry"] == "", x["expiry"], x["name"]))
        # 전체 이력 반환 — 목록 개수/날짜 필터는 프론트에서 처리 (전체보기 지원)
        disposals = rows(con.execute("""
            SELECT d.*, p.name FROM disposal d JOIN product p ON p.id=d.product_id
            ORDER BY d.date DESC, d.id DESC"""))
        shipments = rows(con.execute("""
            SELECT s.date, p.name, COALESCE(pa.name,'거래처 미상') partner,
                   s.qty, s.prod_date, s.expiry
            FROM shipment s JOIN product p ON p.id=s.product_id
            LEFT JOIN partner pa ON pa.id=s.partner_id
            WHERE s.qty>0 ORDER BY s.date DESC, s.id DESC"""))
        summary = {
            "expired": sum(1 for l in lots if l["status"] == "expired"),
            "soon": sum(1 for l in lots if l["status"] == "soon"),
            "ok": sum(1 for l in lots if l["status"] == "ok"),
            "unknown": sum(1 for l in lots if l["status"] == "unknown"),
            "no_shelf": no_shelf,
            "total_qty": round(sum(l["qty"] for l in lots), 3),
            "total_amount": (round(sum(l["qty"] * (l["unit_price"] or 0) for l in lots)) if admin else None),
        }
        return {"lots": lots, "summary": summary, "disposals": disposals,
                "shipments": shipments, "date": upto}
    finally:
        con.close()


@app.post("/api/disposal")
def disposal_create(request: Request, body: dict):
    require_prod_duty(request)
    pid = body.get("product_id")
    qty = float(body.get("qty") or 0)
    if not pid or qty <= 0:
        raise HTTPException(400, "제품과 폐기 수량을 입력하세요")
    date = body.get("date") or dt.date.today().isoformat()
    con = connect()
    try:
        p = con.execute("SELECT name FROM product WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "제품 없음")
        # 재고 초과 검증: 폐기 수량 ≤ 현재고 (지정 LOT이면 그 LOT 재고)
        cl = current_lots(con, pid, date)
        if body.get("prod_date"):
            avail = sum(l["qty"] for l in cl["lots"] if l["made"] == body["prod_date"])
        else:
            avail = cl["stock"]
        if qty - float(avail) > 0.5:
            raise HTTPException(400, f"폐기 수량 {qty:,.0f}개가 "
                                f"{'해당 LOT ' if body.get('prod_date') else ''}재고 {float(avail):,.0f}개를 초과합니다")
        cur = con.execute("""INSERT INTO disposal(date, product_id, qty, prod_date, reason, note)
            VALUES(?,?,?,?,?,?)""",
                          (date, pid, qty, body.get("prod_date") or "",
                           body.get("reason") or "", body.get("note") or ""))
        audit(con, "disposal", f"{date} {p['name']} {qty} ({body.get('reason','')})")
        bump_masters()
        con.commit()
        return {"id": cur.lastrowid}
    finally:
        con.close()


@app.delete("/api/disposal/{did}")
def disposal_delete(request: Request, did: int):
    require_prod_duty(request)
    con = connect()
    try:
        row = con.execute("""SELECT d.*, p.name FROM disposal d
            JOIN product p ON p.id=d.product_id WHERE d.id=?""", (did,)).fetchone()
        if not row:
            raise HTTPException(404, "폐기 기록 없음")
        con.execute("DELETE FROM disposal WHERE id=?", (did,))
        audit(con, "disposal_undo", f"{row['date']} {row['name']} {row['qty']}")
        bump_masters()
        con.commit()
        return {"ok": True}
    finally:
        con.close()


# ── 자재 입출고 이력 ──────────────────────────
@app.get("/api/mathistory/{mid}")
def mat_history(mid: int, limit: int = 40):
    con = connect()
    try:
        mat = con.execute("SELECT name, unit, kind FROM material WHERE id=?", (mid,)).fetchone()
        if not mat:
            raise HTTPException(404, "자재 없음")
        hist = rows(con.execute("""
            SELECT date, prev_qty, in_qty, used_qty, real_qty, order_date, order_qty, src
            FROM material_daily WHERE material_id=? ORDER BY date DESC LIMIT ?""", (mid, limit)))
        last_in = con.execute("""SELECT date, in_qty FROM material_daily
            WHERE material_id=? AND in_qty>0 ORDER BY date DESC LIMIT 1""", (mid,)).fetchone()
        last_use = con.execute("""SELECT date, used_qty FROM material_daily
            WHERE material_id=? AND used_qty>0 ORDER BY date DESC LIMIT 1""", (mid,)).fetchone()
        in_expiry = {}   # 날짜별 입고 유통기한 (입고 카드 기록)
        for r in con.execute("""SELECT date, GROUP_CONCAT(expiry, ', ') e FROM material_in
            WHERE material_id=? AND expiry!='' GROUP BY date""", (mid,)):
            in_expiry[r["date"]] = r["e"]
        in_made = {}     # 날짜별 입고 제조일자
        for r in con.execute("""SELECT date, GROUP_CONCAT(made_date, ', ') e FROM material_in
            WHERE material_id=? AND made_date!='' GROUP BY date""", (mid,)):
            in_made[r["date"]] = r["e"]
        in_po = {}       # 날짜별 입고가 어느 발주서에서 왔는지 (입고 처리 시 note에 '발주 #id' 기록됨)
        for r in con.execute("""SELECT date, note FROM material_in
            WHERE material_id=? AND note LIKE '발주 #%'""", (mid,)):
            m2 = re.match(r"발주 #(\d+)", r["note"] or "")
            if m2:
                in_po.setdefault(r["date"], [])
                if int(m2.group(1)) not in in_po[r["date"]]:
                    in_po[r["date"]].append(int(m2.group(1)))
        # 입고 없는 재고(전일·초기재고)에 수동으로 적은 소비기한·제조일자
        man_expiry = {r["date"]: r["expiry"] for r in con.execute(
            "SELECT date, expiry FROM material_expiry WHERE material_id=? AND expiry!=''", (mid,))}
        man_made = {r["date"]: r["made"] for r in con.execute(
            "SELECT date, made FROM material_expiry WHERE material_id=? AND COALESCE(made,'')!=''", (mid,))}
        # 최초 시작일 = 이 자재의 가장 오래된 기록일 (입고 없이 보유하던 재고의 소비기한 입력 시작점)
        start_row = con.execute("SELECT MIN(date) d FROM material_daily WHERE material_id=?", (mid,)).fetchone()
        start_date = start_row["d"] if start_row else None
        return {"name": mat["name"], "unit": mat["unit"], "kind": mat["kind"], "rows": hist,
                "in_expiry": in_expiry, "in_made": in_made, "in_po": in_po,
                "man_expiry": man_expiry, "man_made": man_made,
                "start_date": start_date,
                "last_in": dict(last_in) if last_in else None,
                "last_use": dict(last_use) if last_use else None}
    finally:
        con.close()


# ── 발주서 (자재 부족 → 거래처 주문) ──────────────────────
def require_stock_duty(request: Request):
    u = request.state.user
    if u["role"] != "admin" and "stock" not in duty_set(u):
        raise HTTPException(403, "자재(재고·입고) 담당 또는 관리자만 가능합니다")


@app.get("/api/po")
def po_list(request: Request, limit: int = 30):
    require_stock_duty(request)
    con = connect()
    try:
        out = rows(con.execute("""
            SELECT po.*, COALESCE(pa.name, NULLIF(po.partner_name,''), '거래처 미지정') partner
            FROM purchase_order po LEFT JOIN partner pa ON pa.id=po.partner_id
            ORDER BY po.id DESC LIMIT ?""", (limit,)))
        for r in out:
            try:
                r["items"] = json.loads(r["items"] or "[]")
            except ValueError:
                r["items"] = []
        return out
    finally:
        con.close()


@app.post("/api/po")
def po_save(request: Request, body: dict):
    """발주서 저장 — 품목은 이름·규격·단위를 스냅샷으로 저장 (자재 정보가 나중에 바뀌어도 발주서는 그대로)."""
    require_stock_duty(request)
    items = body.get("items") or []
    clean = []
    con = connect()
    try:
        for it in items:
            mid = it.get("material_id")
            if not mid:
                continue
            m = con.execute("SELECT name, spec, unit FROM material WHERE id=?", (mid,)).fetchone()
            if not m:
                continue
            clean.append({"material_id": mid, "name": m["name"], "spec": m["spec"] or "",
                          "unit": m["unit"] or "", "qty": float(it.get("qty") or 0),
                          "price": float(it.get("price") or 0)})   # 발주 단가 스냅샷 (월말 정산용)
        if not clean:
            raise HTTPException(400, "발주 품목이 없습니다 — 자재를 추가해주세요")
        cur = con.execute("""INSERT INTO purchase_order(date, partner_id, partner_name, due, note, items, created_by)
            VALUES(?,?,?,?,?,?,?)""",
                          (body.get("date") or dt.date.today().isoformat(),
                           body.get("partner_id") or None,
                           (body.get("partner_name") or "").strip(),
                           body.get("due") or "",
                           body.get("note") or "", json.dumps(clean, ensure_ascii=False),
                           request.state.user.get("username", "")))
        audit(con, "save_po", f"발주서 #{cur.lastrowid} — 품목 {len(clean)}종")
        con.commit()
        return {"ok": True, "id": cur.lastrowid}
    finally:
        con.close()


@app.get("/api/postatus")
def po_status(request: Request, mode: str = "m", date: str = "", q: str = "", limit: int = 50):
    """발주 현황 — 기간(전체/일/주/월/년) + 검색(품목명·거래처명)으로 발주서 목록·거래처별 정산 집계.
    금액 = 입고 처리 때 입력한 실제 단가 × 수량 (미입력 품목은 집계 제외, '미입력'으로 표시).
    집계는 검색·기간에 걸린 전체 기준, 목록은 최근 limit건만 (수량이 많아져도 화면 유지)."""
    require_stock_duty(request)
    money = mcan(request, "mat")   # 자재 단가·금액 열람 권한
    con = connect()
    try:
        if mode == "all":
            a, b = "0000-01-01", "9999-12-31"
            if not date:
                date = dt.date.today().isoformat()
        else:
            if not date:
                date = con.execute("SELECT MAX(date) d FROM purchase_order").fetchone()["d"] \
                    or dt.date.today().isoformat()
            a, b = period_range(mode, date)
        pos = rows(con.execute("""
            SELECT po.*, COALESCE(pa.name, NULLIF(po.partner_name,''), '거래처 미지정') partner
            FROM purchase_order po LEFT JOIN partner pa ON pa.id=po.partner_id
            WHERE po.date BETWEEN ? AND ? ORDER BY po.id DESC""", (a, b)))
        ql = (q or "").strip().lower()
        out_pos, by_part = [], {}
        total_amt, unpriced, recv_cnt = 0.0, 0, 0
        for po in pos:
            try:
                po["items"] = json.loads(po["items"] or "[]")
            except ValueError:
                po["items"] = []
            # 검색: 거래처명 또는 품목명에 걸리는 발주서만
            if ql and ql not in po["partner"].lower() \
               and not any(ql in (it.get("name") or "").lower() for it in po["items"]):
                continue
            amt = 0.0
            for it in po["items"]:
                qn = float(it.get("recv") if it.get("recv") is not None else (it.get("qty") or 0))
                pr = float(it.get("price") or 0)
                it["amount"] = qn * pr if pr > 0 else None
                if pr > 0:
                    amt += qn * pr
                else:
                    unpriced += 1
            po["amount"] = amt
            total_amt += amt
            if po["received_at"]:
                recv_cnt += 1
            bp = by_part.setdefault(po["partner"], {"partner": po["partner"], "cnt": 0, "recv": 0,
                                                    "items": 0, "amount": 0.0, "unpriced": 0})
            bp["cnt"] += 1
            bp["recv"] += 1 if po["received_at"] else 0
            bp["items"] += len(po["items"])
            bp["amount"] += amt
            bp["unpriced"] += sum(1 for it in po["items"] if not (it.get("price") or 0) > 0)
            out_pos.append(po)
        total = len(out_pos)
        out_pos = out_pos[:max(1, int(limit))]
        if not money:   # 금액 열람 권한 없으면 마스킹
            total_amt = None
            for po in out_pos:
                po["amount"] = None
                for it in po["items"]:
                    it["price"] = None
                    it["amount"] = None
            for v in by_part.values():
                v["amount"] = None
        return {"date": date, "mode": mode, "range": [a, b], "pos": out_pos,
                "total": total, "shown": len(out_pos),
                "by_partner": sorted(by_part.values(), key=lambda x: (-(x["amount"] or 0), -x["cnt"])),
                "total_amount": total_amt, "recv_cnt": recv_cnt, "unpriced": unpriced}
    finally:
        con.close()


@app.post("/api/po/{po_id}/receive")
def po_receive(request: Request, po_id: int, body: dict):
    """발주서 입고 처리 — 품목별 실제 입고 수량(+실제 단가)을 원부자재 입고(material_in)에 자동 기록.
    재고 반영은 일일 입력의 입고와 완전히 같은 경로(자동 행 재계산 + 이후 날짜 체인)로 처리된다."""
    require_stock_duty(request)
    con = connect()
    try:
        po = con.execute("SELECT * FROM purchase_order WHERE id=?", (po_id,)).fetchone()
        if not po:
            raise HTTPException(404, "발주서가 없습니다")
        if po["received_at"]:
            raise HTTPException(400, "이미 입고 처리된 발주서입니다")
        rdate = (body.get("date") or "").strip() or dt.date.today().isoformat()
        made = (body.get("made") or "").strip()
        expiry = (body.get("expiry") or "").strip()
        recvs = [it for it in (body.get("items") or [])
                 if it.get("material_id") and float(it.get("qty") or 0) > 0]
        if not recvs:
            raise HTTPException(400, "입고 수량을 1개 이상 입력해주세요")
        pa = con.execute("SELECT name FROM partner WHERE id=?", (po["partner_id"],)).fetchone() \
            if po["partner_id"] else None
        pname = (pa["name"] if pa else "") or (po["partner_name"] or "")
        note = f"발주 #{po_id}" + (f" · {pname}" if pname else "")
        con.execute("INSERT OR IGNORE INTO day_record(date) VALUES(?)", (rdate,))
        for it in recvs:
            con.execute("""INSERT INTO material_in(date, material_id, qty, made_date, expiry, note, partner, price)
                VALUES(?,?,?,?,?,?,?,?)""",
                        (rdate, it["material_id"], float(it["qty"]), made, expiry, note,
                         pname, float(it.get("price") or 0)))
        # 자재별 재고 자동 반영 — day_save의 자동 행 로직과 동일 규칙
        for mid in {it["material_id"] for it in recvs}:
            in_total = con.execute("SELECT COALESCE(SUM(qty),0) q FROM material_in WHERE date=? AND material_id=?",
                                   (rdate, mid)).fetchone()["q"]
            used = con.execute("SELECT COALESCE(SUM(qty),0) q FROM material_usage WHERE date=? AND material_id=?",
                               (rdate, mid)).fetchone()["q"]
            manual = con.execute("SELECT 1 FROM material_daily WHERE date=? AND material_id=? AND src!='auto'",
                                 (rdate, mid)).fetchone()
            if manual:   # 실사 행 = 실재고(측정값) 유지, 입고·사용량만 재계산
                con.execute("""UPDATE material_daily SET in_qty=?, used_qty=prev_qty+?-real_qty
                    WHERE date=? AND material_id=? AND src!='auto'""", (in_total, in_total, rdate, mid))
            else:
                prev_row = con.execute("""SELECT real_qty FROM material_daily
                    WHERE material_id=? AND date<? ORDER BY date DESC LIMIT 1""", (mid, rdate)).fetchone()
                prev = float(prev_row["real_qty"]) if prev_row else 0.0
                con.execute("""INSERT OR REPLACE INTO material_daily
                    (date, material_id, prev_qty, in_qty, real_qty, used_qty, src)
                    VALUES(?,?,?,?,?,?,'auto')""",
                            (rdate, mid, prev, in_total, prev + in_total - used, used))
            ripple_material(con, mid, rdate)   # 이후 날짜 전일재고 체인 재계산
        # 발주서 품목에 입고 수량·실제 단가 스냅샷 기록 (정산 = 이 단가 기준)
        try:
            items = json.loads(po["items"] or "[]")
        except ValueError:
            items = []
        rmap = {it["material_id"]: it for it in recvs}
        for it in items:
            r = rmap.get(it.get("material_id"))
            if r:
                it["recv"] = float(r.get("qty") or 0)
                if float(r.get("price") or 0) > 0:
                    it["price"] = float(r["price"])
        con.execute("""UPDATE purchase_order SET items=?, received_at=datetime('now','localtime'), received_by=?
            WHERE id=?""",
                    (json.dumps(items, ensure_ascii=False), request.state.user.get("username", ""), po_id))
        audit(con, "receive_po", f"발주서 #{po_id} 입고 처리 → {rdate} · {len(recvs)}품목 (재고 자동 반영)")
        con.commit()
        chat_system(f"📥 발주 #{po_id} 입고 완료" + (f" — {pname}" if pname else "")
                    + f" ({len(recvs)}품목, 재고 자동 반영)")
        return {"ok": True, "date": rdate}
    finally:
        con.close()


@app.post("/api/po/{po_id}/unreceive")
def po_unreceive(request: Request, po_id: int):
    """입고 취소 — 입고 처리 때 자동 기록된 원부자재 입고를 삭제하고 재고를 되돌린다.
    발주서는 다시 '진행중'이 되어 재입고 처리할 수 있다."""
    require_stock_duty(request)
    con = connect()
    try:
        po = con.execute("SELECT * FROM purchase_order WHERE id=?", (po_id,)).fetchone()
        if not po:
            raise HTTPException(404, "발주서가 없습니다")
        if not po["received_at"]:
            raise HTTPException(400, "입고 처리되지 않은 발주서입니다")
        # 입고 처리 때 남긴 기록 찾기 — note '발주 #id' 또는 '발주 #id · 거래처' (#1이 #10에 안 걸리게 정확 매칭)
        rows_in = rows(con.execute(
            "SELECT id, date, material_id FROM material_in WHERE note=? OR note LIKE ?",
            (f"발주 #{po_id}", f"발주 #{po_id} ·%")))
        affected = {(r["date"], r["material_id"]) for r in rows_in}
        for r in rows_in:
            con.execute("DELETE FROM material_in WHERE id=?", (r["id"],))
        # 재고 재계산 — 입고 처리와 동일 규칙의 역방향
        for (rdate, mid) in affected:
            in_total = con.execute("SELECT COALESCE(SUM(qty),0) q FROM material_in WHERE date=? AND material_id=?",
                                   (rdate, mid)).fetchone()["q"]
            used = con.execute("SELECT COALESCE(SUM(qty),0) q FROM material_usage WHERE date=? AND material_id=?",
                               (rdate, mid)).fetchone()["q"]
            manual = con.execute("SELECT 1 FROM material_daily WHERE date=? AND material_id=? AND src!='auto'",
                                 (rdate, mid)).fetchone()
            if manual:   # 실사 행: 실재고(측정값) 유지, 입고·사용량만 재계산
                con.execute("""UPDATE material_daily SET in_qty=?, used_qty=prev_qty+?-real_qty
                    WHERE date=? AND material_id=? AND src!='auto'""", (in_total, in_total, rdate, mid))
            elif in_total == 0 and used == 0:   # 입고 취소로 빈 자동 행 — 통째로 제거 (일일 입력에서도 사라짐)
                con.execute("DELETE FROM material_daily WHERE date=? AND material_id=? AND src='auto'", (rdate, mid))
            else:
                prev_row = con.execute("""SELECT real_qty FROM material_daily
                    WHERE material_id=? AND date<? ORDER BY date DESC LIMIT 1""", (mid, rdate)).fetchone()
                prev = float(prev_row["real_qty"]) if prev_row else 0.0
                con.execute("""INSERT OR REPLACE INTO material_daily
                    (date, material_id, prev_qty, in_qty, real_qty, used_qty, src)
                    VALUES(?,?,?,?,?,?,'auto')""",
                            (rdate, mid, prev, in_total, prev + in_total - used, used))
            ripple_material(con, mid, rdate)
        # 발주서 되돌리기 — 입고 수량·입고 단가 스냅샷 제거
        try:
            items = json.loads(po["items"] or "[]")
        except ValueError:
            items = []
        for it in items:
            it.pop("recv", None)
            it["price"] = 0
        con.execute("UPDATE purchase_order SET items=?, received_at='', received_by='' WHERE id=?",
                    (json.dumps(items, ensure_ascii=False), po_id))
        audit(con, "unreceive_po", f"발주서 #{po_id} 입고 취소 — 자동 입고 기록 {len(rows_in)}건 삭제, 재고 원복")
        con.commit()
        return {"ok": True, "removed": len(rows_in)}
    finally:
        con.close()


@app.get("/api/po/{po_id}")
def po_get(request: Request, po_id: int):
    """발주서 단건 — 자재 이력의 입고 기록에서 클릭해 볼 때 사용. 금액은 mat 권한 마스킹."""
    con = connect()
    try:
        po = con.execute("""
            SELECT po.*, COALESCE(pa.name, NULLIF(po.partner_name,''), '거래처 미지정') partner
            FROM purchase_order po LEFT JOIN partner pa ON pa.id=po.partner_id
            WHERE po.id=?""", (po_id,)).fetchone()
        if not po:
            raise HTTPException(404, "발주서가 없습니다")
        po = dict(po)
        try:
            po["items"] = json.loads(po["items"] or "[]")
        except ValueError:
            po["items"] = []
        if not mcan(request, "mat"):
            for it in po["items"]:
                it["price"] = None
        return po
    finally:
        con.close()


@app.delete("/api/po/{po_id}")
def po_delete(request: Request, po_id: int):
    require_stock_duty(request)
    con = connect()
    try:
        po = con.execute("SELECT sent_at, received_at FROM purchase_order WHERE id=?", (po_id,)).fetchone()
        if not po:
            raise HTTPException(404, "발주서가 없습니다")
        # 발송·입고된 발주서 = 거래 기록 — 일반 사용자는 삭제 불가, 관리자만 삭제 가능 (감사 이력에 남음)
        locked = bool(po["sent_at"] or po["received_at"])
        if locked and request.state.user["role"] != "admin":
            raise HTTPException(403, "메일을 보냈거나 입고 처리된 발주서는 관리자만 삭제할 수 있습니다")
        con.execute("DELETE FROM purchase_order WHERE id=?", (po_id,))
        audit(con, "delete_po", f"발주서 #{po_id} 삭제"
              + (" (발송/입고 기록 있음 — 관리자 삭제, 입고분 원부자재 기록은 유지)" if locked else ""))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


# ── 메일(SMTP) 설정 (사용자별) + 발주서 메일 발송 ──────────────
SMTP_KEYS = ("smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from")


def get_user_settings(con, username, keys):
    got = {r["key"]: r["value"] for r in con.execute(
        f"SELECT key, value FROM user_setting WHERE username=? AND key IN ({','.join('?' * len(keys))})",
        (username, *keys))}
    return {k: got.get(k, "") for k in keys}


@app.get("/api/mysmtp")
def mysmtp_get(request: Request):
    """내 메일 계정 설정 조회 — 각 사용자(아이디)마다 자기 계정으로 발송."""
    con = connect()
    try:
        s = get_user_settings(con, request.state.user.get("username", ""), SMTP_KEYS)
        return {"host": s["smtp_host"], "port": s["smtp_port"], "user": s["smtp_user"],
                "from": s["smtp_from"], "has_pass": bool(s["smtp_pass"]),
                "configured": bool(s["smtp_host"] and s["smtp_user"] and s["smtp_pass"])}
    finally:
        con.close()


@app.post("/api/mysmtp")
def mysmtp_save(request: Request, body: dict):
    username = request.state.user.get("username", "")
    con = connect()
    try:
        vals = {"smtp_host": (body.get("host") or "").strip(),
                "smtp_port": str(body.get("port") or "").strip(),
                "smtp_user": (body.get("user") or "").strip(),
                "smtp_from": (body.get("from") or "").strip()}
        if body.get("pass"):   # 비밀번호는 입력했을 때만 교체 (빈칸 = 기존 유지) · 공백 제거(복사 실수 방지)
            vals["smtp_pass"] = re.sub(r"\s+", "", str(body["pass"]))
        for k, v in vals.items():
            con.execute("INSERT INTO user_setting(username, key, value) VALUES(?,?,?)"
                        " ON CONFLICT(username, key) DO UPDATE SET value=excluded.value", (username, k, v))
        audit(con, "smtp_set", f"{username} — 내 메일(SMTP) 설정 변경")
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.post("/api/mysmtp/test")
def mysmtp_test(request: Request):
    """내 메일 설정 테스트 — 내 주소로 테스트 메일을 보내본다 (설정 문제를 발송 전에 확인)."""
    username = request.state.user.get("username", "")
    con = connect()
    try:
        s = get_user_settings(con, username, SMTP_KEYS)
        to = s["smtp_from"] or s["smtp_user"]
        if not to:
            raise HTTPException(400, "먼저 메일 계정을 저장해주세요")
        send_mail(con, username, [to],
                  "[리바이프로덕트] 메일 설정 테스트",
                  "<p>이 메일이 보이면 발주서 메일 설정이 정상입니다. ✅</p>",
                  [], sender_label_of(con, username))
        return {"ok": True, "to": to}
    finally:
        con.close()


@app.get("/api/mysign")
def mysign_get(request: Request):
    """내 사인 이미지 조회 — 발주서 서명란에 들어간다 (없으면 이름 도장으로 대체)."""
    con = connect()
    try:
        s = get_user_settings(con, request.state.user.get("username", ""), ("sign_img",))
        return {"img": s["sign_img"]}
    finally:
        con.close()


@app.post("/api/mysign")
def mysign_save(request: Request, body: dict):
    img = (body.get("img") or "").strip()
    if img and not img.startswith("data:image/"):
        raise HTTPException(400, "이미지 형식이 올바르지 않습니다")
    if len(img) > 800_000:
        raise HTTPException(400, "사인 이미지가 너무 큽니다 — 다시 올려주세요")
    username = request.state.user.get("username", "")
    con = connect()
    try:
        con.execute("INSERT INTO user_setting(username, key, value) VALUES(?,?,?)"
                    " ON CONFLICT(username, key) DO UPDATE SET value=excluded.value",
                    (username, "sign_img", img))
        audit(con, "sign_set", f"{username} — 사인 {'등록' if img else '삭제'}")
        con.commit()
        return {"ok": True}
    finally:
        con.close()


def send_mail(con, username, to_list, subject, html, attachments, sender_label, cc=None):
    """개인 SMTP 발송 — 로그인 사용자의 메일 계정 사용. 포트 465는 SSL, 그 외 STARTTLS."""
    import smtplib
    import base64 as b64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication
    from email.utils import formataddr
    s = get_user_settings(con, username, SMTP_KEYS)
    if not (s["smtp_host"] and s["smtp_user"] and s["smtp_pass"]):
        raise HTTPException(400, "내 메일 계정이 설정되지 않았습니다 — 화면 왼쪽 아래 [내 설정] > 메일에서 등록해주세요")
    port = int(s["smtp_port"] or 465)
    msg = MIMEMultipart()
    msg["From"] = formataddr((sender_label, s["smtp_from"] or s["smtp_user"]))
    msg["To"] = ", ".join(to_list)
    if cc:
        msg["Cc"] = ", ".join(cc)   # send_message가 To+Cc 헤더의 모든 수신자에게 전달
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html", "utf-8"))
    total = 0
    for a in attachments or []:
        data = a.get("data") or ""
        if "," in data[:100]:
            data = data.split(",", 1)[1]
        raw = b64.b64decode(data)
        total += len(raw)
        if total > 20 * 1024 * 1024:
            raise HTTPException(400, "첨부 합계는 20MB 이하만 가능합니다")
        part = MIMEApplication(raw)
        part.add_header("Content-Disposition", "attachment",
                        filename=("utf-8", "", a.get("name") or "file"))
        msg.attach(part)
    # 비밀번호 공백 제거 (앱 비밀번호 복사 시 공백이 섞이는 경우) +
    # 로그인 아이디 재시도: 네이버 등은 전체 주소가 아니라 '아이디'로 로그인해야 하는 경우가 있음
    pw = re.sub(r"\s+", "", s["smtp_pass"])
    user = s["smtp_user"]
    logins = [user]
    if "@" in user:
        logins.append(user.split("@")[0])   # gdgoo@naver.com 실패 시 gdgoo로 재시도
    try:
        last_auth_err = None
        for i, u in enumerate(logins):
            if port == 465:
                sv = smtplib.SMTP_SSL(s["smtp_host"], port, timeout=25)
            else:
                sv = smtplib.SMTP(s["smtp_host"], port, timeout=25)
                sv.starttls()
            try:
                with sv:
                    sv.login(u, pw)
                    sv.send_message(msg)
                last_auth_err = None
                break
            except smtplib.SMTPAuthenticationError as e:
                last_auth_err = e
                continue
        if last_auth_err is not None:
            raise HTTPException(502, "메일 로그인 실패 — 일반 비밀번호가 아니라 2단계 인증 후 발급한 "
                                "앱 비밀번호(애플리케이션 비밀번호)를 입력해야 합니다. 계정·앱 비밀번호를 다시 확인해주세요")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"메일 발송 실패: {e}")


def sender_label_of(con, username):
    """발신 표시명: '리바이프로덕트 이름 직급' — 직급은 인원 등록(staff)의 직책에서 이름 매칭."""
    row = con.execute("SELECT position FROM staff WHERE name=?", (username,)).fetchone()
    pos = (row["position"] if row else "") or ""
    return f"리바이프로덕트 {username}" + (f" {pos}" if pos else "")


@app.post("/api/po/send")
def po_send(request: Request, body: dict):
    """발주서 메일 발송 — 본문 HTML(발주서 표) + 첨부. 발송하면 발주서에 발송 기록."""
    require_stock_duty(request)

    def parse_addrs(v):
        return [t.strip() for t in str(v or "").replace(";", ",").split(",") if t.strip()]

    to = parse_addrs(body.get("to"))
    cc = parse_addrs(body.get("cc"))
    if not to:
        raise HTTPException(400, "받는 메일 주소를 입력해주세요")
    if any("@" not in t for t in to + cc):
        raise HTTPException(400, "메일 주소 형식이 올바르지 않습니다")
    subject = (body.get("subject") or "").strip() or "[리바이프로덕트] 발주서"
    html = body.get("html") or ""
    attachments = body.get("attachments") or []
    attach_names = ", ".join((a.get("name") or "file") for a in attachments)
    con = connect()
    try:
        username = request.state.user.get("username", "")
        try:
            send_mail(con, username, to, subject, html, attachments,
                      sender_label_of(con, username), cc=cc)
        except HTTPException as e:   # 실패도 보낸 메일함에 남긴다 (원인 확인용)
            log_sent_mail(con, username, to, cc, subject, html, attach_names,
                          body.get("po_id") or 0, "failed", str(e.detail))
            con.commit()
            raise
        log_sent_mail(con, username, to, cc, subject, html, attach_names,
                      body.get("po_id") or 0, "sent", "")
        po_id = body.get("po_id")
        po_partner = ""
        if po_id:
            con.execute("UPDATE purchase_order SET sent_at=datetime('now','localtime'), sent_to=? WHERE id=?",
                        (", ".join(to + cc), po_id))
            pr = con.execute("""SELECT COALESCE(pa.name, NULLIF(po.partner_name,''), '') nm
                FROM purchase_order po LEFT JOIN partner pa ON pa.id=po.partner_id
                WHERE po.id=?""", (po_id,)).fetchone()
            po_partner = pr["nm"] if pr else ""
        audit(con, "send_po", f"발주서{'#' + str(po_id) if po_id else ''} 메일 발송 → {', '.join(to)}"
              + (f" (참조 {', '.join(cc)})" if cc else ""))
        con.commit()
        if po_id:
            chat_system(f"📤 발주서 #{po_id} 메일 발송" + (f" — {po_partner}" if po_partner else "")
                        + f" (받는사람 {', '.join(to)})")
        return {"ok": True}
    finally:
        con.close()


@app.post("/api/mail/send")
def mail_send(request: Request, body: dict):
    """일반 메일 발송 — 발주서와 무관한 자유 메일 (문의·안내 등). 로그인 사용자(게스트 제외)."""
    def parse_addrs(v):
        return [t.strip() for t in str(v or "").replace(";", ",").split(",") if t.strip()]

    to = parse_addrs(body.get("to"))
    cc = parse_addrs(body.get("cc"))
    if not to:
        raise HTTPException(400, "받는 메일 주소를 입력해주세요")
    if any("@" not in t for t in to + cc):
        raise HTTPException(400, "메일 주소 형식이 올바르지 않습니다")
    subject = (body.get("subject") or "").strip() or "[리바이프로덕트]"
    html = body.get("html") or ""
    attachments = body.get("attachments") or []
    attach_names = ", ".join((a.get("name") or "file") for a in attachments)
    con = connect()
    try:
        username = request.state.user.get("username", "")
        try:
            send_mail(con, username, to, subject, html, attachments,
                      sender_label_of(con, username), cc=cc)
        except HTTPException as e:
            log_sent_mail(con, username, to, cc, subject, html, attach_names, 0, "failed", str(e.detail))
            con.commit()
            raise
        log_sent_mail(con, username, to, cc, subject, html, attach_names, 0, "sent", "")
        audit(con, "send_mail", f"메일 발송 → {', '.join(to)}" + (f" (참조 {', '.join(cc)})" if cc else ""))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


def log_sent_mail(con, username, to, cc, subject, html, attach_names, po_id, status, error):
    """보낸 메일 이력 기록 — 성공·실패 모두. (본문은 재발송·미리보기용, 첨부는 이름만)"""
    con.execute("""INSERT INTO sent_mail(username, to_addr, cc, subject, body_html, attach_names, po_id, status, error)
        VALUES(?,?,?,?,?,?,?,?,?)""",
                (username, ", ".join(to) if isinstance(to, list) else (to or ""),
                 ", ".join(cc) if isinstance(cc, list) else (cc or ""),
                 subject, html, attach_names, int(po_id or 0), status, error[:500]))


@app.get("/api/sentmail")
def sent_mail_list(request: Request, limit: int = 50):
    """내 보낸 메일함 — 최신순. 관리자는 전원 기록을 본다."""
    me = request.state.user["username"]
    con = connect()
    try:
        if request.state.user["role"] == "admin":
            q = ("SELECT id, username, to_addr, cc, subject, attach_names, po_id, status, error, at"
                 " FROM sent_mail ORDER BY id DESC LIMIT ?")
            p = (max(1, min(limit, 200)),)
        else:
            q = ("SELECT id, username, to_addr, cc, subject, attach_names, po_id, status, error, at"
                 " FROM sent_mail WHERE username=? ORDER BY id DESC LIMIT ?")
            p = (me, max(1, min(limit, 200)))
        return {"items": rows(con.execute(q, p)), "me": me}
    finally:
        con.close()


@app.get("/api/sentmail/{mid}")
def sent_mail_get(request: Request, mid: int):
    """보낸 메일 한 건 상세 (본문 포함) — 본인 또는 관리자."""
    me = request.state.user["username"]
    con = connect()
    try:
        r = con.execute("SELECT * FROM sent_mail WHERE id=?", (mid,)).fetchone()
        if not r:
            raise HTTPException(404, "메일 기록이 없습니다")
        if r["username"] != me and request.state.user["role"] != "admin":
            raise HTTPException(403, "본인이 보낸 메일만 볼 수 있습니다")
        return dict(r)
    finally:
        con.close()


@app.post("/api/sentmail/{mid}/resend")
def sent_mail_resend(request: Request, mid: int):
    """같은 받는사람·제목·본문으로 재발송 — 원본 첨부는 포함되지 않는다 (이력엔 이름만 저장)."""
    require_stock_duty(request)
    me = request.state.user["username"]
    con = connect()
    try:
        r = con.execute("SELECT * FROM sent_mail WHERE id=?", (mid,)).fetchone()
        if not r:
            raise HTTPException(404, "메일 기록이 없습니다")
        if r["username"] != me and request.state.user["role"] != "admin":
            raise HTTPException(403, "본인이 보낸 메일만 재발송할 수 있습니다")
        to = [t.strip() for t in (r["to_addr"] or "").replace(";", ",").split(",") if t.strip()]
        cc = [t.strip() for t in (r["cc"] or "").replace(";", ",").split(",") if t.strip()]
        if not to:
            raise HTTPException(400, "받는사람이 없습니다")
        try:
            send_mail(con, me, to, r["subject"], r["body_html"], [], sender_label_of(con, me), cc=cc)
        except HTTPException as e:
            log_sent_mail(con, me, to, cc, r["subject"], r["body_html"], "", r["po_id"], "failed", str(e.detail))
            con.commit()
            raise
        log_sent_mail(con, me, to, cc, r["subject"], r["body_html"], "", r["po_id"], "sent", "")
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.get("/api/mailtemplates")
def mail_templates(request: Request):
    """메일 상용구 목록 (팀 공용) — 정렬순."""
    con = connect()
    try:
        return rows(con.execute(
            "SELECT id, name, body, created_by, at FROM mail_template ORDER BY sort, id"))
    finally:
        con.close()


@app.post("/api/mailtemplates")
def mail_template_save(request: Request, body: dict):
    """상용구 추가 또는 수정(id 지정 시). 게스트는 미들웨어가 차단."""
    name = (body.get("name") or "").strip()[:60]
    text = body.get("body") or ""
    if not name:
        raise HTTPException(400, "상용구 이름을 입력해주세요")
    tid = body.get("id")
    con = connect()
    try:
        if tid:
            con.execute("UPDATE mail_template SET name=?, body=? WHERE id=?", (name, text, tid))
        else:
            cur = con.execute("INSERT INTO mail_template(name, body, created_by) VALUES(?,?,?)",
                              (name, text, request.state.user.get("username", "")))
            tid = cur.lastrowid
        con.commit()
        return {"ok": True, "id": tid}
    finally:
        con.close()


@app.delete("/api/mailtemplates/{tid}")
def mail_template_delete(request: Request, tid: int):
    con = connect()
    try:
        con.execute("DELETE FROM mail_template WHERE id=?", (tid,))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


# ── 거래처 엑셀 받기 (ERP ESA001M.xlsx / 앱 엑셀 내보내기 CSV) ──────────
# 열 이름 별칭 — ERP 양식·앱 내보내기 양식 어느 쪽이든 헤더 이름으로 매핑
PARTNER_COL_ALIAS = {
    "name": ("거래처명",),
    "biz": ("거래처코드", "사업자번호", "사업자등록번호"),
    "ceo": ("대표자명", "대표자"),
    "phone": ("전화", "연락처"),
    "mobile": ("모바일",),
    "email": ("이메일", "E-MAIL", "EMAIL", "메일"),
    "type": ("유형",),
    "contact": ("담당자",),
    "status": ("상태",),
    "use": ("사용구분",),
}


def _parse_partner_file(content: bytes):
    """xlsx(ERP)·csv(앱 내보내기) → [{name, biz, ceo, phone, mobile, type, contact, status}].
    헤더행은 '거래처명'이 있는 행을 자동 탐색."""
    grid = []
    if content[:2] == b"PK":   # xlsx
        import io
        import openpyxl
        try:
            wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        except Exception:
            raise HTTPException(400, "엑셀(.xlsx) 파일을 읽지 못했습니다 — 원본 그대로 올려주세요")
        for r in wb.worksheets[0].iter_rows(values_only=True):
            grid.append([str(c).strip() if c is not None else "" for c in r])
    else:                      # csv (utf-8 BOM / cp949 모두 허용)
        import csv
        import io
        text = None
        for enc in ("utf-8-sig", "cp949", "utf-8"):
            try:
                text = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise HTTPException(400, "CSV 인코딩을 읽지 못했습니다")
        for r in csv.reader(io.StringIO(text)):
            grid.append([str(c).strip() for c in r])
    # 헤더행: '거래처명' 열이 있는 첫 행
    hdr_i, col = -1, {}
    for i, cells in enumerate(grid[:10]):
        if any(c == "거래처명" for c in cells):
            hdr_i = i
            for key, aliases in PARTNER_COL_ALIAS.items():
                for j, h in enumerate(cells):
                    if h in aliases:
                        col[key] = j
                        break
            break
    if hdr_i < 0 or "name" not in col:
        raise HTTPException(400, "헤더행(거래처명 …)을 찾지 못했습니다 — ERP 거래처등록(ESA001M) 또는 앱 [엑셀(CSV)] 양식을 올려주세요")

    def cell(r, key):
        j = col.get(key)
        v = r[j] if j is not None and j < len(r) else ""
        v = str(v).strip()
        return "" if v in ("—", "None") else v   # 내보내기의 빈값 표시(—)는 빈값으로

    out = []
    for r in grid[hdr_i + 1:]:
        if not cell(r, "name"):
            continue
        out.append({k: cell(r, k) for k in PARTNER_COL_ALIAS})
    return out


@app.post("/api/partners/import")
def partners_import(request: Request, body: dict):
    """거래처 엑셀 받기 — ERP(ESA001M.xlsx)든 앱 [엑셀(CSV)] 내보내기든 그대로 업로드.
    거래처코드/사업자번호 열은 biz_no로 저장. 중복 판정: ①거래처명 ②사업자번호 —
    있으면 빈 필드만 채우고(기존 값 보존) 없으면 신규 등록.
    body.apply=false면 실제 저장 없이 건수만 미리 계산(확인창용)."""
    require_admin(request)
    import base64 as b64
    raw = body.get("data") or ""
    if "," in raw[:100]:                       # dataURL 형식이면 앞부분 제거
        raw = raw.split(",", 1)[1]
    try:
        content = b64.b64decode(raw)
    except Exception:
        raise HTTPException(400, "파일 데이터를 읽지 못했습니다")
    items = _parse_partner_file(content)
    apply_ = bool(body.get("apply"))

    def norm_biz(v):   # 숫자·하이픈만 (엑셀이 숫자로 읽은 값도 문자열로)
        return "".join(ch for ch in str(v) if ch.isdigit() or ch == "-")

    con = connect()
    try:
        existing = rows(con.execute("SELECT id, name, biz_no FROM partner"))
        by_name = {r["name"].strip(): r["id"] for r in existing if r["name"]}
        by_biz = {norm_biz(r["biz_no"]): r["id"] for r in existing if (r["biz_no"] or "").strip()}
        added = updated = skipped = 0
        new_names = []
        for it in items:
            name, biz = it["name"], norm_biz(it["biz"])
            pid = by_name.get(name) or (by_biz.get(biz) if biz else None)
            if pid == -1:   # 미리보기 중 같은 파일 안 중복 행 — 1건만 신규로 집계
                skipped += 1
                continue
            if pid:   # 기존 거래처 — 빈 필드만 채움, 이름/유형/상태/비고는 유지
                cur = con.execute("SELECT biz_no, ceo, phone, mobile, contact, email FROM partner WHERE id=?", (pid,)).fetchone()
                sets, vals = [], []
                for f, nv in (("biz_no", biz), ("ceo", it["ceo"]), ("phone", it["phone"]),
                              ("mobile", it["mobile"]), ("contact", it["contact"]), ("email", it["email"])):
                    if nv and not (cur[f] or "").strip():
                        sets.append(f + "=?"); vals.append(nv)
                if sets:
                    if apply_:
                        con.execute(f"UPDATE partner SET {', '.join(sets)} WHERE id=?", (*vals, pid))
                    updated += 1
                else:
                    skipped += 1
            else:     # 신규 등록 — 유형 열이 있으면 그대로, 없으면 '자재 공급처'(판매 드롭다운 보호)
                status = it["status"] or ("중지" if it["use"].upper() == "NO" else "활성")
                if apply_:
                    cur = con.execute("""INSERT INTO partner(name, type, phone, contact, note, status, biz_no, ceo, mobile, email)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                      (name, it["type"] or "자재 공급처", it["phone"], it["contact"], "",
                                       status, biz, it["ceo"], it["mobile"], it["email"]))
                    by_name[name] = cur.lastrowid
                    if biz:
                        by_biz[biz] = cur.lastrowid
                else:
                    by_name[name] = -1   # 미리보기에서도 파일 내 중복은 1건으로
                    if biz:
                        by_biz[biz] = -1
                added += 1
                if len(new_names) < 5:
                    new_names.append(name)
        if apply_:
            audit(con, "import_partner", f"거래처 엑셀 받기 — 신규 {added} · 채움 {updated} · 변동없음 {skipped}")
            bump_masters()
            con.commit()
        return {"ok": True, "applied": apply_, "added": added, "updated": updated,
                "skipped": skipped, "total": len(items), "new_names": new_names}
    finally:
        con.close()


# ── 기준정보 ─────────────────────────────────


@app.post("/api/masters/{mtype}/reorder")
def masters_reorder(request: Request, mtype: str, body: dict):
    """빠른 편집에서 드래그/이동한 순서를 sort 컬럼에 저장 — 목록·검색이 이 순서를 따른다.
    (미들웨어가 guest의 POST를 이미 차단)"""
    table = "material" if mtype in ("raw", "sub", "semi") else mtype
    if table not in ("product", "material", "partner", "staff", "line"):
        raise HTTPException(400, "순서 변경을 지원하지 않는 항목입니다")
    ids = [int(x) for x in (body.get("ids") or [])]
    if not ids:
        raise HTTPException(400, "순서 목록이 비어 있습니다")
    con = connect()
    try:
        for i, mid in enumerate(ids, 1):
            con.execute(f"UPDATE {table} SET sort=? WHERE id=?", (i, mid))
        audit(con, "reorder_" + mtype, f"{len(ids)}건 순서 변경")
        bump_masters()
        con.commit()
        return {"ok": True, "count": len(ids)}
    finally:
        con.close()


@app.get("/api/masters/{mtype}")
def masters(mtype: str, request: Request):
    con = connect()
    try:
        if mtype == "product":
            # 완제품만 (반제품은 이제 자재 계열 — 아래 material 쿼리)
            data = rows(con.execute("""
                SELECT p.*,
                       COALESCE(os.qty,0) + COALESCE(pr.q,0) - COALESCE(sh.q,0) - COALESCE(dp.q,0) AS stock
                FROM product p
                LEFT JOIN opening_stock os ON os.kind='product' AND os.ref_id=p.id
                LEFT JOIN (SELECT product_id, SUM(prod_qty) q FROM production GROUP BY product_id) pr
                       ON pr.product_id=p.id
                LEFT JOIN (SELECT product_id, SUM(qty) q FROM shipment GROUP BY product_id) sh
                       ON sh.product_id=p.id
                LEFT JOIN (SELECT product_id, SUM(qty) q FROM disposal GROUP BY product_id) dp
                       ON dp.product_id=p.id
                WHERE COALESCE(p.is_semi,0)=0
                ORDER BY p.sort, p.id"""))
        elif mtype in ("raw", "sub", "semi"):
            # raw/sub=원·부재료(반제품 제외) · semi=반제품(직접 생산하는 자재, is_semi=1)
            where = "COALESCE(m.is_semi,0)=1" if mtype == "semi" else "m.kind=? AND COALESCE(m.is_semi,0)=0"
            params = () if mtype == "semi" else (mtype,)
            data = rows(con.execute(f"""
                SELECT m.*, md.real_qty AS stock, md.date AS stock_date, u.avg_use
                FROM material m
                LEFT JOIN (
                  SELECT material_id, real_qty, date,
                         ROW_NUMBER() OVER (PARTITION BY material_id ORDER BY date DESC) rn
                  FROM material_daily) md ON md.material_id=m.id AND md.rn=1
                LEFT JOIN (
                  SELECT material_id, SUM(used_qty) * 1.0 /
                         (SELECT COUNT(DISTINCT date) FROM material_daily
                           WHERE used_qty>0 AND date>=date('now','localtime','-30 day')) avg_use
                  FROM material_daily
                  WHERE used_qty>0 AND date>=date('now','localtime','-30 day')
                  GROUP BY material_id) u ON u.material_id=m.id
                WHERE {where}
                ORDER BY m.sort, m.id""", params))
        elif mtype in MASTER_TABLES:
            data = rows(con.execute(f"SELECT * FROM {MASTER_TABLES[mtype][0]} ORDER BY id"))
        else:
            raise HTTPException(404, "unknown master type")
        # 시급·단가는 금액 권한별 마스킹 (admin은 전체)
        if mtype == "staff" and not mcan(request, "labor"):
            for r in data:
                r["wage"] = None
        if mtype == "product" and not mcan(request, "prod"):
            for r in data:
                r["unit_price"] = None
        if mtype in ("raw", "sub") and not mcan(request, "mat"):
            for r in data:
                r["unit_price"] = None
        return data
    finally:
        con.close()


def _check_line_parent(con, parent_id, self_id=None):
    """라인 소속(parent) 검증: 자기 자신 금지 · 대표 라인만 지정 가능 (2단계 금지)."""
    if not parent_id:
        return
    if self_id is not None and int(parent_id) == int(self_id):
        raise HTTPException(400, "자기 자신을 소속 라인으로 지정할 수 없습니다")
    p = con.execute("SELECT parent_id FROM line WHERE id=?", (parent_id,)).fetchone()
    if not p:
        raise HTTPException(400, "소속 라인이 존재하지 않습니다")
    if p["parent_id"]:
        raise HTTPException(400, "공정 행은 소속 라인이 될 수 없습니다 — 대표 라인을 선택하세요")


@app.post("/api/masters/{mtype}")
def master_create(mtype: str, body: dict):
    key = "material" if mtype in ("raw", "sub", "semi") else mtype
    if key not in MASTER_TABLES:
        raise HTTPException(404, "unknown master type")
    table, cols = MASTER_TABLES[key]
    if mtype in ("raw", "sub"):
        body["kind"] = mtype
    if mtype == "semi":
        body["kind"] = body.get("kind") or "raw"   # 반제품 = 자재(kind raw) + is_semi=1
        body["is_semi"] = 1
    vals = {c: body.get(c) for c in cols if c in body}
    if not vals.get("name"):
        raise HTTPException(400, "name required")
    con = connect()
    try:
        if mtype == "line":
            _check_line_parent(con, vals.get("parent_id"))
        # 반제품 등록 시 같은 이름의 자재가 이미 있으면 → 그 자재를 반제품으로 전환(재고·배합비 참조 유지).
        #  같은 이름 원/부재료가 있는데 새로 넣으면 UNIQUE(kind,name) 위반으로 500 → 전환으로 우회.
        if mtype == "semi":
            exist = con.execute(
                "SELECT id, unit, COALESCE(is_semi,0) is_semi FROM material WHERE name=? "
                "ORDER BY (kind='raw') DESC, id LIMIT 1", (vals["name"],)).fetchone()
            if exist:
                if int(exist["is_semi"]) == 1:
                    raise HTTPException(400, f"이미 같은 이름의 반제품 '{vals['name']}'이(가) 있습니다")
                if not body.get("convert_existing"):
                    # 프론트에 전환 확인 요청 (409) — 기존 자재 정보를 함께 전달
                    raise HTTPException(409, {"code": "semi_exists", "id": exist["id"],
                                             "unit": exist["unit"] or "", "name": vals["name"]})
                # 전환: 단위·재고는 그대로 두고 is_semi=1 + 1배합당 생산량(기존 단위로 환산)만 반영
                eu = (exist["unit"] or "").lower(); fu = (body.get("unit") or "").lower()
                by = float(body.get("batch_yield") or 0)
                if fu == "g" and eu == "kg":
                    by /= 1000
                elif fu == "kg" and eu == "g":
                    by *= 1000
                upd = {"is_semi": 1, "batch_yield": by}
                for c in ("spec", "unit_price", "safety_stock", "shelf_days", "status", "note"):
                    if body.get(c) not in (None, ""):
                        upd[c] = body.get(c)
                setc = ",".join(f"{k}=?" for k in upd)
                con.execute(f"UPDATE material SET {setc} WHERE id=?", (*upd.values(), exist["id"]))
                audit(con, "convert_semi", f"자재#{exist['id']} '{vals['name']}' 반제품 전환 (재고·단위 유지)")
                bump_masters()
                con.commit()
                return {"id": exist["id"], "converted": True}
        ks = ",".join(vals)
        qs = ",".join("?" * len(vals))
        cur = con.execute(f"INSERT INTO {table}({ks}) VALUES({qs})", list(vals.values()))
        new_id = cur.lastrowid
        # 자재 신규 등록 시 초기 단가도 단가 이력에 남긴다 (적용 시작일=등록일 · 이후 기간별 계산의 기준)
        if mtype in ("raw", "sub", "semi"):
            try:
                ip = float(vals.get("unit_price") or 0)
            except (TypeError, ValueError):
                ip = 0.0
            if ip > 0:
                fd = (body.get("price_from") or "").strip()
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", fd):
                    fd = dt.date.today().isoformat()
                con.execute("INSERT INTO material_price(material_id, from_date, price, note, set_at) "
                            "VALUES(?,?,?,?,datetime('now','localtime'))", (new_id, fd, ip, "등록"))
        # 초기재고 → opening_stock
        init_qty = body.get("initial_stock")
        if init_qty not in (None, "", 0):
            kind = "product" if mtype == "product" else "material"
            con.execute("INSERT OR REPLACE INTO opening_stock VALUES(?,?,?,?)",
                        (kind, new_id, dt.date.today().isoformat(), float(init_qty)))
        audit(con, "create_" + mtype, json.dumps(vals, ensure_ascii=False))
        bump_masters()
        con.commit()
        return {"id": new_id}
    finally:
        con.close()


@app.put("/api/masters/{mtype}/{mid}")
def master_update(mtype: str, mid: int, body: dict):
    key = "material" if mtype in ("raw", "sub", "semi") else mtype
    if key not in MASTER_TABLES:
        raise HTTPException(404, "unknown master type")
    table, cols = MASTER_TABLES[key]
    vals = {c: body.get(c) for c in cols if c in body}
    stock_set = body.get("stock_set")
    if not vals and stock_set is None:
        raise HTTPException(400, "no fields")
    if stock_set is not None and float(stock_set) < 0:
        raise HTTPException(400, "현재고에 음수는 입력할 수 없습니다")
    con = connect()
    try:
        old_price = None   # 자재 단가 변경 이력 기록용 (변경 전 값)
        if mtype in ("raw", "sub", "semi") and "unit_price" in vals:
            _op = con.execute("SELECT unit_price FROM material WHERE id=?", (mid,)).fetchone()
            old_price = _op["unit_price"] if _op else None
        if mtype == "line" and "parent_id" in vals:
            _check_line_parent(con, vals.get("parent_id"), mid)
            # 대표 라인을 공정으로 강등하면 그 아래 공정들이 고아가 됨 → 차단
            if vals.get("parent_id"):
                ch = con.execute("SELECT COUNT(*) c FROM line WHERE parent_id=?", (mid,)).fetchone()["c"]
                if ch:
                    raise HTTPException(400, f"이 라인에 소속된 공정이 {ch}개 있습니다 — 먼저 그 공정들의 소속을 옮기세요")
        if vals:
            sets = ",".join(f"{c}=?" for c in vals)
            try:
                con.execute(f"UPDATE {table} SET {sets} WHERE id=?", list(vals.values()) + [mid])
            except sqlite3.IntegrityError:
                # 자재 구분(원↔부) 변경 시 같은 이름이 반대 구분에 이미 있으면 발생
                raise HTTPException(400, "같은 이름의 자재가 대상 구분(원재료/부재료)에 이미 있습니다 — "
                                    "기존 항목을 사용하거나 이름을 구분해 주세요")
        # 자재 단가가 바뀌었거나 '적용 시작일'을 지정했으면 단가 이력에 한 줄 기록 (그 날짜부터 계산에 반영)
        if mtype in ("raw", "sub", "semi") and "unit_price" in vals:
            try:
                newp = float(vals.get("unit_price") or 0)
            except (TypeError, ValueError):
                newp = 0.0
            pf = (body.get("price_from") or "").strip()
            changed = abs(float(old_price or 0) - newp) > 1e-9
            if newp > 0 and (pf or changed):
                fd = pf if re.match(r"^\d{4}-\d{2}-\d{2}$", pf) else dt.date.today().isoformat()
                # 이 자재의 첫 단가 변경이면, 이전 단가를 '이전' 기준(from_date='')으로 먼저 남겨 잃지 않게 한다
                has_hist = con.execute("SELECT COUNT(*) c FROM material_price WHERE material_id=?",
                                       (mid,)).fetchone()["c"]
                if not has_hist and old_price and float(old_price) > 0 and abs(float(old_price) - newp) > 1e-9:
                    con.execute("INSERT INTO material_price(material_id, from_date, price, note, set_at) "
                                "VALUES(?,?,?,?,datetime('now','localtime'))",
                                (mid, "", float(old_price), "이전 단가"))
                con.execute("INSERT INTO material_price(material_id, from_date, price, set_at) "
                            "VALUES(?,?,?,datetime('now','localtime'))", (mid, fd, newp))
                audit(con, "mat_price", f"자재#{mid} 단가 {fd}부터 {newp:g}원")
        # 현재고 직접 수정: 기초재고(opening_stock)를 조정해 계산 재고가 입력값이 되도록
        if mtype == "product" and stock_set is not None:
            cur = con.execute("""
                SELECT COALESCE(os.qty,0) + COALESCE(pr.q,0) - COALESCE(sh.q,0) - COALESCE(dp.q,0) AS stock,
                       COALESCE(os.qty,0) AS opening
                FROM product p
                LEFT JOIN opening_stock os ON os.kind='product' AND os.ref_id=p.id
                LEFT JOIN (SELECT product_id, SUM(prod_qty) q FROM production
                           WHERE product_id=? GROUP BY product_id) pr ON pr.product_id=p.id
                LEFT JOIN (SELECT product_id, SUM(qty) q FROM shipment
                           WHERE product_id=? GROUP BY product_id) sh ON sh.product_id=p.id
                LEFT JOIN (SELECT product_id, SUM(qty) q FROM disposal
                           WHERE product_id=? GROUP BY product_id) dp ON dp.product_id=p.id
                WHERE p.id=?""", (mid, mid, mid, mid)).fetchone()
            delta = float(stock_set) - cur["stock"]
            if abs(delta) > 1e-9:
                con.execute("""INSERT INTO opening_stock(kind, ref_id, date, qty)
                    VALUES('product', ?, date('now','localtime'), ?)
                    ON CONFLICT(kind, ref_id) DO UPDATE SET qty = qty + ?""",
                            (mid, cur["opening"] + delta, delta))
                audit(con, f"stock_adjust_product#{mid}",
                             f"{cur['stock']} -> {stock_set} (기초재고 {delta:+g})")
        # 자재 현재고 수정: 기준일의 실재고 기록으로 반영 (사용량 = 전일 + 입고 − 실재고 재계산)
        # ⚠ 반드시 src='manual'(실사)로 저장 — auto 행을 덮어쓰기만 하면 다음 저장의
        #    자동차감 재계산이 이 보정을 지워버림 (하얀설탕 -525 재발 사고의 원인)
        if mtype in ("raw", "sub") and stock_set is not None:
            sd = body.get("stock_date") or dt.date.today().isoformat()
            ex = con.execute(
                "SELECT * FROM material_daily WHERE material_id=? AND date=?",
                (mid, sd)).fetchone()
            if ex:
                used = ex["prev_qty"] + ex["in_qty"] - float(stock_set)
                con.execute("UPDATE material_daily SET real_qty=?, used_qty=?, src='manual' WHERE id=?",
                            (float(stock_set), used, ex["id"]))
            else:
                prev_row = con.execute("""SELECT real_qty FROM material_daily
                    WHERE material_id=? AND date<? ORDER BY date DESC LIMIT 1""",
                                       (mid, sd)).fetchone()
                prev = prev_row["real_qty"] if prev_row else 0.0
                con.execute("""INSERT INTO material_daily
                    (date, material_id, prev_qty, in_qty, real_qty, used_qty, src)
                    VALUES(?,?,?,?,?,?,'manual')""",
                            (sd, mid, prev, 0, float(stock_set), prev - float(stock_set)))
                con.execute("INSERT OR IGNORE INTO day_record(date) VALUES(?)", (sd,))
            ripple_material(con, mid, sd)   # 이후 날짜 기록의 전일재고 체인 재계산
            audit(con, f"stock_adjust_material#{mid}", f"{sd} 실재고 -> {stock_set}")
        audit(con, f"update_{mtype}#{mid}", json.dumps(vals, ensure_ascii=False))
        bump_masters()
        con.commit()
        return {"ok": True}
    finally:
        con.close()


# 삭제 전 참조 검사: 기록이 있으면 삭제 대신 상태 변경 유도
REF_CHECKS = {
    "product": [("production", "product_id", "생산"), ("shipment", "product_id", "출고"),
                ("material_usage", "product_id", "자재 사용"), ("lot_snapshot", "product_id", "LOT"),
                ("disposal", "product_id", "폐기"),
                ("bom", "product_id", "배합비")],
    "material": [("material_daily", "material_id", "일일 재고"),
                 ("material_usage", "material_id", "자재 사용"), ("bom", "material_id", "배합비")],
    "partner": [("shipment", "partner_id", "출고"), ("material", "partner_id", "자재 공급처")],
    "staff": [("staffing_member", "staff_id", "투입 기록")],
    "line": [("production", "line_id", "생산"), ("staffing", "line_id", "가동 기록"),
             ("product", "line_id", "제품 기본라인")],
}


@app.delete("/api/masters/{mtype}/{mid}")
def master_delete(mtype: str, mid: int):
    key = "material" if mtype in ("raw", "sub", "semi") else mtype
    if key not in MASTER_TABLES:
        raise HTTPException(404, "unknown master type")
    table = MASTER_TABLES[key][0]
    con = connect()
    try:
        row = con.execute(f"SELECT * FROM {table} WHERE id=?", (mid,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        used = []
        for t, col, label in REF_CHECKS.get(key, []):
            n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE {col}=?", (mid,)).fetchone()[0]
            if n:
                used.append(f"{label} {n}건")
        if used:
            raise HTTPException(400,
                f"'{row['name']}'은(는) {', '.join(used)}의 기록이 있어 삭제할 수 없습니다. "
                f"대신 상태를 단종/중단/중지로 바꾸면 목록에서 제외됩니다.")
        con.execute(f"DELETE FROM {table} WHERE id=?", (mid,))
        if key in ("product", "material"):
            con.execute("DELETE FROM opening_stock WHERE kind=? AND ref_id=?",
                        ("product" if key == "product" else "material", mid))
        audit(con, f"delete_{mtype}#{mid}", row["name"])
        bump_masters()
        con.commit()
        return {"ok": True}
    finally:
        con.close()


# ── 대시보드 ─────────────────────────────────


@app.get("/api/dashboard")
def dashboard(request: Request):
    con = connect()
    try:
        last = con.execute("SELECT MAX(date) d FROM production").fetchone()["d"]
        # 최근 14 기록일 추이
        trend = rows(con.execute("""
            SELECT date, SUM(prod) prod, SUM(ship) ship FROM (
              SELECT date, prod_qty prod, 0 ship FROM production
              UNION ALL
              SELECT date, 0, qty FROM shipment)
            GROUP BY date ORDER BY date DESC LIMIT 14"""))
        trend.reverse()
        # 자재 부족 — 안전재고를 설정한 자재만 (미설정·재고 0 자재는 판단 기준이 없어 목록에서 제외)
        low = rows(con.execute("""
            SELECT m.id, m.kind, m.name, m.unit, m.safety_stock, md.real_qty AS stock,
                   md.order_date, md.date
            FROM material m
            JOIN (
              SELECT material_id, real_qty, order_date, date,
                     ROW_NUMBER() OVER (PARTITION BY material_id ORDER BY date DESC) rn
              FROM material_daily) md ON md.material_id=m.id AND md.rn=1
            WHERE m.status!='중단'
              AND m.safety_stock>0 AND md.real_qty<m.safety_stock
            ORDER BY (md.real_qty - m.safety_stock) LIMIT 30"""))
        lastday = {
            "date": last,
            "prod": rows(con.execute("""
                SELECT pr.prod_qty, p.name FROM production pr JOIN product p ON p.id=pr.product_id
                WHERE pr.date=? ORDER BY pr.prod_qty DESC""", (last,))),
            "ship": rows(con.execute("""
                SELECT s.qty, p.name, pa.name partner FROM shipment s
                JOIN product p ON p.id=s.product_id
                LEFT JOIN partner pa ON pa.id=s.partner_id
                WHERE s.date=? ORDER BY s.qty DESC""", (last,))),
        }
        # 소비기한 임박: 계산 LOT 기반 — 제품 소비일 또는 LOT별 지정 기한이 있는 제품 스캔
        today = dt.date.today().isoformat()
        expiry = []
        for p in con.execute("""SELECT id, name FROM product
                WHERE status!='단종' AND (shelf_days>0
                  OR EXISTS(SELECT 1 FROM lot_expiry le
                            WHERE le.product_id=product.id AND le.expiry!=''))"""):
            try:
                cl = current_lots(con, p["id"], today)
            except HTTPException:
                continue
            for l in cl["lots"]:
                if l["expiry"] and l["qty"] > 0:
                    dleft = (dt.date.fromisoformat(l["expiry"]) - dt.date.today()).days
                    expiry.append({"name": p["name"], "qty": l["qty"], "made_date": l["made"],
                                   "expiry": l["expiry"], "days_left": dleft})
        expiry.sort(key=lambda x: x["expiry"])
        lot_warn = sum(1 for x in expiry if x["days_left"] is not None and x["days_left"] <= 7)
        lot_expired = sum(1 for x in expiry if x["days_left"] is not None and x["days_left"] < 0)
        expiry = expiry[:8]
        lot_date = today if expiry else None

        admin = mcan(request, "prod")            # 생산·출고·재고 금액
        can_labor = mcan(request, "labor")       # 노무비
        base = last or today   # 데이터가 없으면 오늘 기준 (빈 값)

        # 1) 오늘 입력 상태
        today_entered = con.execute(
            "SELECT (EXISTS(SELECT 1 FROM production WHERE date=?) OR "
            " EXISTS(SELECT 1 FROM day_record WHERE date=?)) e",
            (today, today)).fetchone()["e"]

        # 2) 최근 기록일 달성률·불량률
        ar = con.execute("""SELECT COALESCE(SUM(plan_qty),0) plan, COALESCE(SUM(prod_qty),0) prod,
                   COALESCE(SUM(defect_qty),0) defect FROM production WHERE date=?""", (last,)).fetchone() \
             if last else None
        ach = {"plan": ar["plan"], "prod": ar["prod"], "defect": ar["defect"]} if ar \
              else {"plan": 0, "prod": 0, "defect": 0}

        # 3) 완제품 재고 (전체 누계 = 기초+생산−출고−폐기)
        pstock = rows(con.execute("""
            SELECT p.name, p.unit_price, p.safety_stock, p.image,
                   COALESCE(os.qty,0)+COALESCE(pb.q,0)-COALESCE(sb.q,0)-COALESCE(dp.q,0) stock
            FROM product p
            LEFT JOIN opening_stock os ON os.kind='product' AND os.ref_id=p.id
            LEFT JOIN (SELECT product_id, SUM(prod_qty) q FROM production GROUP BY product_id) pb ON pb.product_id=p.id
            LEFT JOIN (SELECT product_id, SUM(qty) q FROM shipment GROUP BY product_id) sb ON sb.product_id=p.id
            LEFT JOIN (SELECT product_id, SUM(qty) q FROM disposal GROUP BY product_id) dp ON dp.product_id=p.id
            WHERE p.status!='단종'"""))
        prod_low = sorted(
            [{"name": r["name"], "image": r["image"], "stock": r["stock"], "safety": r["safety_stock"]}
             for r in pstock if r["safety_stock"] and r["safety_stock"] > 0 and r["stock"] < r["safety_stock"]],
            key=lambda x: x["stock"] - x["safety"])[:12]
        prod_stock_qty = sum(r["stock"] for r in pstock if r["stock"] > 0)
        prod_stock_amt = sum(r["stock"] * (r["unit_price"] or 0) for r in pstock if r["stock"] > 0)

        wa, wb = period_range("w", base)
        ma, mb = period_range("m", base)

        # 4) 이번달 생산·출고 금액 (admin)
        prod_month = con.execute("""
            SELECT COALESCE(SUM((pr.prod_qty-pr.defect_qty) *
                     CASE WHEN pr.unit_price>0 THEN pr.unit_price ELSE p.unit_price END),0) a
            FROM production pr JOIN product p ON p.id=pr.product_id
            WHERE pr.date BETWEEN ? AND ?""", (ma, mb)).fetchone()["a"]
        # 출고 금액 = 저장 시점 단가 스냅샷(거래처별 단가 반영) 우선, 없으면(옛 기록) 현재 기본 단가
        ship_month = con.execute("""
            SELECT COALESCE(SUM(s.qty * CASE WHEN s.unit_price>0 THEN s.unit_price
                                             ELSE p.unit_price END),0) a
            FROM shipment s JOIN product p ON p.id=s.product_id
            WHERE s.date BETWEEN ? AND ?""", (ma, mb)).fetchone()["a"]
        # 이번달 매입액 = 발주 입고분(입고일 기준) + 일일 입고 단가 입력분 (단가 미입력 제외)
        buy_month = 0.0
        for r in con.execute("""SELECT items FROM purchase_order
                WHERE received_at!='' AND substr(received_at,1,10) BETWEEN ? AND ?""", (ma, mb)):
            try:
                for it in json.loads(r["items"] or "[]"):
                    if (it.get("price") or 0) > 0:
                        buy_month += (it.get("recv") or it.get("qty") or 0) * it["price"]
            except ValueError:
                pass
        buy_month += con.execute("""SELECT COALESCE(SUM(qty*price),0) a FROM material_in
            WHERE price>0 AND note NOT LIKE '발주 #%' AND date BETWEEN ? AND ?""",
            (ma, mb)).fetchone()["a"]
        # 이번달 노무비 (직원 개인시간→라인 폴백 + 용역 상세→구방식 폴백)
        labor_month = con.execute("""
            SELECT COALESCE(SUM(
              (SELECT COALESCE(SUM(s.wage * CASE WHEN sm.hours>0 THEN sm.hours ELSE st.work_hours END),0)
                 FROM staffing_member sm JOIN staff s ON s.id=sm.staff_id WHERE sm.staffing_id=st.id)
              + COALESCE((SELECT SUM(sa.hours * sa.wage) FROM staffing_agency sa
                          WHERE sa.staffing_id=st.id),
                         st.agency_hours * st.agency_wage)),0) v
            FROM staffing st WHERE st.date BETWEEN ? AND ?""", (ma, mb)).fetchone()["v"]

        # 5) 거래처별 출고 비중 (이번달)
        ship_partner = rows(con.execute("""
            SELECT COALESCE(pa.name,'거래처 미상') partner, SUM(s.qty) qty
            FROM shipment s LEFT JOIN partner pa ON pa.id=s.partner_id
            WHERE s.qty>0 AND s.date BETWEEN ? AND ?
            GROUP BY partner ORDER BY qty DESC""", (ma, mb)))

        # 6) 인원 가동률 (최근 기록일) — 같은 라인명 = 한 물리 라인 (공정 행들은 max로 합침)
        us = None
        if last:
            grp = {}
            hc_total = 0.0
            for r in con.execute("""
                SELECT COALESCE(pl.name, l.name, '행'||st.id) lname, st.work_hours wh,
                       COALESCE(NULLIF(st.target_hours,0), l.std_hours, 8) std,
                       st.headcount + st.agency_count hc
                FROM staffing st LEFT JOIN line l ON l.id=st.line_id
                LEFT JOIN line pl ON pl.id=l.parent_id
                WHERE st.date=?""", (last,)):
                g = grp.setdefault(r["lname"], {"wh": 0.0, "std": 0.0})
                g["wh"] = max(g["wh"], float(r["wh"] or 0))
                g["std"] = max(g["std"], float(r["std"] or 0))
                hc_total += float(r["hc"] or 0)
            us = {"wh": sum(g["wh"] for g in grp.values()),
                  "std": sum(g["std"] for g in grp.values()),
                  "hc": hc_total, "lines": len(grp)}
        labor_won = con.execute("""
            SELECT COALESCE(SUM(
              (SELECT COALESCE(SUM(s.wage * CASE WHEN sm.hours>0 THEN sm.hours ELSE st.work_hours END),0)
                 FROM staffing_member sm JOIN staff s ON s.id=sm.staff_id WHERE sm.staffing_id=st.id)
              + COALESCE((SELECT SUM(sa.hours * sa.wage) FROM staffing_agency sa
                          WHERE sa.staffing_id=st.id),
                         st.agency_hours * st.agency_wage)),0) labor
            FROM staffing st WHERE st.date=?""", (last,)).fetchone()["labor"] if last else 0
        util = {"rate": (round(us["wh"] / us["std"] * 100) if us and us["std"] else None),
                "headcount": (us["hc"] if us else 0),
                "lines": (us["lines"] if us else 0),
                "labor": (labor_won if can_labor else None)}

        # 8) 이번주 생산 TOP 제품
        top_prod = rows(con.execute("""
            SELECT p.name, p.image, SUM(pr.prod_qty) qty
            FROM production pr JOIN product p ON p.id=pr.product_id
            WHERE pr.date BETWEEN ? AND ? AND pr.prod_qty>0
            GROUP BY pr.product_id ORDER BY qty DESC LIMIT 5""", (wa, wb)))

        kpi = {
            "low_raw": sum(1 for x in low if x["kind"] == "raw"),
            "low_sub": sum(1 for x in low if x["kind"] == "sub"),
            "last_prod": sum(x["prod_qty"] for x in lastday["prod"]),
            "last_ship": sum(x["qty"] for x in lastday["ship"]),
            "days": con.execute("SELECT COUNT(*) c FROM day_record").fetchone()["c"],
            "products": con.execute(
                "SELECT COUNT(*) c FROM product WHERE status!='단종'").fetchone()["c"],
        }
        return {"kpi": kpi, "trend": trend, "low": low, "lastday": lastday,
                "expiry": expiry, "lot_date": lot_date, "lot_warn": lot_warn,
                "lot_expired": lot_expired,
                "today": today, "today_entered": bool(today_entered), "last_day": last,
                "ach": ach, "prod_low": prod_low,
                "prod_stock_qty": prod_stock_qty,
                "prod_stock_amt": (prod_stock_amt if admin else None),
                "prod_low_cnt": len(prod_low),
                "money": ({"prod": prod_month, "ship": ship_month, "buy": buy_month,
                           "label": base[:7]} if admin else None),
                "month_labor": (round(labor_month) if can_labor else None),
                "ship_partner": ship_partner, "util": util, "top_prod": top_prod,
                "week": [wa, wb], "month_label": base[:7]}
    finally:
        con.close()


# ── 생산 현황 ────────────────────────────────
def price_maps(con):
    """기본단가·거래처별단가 조회용. (base{pid:price}, pp{(pid,partner):price}, haspp{pid})"""
    base = {r["id"]: (r["unit_price"] or 0) for r in con.execute("SELECT id, unit_price FROM product")}
    pp = {}
    for r in con.execute("SELECT product_id, partner_id, price FROM product_price WHERE price>0"):
        pp[(r["product_id"], r["partner_id"])] = r["price"]
    haspp = {pid for (pid, _) in pp}
    return base, pp, haspp


def prod_amounts(con, a, b):
    """(date, product_id) → (생산금액, priced).
    거래처 분배(prod_split) 수량 × 그 거래처 단가(없으면 기본단가), 미분배분은 기본단가.
    → 거래처별 판매가가 다르면 생산금액도 거래처 구성대로 달라진다."""
    base, pp, haspp = price_maps(con)
    prods = {}
    for r in con.execute("SELECT date, product_id, prod_qty, unit_price FROM production "
                         "WHERE date BETWEEN ? AND ?", (a, b)):
        prods[(r["date"], r["product_id"])] = [float(r["prod_qty"] or 0), float(r["unit_price"] or 0)]
    dist = {}
    for r in con.execute("SELECT date, product_id, partner_id, qty FROM prod_split "
                         "WHERE date BETWEEN ? AND ? AND qty>0", (a, b)):
        dist.setdefault((r["date"], r["product_id"]), []).append((r["partner_id"], float(r["qty"])))
    out = {}
    for (d, pid), (qty, snap) in prods.items():
        bp = snap if snap > 0 else base.get(pid, 0)     # 미분배·기본은 저장 시점 단가(스냅샷) 우선
        amt, used = 0.0, 0.0
        for partner_id, sq in dist.get((d, pid), []):
            amt += sq * pp.get((pid, partner_id), bp)   # 거래처 단가(현재값) > 기본
            used += sq
        if qty - used > 1e-9:
            amt += (qty - used) * bp
        out[(d, pid)] = (round(amt), (bp > 0) or (pid in haspp))
    return out


@app.get("/api/prodstatus")
def prodstatus(request: Request, mode: str = "d", date: str = ""):
    con = connect()
    try:
        admin = mcan(request, "prod")   # 생산금액·단가 열람 권한
        if not date:
            date = con.execute("SELECT MAX(date) d FROM production").fetchone()["d"] \
                or dt.date.today().isoformat()   # 기록이 없으면 오늘 (빈 현황)
        if mode == "d":
            data = rows(con.execute("""
                SELECT p.id product_id, p.name, pr.plan_qty, pr.prod_qty, pr.defect_qty,
                       pr.defect_reason, pr.line_id, l.name line, COALESCE(s.q,0) ship
                FROM production pr
                JOIN product p ON p.id=pr.product_id
                LEFT JOIN line l ON l.id=pr.line_id
                LEFT JOIN (SELECT product_id, SUM(qty) q FROM shipment WHERE date=?
                           GROUP BY product_id) s ON s.product_id=pr.product_id
                WHERE pr.date=? ORDER BY pr.prod_qty DESC""", (date, date)))
            amounts = prod_amounts(con, date, date)   # 거래처 분배 반영 생산금액
            for r in data:
                amt, priced = amounts.get((date, r["product_id"]), (0, False))
                r["amount"] = amt if admin else None
                r["priced"] = priced
            dates = [r["date"] for r in con.execute(
                "SELECT DISTINCT date FROM production ORDER BY date")]
            return {"date": date, "rows": data, "dates": dates}
        if mode == "w":
            d0 = dt.date.fromisoformat(date)
            mon = d0 - dt.timedelta(days=d0.weekday())
            days = [(mon + dt.timedelta(days=i)).isoformat() for i in range(7)]
            data = rows(con.execute(f"""
                SELECT p.name, pr.date, SUM(pr.prod_qty) q
                FROM production pr JOIN product p ON p.id=pr.product_id
                WHERE pr.date IN ({','.join('?'*7)})
                GROUP BY p.name, pr.date""", days))
            return {"start": days[0], "end": days[-1], "days": days, "rows": data}
        if mode == "m":
            ym = date[:7]
            data = rows(con.execute("""
                SELECT pr.date, SUM(pr.prod_qty) prod, SUM(pr.defect_qty) defect, SUM(pr.plan_qty) plan
                FROM production pr WHERE substr(pr.date,1,7)=? GROUP BY pr.date ORDER BY pr.date""", (ym,)))
            # 거래처 분배 반영 금액을 날짜별로 합산
            amt_by_date = {}
            for (d, _pid), (amt, _p) in prod_amounts(con, ym + "-01", ym + "-31").items():
                amt_by_date[d] = amt_by_date.get(d, 0) + amt
            ship = {r["date"]: r["q"] for r in con.execute(
                "SELECT date, SUM(qty) q FROM shipment WHERE substr(date,1,7)=? GROUP BY date",
                (ym,))}
            for r in data:
                r["ship"] = ship.get(r["date"], 0)
                r["amount"] = amt_by_date.get(r["date"], 0) if admin else None
            return {"month": ym, "rows": data}
        if mode == "y":
            yr = date[:4]
            data = rows(con.execute("""
                SELECT substr(pr.date,1,7) ym, SUM(pr.prod_qty) prod, SUM(pr.defect_qty) defect
                FROM production pr WHERE substr(pr.date,1,4)=? GROUP BY ym ORDER BY ym""", (yr,)))
            amt_by_ym = {}
            for (d, _pid), (amt, _p) in prod_amounts(con, yr + "-01-01", yr + "-12-31").items():
                amt_by_ym[d[:7]] = amt_by_ym.get(d[:7], 0) + amt
            ship = {r["ym"]: r["q"] for r in con.execute(
                "SELECT substr(date,1,7) ym, SUM(qty) q FROM shipment WHERE substr(date,1,4)=? GROUP BY ym",
                (yr,))}
            for r in data:
                r["ship"] = ship.get(r["ym"], 0)
                r["amount"] = amt_by_ym.get(r["ym"], 0) if admin else None
            return {"year": yr, "rows": data}
        raise HTTPException(400, "mode must be d/w/m/y")
    finally:
        con.close()


def calc_work_hours(start, end, brk):
    """출근·퇴근(HH:MM) + 휴게(분) → 근무시간(시간, 소수 2자리). 시각이 불완전하면 None.
    퇴근이 출근보다 이르면 자정을 넘긴 근무로 보고 +24시간."""
    def to_min(t):
        t = (t or "").strip()
        if not t or ":" not in t:
            return None
        try:
            h, m = t.split(":")[:2]
            return int(h) * 60 + int(m)
        except ValueError:
            return None
    s, e = to_min(start), to_min(end)
    if s is None or e is None:
        return None
    if e < s:
        e += 24 * 60
    net = e - s - (float(brk) if brk else 0)
    return max(0.0, round(net / 60.0, 2))


def period_range(mode: str, date: str):
    d0 = dt.date.fromisoformat(date)
    if mode == "d":
        return date, date
    if mode == "w":
        mon = d0 - dt.timedelta(days=d0.weekday())
        return mon.isoformat(), (mon + dt.timedelta(days=6)).isoformat()
    if mode == "m":
        nxt = (d0.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return date[:7] + "-01", (nxt - dt.timedelta(days=1)).isoformat()
    return date[:4] + "-01-01", date[:4] + "-12-31"


@app.get("/api/monthreport")
def month_report(request: Request, ym: str = ""):
    """월간 마감 리포트 — 생산·출고·자재 사용액·발주 입고액·노무비를 한 달 기준으로 집계.
    노무비가 포함되므로 admin 전용. 자재 단가는 월말 이전 마지막 발주 입고 단가, 없으면 기준 단가."""
    require_admin(request)
    if not re.match(r"^\d{4}-\d{2}$", ym or ""):
        ym = dt.date.today().isoformat()[:7]
    a, b = period_range("m", ym + "-01")
    con = connect()
    try:
        prod = rows(con.execute("""
            SELECT p.name, SUM(pr.prod_qty) qty, SUM(pr.defect_qty) defect,
                   SUM(pr.prod_qty * pr.unit_price) amount
            FROM production pr JOIN product p ON p.id=pr.product_id
            WHERE pr.date BETWEEN ? AND ? AND (pr.prod_qty>0 OR pr.defect_qty>0)
            GROUP BY p.id ORDER BY qty DESC""", (a, b)))
        prod_days = con.execute("""SELECT COUNT(DISTINCT date) d FROM production
            WHERE date BETWEEN ? AND ? AND prod_qty>0""", (a, b)).fetchone()["d"]
        ship_pa = rows(con.execute("""
            SELECT COALESCE(pa.name,'거래처 미상') partner, SUM(s.qty) qty
            FROM shipment s LEFT JOIN partner pa ON pa.id=s.partner_id
            WHERE s.date BETWEEN ? AND ? AND s.qty>0
            GROUP BY COALESCE(pa.name,'거래처 미상') ORDER BY qty DESC""", (a, b)))
        ship_pr = rows(con.execute("""
            SELECT p.name, SUM(s.qty) qty
            FROM shipment s JOIN product p ON p.id=s.product_id
            WHERE s.date BETWEEN ? AND ? AND s.qty>0
            GROUP BY p.id ORDER BY qty DESC""", (a, b)))
        # 자재 사용액 — 사용량 × 단가 (월말 이전 마지막 발주 입고 단가 → 없으면 기준 단가)
        used = rows(con.execute("""
            SELECT m.id, m.name, m.unit, m.unit_price, SUM(md.used_qty) used
            FROM material_daily md JOIN material m ON m.id=md.material_id
            WHERE md.date BETWEEN ? AND ? AND md.used_qty>0
            GROUP BY m.id ORDER BY used DESC""", (a, b)))
        # 자재별 최신 실입고 단가 (발주 입고 + 일일 입고, 월말 이전) — 없으면 기준 단가로 폴백(아래)
        last_price = latest_material_prices(con, upto=b)
        for r in used:
            r["price"] = last_price.get(r["id"]) or r["unit_price"] or 0
            r["amount"] = round(r["used"] * r["price"])
            r.pop("unit_price", None)
        used.sort(key=lambda x: -x["amount"])
        # 발주 입고액 — 이 달 입고 처리분 (실제 매입), 단가 미입력 품목은 집계 제외하고 건수만 안내
        po_pa, unpriced = {}, 0
        for r in con.execute("""SELECT COALESCE(pa.name, NULLIF(po.partner_name,''), '미지정') pname, po.items
                FROM purchase_order po LEFT JOIN partner pa ON pa.id=po.partner_id
                WHERE po.received_at!='' AND substr(po.received_at,1,10) BETWEEN ? AND ?""", (a, b)):
            try:
                its = json.loads(r["items"] or "[]")
            except ValueError:
                continue
            for it in its:
                q = it.get("recv") or it.get("qty") or 0
                if (it.get("price") or 0) > 0:
                    po_pa[r["pname"]] = po_pa.get(r["pname"], 0) + round(q * it["price"])
                else:
                    unpriced += 1
        # 일일 입력에서 단가를 직접 입력한 입고분도 매입액에 포함 (발주 유래 행은 위에서 집계됨)
        for r in con.execute("""SELECT COALESCE(NULLIF(partner,''),'미지정') pname, SUM(qty*price) amt
                FROM material_in WHERE price>0 AND note NOT LIKE '발주 #%' AND date BETWEEN ? AND ?
                GROUP BY COALESCE(NULLIF(partner,''),'미지정')""", (a, b)):
            po_pa[r["pname"]] = po_pa.get(r["pname"], 0) + round(r["amt"] or 0)
        po_in = sorted(({"partner": k, "amount": v} for k, v in po_pa.items()),
                       key=lambda x: -x["amount"])
        # 노무비 — 직원(개인시간, 미입력 시 라인 실가동 폴백) + 용역(상세행, 없으면 구 방식 합계 폴백)
        HRS = "CASE WHEN sm.hours>0 THEN sm.hours ELSE st.work_hours END"
        lm = con.execute(f"""SELECT COALESCE(SUM({HRS}),0) hours, COALESCE(SUM({HRS}*s.wage),0) labor
            FROM staffing_member sm JOIN staffing st ON st.id=sm.staffing_id JOIN staff s ON s.id=sm.staff_id
            WHERE st.date BETWEEN ? AND ?""", (a, b)).fetchone()
        ag = con.execute("""SELECT COALESCE(SUM(sa.hours),0) hours, COALESCE(SUM(sa.hours*sa.wage),0) labor
            FROM staffing_agency sa JOIN staffing st ON st.id=sa.staffing_id
            WHERE st.date BETWEEN ? AND ?""", (a, b)).fetchone()
        legacy = con.execute("""SELECT COALESCE(SUM(agency_hours),0) hours,
                                       COALESCE(SUM(agency_hours*agency_wage),0) labor
            FROM staffing st WHERE st.date BETWEEN ? AND ?
              AND NOT EXISTS (SELECT 1 FROM staffing_agency sa WHERE sa.staffing_id=st.id)""",
            (a, b)).fetchone()
        return {"ym": ym, "from": a, "to": b, "prod_days": prod_days,
                "prod": prod,
                "prod_total": {"qty": sum(r["qty"] or 0 for r in prod),
                               "defect": sum(r["defect"] or 0 for r in prod),
                               "amount": round(sum(r["amount"] or 0 for r in prod))},
                "ship_partner": ship_pa, "ship_product": ship_pr,
                "ship_total": sum(r["qty"] or 0 for r in ship_pa),
                "mat_used": used[:20], "mat_used_more": max(0, len(used) - 20),
                "mat_used_total": sum(r["amount"] for r in used),
                "po_in": po_in, "po_in_total": sum(r["amount"] for r in po_in),
                "po_unpriced": unpriced,
                "labor": {"staff_hours": lm["hours"], "staff": round(lm["labor"]),
                          "agency_hours": ag["hours"] + legacy["hours"],
                          "agency": round(ag["labor"] + legacy["labor"]),
                          "total": round(lm["labor"] + ag["labor"] + legacy["labor"])}}
    finally:
        con.close()


@app.get("/api/shipstatus")
def shipstatus(request: Request, mode: str = "d", date: str = ""):
    """출고 현황: 기간 내 출고를 개별 건 + 제품별·거래처별 집계로."""
    admin = mcan(request, "prod")   # 출고 금액·단가 열람 권한
    con = connect()
    try:
        if not date:
            date = con.execute("SELECT MAX(date) d FROM shipment").fetchone()["d"] \
                or dt.date.today().isoformat()
        a, b = period_range(mode, date)
        rows_ = rows(con.execute("""
            SELECT s.date, p.name, COALESCE(pa.name,'거래처 미상') partner,
                   s.qty, s.prod_date, s.expiry,
                   -- 단가 = 저장 시점 스냅샷(거래처별 단가 반영) > 현재 기본 단가
                   CASE WHEN s.unit_price>0 THEN s.unit_price
                        WHEN p.unit_price>0 THEN p.unit_price ELSE 0 END price
            FROM shipment s JOIN product p ON p.id=s.product_id
            LEFT JOIN partner pa ON pa.id=s.partner_id
            WHERE s.qty>0 AND s.date BETWEEN ? AND ?
            ORDER BY s.date DESC, p.name""", (a, b)))
        by_prod, by_part = {}, {}
        for r in rows_:
            r["amount"] = r["qty"] * (r["price"] or 0)
            bp = by_prod.setdefault(r["name"], {"name": r["name"], "qty": 0.0, "amount": 0.0})
            bp["qty"] += r["qty"]; bp["amount"] += r["amount"]
            pt = by_part.setdefault(r["partner"], {"partner": r["partner"], "qty": 0.0, "amount": 0.0})
            pt["qty"] += r["qty"]; pt["amount"] += r["amount"]
        if not admin:
            for r in rows_:
                r["price"] = None; r["amount"] = None
            for v in list(by_prod.values()) + list(by_part.values()):
                v["amount"] = None
        return {"date": date, "range": [a, b], "mode": mode, "rows": rows_,
                "by_product": sorted(by_prod.values(), key=lambda x: -x["qty"]),
                "by_partner": sorted(by_part.values(), key=lambda x: -x["qty"]),
                "total_qty": sum(r["qty"] for r in rows_),
                "total_amount": (sum(r["amount"] or 0 for r in rows_) if admin else None)}
    finally:
        con.close()


@app.get("/api/prodreport")
def prodreport(request: Request, mode: str = "d", date: str = ""):
    """생산 현황 보고서 섹션 2~5: 원부자재 소모 / 인원·가동 / 완제품 재고 / 특이사항."""
    con = connect()
    try:
        if not date:
            date = con.execute("SELECT MAX(date) d FROM production").fetchone()["d"] \
                or dt.date.today().isoformat()   # 기록이 하나도 없으면 오늘 (빈 보고서)
        a, b = period_range(mode, date)
        # 2) 원부자재 소모 (기초=첫 기록일 전일재고, 기말=마지막 기록일 실재고)
        materials = rows(con.execute("""
            SELECT m.id, m.name, m.unit, m.unit_price, m.kind,
                   (SELECT prev_qty FROM material_daily x WHERE x.material_id=m.id
                     AND x.date BETWEEN ? AND ? ORDER BY x.date LIMIT 1) open,
                   SUM(md.in_qty) inq, SUM(md.used_qty) used,
                   (SELECT real_qty FROM material_daily x WHERE x.material_id=m.id
                     AND x.date BETWEEN ? AND ? ORDER BY x.date DESC LIMIT 1) close
            FROM material_daily md JOIN material m ON m.id=md.material_id
            WHERE md.date BETWEEN ? AND ?
            GROUP BY m.id
            HAVING SUM(md.used_qty)>0 OR SUM(md.in_qty)>0
            ORDER BY SUM(md.used_qty) DESC""", (a, b, a, b, a, b)))
        # 수율(로스): 배합비 × 기간 생산수량 = 이론 사용량
        theo = {r["material_id"]: r["theo"] for r in con.execute("""
            SELECT b.material_id,
                   SUM(CASE WHEN m.pack_count>0 AND COALESCE(b.qty_per_unit,0)=0
                              THEN pr.prod_qty/m.pack_count            -- 개수 자재: 생산수량 ÷ 개입수
                            WHEN b.unit='g' AND m.unit='kg'
                              THEN pr.prod_qty*b.qty_per_unit/1000.0
                            ELSE pr.prod_qty*b.qty_per_unit END) theo
            FROM bom b
            JOIN production pr ON pr.product_id=b.product_id AND pr.date BETWEEN ? AND ?
            JOIN material m ON m.id=b.material_id
            GROUP BY b.material_id""", (a, b))}
        for r in materials:
            r["theo"] = theo.get(r["id"])
        # 3) 인원·가동 — 노무비 = Σ(개인 투입시간 × 시급), 시간 미입력 인원은 라인 실가동 시간으로 폴백
        staffing = rows(con.execute("""
            SELECT st.date, COALESCE(pl.name, l.name, '—') line, COALESCE(l.process,'') process,
                   COALESCE(NULLIF(st.target_hours,0), l.std_hours, 8) std_hours,
                   st.headcount + st.agency_count headcount, st.work_hours, st.stop_reason,
                   st.agency_count,
                   (SELECT COALESCE(SUM(s.wage),0) FROM staffing_member sm
                     JOIN staff s ON s.id=sm.staff_id WHERE sm.staffing_id=st.id)
                     + COALESCE((SELECT SUM(sa.wage) FROM staffing_agency sa
                                 WHERE sa.staffing_id=st.id),
                                st.agency_count * st.agency_wage) wage_sum,
                   (SELECT COALESCE(SUM(s.wage * CASE WHEN sm.hours>0 THEN sm.hours
                                                      ELSE st.work_hours END),0)
                      FROM staffing_member sm
                      JOIN staff s ON s.id=sm.staff_id WHERE sm.staffing_id=st.id)
                     + COALESCE((SELECT SUM(sa.hours * sa.wage) FROM staffing_agency sa
                                 WHERE sa.staffing_id=st.id),
                                st.agency_hours * st.agency_wage) labor
            FROM staffing st LEFT JOIN line l ON l.id=st.line_id
            LEFT JOIN line pl ON pl.id=l.parent_id
            WHERE st.date BETWEEN ? AND ? ORDER BY st.date, st.id""", (a, b)))
        # 3.5) 용역 정산 — 업체별 × 날짜별 인원(남/여)·시간·노무비 (staffing_agency 상세 기준)
        agency_report = rows(con.execute("""
            SELECT st.date, COALESCE(pa.name, '업체 미지정') partner,
                   COUNT(*) cnt,
                   SUM(CASE WHEN sa.gender='남' THEN 1 ELSE 0 END) male,
                   SUM(CASE WHEN sa.gender='여' THEN 1 ELSE 0 END) female,
                   SUM(sa.hours) hours, SUM(sa.hours * sa.wage) labor
            FROM staffing_agency sa
            JOIN staffing st ON st.id=sa.staffing_id
            LEFT JOIN partner pa ON pa.id=sa.partner_id
            WHERE st.date BETWEEN ? AND ?
            GROUP BY st.date, COALESCE(pa.name, '업체 미지정')
            ORDER BY partner, st.date""", (a, b)))
        # 4) 완제품 재고현황
        stock = rows(con.execute("""
            SELECT p.id, p.name, p.unit_price,
                   COALESCE(os.qty,0)+COALESCE(pb.q,0)-COALESCE(sb.q,0)-COALESCE(db.q,0) AS open,
                   COALESCE(pp.q,0) prod, COALESCE(pp.defect,0) defect, COALESCE(sp.q,0) ship,
                   COALESCE(dd.q,0) disp
            FROM product p
            LEFT JOIN opening_stock os ON os.kind='product' AND os.ref_id=p.id
            LEFT JOIN (SELECT product_id, SUM(prod_qty) q FROM production WHERE date<?
                       GROUP BY product_id) pb ON pb.product_id=p.id
            LEFT JOIN (SELECT product_id, SUM(qty) q FROM shipment WHERE date<?
                       GROUP BY product_id) sb ON sb.product_id=p.id
            LEFT JOIN (SELECT product_id, SUM(qty) q FROM disposal WHERE date<?
                       GROUP BY product_id) db ON db.product_id=p.id
            LEFT JOIN (SELECT product_id, SUM(prod_qty) q, SUM(defect_qty) defect
                       FROM production WHERE date BETWEEN ? AND ?
                       GROUP BY product_id) pp ON pp.product_id=p.id
            LEFT JOIN (SELECT product_id, SUM(qty) q FROM shipment WHERE date BETWEEN ? AND ?
                       GROUP BY product_id) sp ON sp.product_id=p.id
            LEFT JOIN (SELECT product_id, SUM(qty) q FROM disposal WHERE date BETWEEN ? AND ?
                       GROUP BY product_id) dd ON dd.product_id=p.id
            WHERE COALESCE(pp.q,0)>0 OR COALESCE(sp.q,0)>0 OR COALESCE(dd.q,0)>0
               OR COALESCE(os.qty,0)+COALESCE(pb.q,0)-COALESCE(sb.q,0)-COALESCE(db.q,0) != 0
            ORDER BY p.sort, p.id""", (a, a, a, a, b, a, b, a, b)))
        # 각 완제품의 기말(=기간 끝 b) 시점 LOT(생산일/소비기한/거래처/포장) — 표 소비기한 요약 + 클릭 상세용
        pmap = {row["id"]: row["name"] for row in con.execute("SELECT id, name FROM partner")}
        # 포장 자재 개입수(pack_count) — LOT 박스 수 계산용
        packmap = {row["id"]: (row["name"], row["pack_count"])
                   for row in con.execute("SELECT id, name, pack_count FROM material")}
        base_price, part_price, _ = price_maps(con)   # 거래처별 재고금액 계산용
        today_iso = dt.date.today().isoformat()
        for r in stock:
            close_qty = (r["open"] or 0) + (r["prod"] or 0) - (r["ship"] or 0) - (r["disp"] or 0)
            r["close"] = close_qty
            bp = base_price.get(r["id"], 0) or (r["unit_price"] or 0)
            r["amount"] = 0                            # 재고금액 = Σ LOT수량 × 그 거래처 단가
            if close_qty > 0.5:
                cl = current_lots(con, r["id"], b)
                for l in cl["lots"]:
                    l["partner"] = pmap.get(l.get("partner_id"))
                    # LOT 거래처 단가(없으면 기본) × 수량 = 이 LOT 금액
                    lp = part_price.get((r["id"], l.get("partner_id")), bp)
                    l["price"] = lp
                    l["amount"] = round(l["qty"] * lp)
                    r["amount"] += l["amount"]
                    # D-day: 소비기한까지 남은 일수 (오늘 기준)
                    try:
                        l["dday"] = (dt.date.fromisoformat(l["expiry"]) -
                                     dt.date.fromisoformat(today_iso)).days if l["expiry"] else None
                    except ValueError:
                        l["dday"] = None
                    # 포장 개입수 → 박스 수 (예: 5,040개 ÷ 30개입 = 168박스)
                    pm = l.get("pack_mid")
                    if pm and packmap.get(pm) and (packmap[pm][1] or 0) > 0:
                        pc = packmap[pm][1]
                        l["pack_count"] = pc
                        l["pack_name"] = packmap[pm][0]
                        l["boxes"] = round(l["qty"] / pc, 1)
                    elif l.get("pack_set"):
                        l["pack_name"] = l["pack_set"]      # 세트는 멤버별 개입수가 달라 박스수 생략
                r["lots"] = cl["lots"]
                exps = sorted(l["expiry"] for l in cl["lots"] if l["expiry"])
                r["exp_min"] = exps[0] if exps else None
                r["exp_max"] = exps[-1] if exps else None
            else:
                r["lots"] = []
                r["exp_min"] = r["exp_max"] = None
        # 5) 특이사항 (메모/수불부 비고/정지사유)
        memos = rows(con.execute("""
            SELECT date, '일일 메모' src, memo txt FROM day_record
             WHERE date BETWEEN ? AND ? AND memo!=''
            UNION ALL
            SELECT pr.date, p.name, pr.note FROM production pr
             JOIN product p ON p.id=pr.product_id
             WHERE pr.date BETWEEN ? AND ? AND pr.note!=''
            UNION ALL
            SELECT st.date, COALESCE(l.name,'라인')||' 정지', st.stop_reason FROM staffing st
             LEFT JOIN line l ON l.id=st.line_id
             WHERE st.date BETWEEN ? AND ? AND st.stop_reason!=''
            ORDER BY date""", (a, b, a, b, a, b)))
        # 금액 권한별 마스킹: 노무비(labor) / 자재 단가(mat) / 완제품 단가(prod)
        if not mcan(request, "labor"):
            for r in staffing:
                r["wage_sum"] = None
                r["labor"] = None
            for r in agency_report:
                r["labor"] = None
        if not mcan(request, "mat"):
            for r in materials:
                r["unit_price"] = None
        if not mcan(request, "prod"):
            for r in stock:
                r["unit_price"] = None
                r["amount"] = None
                for l in r.get("lots") or []:
                    l["price"] = None
                    l["amount"] = None
        return {"range": [a, b], "materials": materials, "staffing": staffing,
                "agency_report": agency_report,
                "stock": stock, "memos": memos}
    finally:
        con.close()


# ── 인원 관리 (사람별 근무시간 집계 — admin 전용) ─────────────
@app.get("/api/staffhours")
def staffhours(request: Request, mode: str = "m", date: str = ""):
    """사람별 근무시간·근무일수·노무비를 일/주/월/년 기간으로 집계. admin 전용.
    개인 투입시간(sm.hours) 미입력 시 라인 실가동(st.work_hours)으로 폴백 — prodreport와 동일 규칙."""
    require_admin(request)
    con = connect()
    try:
        if not date:
            date = con.execute("SELECT MAX(date) d FROM staffing").fetchone()["d"] \
                or dt.date.today().isoformat()
        a, b = period_range(mode, date)
        HRS = "CASE WHEN sm.hours>0 THEN sm.hours ELSE st.work_hours END"
        members = rows(con.execute(f"""
            SELECT s.id, s.name, s.wage,
                   SUM({HRS}) hours,
                   COUNT(DISTINCT st.date) days,
                   SUM({HRS} * s.wage) labor
            FROM staffing_member sm
            JOIN staffing st ON st.id=sm.staffing_id
            JOIN staff s ON s.id=sm.staff_id
            WHERE st.date BETWEEN ? AND ?
            GROUP BY s.id, s.name, s.wage
            ORDER BY hours DESC, s.name""", (a, b)))
        for r in members:
            r["avg"] = (r["hours"] / r["days"]) if r["days"] else 0
        # 용역 — 이름이 없어 개인 집계 불가 → 업체별 별도 합계 (person-days = 투입 건수)
        agency = rows(con.execute("""
            SELECT COALESCE(pa.name,'업체 미지정') partner,
                   COUNT(*) persondays, COUNT(DISTINCT st.date) days,
                   SUM(sa.hours) hours, SUM(sa.hours * sa.wage) labor
            FROM staffing_agency sa
            JOIN staffing st ON st.id=sa.staffing_id
            LEFT JOIN partner pa ON pa.id=sa.partner_id
            WHERE st.date BETWEEN ? AND ?
            GROUP BY COALESCE(pa.name,'업체 미지정')
            ORDER BY hours DESC""", (a, b)))
        # 구(舊) 방식 용역 집계(개인 상세행 없이 staffing에 합계만 있는 경우) 폴백
        legacy = con.execute("""
            SELECT COALESCE(SUM(st.agency_hours),0) hours,
                   COALESCE(SUM(st.agency_hours * st.agency_wage),0) labor,
                   COUNT(DISTINCT st.date) days,
                   COALESCE(SUM(st.agency_count),0) persondays
            FROM staffing st
            WHERE st.date BETWEEN ? AND ? AND st.agency_hours>0
              AND NOT EXISTS(SELECT 1 FROM staffing_agency sa WHERE sa.staffing_id=st.id)
            """, (a, b)).fetchone()
        if legacy and (legacy["hours"] or 0) > 0:
            agency.append({"partner": "용역(구 입력)", "persondays": legacy["persondays"],
                           "days": legacy["days"], "hours": legacy["hours"], "labor": legacy["labor"]})
        return {"date": date, "mode": mode, "range": [a, b],
                "members": members, "agency": agency,
                "total_hours": sum(r["hours"] or 0 for r in members),
                "total_labor": sum(r["labor"] or 0 for r in members),
                "agency_hours": sum(r["hours"] or 0 for r in agency),
                "agency_labor": sum(r["labor"] or 0 for r in agency)}
    finally:
        con.close()


@app.get("/api/staffdays/{staff_id}")
def staffdays(request: Request, staff_id: int, mode: str = "m", date: str = ""):
    """한 사람의 출근일 상세 — 인원 관리 이름 클릭 팝업용. 날짜별 라인·출퇴근·근무시간·노무비."""
    require_admin(request)
    con = connect()
    try:
        if not date:
            date = con.execute("SELECT MAX(date) d FROM staffing").fetchone()["d"] \
                or dt.date.today().isoformat()
        a, b = period_range(mode, date)
        s = con.execute("SELECT name, wage FROM staff WHERE id=?", (staff_id,)).fetchone()
        rows_ = rows(con.execute("""
            SELECT st.date, COALESCE(pl.name, l.name, '—') line, COALESCE(l.process,'') process,
                   CASE WHEN sm.hours>0 THEN sm.hours ELSE st.work_hours END hours,
                   sm.start_time start, sm.end_time end, sm.break_min brk
            FROM staffing_member sm
            JOIN staffing st ON st.id=sm.staffing_id
            LEFT JOIN line l ON l.id=st.line_id
            LEFT JOIN line pl ON pl.id=l.parent_id
            WHERE sm.staff_id=? AND st.date BETWEEN ? AND ?
            ORDER BY st.date, line""", (staff_id, a, b)))
        wage = (s["wage"] if s else 0) or 0
        for r in rows_:
            r["labor"] = round((r["hours"] or 0) * wage)
        return {"staff_id": staff_id, "name": s["name"] if s else "?",
                "wage": wage, "range": [a, b], "mode": mode,
                "days": len({r["date"] for r in rows_}), "rows": rows_,
                "total_hours": sum(r["hours"] or 0 for r in rows_),
                "total_labor": sum(r["labor"] or 0 for r in rows_)}
    finally:
        con.close()


@app.get("/api/staffledger")
def staff_ledger(request: Request, mode: str = "m", date: str = ""):
    """전체 인원 출퇴근 원장 — 인원(행) × 근무한 날짜(열) × 근무시간 매트릭스.
    개인 투입시간(sm.hours) 우선, 없으면 그 라인 실가동(st.work_hours)."""
    require_admin(request)
    con = connect()
    try:
        if not date:
            date = con.execute("SELECT MAX(date) d FROM staffing").fetchone()["d"] \
                or dt.date.today().isoformat()
        a, b = period_range(mode, date)
        staff = {r["id"]: r for r in con.execute("SELECT id, name, wage FROM staff")}
        att = rows(con.execute("""
            SELECT sm.staff_id sid, st.date d,
                   SUM(CASE WHEN sm.hours>0 THEN sm.hours ELSE st.work_hours END) hours,
                   MIN(NULLIF(sm.start_time,'')) start, MAX(NULLIF(sm.end_time,'')) end
            FROM staffing_member sm JOIN staffing st ON st.id=sm.staffing_id
            WHERE st.date BETWEEN ? AND ?
            GROUP BY sm.staff_id, st.date ORDER BY st.date""", (a, b)))
        cell = {}                    # (sid, date) -> {h, s, e}
        dates_set = set()
        for r in att:
            cell[(r["sid"], r["d"])] = {"h": round(r["hours"] or 0, 2),
                                        "s": r["start"] or "", "e": r["end"] or ""}
            dates_set.add(r["d"])
        dates = sorted(dates_set)
        srows = []
        for sid, s in staff.items():
            days = [d for d in dates if (sid, d) in cell]
            if not days:
                continue             # 이 기간 출근 없는 사람은 원장에서 제외
            th = sum(cell[(sid, d)]["h"] for d in days)
            srows.append({"id": sid, "name": s["name"], "wage": s["wage"] or 0,
                          "cells": {d: cell[(sid, d)] for d in days},
                          "days": len(days), "hours": round(th, 1),
                          "labor": round(th * (s["wage"] or 0))})
        srows.sort(key=lambda x: x["name"])
        date_tot = {d: {"h": round(sum(cell[(s["id"], d)]["h"] for s in srows if (s["id"], d) in cell), 1),
                        "n": sum(1 for s in srows if (s["id"], d) in cell)} for d in dates}
        return {"mode": mode, "date": date, "range": [a, b], "dates": dates,
                "staff": srows, "date_tot": date_tot, "days_cnt": len(dates),
                "grand_hours": round(sum(r["hours"] for r in srows), 1),
                "grand_labor": round(sum(r["labor"] for r in srows))}
    finally:
        con.close()


# ── 일일 기록 (조회/저장) ─────────────────────


@app.get("/api/calendar")
def calendar_dates(ym: str, src: str = ""):
    """그 달에 '데이터가 있는 날'만 반환(달력 점 표시용). src로 종류별 필터:
    staffing=근무 있는 날, po=발주 있는 날, 기본=모든 활동."""
    con = connect()
    try:
        if src == "staffing":
            ds = [r["date"] for r in con.execute(
                "SELECT DISTINCT date FROM staffing WHERE substr(date,1,7)=? ORDER BY date", (ym,))]
        elif src == "po":
            ds = [r["date"] for r in con.execute(
                "SELECT DISTINCT date FROM purchase_order WHERE substr(date,1,7)=? ORDER BY date", (ym,))]
        else:
            # 실제 데이터가 있는 날만 점 표시 — 데이터를 지우고 저장하면 빈 day_record가 남는데,
            # 그 빈 날에는 점을 찍지 않는다(메모가 있으면 표시).
            ds = [r["date"] for r in con.execute("""
                SELECT date FROM day_record d WHERE substr(date,1,7)=? AND (
                    COALESCE(memo,'')!='' OR
                    EXISTS(SELECT 1 FROM production     WHERE date=d.date) OR
                    EXISTS(SELECT 1 FROM shipment       WHERE date=d.date) OR
                    EXISTS(SELECT 1 FROM material_usage WHERE date=d.date) OR
                    EXISTS(SELECT 1 FROM material_in    WHERE date=d.date) OR
                    EXISTS(SELECT 1 FROM material_daily WHERE date=d.date AND src='manual') OR
                    EXISTS(SELECT 1 FROM staffing       WHERE date=d.date))
                ORDER BY date""", (ym,))]
        return {"dates": ds}
    finally:
        con.close()


@app.get("/api/day/{date}")
def day_get(date: str, request: Request):
    con = connect()
    try:
        rec = con.execute("SELECT * FROM day_record WHERE date=?", (date,)).fetchone()
        production = rows(con.execute("""
            SELECT pr.*, p.name, COALESCE(le.expiry,'') expiry,
                   (SELECT json_group_array(json_object('qty', qty, 'expiry', expiry, 'pack_mid', pack_mid, 'pack_set', pack_set, 'partner_id', partner_id))
                      FROM (SELECT qty, expiry, pack_mid, pack_set, partner_id FROM lot_plan
                            WHERE product_id=pr.product_id AND made=pr.date
                            ORDER BY seq, id)) lot_splits,
                   (SELECT json_group_array(json_object('partner_id', partner_id, 'qty', qty))
                      FROM (SELECT partner_id, qty FROM prod_split
                            WHERE product_id=pr.product_id AND date=pr.date ORDER BY id)) prod_splits
            FROM production pr JOIN product p ON p.id=pr.product_id
            LEFT JOIN lot_expiry le ON le.product_id=pr.product_id AND le.made=pr.date
            WHERE pr.date=? ORDER BY pr.id""", (date,)))
        shipment = rows(con.execute("""
            SELECT s.*, p.name, pa.name partner FROM shipment s
            JOIN product p ON p.id=s.product_id
            LEFT JOIN partner pa ON pa.id=s.partner_id
            WHERE s.date=? ORDER BY s.id""", (date,)))
        materials = rows(con.execute("""
            SELECT md.*, m.name, m.unit, m.kind FROM material_daily md
            JOIN material m ON m.id=md.material_id
            WHERE md.date=? ORDER BY m.kind, m.sort""", (date,)))
        staffing = rows(con.execute("""
            SELECT st.*, l.name line,
                   COALESCE(pl.name, l.name, '—') line_group, COALESCE(l.process,'') process,
                   (SELECT json_group_array(staff_id) FROM staffing_member sm
                     WHERE sm.staffing_id=st.id) member_ids,
                   (SELECT json_group_array(json_object('id', sm.staff_id, 'h', sm.hours,
                                                        'start', sm.start_time, 'end', sm.end_time,
                                                        'brk', sm.break_min))
                     FROM staffing_member sm WHERE sm.staffing_id=st.id) members,
                   (SELECT json_group_array(json_object('h', sa.hours, 'w', sa.wage,
                                                        'g', sa.gender, 'pid', sa.partner_id,
                                                        'start', sa.start_time, 'end', sa.end_time,
                                                        'brk', sa.break_min))
                     FROM (SELECT hours, wage, gender, partner_id, start_time, end_time, break_min
                           FROM staffing_agency
                           WHERE staffing_id=st.id ORDER BY seq) sa) agency
            FROM staffing st LEFT JOIN line l ON l.id=st.line_id
            LEFT JOIN line pl ON pl.id=l.parent_id
            WHERE st.date=? ORDER BY st.id""", (date,)))
        if not mcan(request, "labor"):   # 용역 시급도 시급 — 노무비 권한
            for r in staffing:
                r["agency_wage"] = None
                try:
                    ag = json.loads(r.get("agency") or "[]")
                    for a in ag:
                        a["w"] = None
                    r["agency"] = json.dumps(ag)
                except (ValueError, TypeError):
                    r["agency"] = "[]"
        # 자재 전일재고: 직전 기록일 real_qty
        prev = rows(con.execute("""
            SELECT md.material_id, md.real_qty FROM material_daily md
            JOIN (SELECT material_id, MAX(date) d FROM material_daily WHERE date<?
                  GROUP BY material_id) x
              ON x.material_id=md.material_id AND x.d=md.date""", (date,)))
        # 직전 기록일 자재 목록 (불러오기용)
        prev_date_row = con.execute(
            "SELECT MAX(date) d FROM material_daily WHERE date<?", (date,)).fetchone()
        prev_date = prev_date_row["d"] if prev_date_row else None
        prev_materials = rows(con.execute("""
            SELECT md.material_id, md.real_qty, m.name, m.unit, m.kind
            FROM material_daily md JOIN material m ON m.id=md.material_id
            WHERE md.date=? ORDER BY m.kind, m.sort""", (prev_date,))) if prev_date else []
        lots = rows(con.execute("""
            SELECT ls.*, p.name FROM lot_snapshot ls JOIN product p ON p.id=ls.product_id
            WHERE ls.date=? ORDER BY p.sort, ls.kind DESC, ls.slot""", (date,)))
        mat_in = rows(con.execute("""
            SELECT mi.*, m.name, m.unit FROM material_in mi
            JOIN material m ON m.id=mi.material_id
            WHERE mi.date=? ORDER BY mi.id""", (date,)))
        # 발주됐는데 아직 입고 기록이 없는 자재 (최근 30일) → 입고 카드 자동 제안용
        pending_orders = rows(con.execute("""
            SELECT m.id material_id, m.name, m.unit, o.date rec_date, o.order_qty, o.order_date
            FROM (SELECT material_id, MAX(date) d FROM material_daily
                  WHERE (order_qty>0 OR COALESCE(order_date,'')!='')
                    AND date<=? AND date>=date(?, '-30 day')
                  GROUP BY material_id) x
            JOIN material_daily o ON o.material_id=x.material_id AND o.date=x.d
            JOIN material m ON m.id=o.material_id
            WHERE NOT EXISTS (SELECT 1 FROM material_in mi
                              WHERE mi.material_id=o.material_id
                                AND mi.date>o.date AND mi.date<=?)
            ORDER BY o.date DESC""", (date, date, date)))
        usage = rows(con.execute("""
            SELECT mu.product_id, mu.material_id, mu.qty, mu.block FROM material_usage mu
            WHERE mu.date=? ORDER BY mu.product_id, mu.block, mu.qty DESC""", (date,)))
        # 반제품 생산 (완제품 생산실적과 분리) — 그날 반제품별 배합수 (구: 제품형)
        semi_prod = rows(con.execute(
            "SELECT semi_id, batches, qty FROM semi_production WHERE date=? ORDER BY id", (date,)))
        # 반제품(자재) 생산 — 그날 반제품 자재별 배합수·생산량
        semi_mat_prod = rows(con.execute(
            "SELECT material_id, batches, qty FROM semi_mat_prod WHERE date=? ORDER BY material_id", (date,)))
        photos = rows(con.execute(
            "SELECT id, file, note, at FROM day_photo WHERE date=? ORDER BY id", (date,)))
        # 직전 '생산' 기록일 (어제처럼 복사용 — 자재 prev_date와 별개)
        ppd = con.execute("SELECT MAX(date) d FROM production WHERE date<?", (date,)).fetchone()
        prev_prod_date = ppd["d"] if ppd else None
        # 동시 편집 감지: 이 사용자가 이 날짜를 열었음을 표시 + 같은 날짜 열람 중인 다른 사용자
        me = request.state.user
        me["editing"] = {"date": date, "t": time.time()}
        viewers = sorted({s["username"] for s in SESSIONS.values()
                          if s.get("editing") and s["editing"]["date"] == date
                          and time.time() - s["editing"]["t"] < 180
                          and s["username"] != me.get("username")})
        return {"date": date, "exists": rec is not None,
                "memo": rec["memo"] if rec else "",
                "version": rec["updated_at"] if rec else None, "viewers": viewers,
                "production": production, "shipment": shipment,
                "materials": materials, "mat_in": mat_in, "pending_orders": pending_orders,
                "staffing": staffing, "lots": lots, "usage": usage,
                "semi_prod": semi_prod, "semi_mat_prod": semi_mat_prod, "photos": photos,
                "prev_stock": {r["material_id"]: r["real_qty"] for r in prev},
                "prev_date": prev_date, "prev_materials": prev_materials,
                "prev_prod_date": prev_prod_date}
    finally:
        con.close()


@app.post("/api/day/{date}")
def day_save(request: Request, date: str, body: dict):
    """부분 저장: body에 포함된 섹션만 갱신 — 생산 탭(production/shipment/usage/staffing/memo)과
    재고 탭(materials/mat_in)을 담당자가 따로 저장해도 서로의 데이터를 건드리지 않는다."""
    # 담당(duty) 강제: 지정된 담당 항목만 저장 가능 (복수 지정 가능 · admin은 전체)
    # 특이사항(memo)은 담당이 하나라도 있으면 허용 — 공용 메모라 담당을 따로 두지 않는다
    user = request.state.user
    mine = duty_set(user)
    if not mine:
        raise HTTPException(403, "담당이 지정되지 않은 계정은 일일 입력을 저장할 수 없습니다 — 관리자에게 담당 지정을 요청하세요")
    bad = sorted({DUTY_SECTION[s] for s in DUTY_SECTION if s in body and DUTY_SECTION[s] not in mine})
    if bad:
        raise HTTPException(403, "담당이 아닌 항목은 저장할 수 없습니다 — "
                            + ", ".join(DUTY_KO[k] for k in bad))
    con = connect()
    try:
        # 동시 편집 충돌: 내가 이 날짜를 연 이후 다른 사용자가 저장했으면 409 (force=덮어쓰기)
        if "base_version" in body and not body.get("force"):
            rec0 = con.execute("SELECT updated_at FROM day_record WHERE date=?", (date,)).fetchone()
            if rec0 and body.get("base_version") and rec0["updated_at"] != body["base_version"]:
                raise HTTPException(409, "다른 사용자가 이 날짜를 먼저 저장했습니다")
        con.execute("INSERT OR IGNORE INTO day_record(date, status) VALUES(?, 'saved')", (date,))
        if "memo" in body:
            con.execute("UPDATE day_record SET memo=?, status='saved' WHERE date=?",
                        (body.get("memo", ""), date))
        affected_pids = set()   # 저장 후 재고 음수 검증 대상 (생산·출고 변경 제품)
        if "production" in body:
            # 삭제 전 기존 생산 제품도 검증 대상 (행 삭제·수량 축소가 재고를 음수로 만들 수 있음)
            affected_pids |= {r["product_id"] for r in con.execute(
                "SELECT DISTINCT product_id FROM production WHERE date=?", (date,))}
            con.execute("DELETE FROM production WHERE date=?", (date,))
            for r in body.get("production", []):
                if not r.get("product_id"):
                    continue
                plan = float(r.get("plan_qty") or 0)
                prod = float(r.get("prod_qty") or 0)
                defect = float(r.get("defect_qty") or 0)
                batches = float(r.get("batches") or 0)
                if min(plan, prod, defect, batches) < 0:
                    nm = con.execute("SELECT name FROM product WHERE id=?", (r["product_id"],)).fetchone()
                    raise HTTPException(400, f"'{nm['name'] if nm else r['product_id']}' 생산실적에 "
                                        "음수 수량은 저장할 수 없습니다")
                if defect - prod > 0.5:
                    nm = con.execute("SELECT name FROM product WHERE id=?", (r["product_id"],)).fetchone()
                    raise HTTPException(400, f"'{nm['name'] if nm else r['product_id']}' 불량 {defect:,.0f}개가 "
                                        f"생산 {prod:,.0f}개보다 많습니다 (양품이 음수가 됩니다)")
                split_sum = sum(float(sp.get("qty") or 0) for sp in (r.get("lot_splits") or []))
                if split_sum - prod > 0.5:
                    nm = con.execute("SELECT name FROM product WHERE id=?", (r["product_id"],)).fetchone()
                    raise HTTPException(400, f"'{nm['name'] if nm else r['product_id']}' 소비기한 분할 합계 "
                                        f"{split_sum:,.0f}개가 생산수량 {prod:,.0f}개를 초과합니다")
                affected_pids.add(r["product_id"])
                con.execute("""INSERT OR REPLACE INTO production
                    (date, product_id, line_id, plan_qty, prod_qty, defect_qty, batches, defect_reason, unit_price)
                    VALUES(?,?,?,?,?,?,?,?,
                      COALESCE((SELECT unit_price FROM product WHERE id=?),0))""",
                            (date, r["product_id"], r.get("line_id"),
                             plan, prod, defect, batches,
                             (r.get("defect_reason") or "")[:200],
                             r["product_id"]))
                # 생산 수량의 거래처별 분배 — prod_split 교체 (합계 = prod_qty는 프론트가 유지)
                if "prod_splits" in r:
                    con.execute("DELETE FROM prod_split WHERE date=? AND product_id=?",
                                (date, r["product_id"]))
                    for sp in (r.get("prod_splits") or []):
                        q = float(sp.get("qty") or 0)
                        if q <= 0:
                            continue
                        con.execute("INSERT INTO prod_split(date, product_id, partner_id, qty)"
                                    " VALUES(?,?,?,?)",
                                    (date, r["product_id"], sp.get("partner_id") or None, q))
                # 이 생산 LOT의 소비기한 분할 (수량별 여러 소비기한) — lot_plan 교체.
                # 분할이 있으면 current_lots에서 우선 적용, 없으면 lot_expiry/제품 소비일 폴백 (그대로 둠)
                if "lot_splits" in r:
                    con.execute("DELETE FROM lot_plan WHERE product_id=? AND made=?",
                                (r["product_id"], date))
                    for i, sp in enumerate(r.get("lot_splits") or []):
                        q = float(sp.get("qty") or 0)
                        if q <= 0 or not sp.get("expiry"):
                            continue
                        con.execute("""INSERT INTO lot_plan(product_id, made, seq, qty, expiry, pack_mid, pack_set, partner_id)
                            VALUES(?,?,?,?,?,?,?,?)""", (r["product_id"], date, i, q, sp["expiry"],
                                                     int(sp["pack_mid"]) if sp.get("pack_mid") else None,
                                                     sp.get("pack_set") or "",
                                                     int(sp["partner_id"]) if sp.get("partner_id") else None))
                elif "expiry" in r:   # 구버전 클라이언트: 단일 소비기한
                    if r.get("expiry"):
                        con.execute("INSERT OR REPLACE INTO lot_expiry(product_id, made, expiry)"
                                    " VALUES(?,?,?)", (r["product_id"], date, r["expiry"]))
                    else:
                        con.execute("DELETE FROM lot_expiry WHERE product_id=? AND made=?",
                                    (r["product_id"], date))
            # 반제품 소비 자동 기록 — 완제품 배합수 × (완제품 1배합당 반제품 소요량).
            # 생산이 저장될 때마다 그날 것을 통째로 다시 계산하므로 수량 축소·행 삭제도 그대로 반영된다.
            con.execute("DELETE FROM semi_usage WHERE date=?", (date,))
            for r in body.get("production", []):
                pid = r.get("product_id")
                batches = float(r.get("batches") or 0)
                if not pid or batches <= 0:
                    continue
                for ing in con.execute(
                        "SELECT semi_id, SUM(qty_per_unit) q FROM semi_ingredient"
                        " WHERE product_id=? GROUP BY semi_id", (pid,)):
                    if ing["q"]:
                        con.execute("""INSERT OR REPLACE INTO semi_usage(date, semi_id, product_id, qty)
                            VALUES(?,?,?,?)""", (date, ing["semi_id"], pid, ing["q"] * batches))
        # ── 반제품 생산 (완제품 생산실적과 분리) — 생산량(qty)만큼 반제품 재고 증가(원재료처럼) ──
        # qty = 실제 만든 양(kg 등, 반제품 단위). 프론트에서 직접 입력하거나 배합수×1배합당생산량으로 자동.
        if "semi_prod" in body:
            con.execute("DELETE FROM semi_production WHERE date=?", (date,))
            for r in body.get("semi_prod", []):
                sid = r.get("semi_id")
                batches = float(r.get("batches") or 0)
                qty = float(r.get("qty") or 0)
                if not sid or (batches <= 0 and qty <= 0):
                    continue
                if qty <= 0:   # 생산량 미입력 → 배합수 × 1배합당 생산량으로 폴백
                    by = con.execute("SELECT batch_yield FROM product WHERE id=?", (sid,)).fetchone()
                    qty = batches * float((by["batch_yield"] if by else 0) or 0)
                con.execute("INSERT INTO semi_production(date, semi_id, batches, qty) VALUES(?,?,?,?)",
                            (date, sid, batches, qty))
        if "shipment" in body:
            # 재고 초과 검증: 제품별 그날 출고 합 ≤ 그날 제외 가용재고 (기초+생산−다른날출고−폐기)
            affected_pids |= {r["product_id"] for r in con.execute(
                "SELECT DISTINCT product_id FROM shipment WHERE date=?", (date,))}
            new_ship = {}
            for r in body.get("shipment", []):
                if r.get("product_id") and r.get("qty"):
                    if float(r["qty"]) < 0:
                        nm = con.execute("SELECT name FROM product WHERE id=?", (r["product_id"],)).fetchone()
                        raise HTTPException(400, f"'{nm['name'] if nm else r['product_id']}' 출고량에 "
                                            "음수는 저장할 수 없습니다")
                    affected_pids.add(r["product_id"])
                    new_ship[r["product_id"]] = new_ship.get(r["product_id"], 0.0) + float(r["qty"])
            for pid_, qsum in new_ship.items():
                avail = con.execute("""SELECT
                    COALESCE((SELECT SUM(qty) FROM opening_stock WHERE kind='product' AND ref_id=?),0)
                    + COALESCE((SELECT SUM(prod_qty) FROM production WHERE product_id=?),0)
                    - COALESCE((SELECT SUM(qty) FROM shipment WHERE product_id=? AND date!=?),0)
                    - COALESCE((SELECT SUM(qty) FROM disposal WHERE product_id=?),0) v""",
                    (pid_, pid_, pid_, date, pid_)).fetchone()["v"]
                if qsum - float(avail) > 0.5:
                    nm = con.execute("SELECT name FROM product WHERE id=?", (pid_,)).fetchone()
                    raise HTTPException(400, f"'{nm['name'] if nm else pid_}' 출고량 {qsum:,.0f}개가 "
                                        f"가용 재고 {float(avail):,.0f}개를 초과합니다")
            con.execute("DELETE FROM shipment WHERE date=?", (date,))
            for r in body.get("shipment", []):
                if not r.get("product_id") or not r.get("qty"):
                    continue
                # 판매 단가 스냅샷: 거래처별 단가 > 제품 기본 단가.
                # 저장 시점 값을 박아두어 나중에 단가를 바꿔도 과거 출고 금액이 변하지 않는다.
                price = con.execute("""SELECT COALESCE(
                    (SELECT price FROM product_price WHERE product_id=? AND partner_id=? AND price>0),
                    (SELECT unit_price FROM product WHERE id=?), 0)""",
                    (r["product_id"], r.get("partner_id"), r["product_id"])).fetchone()[0]
                con.execute("INSERT INTO shipment(date,product_id,partner_id,qty,prod_date,expiry,lot_no,unit_price)"
                            " VALUES(?,?,?,?,?,?,?,?)",
                            (date, r["product_id"], r.get("partner_id"), float(r["qty"]),
                             r.get("prod_date") or "", r.get("expiry") or "", int(r.get("lot_no") or 0),
                             float(price or 0)))
        # ── 자재 (입고/실사/사용 — 셋 중 하나라도 오면 재고 자동 반영 재계산) ──
        touch_mat = ("materials" in body) or ("mat_in" in body) or ("usage" in body) or ("semi_mat_prod" in body)
        mid_q = ("SELECT material_id FROM material_daily WHERE date=?"
                 " UNION SELECT material_id FROM material_in WHERE date=?"
                 " UNION SELECT material_id FROM material_usage WHERE date=?")
        affected_mids = ({r["material_id"] for r in con.execute(mid_q, (date, date, date))}
                         if touch_mat else set())   # 처리 전 스냅샷 (행이 삭제되는 자재도 체인 재계산)
        in_totals = {}
        if "mat_in" in body:
            con.execute("DELETE FROM material_in WHERE date=?", (date,))
            for r in body.get("mat_in", []):
                if not r.get("material_id") or not r.get("qty"):
                    continue
                q = float(r["qty"])
                if q < 0:
                    nm = con.execute("SELECT name FROM material WHERE id=?", (r["material_id"],)).fetchone()
                    raise HTTPException(400, f"'{nm['name'] if nm else r['material_id']}' 입고량에 "
                                        "음수는 저장할 수 없습니다")
                con.execute("""INSERT INTO material_in(date, material_id, qty, made_date, expiry, note, partner, price)
                    VALUES(?,?,?,?,?,?,?,?)""",
                            (date, r["material_id"], q, r.get("made") or "",
                             r.get("expiry") or "", r.get("note") or "",
                             (r.get("partner") or "").strip(), float(r.get("price") or 0)))
                in_totals[r["material_id"]] = in_totals.get(r["material_id"], 0.0) + q
        else:  # 이 저장에 입고가 없으면 기존 저장분 사용 (반제품 생산 입고는 아래 semi_in에서 더하므로 제외)
            for r in con.execute("SELECT material_id, SUM(qty) q FROM material_in"
                                 " WHERE date=? AND COALESCE(note,'')!='[반제품생산]' GROUP BY material_id", (date,)):
                in_totals[r["material_id"]] = float(r["q"] or 0)
        if "materials" in body:
            con.execute("DELETE FROM material_daily WHERE date=?", (date,))
            for r in body.get("materials", []):
                if not r.get("material_id"):
                    continue
                mid = r["material_id"]
                prev = float(r.get("prev_qty") or 0)
                inq = in_totals[mid] if mid in in_totals else float(r.get("in_qty") or 0)
                real = float(r.get("real_qty") or 0)
                con.execute("""INSERT OR REPLACE INTO material_daily
                    (date, material_id, prev_qty, in_qty, real_qty, used_qty, order_date, order_qty)
                    VALUES(?,?,?,?,?,?,?,?)""",
                            (date, mid, prev, inq, real, prev + inq - real,
                             r.get("order_date", ""), float(r.get("order_qty") or 0)))
        elif touch_mat:   # 실사는 안 왔지만 입고/사용이 바뀜 → 자동 행만 재계산 (실사 행 보존)
            con.execute("DELETE FROM material_daily WHERE date=? AND src='auto'", (date,))
        if "usage" in body:
            con.execute("DELETE FROM material_usage WHERE date=?", (date,))
            for r in body.get("usage", []):
                # 자재를 고른 행은 사용량 미입력도 0으로 저장(행 유지). 자재 미선택 행만 건너뛴다.
                # (실측 추정은 estimate에서 qty>0만 집계하므로 0 저장이 추정을 왜곡하지 않는다)
                if not r.get("material_id"):
                    continue
                q = float(r.get("qty") or 0)
                if q < 0:
                    nm = con.execute("SELECT name FROM material WHERE id=?", (r["material_id"],)).fetchone()
                    raise HTTPException(400, f"'{nm['name'] if nm else r['material_id']}' 자재 사용량에 "
                                        "음수는 저장할 수 없습니다")
                con.execute("""INSERT OR REPLACE INTO material_usage
                    (date, material_id, product_id, qty, block) VALUES(?,?,?,?,?)""",
                            (date, r["material_id"], r.get("product_id"), q,
                             r.get("block") or ""))
        # ── 반제품 생산 (반제품=직접 만드는 자재) — 배합수 × 1배합당 생산량 = 반제품 입고,
        #    원재료는 레시피(semi_bom)대로 사용. 반제품 입고는 note='[반제품생산]', 원재료 사용은 block='semi:<id>'로 표시. ──
        semi_in, semi_used = {}, {}
        if "semi_mat_prod" in body:
            con.execute("DELETE FROM material_in WHERE date=? AND note='[반제품생산]'", (date,))
            con.execute("DELETE FROM material_usage WHERE date=? AND product_id IS NULL AND block LIKE 'semi:%'", (date,))
            con.execute("DELETE FROM semi_mat_prod WHERE date=?", (date,))
            for r in body.get("semi_mat_prod", []):
                mid = r.get("material_id")
                batches = float(r.get("batches") or 0)
                if not mid or batches <= 0:
                    continue
                mrow = con.execute("SELECT batch_yield FROM material WHERE id=?", (mid,)).fetchone()
                prod = batches * float((mrow["batch_yield"] if mrow else 0) or 0)
                con.execute("INSERT OR REPLACE INTO semi_mat_prod(date, material_id, batches, qty)"
                            " VALUES(?,?,?,?)", (date, mid, batches, prod))
                if prod > 0:
                    con.execute("INSERT INTO material_in(date, material_id, qty, note) VALUES(?,?,?,?)",
                                (date, mid, prod, "[반제품생산]"))
                    semi_in[mid] = semi_in.get(mid, 0.0) + prod
                for b in con.execute("""SELECT sb.material_id, sb.qty_per_unit, sb.unit sunit, m.unit munit
                        FROM semi_bom sb JOIN material m ON m.id=sb.material_id WHERE sb.semi_id=?""", (mid,)):
                    ru = batches * float(b["qty_per_unit"] or 0)
                    su = (b["sunit"] or "g").lower(); mu = (b["munit"] or "").lower()   # 레시피 단위 → 자재 재고 단위 환산
                    if su == "g" and mu == "kg":
                        ru /= 1000
                    elif su == "kg" and mu == "g":
                        ru *= 1000
                    if ru > 0:
                        con.execute("INSERT INTO material_usage(date, material_id, product_id, qty, block)"
                                    " VALUES(?,?,NULL,?,?)", (date, b["material_id"], ru, "semi:" + str(mid)))
                        semi_used[b["material_id"]] = semi_used.get(b["material_id"], 0.0) + ru
            for _mid, _q in semi_in.items():   # 반제품 입고를 그날 입고 합계에 반영
                in_totals[_mid] = in_totals.get(_mid, 0.0) + _q
        if touch_mat:
            # 자동 반영: 실사(수동 자재행)가 없는 자재는 전일재고 + 입고 − 사용 합계로 계산
            if "usage" in body:
                sums = {}
                for r in body.get("usage", []):
                    if r.get("material_id") and r.get("qty"):   # 기타 사용(제품 없음)도 재고 차감에 포함
                        sums[r["material_id"]] = sums.get(r["material_id"], 0.0) + float(r["qty"])
                for _mid, _q in semi_used.items():   # 반제품 원재료 사용은 body.usage 밖 → 여기서만 합산
                    sums[_mid] = sums.get(_mid, 0.0) + _q
            else:   # 이 저장에 사용 기록이 없으면 기존 저장분 사용 (반제품 원재료 사용도 이미 테이블에 있음 → 중복 합산 안 함)
                sums = {r["material_id"]: float(r["q"] or 0) for r in con.execute(
                    "SELECT material_id, SUM(qty) q FROM material_usage WHERE date=? GROUP BY material_id",
                    (date,))}
            if "materials" in body:
                explicit = {r["material_id"] for r in body.get("materials", [])
                            if r.get("material_id")}
            else:   # 기존 실사 행이 우선
                explicit = {r["material_id"] for r in con.execute(
                    "SELECT material_id FROM material_daily WHERE date=? AND src!='auto'", (date,))}
            for mid in (set(sums) | set(in_totals)) - explicit:
                used = sums.get(mid, 0.0)
                inq = in_totals.get(mid, 0.0)
                prev_row = con.execute("""SELECT real_qty FROM material_daily
                    WHERE material_id=? AND date<? ORDER BY date DESC LIMIT 1""",
                                       (mid, date)).fetchone()
                prev = prev_row["real_qty"] if prev_row else 0.0
                con.execute("""INSERT OR REPLACE INTO material_daily
                    (date, material_id, prev_qty, in_qty, real_qty, used_qty, src)
                    VALUES(?,?,?,?,?,?,'auto')""",
                            (date, mid, prev, inq, prev + inq - used, used))
            # 이후 날짜 체인 재계산 — 과거 날짜를 고쳐도 미래 기록의 전일재고가 따라오도록
            affected_mids |= {r["material_id"] for r in con.execute(mid_q, (date, date, date))}
            for mid in affected_mids:
                ripple_material(con, mid, date)
        if "staffing" in body:
            old = [r["id"] for r in con.execute("SELECT id FROM staffing WHERE date=?", (date,))]
            for sid in old:
                con.execute("DELETE FROM staffing_member WHERE staffing_id=?", (sid,))
                con.execute("DELETE FROM staffing_agency WHERE staffing_id=?", (sid,))
            con.execute("DELETE FROM staffing WHERE date=?", (date,))
            for r in body.get("staffing", []):
                # 용역 개인별 [{h, w}] — 시급이 서로 다른 용역 지원. 집계 컬럼은 하위호환용으로 유지
                # (agency_wage = 가중평균 → 구버전 산식 agency_hours×agency_wage 도 같은 노무비)
                agency = r.get("agency")
                if isinstance(agency, list):
                    ags = []
                    for a in agency:
                        astart = (a.get("start") or "").strip()
                        aend = (a.get("end") or "").strip()
                        abrk = float(a.get("brk") or 0)
                        # 출근·퇴근이 모두 있으면 근무시간을 서버가 확정 계산 (정직원과 동일 규칙)
                        calc = calc_work_hours(astart, aend, abrk)
                        h = calc if calc is not None else float(a.get("h") or 0)
                        ags.append((h, float(a.get("w") or 0), (a.get("g") or "")[:2],
                                    a.get("pid") or None, astart, aend, abrk))
                    if any(h < 0 or w < 0 for h, w, *_ in ags):
                        raise HTTPException(400, "용역 시간·시급에 음수는 저장할 수 없습니다")
                    ag_cnt = len(ags)
                    ag_hours = sum(a[0] for a in ags)
                    labor = sum(a[0] * a[1] for a in ags)
                    ag_wage = (labor / ag_hours) if ag_hours > 0 else (ags[0][1] if ags else 0)
                else:   # 구버전 클라이언트: 집계값만
                    ags = None
                    ag_cnt = float(r.get("agency_count") or 0)
                    ag_hours = float(r.get("agency_hours") or 0)
                    ag_wage = float(r.get("agency_wage") or 0)
                cur = con.execute("""INSERT INTO staffing
                    (date, line_id, headcount, agency_count, agency_hours, agency_wage, target_hours, work_hours, stop_reason)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                                  (date, r.get("line_id"), float(r.get("headcount") or 0),
                                   ag_cnt, ag_hours, ag_wage,
                                   float(r.get("target_hours") or 0),
                                   float(r.get("work_hours") or 0), r.get("stop_reason", "")))
                if ags:
                    for i, (h, w, g, pid, astart, aend, abrk) in enumerate(ags):
                        con.execute("INSERT INTO staffing_agency"
                                    "(staffing_id, seq, hours, wage, gender, partner_id, start_time, end_time, break_min)"
                                    " VALUES(?,?,?,?,?,?,?,?,?)",
                                    (cur.lastrowid, i, h, w, g, pid, astart, aend, abrk))
                members = r.get("members")
                if members is None:   # 구버전 클라이언트 호환
                    members = [{"id": sid, "h": 0} for sid in r.get("member_ids", [])]
                for m in members:
                    if not m.get("id"):
                        continue
                    start = (m.get("start") or "").strip()
                    end = (m.get("end") or "").strip()
                    brk = float(m.get("brk") or 0)
                    # 출근·퇴근이 모두 있으면 근무시간을 서버가 확정 계산, 아니면 수동 입력값(h) 사용
                    calc = calc_work_hours(start, end, brk)
                    hours = calc if calc is not None else float(m.get("h") or 0)
                    con.execute("INSERT OR IGNORE INTO staffing_member"
                                "(staffing_id, staff_id, hours, start_time, end_time, break_min)"
                                " VALUES(?,?,?,?,?,?)",
                                (cur.lastrowid, m["id"], hours, start, end, brk))
        # ── 최종 재고 무결성 검증: 이번 저장으로 어느 제품이든 계산 재고가 음수가 되면 전체 롤백 ──
        # (예: 이미 출고된 과거 생산을 축소·삭제 → 재고 −N. 커밋이 이 아래 한 번뿐이라 400이면 안전하게 취소됨)
        for pid_ in affected_pids:
            stock = con.execute("""SELECT
                COALESCE((SELECT SUM(qty) FROM opening_stock WHERE kind='product' AND ref_id=?),0)
                + COALESCE((SELECT SUM(prod_qty) FROM production WHERE product_id=?),0)
                - COALESCE((SELECT SUM(qty) FROM shipment WHERE product_id=?),0)
                - COALESCE((SELECT SUM(qty) FROM disposal WHERE product_id=?),0) v""",
                (pid_, pid_, pid_, pid_)).fetchone()["v"]
            if float(stock) < -0.5:
                nm = con.execute("SELECT name FROM product WHERE id=?", (pid_,)).fetchone()
                raise HTTPException(400, f"'{nm['name'] if nm else pid_}' 재고가 {float(stock):,.0f}개(음수)가 됩니다 — "
                                    "이미 출고·폐기된 수량보다 적게 생산을 저장할 수 없습니다. "
                                    "출고 기록을 먼저 줄이거나 생산수량을 확인하세요")
        con.execute("UPDATE day_record SET updated_at=datetime('now','localtime') WHERE date=?", (date,))
        audit(con, "save_day", f"{date} [{','.join(k for k in ('production','shipment','materials','mat_in','usage','staffing','memo') if k in body)}]")
        bump_masters()
        con.commit()
        DAY_SAVED_BY[date] = user["username"]   # 같은 날짜를 보고 있는 사람에게 '누가 저장했는지' 알림
        return {"ok": True}
    finally:
        con.close()


# ── 원가·수익성 (admin — 배합비×자재단가 + 개당 노무비) ──
COUNT_UNITS_SET = {"개", "ea", "EA", "매", "장", "롤", "박스", "묶음", "봉", "set", "세트", "팩"}


def bom_qty_per_unit(m, b):
    """제품 1개당 이 자재 소요량 — 자재 단위 기준 (원가·소요량 계산 공통 환산).
    개수 단위 자재는 1÷개입수, 그 외는 배합 단위↔자재 단위(g/kg) 환산."""
    mu = (m["unit"] or "").strip()
    if mu in COUNT_UNITS_SET and (m["pack_count"] or 0) > 0:
        return 1.0 / float(m["pack_count"])
    qty = float(b["qty_per_unit"] or 0)
    bu = (b["unit"] or "g").lower()
    if bu != mu.lower():
        if bu == "g" and mu.lower() == "kg":
            qty /= 1000
        elif bu == "kg" and mu.lower() == "g":
            qty *= 1000
    return qty


@app.get("/api/costs")
def costs(request: Request):
    if not mcan(request, "cost"):
        raise HTTPException(403, "원가 열람 권한이 없습니다")
    con = connect()
    try:
        mats = {r["id"]: r for r in con.execute(
            "SELECT id, name, unit, unit_price, pack_count FROM material")}
        # 개당 노무비 = 최근 30일 노무비 합 ÷ 양품 생산 합 (전 제품 공통 배분 — 근사치)
        since = (dt.date.today() - dt.timedelta(days=30)).isoformat()
        lab = con.execute("""SELECT COALESCE(SUM(
              (SELECT COALESCE(SUM(s.wage * CASE WHEN sm.hours>0 THEN sm.hours ELSE st.work_hours END),0)
                 FROM staffing_member sm JOIN staff s ON s.id=sm.staff_id WHERE sm.staffing_id=st.id)
              + COALESCE((SELECT SUM(sa.hours * sa.wage) FROM staffing_agency sa
                          WHERE sa.staffing_id=st.id),
                         st.agency_hours * st.agency_wage)),0) v
            FROM staffing st WHERE st.date>=?""", (since,)).fetchone()["v"]
        good = con.execute(
            "SELECT COALESCE(SUM(prod_qty - defect_qty),0) v FROM production WHERE date>=?",
            (since,)).fetchone()["v"]
        labor_rate = (lab / good) if good > 0 else 0.0
        latest = latest_material_prices(con)   # 자재별 실입고 단가 (발주 입고 + 일일 입고) — 없으면 기준 단가
        boms = {}
        for b in con.execute("SELECT product_id, material_id, qty_per_unit, unit FROM bom"):
            boms.setdefault(b["product_id"], []).append(b)

        def mat_cost_of(rows_b):
            """제품 배합비(원부재료)만의 1개당 원가 (mat_cost, 단가미입력 수, 상세)."""
            mat_cost, missing, detail = 0.0, 0, []
            for b in rows_b or []:
                m = mats.get(b["material_id"])
                if not m:
                    continue
                mu = (m["unit"] or "").strip()
                qty = bom_qty_per_unit(m, b)
                actual = float(latest.get(b["material_id"]) or 0)
                price = actual if actual > 0 else float(m["unit_price"] or 0)
                psrc = "실입고" if actual > 0 else ("기준" if price > 0 else "")
                cost = qty * price
                if price <= 0:
                    missing += 1
                mat_cost += cost
                detail.append({"name": m["name"], "qty": round(qty, 5), "unit": mu,
                               "price": price, "cost": round(cost, 2), "src": psrc})
            return mat_cost, missing, detail

        # 반제품 1개당 원가 = 그 반제품의 원재료 배합비 원가 (롤업용)
        pnames = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM product")}
        semi_unit_cost = {}
        for sid in [r["id"] for r in con.execute("SELECT id FROM product WHERE COALESCE(is_semi,0)=1")]:
            semi_unit_cost[sid] = mat_cost_of(boms.get(sid))[0]
        # 완제품별 반제품 재료 구성
        semi_ings = {}
        for si in con.execute("SELECT product_id, semi_id, qty_per_unit FROM semi_ingredient"):
            semi_ings.setdefault(si["product_id"], []).append(si)

        out, no_bom = [], 0
        for p in con.execute("""SELECT id, name, image, unit_price, batch_yield FROM product
                WHERE status!='단종' AND COALESCE(is_semi,0)=0 ORDER BY sort, id"""):
            rows_b = boms.get(p["id"])
            ings = semi_ings.get(p["id"])
            if not rows_b and not ings:
                no_bom += 1
                continue
            mat_cost, missing, detail = mat_cost_of(rows_b)
            # 반제품 재료 원가 롤업 — 반제품은 '완제품 1배합당' 소요량이므로 1개당 = ÷ 완제품 1배합 생산수량
            by = float(p["batch_yield"] or 0)
            for si in (ings or []):
                q_batch = float(si["qty_per_unit"] or 0)          # 완제품 1배합당 반제품 소요량
                q = q_batch / by if by > 0 else 0                 # 완제품 1개당 (수율 없으면 0 — 원가 계산 불가)
                suc = float(semi_unit_cost.get(si["semi_id"], 0) or 0)
                cost = q * suc
                if suc <= 0 or by <= 0:
                    missing += 1
                mat_cost += cost
                detail.append({"name": "🧫 " + pnames.get(si["semi_id"], "반제품"),
                               "qty": round(q, 5), "unit": "", "price": round(suc, 2),
                               "cost": round(cost, 2), "src": "반제품"})
            detail.sort(key=lambda x: -x["cost"])
            out.append({"id": p["id"], "name": p["name"], "image": p["image"],
                        "sell": float(p["unit_price"] or 0),
                        "mat_cost": round(mat_cost, 2), "missing": missing, "detail": detail})
        return {"labor_rate": round(labor_rate, 2), "labor_total": round(lab),
                "good_total": good, "since": since, "rows": out, "no_bom": no_bom}
    finally:
        con.close()


# ── 분석 (martin_data 대시보드 이식용 원천데이터) ──


@app.get("/api/analytics")
def analytics():
    """전체 이력 원천: 제품 × 날짜별 생산/출고 + 기초재고. 집계는 클라이언트."""
    con = connect()
    try:
        products = rows(con.execute("""
            SELECT p.id, p.name, p.category, COALESCE(os.qty,0) opening
            FROM product p
            LEFT JOIN opening_stock os ON os.kind='product' AND os.ref_id=p.id
            ORDER BY p.sort, p.id"""))
        prod = rows(con.execute(
            "SELECT date, product_id pid, SUM(prod_qty) p FROM production GROUP BY date, product_id"))
        ship = rows(con.execute(
            "SELECT date, product_id pid, SUM(qty) s FROM shipment GROUP BY date, product_id"))
        disp = rows(con.execute(
            "SELECT date, product_id pid, SUM(qty) q FROM disposal GROUP BY date, product_id"))
        defect = rows(con.execute(
            "SELECT date, product_id pid, SUM(defect_qty) d FROM production"
            " WHERE defect_qty>0 GROUP BY date, product_id"))
        reasons = rows(con.execute(
            "SELECT date, COALESCE(NULLIF(defect_reason,''),'사유 미입력') reason, SUM(defect_qty) q"
            " FROM production WHERE defect_qty>0 GROUP BY date, reason"))
        return {"products": products, "prod": prod, "ship": ship, "disp": disp,
                "defect": defect, "reasons": reasons}
    finally:
        con.close()


@app.get("/api/ledger")
def ledger(request: Request, date: str = ""):
    """원료수불부 — 종이 양식(파일철) 그대로: 행=원재료, 열=제품별 사용량.
    자재별 전일재고·금일입고·당일사용·사용후재고 + 제품별 사용량 + 입고 소비기한. 하루 단위."""
    date = date or dt.date.today().isoformat()
    con = connect()
    try:
        products = rows(con.execute(
            "SELECT id, name FROM product WHERE status!='단종' ORDER BY sort, id"))
        mats = rows(con.execute(
            "SELECT id, name, unit, shelf_days FROM material WHERE kind='raw' AND status!='중단' ORDER BY sort, id"))
        md = {r["material_id"]: r for r in con.execute(
            "SELECT material_id, prev_qty, in_qty, used_qty, real_qty FROM material_daily WHERE date=?",
            (date,))}
        usage = {}   # material_id -> {product_id: qty}
        for r in con.execute("""SELECT material_id, product_id, SUM(qty) q FROM material_usage
                WHERE date=? AND product_id IS NOT NULL GROUP BY material_id, product_id""", (date,)):
            usage.setdefault(r["material_id"], {})[r["product_id"]] = r["q"]
        # ── FEFO(짧은 소비기한 먼저) 활성 배치 계산 ──
        # 보유량(그날까지 최신 실재고) — 그날 기록이 없으면 이전 최신값 이어서
        onhand = {}
        for r in con.execute("""SELECT md.material_id, md.real_qty FROM material_daily md
                JOIN (SELECT material_id, MAX(date) mx FROM material_daily WHERE date<=? GROUP BY material_id) x
                  ON x.material_id=md.material_id AND x.mx=md.date""", (date,)):
            onhand[r["material_id"]] = r["real_qty"]
        # 입고 배치들(그날까지) — 입고일·수량·소비기한·제조일
        batches = {}
        for r in con.execute("""SELECT material_id, date, qty, expiry, made_date FROM material_in
                WHERE date<=?""", (date,)):
            batches.setdefault(r["material_id"], []).append(
                {"in": r["date"], "qty": float(r["qty"] or 0),
                 "exp": r["expiry"] or "", "made": r["made_date"] or ""})
        # carry-forward 소비기한 — 그날까지(≤date) 가장 최근에 입력된 소비기한.
        # 입고분(material_in.expiry) + 입고 없는 재고 수동입력(material_expiry) 중 가장 최근 날짜.
        # FEFO 활성 배치를 못 잡을 때(초기·잉여 재고 등)의 폴백 — 입력한 기한이 표에서 사라지지 않게 한다.
        eff = {}   # material_id -> (date, expiry, made, is_in)
        for r in con.execute("SELECT material_id, date, expiry, made_date FROM material_in"
                             " WHERE date<=? AND COALESCE(expiry,'')!=''", (date,)):
            cur = eff.get(r["material_id"])
            if cur is None or r["date"] >= cur[0]:
                eff[r["material_id"]] = (r["date"], r["expiry"], r["made_date"] or "", True)
        for r in con.execute("SELECT material_id, date, expiry FROM material_expiry"
                             " WHERE date<=? AND COALESCE(expiry,'')!=''", (date,)):
            cur = eff.get(r["material_id"])
            if cur is None or r["date"] > cur[0]:   # 더 나중 날짜면 수동 입력이 우선
                eff[r["material_id"]] = (r["date"], r["expiry"], "", False)
        # shelf_days 자동추정 기준일 + 최근 입고(입고일·제조일)
        # — 소비기한이 없어도 입고일·제조일은 최근 입고에서 이어서 표시하기 위한 값
        base_date = {}
        last_in = {}   # material_id -> (in_date, made)
        for r in con.execute("""SELECT mi.material_id, mi.made_date, mi.date FROM material_in mi
                JOIN (SELECT material_id, MAX(date) mx FROM material_in WHERE date<=? GROUP BY material_id) x
                  ON x.material_id=mi.material_id AND x.mx=mi.date
                WHERE mi.date<=?""", (date, date)):
            base_date[r["material_id"]] = r["made_date"] or r["date"]
            last_in[r["material_id"]] = (r["date"], r["made_date"] or "")
        # 최근 입고 비고 (carry-forward, 수불부 비고 열 표시용)
        note_cf = {}
        for r in con.execute("SELECT material_id, note FROM material_in"
                             " WHERE date<=? AND COALESCE(note,'')!='' ORDER BY date", (date,)):
            note_cf[r["material_id"]] = r["note"]   # date 오름차순 → 최신 비고가 최종적으로 남음
        # 입고 없는 재고에 수동 입력한 제조일자 (carry-forward) — 입고 제조일이 없을 때 폴백
        man_made_cf = {}
        for r in con.execute("SELECT material_id, date, made FROM material_expiry"
                             " WHERE date<=? AND COALESCE(made,'')!=''", (date,)):
            cur = man_made_cf.get(r["material_id"])
            if cur is None or r["date"] >= cur[0]:
                man_made_cf[r["material_id"]] = (r["date"], r["made"])

        def fefo_active(mid):
            """지금 소진 중인 배치 = 보유량을 소비기한 늦은 배치부터 채우고, 남은 것 중
            소비기한이 가장 이른 배치. 보유량이 기록 배치 합보다 크면 초기재고(None→fallback)."""
            bs = batches.get(mid)
            rq = onhand.get(mid)
            if rq is None or rq <= 0 or not bs:
                return None
            # 소비기한 오름차순(빈 값은 입고일로 대체) = 소진 우선순위
            bs_sorted = sorted(bs, key=lambda b: (b["exp"] or b["in"], b["in"]))
            if rq > sum(b["qty"] for b in bs_sorted) + 1e-4:
                return None   # 기록 배치보다 많이 보유 → 초기재고가 소진 중
            remaining, active = rq, None
            for b in reversed(bs_sorted):       # 소비기한 늦은 배치부터 채움
                if remaining <= 1e-4:
                    break
                take = min(remaining, b["qty"])
                if take > 0:
                    active = b
                    remaining -= take
            return active

        def fefo_consumed_today(mid, prev, used):
            """당일 사용량(used)이 FEFO로 소진한 배치들의 소비기한 목록 (짧은 기한부터).
            전일재고(prev)를 소비기한 늦은 배치부터 채워 배치별 보유량을 복원한 뒤,
            당일 사용량을 소비기한 이른 배치부터 차감 — 걸친 배치가 여럿이면 기한도 여럿."""
            bs = batches.get(mid)
            if not bs or prev is None or not used or used <= 1e-4:
                return []
            bs_sorted = sorted(bs, key=lambda b: (b["exp"] or b["in"], b["in"]))
            total = sum(b["qty"] for b in bs_sorted)
            rem = [0.0] * len(bs_sorted)
            fill = min(float(prev), total)            # 초과분은 초기재고(기한 미상) → 제외
            for i in range(len(bs_sorted) - 1, -1, -1):
                if fill <= 1e-4:
                    break
                take = min(fill, bs_sorted[i]["qty"])
                rem[i] = take
                fill -= take
            need, exps = float(used), []
            for i, b in enumerate(bs_sorted):
                if need <= 1e-4:
                    break
                take = min(need, rem[i])
                if take > 1e-4 and b["exp"]:
                    exps.append(b["exp"])
                need -= take
            seen, out = set(), []                     # 중복 제거(순서 유지)
            for e in exps:
                if e not in seen:
                    seen.add(e)
                    out.append(e)
            return out

        def est_expiry(base, sd):
            try:
                return (dt.date.fromisoformat(base) + dt.timedelta(days=int(sd))).isoformat()
            except (ValueError, TypeError):
                return ""

        out_rows = []
        col_total = {p["id"]: 0.0 for p in products}
        in_total = 0.0
        for m in mats:
            d = md.get(m["id"])
            u = usage.get(m["id"], {})
            sd = m["shelf_days"] or 0
            act = fefo_active(m["id"])
            in_date = made = exp = ""
            exp_est = False
            if act:                                   # FEFO 활성 배치
                in_date, made, exp = act["in"], act["made"], act["exp"]
            # 활성 배치가 없거나(초기·잉여재고) 그 배치에 소비기한이 없으면
            # → 최근 입력된 소비기한(carry-forward)으로 폴백해 입력값이 사라지지 않게 한다.
            if not exp:
                e = eff.get(m["id"])
                if e:
                    exp = e[1]
                    if e[3]:                          # 입고분에서 온 기한이면 입고일·제조일도 함께
                        in_date = in_date or e[0]
                        made = made or e[2]
                elif sd > 0 and base_date.get(m["id"]):   # 입력 기한이 전혀 없으면 shelf_days 추정
                    exp = est_expiry(base_date[m["id"]], sd)
                    exp_est = bool(exp)
            # 입고일자·제조일자가 아직 비어 있으면 최근 입고에서 이어서 (소비기한이 없어도 제조일 표시)
            li = last_in.get(m["id"])
            if li:
                in_date = in_date or li[0]
                made = made or li[1]
            if not made:                              # 입고 제조일이 없으면 수동 제조일에서 이어서
                mm = man_made_cf.get(m["id"])
                if mm:
                    made = mm[1]
            # 당일 사용량이 여러 소비기한 배치에 걸치면 그 기한을 모두 표시 (예: "2026-07-01, 2026-07-02")
            exps_today = fefo_consumed_today(m["id"], d["prev_qty"] if d else None,
                                             d["used_qty"] if d else None)
            if len(exps_today) > 1:
                exp = ", ".join(exps_today)
                exp_est = False
            row = {"id": m["id"], "name": m["name"], "unit": m["unit"] or "",
                   "prev": (d["prev_qty"] if d else None), "in": (d["in_qty"] if d else None),
                   "used": (d["used_qty"] if d else None), "real": (d["real_qty"] if d else None),
                   "usage": u, "expiry": exp, "expiry_est": exp_est,
                   "made": made, "in_date": in_date, "note": note_cf.get(m["id"], "")}
            out_rows.append(row)
            if row["in"]:
                in_total += row["in"]
            for pid, q in u.items():
                if pid in col_total:
                    col_total[pid] += q
        prev = con.execute("SELECT MAX(date) v FROM material_daily WHERE date<?", (date,)).fetchone()["v"]
        nxt = con.execute("SELECT MIN(date) v FROM material_daily WHERE date>?", (date,)).fetchone()["v"]
        return {"date": date, "today": dt.date.today().isoformat(),
                "products": products, "rows": out_rows,
                "col_total": col_total, "in_total": in_total, "prev": prev, "next": nxt}
    finally:
        con.close()


@app.get("/api/finledger")
def fin_ledger(request: Request, date: str = ""):
    """완제품 수불부 — 제품별 전일재고·금일생산·금일출고·금일재고 + 생산일자별 LOT 소비기한.
    엑셀 '완제품 수불부' 양식(좌: 일일 수불 / 우: LOT별 재고·소비기한)을 그대로 옮긴 것."""
    date = date or dt.date.today().isoformat()
    con = connect()
    try:
        products = rows(con.execute("""
            SELECT p.id, p.name, p.category, p.spec, COALESCE(p.fin_split,0) fin_split,
                   COALESCE(p.shelf_days,0) shelf_days, COALESCE(os.qty,0) opening
            FROM product p
            LEFT JOIN opening_stock os ON os.kind='product' AND os.ref_id=p.id
            WHERE COALESCE(p.is_semi,0)=0 AND p.status!='단종'
            ORDER BY p.sort, p.id"""))

        def sums(table, qcol, cmp):
            q = f"SELECT product_id pid, SUM({qcol}) q FROM {table} WHERE date{cmp}? GROUP BY product_id"
            return {r["pid"]: float(r["q"] or 0) for r in con.execute(q, (date,))}
        prod_b, prod_o = sums("production", "prod_qty", "<"), sums("production", "prod_qty", "=")
        ship_b, ship_o = sums("shipment", "qty", "<"), sums("shipment", "qty", "=")
        disp_b, disp_o = sums("disposal", "qty", "<"), sums("disposal", "qty", "=")

        # 거래처 이름 — 분리 표시는 제품 플래그(fin_split)로만 제어, 거래처는 배분처 이름만 사용
        partners = {r["id"]: {"name": r["name"]}
                    for r in con.execute("SELECT id, name FROM partner")}
        # 개입수(pack_count) — LOT 포장(pack_mid=포장자재 / pack_set=세트)에서 유도
        pack_by_mid = {r["id"]: float(r["pack_count"] or 0)
                       for r in con.execute("SELECT id, pack_count FROM material WHERE COALESCE(pack_count,0)>0")}
        pack_by_set = {r["set_name"]: float(r["pc"] or 0) for r in con.execute(
            """SELECT s.set_name, MAX(m.pack_count) pc FROM pack_set_member s
               JOIN material m ON m.id=s.material_id GROUP BY s.set_name""")}

        def lot_pack(l):
            mid, ps = l.get("pack_mid"), l.get("pack_set") or ""
            if mid and pack_by_mid.get(mid):
                return pack_by_mid[mid]
            if ps and pack_by_set.get(ps):
                return pack_by_set[ps]
            return 0

        # 오늘 거래처별 생산 분배·출고 (분리표시 행의 금일생산·금일출고 원천)
        prodsplit_today = {}
        for r in con.execute("""SELECT product_id pid, partner_id sp, SUM(qty) q FROM prod_split
            WHERE date=? AND partner_id IS NOT NULL GROUP BY product_id, partner_id""", (date,)):
            prodsplit_today[(r["pid"], r["sp"])] = float(r["q"] or 0)
        ship_by_pp = {}
        for r in con.execute("""SELECT product_id pid, partner_id sp, SUM(qty) q FROM shipment
            WHERE date=? AND partner_id IS NOT NULL GROUP BY product_id, partner_id""", (date,)):
            ship_by_pp[(r["pid"], r["sp"])] = float(r["q"] or 0)

        def lot_out(l):
            return {"made": l["made"], "qty": l["qty"], "expiry": l["expiry"], "pack": lot_pack(l)}

        # 생산 LOT별 개입수 — 출고분에도 재고 LOT과 동일한 개입수를 붙이기 위해 lot_plan 포장에서 유도
        pack_by_lot = {}
        for r in con.execute("""SELECT product_id pid, made, COALESCE(expiry,'') exp, pack_mid, pack_set
            FROM lot_plan WHERE qty>0"""):
            pk = lot_pack({"pack_mid": r["pack_mid"], "pack_set": r["pack_set"]})
            if pk:
                pack_by_lot[(r["pid"], r["made"], r["exp"])] = pk
                pack_by_lot.setdefault((r["pid"], r["made"]), pk)   # 소비기한 안 맞을 때 생산일만으로 보정

        # 오늘 출고를 제품·거래처·생산일자·소비기한별로 모아 '출고 소비기한(생산일자)' 표시에 사용
        raw_ship = {}
        for r in con.execute("""SELECT product_id pid, partner_id sp,
                COALESCE(prod_date,'') made, COALESCE(expiry,'') exp, qty
            FROM shipment WHERE date=? AND qty>0""", (date,)):
            raw_ship.setdefault(r["pid"], []).append(
                (r["sp"], r["made"], r["exp"], float(r["qty"])))

        def ship_lots_for(pid, shelf, want=None, exclude=None):
            """그날 출고분을 (생산일자, 소비기한)별로 합산 — 소비기한 미지정분은 생산일+제품 소비일로 추정."""
            agg = {}
            for sp, made, exp, q in raw_ship.get(pid, []):
                if want is not None and sp != want:
                    continue
                if exclude is not None and sp in exclude:
                    continue
                e = exp
                if not e and made and shelf:
                    try:
                        e = (dt.date.fromisoformat(made) + dt.timedelta(days=int(shelf))).isoformat()
                    except ValueError:
                        e = ""
                key = (made, e)
                agg[key] = agg.get(key, 0.0) + q
            return [{"made": k[0], "expiry": k[1], "qty": round(v, 3),
                     "pack": pack_by_lot.get((pid, k[0], k[1])) or pack_by_lot.get((pid, k[0])) or 0}
                    for k, v in sorted(agg.items())]

        tot_prev = tot_prod = tot_ship = tot_stock = 0.0   # 전체 합계
        out = []
        for p in products:
            pid = p["id"]
            prev = p["opening"] + prod_b.get(pid, 0) - ship_b.get(pid, 0) - disp_b.get(pid, 0)
            tp, ts, td = prod_o.get(pid, 0), ship_o.get(pid, 0), disp_o.get(pid, 0)
            stock = prev + tp - ts - td
            try:
                raw_lots = [l for l in current_lots(con, pid, date)["lots"] if l.get("qty", 0) > 0.0001]
            except Exception:
                raw_lots = []
            shelf = p["shelf_days"]
            tot_prev += prev; tot_prod += tp; tot_ship += ts; tot_stock += stock   # 전체 합계 누적
            base = {"id": pid, "name": p["name"], "category": p["category"] or "",
                    "spec": p["spec"] or "", "prev": round(prev, 3), "prod": round(tp, 3),
                    "ship": round(ts, 3), "disp": round(td, 3), "stock": round(stock, 3),
                    "lots": [lot_out(l) for l in raw_lots],
                    "ship_lots": ship_lots_for(pid, shelf), "moved": bool(tp or ts or td)}

            # 이 제품의 '거래처 분리 표시'가 켜져 있으면(fin_split=1) 배분된 모든 거래처를 '거래처명 제품명' 행으로 분리
            grp_lots = {}
            for l in raw_lots:
                grp_lots.setdefault(l.get("partner_id"), []).append(l)
            if not int(p["fin_split"] or 0):
                out.append(base)
                continue
            show_ids = {sp for sp in grp_lots if sp and sp in partners}
            show_ids |= {sp for (q_pid, sp) in prodsplit_today if q_pid == pid and sp in partners}
            show_ids |= {sp for (q_pid, sp) in ship_by_pp if q_pid == pid and sp in partners}

            if not show_ids:
                out.append(base)
                continue

            sum_prev = sum_prod = sum_ship = sum_stock = 0.0
            split_rows = []
            for sp in sorted(show_ids, key=lambda x: partners[x]["name"]):
                glots = grp_lots.get(sp, [])
                stock_p = sum(l["qty"] for l in glots)
                prod_p = prodsplit_today.get((pid, sp), 0)
                ship_p = ship_by_pp.get((pid, sp), 0)
                prev_p = round(stock_p - prod_p + ship_p, 3)   # 전일재고 = 금일재고 − 금일생산 + 금일출고
                sum_prev += prev_p; sum_prod += prod_p; sum_ship += ship_p; sum_stock += stock_p
                split_rows.append({"id": pid, "name": partners[sp]["name"] + " " + p["name"],
                    "category": p["category"] or "", "spec": p["spec"] or "",
                    "prev": prev_p, "prod": round(prod_p, 3), "ship": round(ship_p, 3), "disp": 0,
                    "stock": round(stock_p, 3), "lots": [lot_out(l) for l in glots],
                    "ship_lots": ship_lots_for(pid, shelf, want=sp),
                    "moved": bool(prod_p or ship_p or stock_p or prev_p), "partner": True})
            # 잔여(거래처 미지정분) — 합계가 제품 전체와 맞도록 차감해서 산출
            res_prev = round(prev - sum_prev, 3); res_prod = round(tp - sum_prod, 3)
            res_ship = round(ts - sum_ship, 3); res_stock = round(stock - sum_stock, 3)
            res_lots = [lot_out(l) for l in raw_lots
                        if not (l.get("partner_id") in show_ids)]
            res_ship_lots = ship_lots_for(pid, shelf, exclude=show_ids)
            out.extend(split_rows)
            if abs(res_prev) > 1e-6 or abs(res_prod) > 1e-6 or abs(res_ship) > 1e-6 \
               or abs(res_stock) > 1e-6 or res_lots or res_ship_lots:
                out.append({"id": pid, "name": p["name"], "category": p["category"] or "",
                    "spec": p["spec"] or "", "prev": res_prev, "prod": res_prod,
                    "ship": res_ship, "disp": round(td, 3), "stock": res_stock,
                    "lots": res_lots, "ship_lots": res_ship_lots,
                    "moved": bool(res_prod or res_ship or res_stock)})
        prev_d = con.execute(
            "SELECT MAX(date) v FROM (SELECT date FROM production WHERE date<? "
            "UNION SELECT date FROM shipment WHERE date<?)", (date, date)).fetchone()["v"]
        next_d = con.execute(
            "SELECT MIN(date) v FROM (SELECT date FROM production WHERE date>? "
            "UNION SELECT date FROM shipment WHERE date>?)", (date, date)).fetchone()["v"]
        return {"date": date, "today": dt.date.today().isoformat(),
                "rows": out, "prev": prev_d, "next": next_d,
                "totals": {"prev": round(tot_prev, 3), "prod": round(tot_prod, 3),
                           "ship": round(tot_ship, 3), "stock": round(tot_stock, 3)}}
    finally:
        con.close()


# ── 특이사항(일일 메모) 목록 ─────────────────────────────
@app.get("/api/memos")
def memos_list(request: Request, page: int = 1, per: int = 100):
    """일일 입력에 적은 특이사항(메모)을 날짜별로 모아 최신순으로 — 페이지네이션."""
    con = connect()
    try:
        per = max(1, min(500, int(per or 100)))
        page = max(1, int(page or 1))
        total = con.execute(
            "SELECT COUNT(*) c FROM day_record WHERE TRIM(COALESCE(memo,''))!=''").fetchone()["c"]
        items = rows(con.execute(
            "SELECT date, memo FROM day_record WHERE TRIM(COALESCE(memo,''))!='' "
            "ORDER BY date DESC LIMIT ? OFFSET ?", (per, (page - 1) * per)))
        return {"items": items, "total": total, "page": page, "per": per}
    finally:
        con.close()


# ── 배합비 (BOM) ─────────────────────────────


@app.get("/api/bom")
def bom_all():
    """전체 배합비 (일일 입력의 '배합비 자동 채우기' 캐시용)."""
    con = connect()
    try:
        return rows(con.execute("""SELECT product_id, material_id, qty_per_unit, unit,
            block, batch_qty, block_yield, partner_id, partner_ids FROM bom"""))
    finally:
        con.close()


@app.get("/api/bom/{product_id}")
def bom_get(product_id: int):
    con = connect()
    try:
        data = rows(con.execute("""
            SELECT b.*, m.name, m.kind, m.unit AS mat_unit FROM bom b
            JOIN material m ON m.id=b.material_id
            WHERE b.product_id=? ORDER BY b.id""", (product_id,)))
        return data
    finally:
        con.close()


COUNT_UNITS = {"개", "ea", "EA", "매", "장", "롤", "박스", "묶음", "봉", "set", "세트", "팩"}


# ── 반제품 재료 (완제품 배합비에 들어가는 반제품) ─────────────
@app.get("/api/semiing/{product_id}")
def semiing_get(product_id: int):
    """이 완제품의 배합비에 포함된 반제품 재료 목록 (반제품명·규격·단위 함께)."""
    con = connect()
    try:
        return {"items": rows(con.execute("""
            SELECT si.semi_id, si.qty_per_unit, si.unit,
                   p.name, p.spec, p.status
            FROM semi_ingredient si JOIN product p ON p.id=si.semi_id
            WHERE si.product_id=? ORDER BY si.id""", (product_id,)))}
    finally:
        con.close()


@app.post("/api/semiing/{product_id}")
def semiing_save(product_id: int, request: Request, body: dict):
    """완제품의 반제품 재료 구성을 통째로 교체."""
    if request.state.user["role"] == "guest":
        raise HTTPException(403, "보기 전용 계정은 사용할 수 없습니다")
    con = connect()
    try:
        con.execute("DELETE FROM semi_ingredient WHERE product_id=?", (product_id,))
        n = 0
        for it in body.get("items", []):
            sid = it.get("semi_id")
            if not sid or int(sid) == int(product_id):   # 자기 자신은 재료가 될 수 없음
                continue
            con.execute("""INSERT INTO semi_ingredient(product_id, semi_id, qty_per_unit, unit)
                VALUES(?,?,?,?)""",
                        (product_id, int(sid), float(it.get("qty_per_unit") or 0),
                         it.get("unit") or ""))
            n += 1
        audit(con, "save_semiing", f"제품#{product_id} 반제품 재료 {n}종")
        bump_masters()
        con.commit()
        return {"ok": True, "count": n}
    finally:
        con.close()


@app.post("/api/bom/{product_id}")
def bom_save(product_id: int, body: dict):
    con = connect()
    try:
        con.execute("DELETE FROM bom WHERE product_id=?", (product_id,))
        for r in body.get("rows", []):
            mid = r.get("material_id")
            if not mid:
                continue
            # 수량 미입력 행도 0으로 저장 — 행·납품처 지정이 사라지지 않게 (개수 자재는 어차피 개입수로 계산)
            # 납품처 복수: 리스트/문자열 모두 허용 → "1,3" 정규화 (첫 항목은 구 partner_id에도 저장 — 하위호환)
            raw_pids = r.get("partner_ids")
            if isinstance(raw_pids, list):
                pid_list = [int(x) for x in raw_pids if x]
            else:
                pid_list = [int(x) for x in str(raw_pids or "").split(",") if str(x).strip().isdigit()]
            if not pid_list and r.get("partner_id"):
                pid_list = [int(r["partner_id"])]
            con.execute("""INSERT INTO bom(product_id, material_id, qty_per_unit, unit,
                block, batch_qty, block_yield, partner_id, partner_ids, note)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (product_id, mid, float(r.get("qty_per_unit") or 0),
                         r.get("unit", "g"), r.get("block") or "",
                         float(r.get("batch_qty") or 0), float(r.get("block_yield") or 0),
                         (pid_list[0] if pid_list else None),
                         ",".join(map(str, pid_list)),
                         r.get("note", "")))
        # 반죽 블록 수율 = 제품 1배합당 생산수량 (전체무게 ÷ 분할무게 공식으로 계산된 값)
        if body.get("batch_yield"):
            con.execute("UPDATE product SET batch_yield=? WHERE id=?",
                        (float(body["batch_yield"]), product_id))
        audit(con, "save_bom", str(product_id))
        bump_masters()
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.post("/api/bom_replace")   # ※ /api/bom/{product_id}와 경로가 겹치지 않게 별도 경로 사용
def bom_replace_material(request: Request, body: dict):
    """자재 일괄 교체 — 모든 제품 배합비에서 from 자재를 to 자재로 바꾼다 (수량·구분·납품처 유지).
    권한: 배합비 저장과 동일 — 게스트만 불가 (일반 사용자 허용)."""
    if request.state.user["role"] == "guest":
        raise HTTPException(403, "보기 전용 계정은 사용할 수 없습니다")
    frm, to = body.get("from"), body.get("to")
    if not frm or not to:
        raise HTTPException(400, "교체할 자재를 선택해주세요")
    if int(frm) == int(to):
        raise HTTPException(400, "같은 자재로는 교체할 수 없습니다")
    con = connect()
    try:
        names = {r["id"]: r["name"] for r in con.execute(
            "SELECT id, name FROM material WHERE id IN (?,?)", (frm, to))}
        if len(names) < 2:
            raise HTTPException(404, "자재를 찾을 수 없습니다")
        prods = [r["name"] for r in con.execute("""
            SELECT DISTINCT p.name FROM bom b JOIN product p ON p.id=b.product_id
            WHERE b.material_id=? ORDER BY p.sort, p.id""", (frm,))]
        if not prods:
            raise HTTPException(400, "이 자재가 들어간 배합비가 없습니다")
        con.execute("UPDATE bom SET material_id=? WHERE material_id=?", (to, frm))
        audit(con, "replace_bom_mat", f"배합비 자재 교체: {names[int(frm)]} → {names[int(to)]} ({len(prods)}개 제품)")
        bump_masters()
        con.commit()
        return {"ok": True, "products": len(prods), "names": prods}
    finally:
        con.close()


@app.delete("/api/bom/{product_id}")
def bom_delete(request: Request, product_id: int):
    """이 제품의 배합비 전체 삭제 — 자재는 그대로, 배합 행만 지운다.
    제품의 1배합당 생산수량(batch_yield)도 함께 초기화(배합비 근거가 사라지므로)."""
    require_admin(request)
    con = connect()
    try:
        n = con.execute("DELETE FROM bom WHERE product_id=?", (product_id,)).rowcount
        con.execute("UPDATE product SET batch_yield=0 WHERE id=?", (product_id,))
        nm = con.execute("SELECT name FROM product WHERE id=?", (product_id,)).fetchone()
        audit(con, "delete_bom", f"{nm['name'] if nm else product_id}: 배합 {n}행 삭제")
        bump_masters()
        con.commit()
        return {"ok": True, "removed": n}
    finally:
        con.close()


@app.get("/api/bom/{product_id}/estimate")
def bom_estimate(product_id: int):
    """원료수불부 실측(material_usage) × 생산실적으로 1개당 소요량 추정."""
    con = connect()
    try:
        data = rows(con.execute("""
            SELECT mu.material_id, m.name, m.kind, m.unit,
                   SUM(mu.qty) tot_use, SUM(pr.prod_qty) tot_prod,
                   COUNT(DISTINCT mu.date) days
            FROM material_usage mu
            JOIN production pr ON pr.date=mu.date AND pr.product_id=mu.product_id
            JOIN material m ON m.id=mu.material_id
            WHERE mu.product_id=? AND pr.prod_qty>0 AND mu.qty>0
            GROUP BY mu.material_id
            HAVING SUM(mu.qty)>0
            ORDER BY SUM(mu.qty) DESC""", (product_id,)))
        out = []
        for r in data:
            per = r["tot_use"] / r["tot_prod"]
            if r["unit"] == "kg":
                out.append({"material_id": r["material_id"], "name": r["name"],
                            "kind": r["kind"], "qty_per_unit": round(per * 1000, 2),
                            "unit": "g", "days": r["days"]})
            else:
                out.append({"material_id": r["material_id"], "name": r["name"],
                            "kind": r["kind"], "qty_per_unit": round(per, 4),
                            "unit": r["unit"], "days": r["days"]})
        return out
    finally:
        con.close()


@app.post("/api/planneeds")
def plan_needs(request: Request, body: dict):
    """생산계획(제품별 계획수량) → 배합비 전개로 자재 소요량 + 현재고 + 부족분.
    현재고 = 자재별 최신 실사(material_daily.real_qty, lowstock와 동일 기준).
    부족분은 그대로 [일괄 발주]로 넘길 수 있다."""
    require_stock_duty(request)
    plans = [(p.get("product_id"), float(p.get("qty") or 0))
             for p in (body.get("plans") or [])
             if p.get("product_id") and float(p.get("qty") or 0) > 0]
    if not plans:
        return {"plans": [], "needs": []}
    con = connect()
    try:
        mats = {r["id"]: r for r in con.execute(
            "SELECT id, name, unit, pack_count, unit_price, safety_stock, partner_id FROM material")}
        boms = {}
        for b in con.execute("SELECT product_id, material_id, qty_per_unit, unit FROM bom"):
            boms.setdefault(b["product_id"], []).append(b)
        stock = {r["material_id"]: r["real_qty"] for r in con.execute("""
            SELECT md.material_id, md.real_qty FROM material_daily md
            JOIN (SELECT material_id mid, MAX(date) d FROM material_daily GROUP BY material_id) x
              ON x.mid=md.material_id AND x.d=md.date""")}
        # 반제품 재료 구성 + 반제품 현재고 (반제품 소요 → 부족분은 원재료로 전개)
        semi_ings = {}
        for si in con.execute("SELECT product_id, semi_id, qty_per_unit FROM semi_ingredient"):
            semi_ings.setdefault(si["product_id"], []).append(si)
        semi_stock = {r["id"]: (r["stock"] or 0) for r in con.execute("""
            SELECT p.id, COALESCE(os.qty,0)+COALESCE(pr.q,0)-COALESCE(su.q,0) AS stock
            FROM product p
            LEFT JOIN opening_stock os ON os.kind='product' AND os.ref_id=p.id
            LEFT JOIN (SELECT semi_id, SUM(qty) q FROM semi_production GROUP BY semi_id) pr ON pr.semi_id=p.id
            LEFT JOIN (SELECT semi_id, SUM(qty) q FROM semi_usage GROUP BY semi_id) su ON su.semi_id=p.id
            WHERE COALESCE(p.is_semi,0)=1""")}
        pnames = {r["id"]: r["name"] for r in con.execute("SELECT id, name, spec FROM product")}
        pspec = {r["id"]: (r["spec"] or "") for r in con.execute("SELECT id, spec FROM product")}
        byld = {r["id"]: float(r["batch_yield"] or 0) for r in con.execute("SELECT id, batch_yield FROM product")}

        need = {}
        semi_need = {}   # semi_id -> 필요량 (완제품 계획에서)
        plan_out, no_bom = [], []
        for pid, qty in plans:
            pr = con.execute("SELECT name FROM product WHERE id=?", (pid,)).fetchone()
            nm = pr["name"] if pr else str(pid)
            rows_b = boms.get(pid)
            ings = semi_ings.get(pid)
            plan_out.append({"product_id": pid, "name": nm, "qty": qty, "bom": bool(rows_b or ings)})
            if not rows_b and not ings:
                no_bom.append(nm)
                continue
            for b in (rows_b or []):
                m = mats.get(b["material_id"])
                if not m:
                    continue
                need[b["material_id"]] = need.get(b["material_id"], 0) + bom_qty_per_unit(m, b) * qty
            # 반제품 소요 = 완제품 배합수 × 1배합당 소요량 (배합수 = 계획수량 ÷ 완제품 1배합 생산수량)
            by = byld.get(pid, 0)
            batches = (qty / by) if by > 0 else 0
            for si in (ings or []):
                semi_need[si["semi_id"]] = semi_need.get(si["semi_id"], 0) + float(si["qty_per_unit"] or 0) * batches
        # 부족한 반제품은 새로 생산해야 하므로, (필요−현재고)만큼 원재료로 전개
        semi_out = []
        for sid, req in semi_need.items():
            have = float(semi_stock.get(sid, 0) or 0)
            to_produce = req - have
            semi_out.append({"semi_id": sid, "name": pnames.get(sid, str(sid)),
                             "unit": pspec.get(sid, ""), "need": round(req, 3),
                             "stock": round(have, 3),
                             "shortfall": round(to_produce, 3) if to_produce > 0 else 0,
                             "short": to_produce > 0})
            if to_produce > 0:
                for b in boms.get(sid, []):
                    m = mats.get(b["material_id"])
                    if not m:
                        continue
                    need[b["material_id"]] = need.get(b["material_id"], 0) + bom_qty_per_unit(m, b) * to_produce
        semi_out.sort(key=lambda x: (not x["short"], x["name"]))
        latest = latest_material_prices(con)
        pa_names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM partner")}
        out = []
        for mid, req in need.items():
            m = mats[mid]
            have = float(stock.get(mid) or 0)
            short = req - have
            price = latest.get(mid) or m["unit_price"] or 0
            out.append({"material_id": mid, "name": m["name"], "unit": m["unit"] or "",
                        "need": round(req, 3), "stock": round(have, 3),
                        "shortfall": round(short, 3) if short > 0 else 0, "short": short > 0,
                        "partner": pa_names.get(m["partner_id"], "") if m["partner_id"] else "",
                        "price": price, "amount": round(short * price) if short > 0 and price else 0})
        out.sort(key=lambda x: (not x["short"], x["name"]))
        return {"plans": plan_out, "needs": out, "no_bom": no_bom,
                "semi_needs": semi_out,
                "short_cnt": sum(1 for x in out if x["short"]),
                "semi_short_cnt": sum(1 for x in semi_out if x["short"]),
                "short_amount": sum(x["amount"] for x in out)}
    finally:
        con.close()


# ── 사용처 분석 / 기록 검색 ───────────────────


@app.get("/api/usage")
def usage(material_id: int, date: str):
    con = connect()
    try:
        mat = con.execute("SELECT * FROM material WHERE id=?", (material_id,)).fetchone()
        if not mat:
            raise HTTPException(404, "material not found")
        data = rows(con.execute("""
            SELECT COALESCE(p.name, '기타 사용 (생산 외)') name, SUM(mu.qty) qty,
                   (SELECT prod_qty FROM production pr
                     WHERE pr.date=mu.date AND pr.product_id=mu.product_id) prod_qty
            FROM material_usage mu LEFT JOIN product p ON p.id=mu.product_id
            WHERE mu.material_id=? AND mu.date=?
            GROUP BY mu.product_id ORDER BY qty DESC""", (material_id, date)))
        md = con.execute("SELECT used_qty FROM material_daily WHERE material_id=? AND date=?",
                         (material_id, date)).fetchone()
        # 매트릭스에 해당일 데이터 없으면 최근 사용일 표시
        near = None
        if not data:
            near = con.execute("""SELECT date FROM material_usage
                WHERE material_id=? AND date<=? ORDER BY date DESC LIMIT 1""",
                               (material_id, date)).fetchone()
            if near:
                data = rows(con.execute("""
                    SELECT COALESCE(p.name, '기타 사용 (생산 외)') name, SUM(mu.qty) qty, NULL prod_qty
                    FROM material_usage mu LEFT JOIN product p ON p.id=mu.product_id
                    WHERE mu.material_id=? AND mu.date=?
                    GROUP BY mu.product_id ORDER BY qty DESC""",
                                        (material_id, near["date"])))
        types = rows(con.execute("""
            SELECT type, qty FROM material_usage_type
            WHERE material_id=? AND date=? ORDER BY qty DESC""",
                                 (material_id, near["date"] if near else date)))
        return {"material": mat["name"], "unit": mat["unit"], "date": date,
                "shown_date": near["date"] if near else date,
                "actual_used": md["used_qty"] if md else None, "rows": data,
                "types": types}
    finally:
        con.close()


@app.get("/api/searchall")
def search_all(frm: str = "", to: str = ""):
    """전체 기록: 기간 내 모든 제품의 날짜×제품별 생산·출고 (기간 미지정=최근 200건)."""
    con = connect()
    try:
        rng, params = "", []
        if frm:
            rng += " AND x.date>=?"; params.append(frm)
        if to:
            rng += " AND x.date<=?"; params.append(to)
        limit = 200 if not (frm or to) else 2000
        data = rows(con.execute(f"""
            SELECT x.date, p.name, SUM(x.prod) prod, SUM(x.ship) ship FROM (
              SELECT date, product_id, prod_qty prod, 0 ship FROM production
              UNION ALL
              SELECT date, product_id, 0, qty FROM shipment) x
            JOIN product p ON p.id=x.product_id
            WHERE 1=1 {rng}
            GROUP BY x.date, x.product_id
            HAVING SUM(x.prod)>0 OR SUM(x.ship)>0
            ORDER BY x.date DESC, p.name LIMIT {limit}""", params))
        return {"rows": data}
    finally:
        con.close()


@app.get("/api/search")
def search(q: str, frm: str = "", to: str = ""):
    """품목 검색: 기간(frm~to) 지정 시 그 범위 전체, 미지정 시 최근 30건."""
    con = connect()
    try:
        prods = rows(con.execute(
            "SELECT id, name FROM product WHERE name LIKE ? ORDER BY sort LIMIT 8",
            (f"%{q}%",)))
        hist = []
        if prods:
            pid = prods[0]["id"]
            rng, params = "", [pid, pid]
            if frm:
                rng += " AND d.date>=?"
                params.append(frm)
            if to:
                rng += " AND d.date<=?"
                params.append(to)
            limit = "LIMIT 30" if not (frm or to) else "LIMIT 1000"
            hist = rows(con.execute(f"""
                SELECT d.date, COALESCE(pr.prod_qty,0) prod, COALESCE(s.q,0) ship
                FROM day_record d
                LEFT JOIN production pr ON pr.date=d.date AND pr.product_id=?
                LEFT JOIN (SELECT date, SUM(qty) q FROM shipment WHERE product_id=?
                           GROUP BY date) s ON s.date=d.date
                WHERE (COALESCE(pr.prod_qty,0)>0 OR COALESCE(s.q,0)>0){rng}
                ORDER BY d.date DESC {limit}""", params))
        return {"products": prods, "history": hist}
    finally:
        con.close()


# ── 정적 파일 ────────────────────────────────

app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.mount("/image", StaticFiles(directory=IMAGE_DIR), name="image")   # 제품 이미지 (exe 옆 Image/)
app.mount("/dayphoto", StaticFiles(directory=PHOTO_DIR), name="dayphoto")   # 일일 생산 사진
app.mount("/chatfile", StaticFiles(directory=CHAT_DIR), name="chatfile")    # 채팅 첨부


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


def _wait_port_free(port, timeout=25):
    """업데이트 재시작 등으로 직전 인스턴스가 아직 포트를 듣고 있으면 기다린다.
    127.0.0.1:port로 연결이 되면(=누가 듣고 있음) 잠깐 대기, 연결이 거부되면 비어 있는 것."""
    import socket as _sock
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        s.settimeout(0.4)
        try:
            s.connect(("127.0.0.1", port))
            s.close()             # 연결됨 = 이전 서버가 아직 살아있음 → 대기
            _t.sleep(0.5)
        except OSError:
            s.close()
            return True           # 연결 거부 = 포트 비어있음
    return False


if __name__ == "__main__":
    import socket
    import threading
    import webbrowser
    import uvicorn
    # 업데이트 후 자동 재시작 시 직전 exe가 포트를 놓을 때까지 기다린다 (개발 프리뷰는 고유 포트라 생략)
    if not os.environ.get("PORT"):
        _wait_port_free(int(os.environ.get("PORT", "8600")), 25)
    init_db()
    init_chat_db()
    purge_old_chat(CHAT_DIR)      # 보관 주기 지난 대화 정리
    ensure_admin()
    _backfill_matin_po()          # v1.24 이전 발주 입고분에 거래처·단가 소급 (빈 행만)
    port = int(os.environ.get("PORT", "8600"))
    SERVE_PORT["v"] = port
    url = f"http://127.0.0.1:{port}"
    # 같은 네트워크(공유기)의 다른 PC에서 접속할 수 있는 LAN 주소 탐지
    lan_ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # 실제 전송 없음 — 로컬 IP 확인용
        lan_ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    print("=" * 52)
    print(f"  REBYPRODUCT 재고관리  -  {url}")
    if lan_ip:
        print(f"  다른 PC에서 접속:  http://{lan_ip}:{port}")
        print("  (최초 1회 '서버_방화벽허용.bat'을 관리자로 실행하세요)")
    print("  이 창을 닫으면 프로그램이 종료됩니다.")
    print("=" * 52)
    if not os.environ.get("PORT"):   # 개발(프리뷰) 실행 시엔 브라우저 자동오픈 생략
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # 자동 백업: 매일 1회 (기동 직후 오늘자 없으면 즉시) — 백업/자동백업_날짜.db, 30개 보관
    threading.Thread(target=_backup_scheduler, daemon=True).start()
    # 소비기한 아침 알림: 매일 7시 이후 1회, 임박·만료 LOT이 있으면 채팅에 게시
    threading.Thread(target=_alert_scheduler, daemon=True).start()
    # 외부 접속 터널: cloudflared.exe가 옆에 있고 켜져 있으면 자동 시작 (개발 실행 PORT 지정 시엔 생략)
    if not os.environ.get("PORT") and tunnel_enabled() and cloudflared_path():
        print("  외부 접속(cloudflared) 시작 중… 주소는 [관리 도구]·채팅에서 확인하세요")
        threading.Timer(2.0, start_tunnel).start()
    # 0.0.0.0 = 같은 네트워크의 다른 PC도 접속 가능 (로그인으로 접근 통제)
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=port, log_level="warning")
