# -*- coding: utf-8 -*-
"""martin_stock — 리바이프로덕트 재고 관리 웹앱 (FastAPI + SQLite).

실행:  python app/main.py   →  http://127.0.0.1:8600
"""
import os
import re
import sys
import json
import time
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
APP_VERSION = "1.2.0"     # 새 버전 배포 시 이 값을 올리고 version.json의 version과 맞춘다
# 새 버전 정보(version.json)를 읽어올 주소.
#   1순위: exe 옆 update_url.txt 파일 (재빌드 없이 호스트 변경 가능)
#   2순위: 아래 기본값 (배포 전 GitHub Releases 등의 raw 주소로 교체)
UPDATE_MANIFEST_URL = ""  # 예: https://github.com/<user>/<repo>/releases/latest/download/version.json


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
                            "shelf_days", "safety_stock", "batch_yield", "status", "note"]),
    "material": ("material", ["kind", "name", "spec", "unit", "pack_count", "pack_set", "unit_price", "partner_id",
                              "safety_stock", "prod_mult", "prod_per", "status", "note"]),
    "partner": ("partner", ["name", "type", "phone", "contact", "note", "status"]),
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


def hashpw(pw: str) -> str:
    return hashlib.sha256(("rebyproduct:" + pw).encode()).hexdigest()


def ensure_admin():
    con = connect()
    try:
        n = con.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        if n == 0:
            con.execute("INSERT OR IGNORE INTO users(username, pw_hash, role) VALUES(?,?,?)",
                        ("admin", hashpw("1"), "admin"))
            con.commit()
    finally:
        con.close()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path != "/api/login":
        user = SESSIONS.get(request.cookies.get("sid"))
        if not user:
            return JSONResponse({"detail": "로그인이 필요합니다"}, status_code=401)
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
    return response


@app.post("/api/login")
def login(body: dict, response: Response):
    con = connect()
    try:
        u = con.execute("SELECT * FROM users WHERE username=?",
                        ((body.get("username") or "").strip(),)).fetchone()
        if not u or u["pw_hash"] != hashpw(body.get("password") or ""):
            raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다")
        token = secrets.token_hex(16)
        duty = (u["duty"] if "duty" in u.keys() else "all") or "all"
        mp = (u["money_perms"] if "money_perms" in u.keys() else "") or ""
        SESSIONS[token] = {"id": u["id"], "username": u["username"], "role": u["role"],
                           "duty": duty, "money_perms": mp, "seen": time.time()}
        response.set_cookie("sid", token, httponly=True, samesite="lax")
        return {"username": u["username"], "role": u["role"], "duty": duty,
                "money_perms": sorted(money_set(SESSIONS[token]))}
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
            "money_perms": sorted(money_set(u))}


@app.post("/api/password")
def change_password(request: Request, body: dict):
    u = request.state.user
    con = connect()
    try:
        row = con.execute("SELECT pw_hash FROM users WHERE id=?", (u["id"],)).fetchone()
        if row["pw_hash"] != hashpw(body.get("old") or ""):
            raise HTTPException(400, "기존 비밀번호가 올바르지 않습니다")
        if not (body.get("new") or "").strip():
            raise HTTPException(400, "새 비밀번호를 입력하세요")
        con.execute("UPDATE users SET pw_hash=? WHERE id=?", (hashpw(body["new"]), u["id"]))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


def require_admin(request: Request):
    if request.state.user["role"] != "admin":
        raise HTTPException(403, "관리자(admin)만 가능합니다")


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
    if role not in ("admin", "op", "guest"):
        raise HTTPException(400, "권한은 admin/op/guest 중 하나여야 합니다")
    if role == "admin":
        duty = "all"   # 관리자는 항상 전체
    con = connect()
    try:
        try:
            con.execute("INSERT INTO users(username, pw_hash, role, duty) VALUES(?,?,?,?)",
                        (name, hashpw(pw), role, duty))
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
        # 선입선출 = 소비기한 임박(이른) 순, 기한 미상은 뒤 (생산일 보조 정렬)
        # protect_made: 그 생산일 LOT은 차감하지 않음 (재고 부족 보정이 당일 생산분을 지우지 않게)
        for l in sorted(lots, key=lambda x: (x["expiry"] == "", x["expiry"], x["made"])):
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
def do_backup(tag="자동백업"):
    """sqlite3 온라인 백업 API — 사용 중(WAL)에도 안전하게 스냅샷."""
    BACKUP_DIR.mkdir(exist_ok=True)
    name = f"{tag}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    src = connect()
    try:
        dest = sqlite3.connect(str(BACKUP_DIR / name))
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    # 자동백업만 최근 30개 보관 (수동백업·복원전 스냅샷·기존 백업 파일은 안 지움)
    autos = sorted(BACKUP_DIR.glob("자동백업_*.db"))
    for p in autos[:-30]:
        try:
            p.unlink()
        except OSError:
            pass
    return name


def _backup_scheduler():
    """1시간마다 확인 — 오늘자 자동백업이 없으면 생성 (기동 직후 1회 포함)."""
    while True:
        try:
            today = dt.date.today().strftime("%Y%m%d")
            if not list(BACKUP_DIR.glob(f"자동백업_{today}_*.db")):
                do_backup()
        except Exception:
            pass
        time.sleep(3600)


@app.get("/api/backups")
def backups_list(request: Request):
    require_admin(request)
    BACKUP_DIR.mkdir(exist_ok=True)
    out = []
    for p in sorted(BACKUP_DIR.glob("*.db"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size,
                    "at": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    return out


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
    path = BACKUP_DIR / name
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
               "raw": {"unit_price", "safety_stock", "pack_count"},
               "sub": {"unit_price", "safety_stock", "pack_count"},
               "staff": {"wage"}}
    if mtype not in allowed:
        raise HTTPException(400, "이 탭은 일괄 가져오기를 지원하지 않습니다")
    table = "material" if mtype in ("raw", "sub") else mtype
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


def fetch_manifest():
    """version.json 읽기 — {version, url, notes, sha256?}. 실패 시 예외."""
    import urllib.request
    url = manifest_url()
    if not url:
        raise RuntimeError("업데이트 주소가 설정되지 않았습니다 (update_url.txt)")
    if not url.lower().startswith("https://"):
        raise RuntimeError("업데이트 주소는 https 여야 합니다")
    req = urllib.request.Request(url, headers={"User-Agent": "martin_stock-updater"})
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
        req = urllib.request.Request(m["url"], headers={"User-Agent": "martin_stock-updater"})
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
    # 교체 배치: 이 exe가 종료되길 기다렸다 새 파일로 바꾸고 재실행 후 자기 삭제
    bat = exe.with_name("_자동업데이트.bat")
    bat.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "title 재고관리 업데이트\r\n"
        "echo 업데이트 적용 중 - 잠시만 기다려 주세요...\r\n"
        ':wait\r\n'
        f'move /y "{newexe.name}" "{exe.name}" >nul 2>&1\r\n'
        "if errorlevel 1 (\r\n"
        "  timeout /t 1 /nobreak >nul\r\n"
        "  goto wait\r\n"
        ")\r\n"
        f'start "" "{exe.name}"\r\n'
        'del "%~f0"\r\n', encoding="utf-8")

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
        # 안전재고 미설정 + 재고 0/음수 — 기준이 없어 발주 판단 불가 (설정 유도)
        unset = con.execute("""
            SELECT COUNT(*) c FROM material m
            JOIN (SELECT material_id, real_qty,
                         ROW_NUMBER() OVER (PARTITION BY material_id ORDER BY date DESC) rn
                  FROM material_daily) md ON md.material_id=m.id AND md.rn=1
            WHERE m.status!='중단' AND COALESCE(m.safety_stock,0)<=0 AND md.real_qty<=0""").fetchone()["c"]
        return {"items": items, "unset": unset}
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
CHAT_MSG_COLS = "id, username, text, kind, mentions, file, fname, fkind, at"
_PURGED = {"day": ""}      # 하루 1회만 보관주기 정리
DAY_SAVED_BY = {}          # 날짜 → 마지막 저장자 (동시 편집 알림 문구용 · 재시작 시 비어도 무방)


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
            f"SELECT {CHAT_MSG_COLS} FROM chat WHERE day=? AND id>? ORDER BY id LIMIT 200",
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
    finally:
        con.close()
    return {"online": online, "count": len(online), "me": me, "messages": msgs,
            "last_id": last, "day": today, "reads": reads, "mention_unread": mention,
            "users": chat_usernames(), "mver": MASTERS_VER["v"],
            "viewers": viewers, "day_ver": day_ver, "day_by": DAY_SAVED_BY.get(edit)}


@app.get("/api/chat/day")
def chat_day(d: str = ""):
    """지난 대화 보기 — 그날 메시지 + 대화가 있는 이전/다음 날짜 (◀ ▶ 이동용)."""
    day = d or dt.date.today().isoformat()
    con = chat_connect()
    try:
        msgs = rows(con.execute(
            f"SELECT {CHAT_MSG_COLS} FROM chat WHERE day=? ORDER BY id LIMIT 500", (day,)))
        prev = con.execute("SELECT MAX(day) v FROM chat WHERE day<?", (day,)).fetchone()["v"]
        nxt = con.execute("SELECT MIN(day) v FROM chat WHERE day>?", (day,)).fetchone()["v"]
        reads = {r["username"]: r["last_id"] for r in con.execute(
            "SELECT username, last_id FROM chat_read")}
        return {"day": day, "messages": msgs, "prev": prev, "next": nxt, "reads": reads,
                "today": dt.date.today().isoformat(), "retention": CHAT_RETENTION_DAYS}
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
    con = chat_connect()
    try:
        cur = con.execute(
            """INSERT INTO chat(day, username, text, mentions, file, fname, fkind)
               VALUES(?,?,?,?,?,?,?)""",
            (dt.date.today().isoformat(), request.state.user["username"], text,
             parse_mentions(text), stored, fname, fkind))
        con.commit()
        return {"id": cur.lastrowid}
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
        return {"name": mat["name"], "unit": mat["unit"], "kind": mat["kind"], "rows": hist,
                "in_expiry": in_expiry,
                "last_in": dict(last_in) if last_in else None,
                "last_use": dict(last_use) if last_use else None}
    finally:
        con.close()


# ── 기준정보 ─────────────────────────────────


@app.get("/api/masters/{mtype}")
def masters(mtype: str, request: Request):
    con = connect()
    try:
        if mtype == "product":
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
                ORDER BY p.sort, p.id"""))
        elif mtype in ("raw", "sub"):
            data = rows(con.execute("""
                SELECT m.*, md.real_qty AS stock, md.date AS stock_date, u.avg_use
                FROM material m
                LEFT JOIN (
                  SELECT material_id, real_qty, date,
                         ROW_NUMBER() OVER (PARTITION BY material_id ORDER BY date DESC) rn
                  FROM material_daily) md ON md.material_id=m.id AND md.rn=1
                LEFT JOIN (
                  -- 일평균 사용량 = 최근 30일 사용 합 ÷ 사용 기록일 수 (생산 없던 날 제외)
                  SELECT material_id, SUM(used_qty) * 1.0 /
                         (SELECT COUNT(DISTINCT date) FROM material_daily
                           WHERE used_qty>0 AND date>=date('now','localtime','-30 day')) avg_use
                  FROM material_daily
                  WHERE used_qty>0 AND date>=date('now','localtime','-30 day')
                  GROUP BY material_id) u ON u.material_id=m.id
                WHERE m.kind=?
                ORDER BY m.sort, m.id""", (mtype,)))
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
    key = "material" if mtype in ("raw", "sub") else mtype
    if key not in MASTER_TABLES:
        raise HTTPException(404, "unknown master type")
    table, cols = MASTER_TABLES[key]
    if mtype in ("raw", "sub"):
        body["kind"] = mtype
    vals = {c: body.get(c) for c in cols if c in body}
    if not vals.get("name"):
        raise HTTPException(400, "name required")
    con = connect()
    try:
        if mtype == "line":
            _check_line_parent(con, vals.get("parent_id"))
        ks = ",".join(vals)
        qs = ",".join("?" * len(vals))
        cur = con.execute(f"INSERT INTO {table}({ks}) VALUES({qs})", list(vals.values()))
        new_id = cur.lastrowid
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
    key = "material" if mtype in ("raw", "sub") else mtype
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
    key = "material" if mtype in ("raw", "sub") else mtype
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
        # 자재 부족 (안전재고 설정된 것 우선, 미설정 시 재고 0/음수)
        low = rows(con.execute("""
            SELECT m.id, m.kind, m.name, m.unit, m.safety_stock, md.real_qty AS stock,
                   md.order_date, md.date
            FROM material m
            JOIN (
              SELECT material_id, real_qty, order_date, date,
                     ROW_NUMBER() OVER (PARTITION BY material_id ORDER BY date DESC) rn
              FROM material_daily) md ON md.material_id=m.id AND md.rn=1
            WHERE m.status!='중단'
              AND ((m.safety_stock>0 AND md.real_qty<m.safety_stock)
                   OR (m.safety_stock<=0 AND md.real_qty<=0))
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
                "money": ({"prod": prod_month, "ship": ship_month, "label": base[:7]} if admin else None),
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


# ── 일일 기록 (조회/저장) ─────────────────────


@app.get("/api/calendar")
def calendar_dates(ym: str):
    con = connect()
    try:
        ds = [r["date"] for r in con.execute(
            "SELECT date FROM day_record WHERE substr(date,1,7)=? ORDER BY date", (ym,))]
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
                   (SELECT json_group_array(json_object('id', sm.staff_id, 'h', sm.hours))
                     FROM staffing_member sm WHERE sm.staffing_id=st.id) members,
                   (SELECT json_group_array(json_object('h', sa.hours, 'w', sa.wage,
                                                        'g', sa.gender, 'pid', sa.partner_id))
                     FROM (SELECT hours, wage, gender, partner_id FROM staffing_agency
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
                "staffing": staffing, "lots": lots, "usage": usage, "photos": photos,
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
        touch_mat = ("materials" in body) or ("mat_in" in body) or ("usage" in body)
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
                con.execute("""INSERT INTO material_in(date, material_id, qty, expiry, note)
                    VALUES(?,?,?,?,?)""",
                            (date, r["material_id"], q, r.get("expiry") or "", r.get("note") or ""))
                in_totals[r["material_id"]] = in_totals.get(r["material_id"], 0.0) + q
        else:  # 이 저장에 입고가 없으면 기존 저장분 사용
            for r in con.execute("SELECT material_id, SUM(qty) q FROM material_in WHERE date=? GROUP BY material_id", (date,)):
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
                if not r.get("material_id") or not r.get("qty"):
                    continue   # 제품은 없어도 됨 (기타 사용 — 생산 외 용도)
                if float(r["qty"]) < 0:
                    nm = con.execute("SELECT name FROM material WHERE id=?", (r["material_id"],)).fetchone()
                    raise HTTPException(400, f"'{nm['name'] if nm else r['material_id']}' 자재 사용량에 "
                                        "음수는 저장할 수 없습니다")
                con.execute("""INSERT OR REPLACE INTO material_usage
                    (date, material_id, product_id, qty, block) VALUES(?,?,?,?,?)""",
                            (date, r["material_id"], r.get("product_id"), float(r["qty"]),
                             r.get("block") or ""))
        if touch_mat:
            # 자동 반영: 실사(수동 자재행)가 없는 자재는 전일재고 + 입고 − 사용 합계로 계산
            if "usage" in body:
                sums = {}
                for r in body.get("usage", []):
                    if r.get("material_id") and r.get("qty"):   # 기타 사용(제품 없음)도 재고 차감에 포함
                        sums[r["material_id"]] = sums.get(r["material_id"], 0.0) + float(r["qty"])
            else:   # 이 저장에 사용 기록이 없으면 기존 저장분 사용
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
                    ags = [(float(a.get("h") or 0), float(a.get("w") or 0),
                            (a.get("g") or "")[:2], a.get("pid") or None) for a in agency]
                    if any(h < 0 or w < 0 for h, w, _, _ in ags):
                        raise HTTPException(400, "용역 시간·시급에 음수는 저장할 수 없습니다")
                    ag_cnt = len(ags)
                    ag_hours = sum(h for h, _, _, _ in ags)
                    labor = sum(h * w for h, w, _, _ in ags)
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
                    for i, (h, w, g, pid) in enumerate(ags):
                        con.execute("INSERT INTO staffing_agency(staffing_id, seq, hours, wage, gender, partner_id)"
                                    " VALUES(?,?,?,?,?,?)", (cur.lastrowid, i, h, w, g, pid))
                members = r.get("members")
                if members is None:   # 구버전 클라이언트 호환
                    members = [{"id": sid, "h": 0} for sid in r.get("member_ids", [])]
                for m in members:
                    if not m.get("id"):
                        continue
                    con.execute("INSERT OR IGNORE INTO staffing_member(staffing_id, staff_id, hours)"
                                " VALUES(?,?,?)",
                                (cur.lastrowid, m["id"], float(m.get("h") or 0)))
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
        boms = {}
        for b in con.execute("SELECT product_id, material_id, qty_per_unit, unit FROM bom"):
            boms.setdefault(b["product_id"], []).append(b)
        out, no_bom = [], 0
        for p in con.execute("""SELECT id, name, image, unit_price FROM product
                WHERE status!='단종' ORDER BY sort, id"""):
            rows_b = boms.get(p["id"])
            if not rows_b:
                no_bom += 1
                continue
            mat_cost, missing, detail = 0.0, 0, []
            for b in rows_b:
                m = mats.get(b["material_id"])
                if not m:
                    continue
                mu = (m["unit"] or "").strip()
                if mu in COUNT_UNITS_SET and (m["pack_count"] or 0) > 0:
                    qty = 1.0 / float(m["pack_count"])   # 개수 자재: 1개당 = 1 ÷ 개입수
                else:
                    qty = float(b["qty_per_unit"] or 0)
                    bu = (b["unit"] or "g").lower()
                    if bu != mu.lower():                 # 배합 단위 ↔ 자재 단위 환산
                        if bu == "g" and mu.lower() == "kg":
                            qty /= 1000
                        elif bu == "kg" and mu.lower() == "g":
                            qty *= 1000
                price = float(m["unit_price"] or 0)
                cost = qty * price
                if price <= 0:
                    missing += 1
                mat_cost += cost
                detail.append({"name": m["name"], "qty": round(qty, 5), "unit": mu,
                               "price": price, "cost": round(cost, 2)})
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
            WHERE mu.product_id=? AND pr.prod_qty>0
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


if __name__ == "__main__":
    import socket
    import threading
    import webbrowser
    import uvicorn
    init_db()
    init_chat_db()
    purge_old_chat(CHAT_DIR)      # 보관 주기 지난 대화 정리
    ensure_admin()
    port = int(os.environ.get("PORT", "8600"))
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
    # 0.0.0.0 = 같은 네트워크의 다른 PC도 접속 가능 (로그인으로 접근 통제)
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=port, log_level="warning")
