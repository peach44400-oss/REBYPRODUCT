# -*- coding: utf-8 -*-
"""구양식(2025) 유래 '원재료' 포장류 정리 (2026-07-09 사용자 확정: 이관 후 삭제).

- 대상: kind='raw' AND note='구양식(2025) 명칭' AND 이름에 트레이/슬리브/BOX/포장
- 기록(사용/일일재고 등)이 있으면 같은 이름의 부재료판(정본)으로 이관 후 삭제
- 부재료판이 없고 기록도 없으면 그냥 삭제 / 부재료판이 없는데 기록이 있으면 보류(보고)
"""
import sys, io, re, shutil, sqlite3, datetime as dt
from pathlib import Path

if hasattr(sys.stdout, "buffer") and (sys.stdout.encoding or "").lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "martin_stock.db"


def mkey(s):
    return re.sub(r"[\s()（）_·•]", "", str(s or ""))


REF_TABLES = ["material_daily", "material_in", "material_usage", "material_usage_type", "bom"]


def main():
    bak = BASE / "백업" / f"백업_{dt.date.today():%Y%m%d}_원재료포장류삭제전.db"
    shutil.copy2(DB, bak)
    print("백업:", bak.name)

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    targets = con.execute("""SELECT id, name FROM material
        WHERE kind='raw' AND note='구양식(2025) 명칭'
          AND (name LIKE '%트레이%' OR name LIKE '%슬리브%' OR name LIKE '%BOX%' OR name LIKE '%포장%')
        ORDER BY id""").fetchall()
    subs = {mkey(r["name"]): r["id"] for r in con.execute("SELECT id, name FROM material WHERE kind='sub'")}

    def ref_count(mid):
        n = 0
        for t in REF_TABLES:
            n += con.execute(f"SELECT COUNT(*) FROM {t} WHERE material_id=?", (mid,)).fetchone()[0]
        return n

    moved, deleted, held = [], [], []
    for r in targets:
        rid, name = r["id"], r["name"]
        refs = ref_count(rid)
        twin = subs.get(mkey(name))
        if refs and not twin:
            held.append((name, refs))
            continue
        if refs and twin:
            # 이관 — UNIQUE 충돌 시 수량 합산 후 원본 행 삭제
            # material_usage: UNIQUE(date, material_id, product_id, block)
            for u in con.execute("SELECT * FROM material_usage WHERE material_id=?", (rid,)).fetchall():
                dup = con.execute("""SELECT id FROM material_usage WHERE date=? AND material_id=?
                    AND product_id=? AND block=?""", (u["date"], twin, u["product_id"], u["block"])).fetchone()
                if dup:
                    con.execute("UPDATE material_usage SET qty=qty+? WHERE id=?", (u["qty"], dup["id"]))
                    con.execute("DELETE FROM material_usage WHERE id=?", (u["id"],))
                else:
                    con.execute("UPDATE material_usage SET material_id=? WHERE id=?", (twin, u["id"]))
            # material_daily: UNIQUE(date, material_id) — 이관 후 전일/실재고 재계산
            for d in con.execute("SELECT * FROM material_daily WHERE material_id=? ORDER BY date", (rid,)).fetchall():
                dup = con.execute("SELECT id FROM material_daily WHERE date=? AND material_id=?",
                                  (d["date"], twin)).fetchone()
                if dup:   # 정본에 같은 날 기록 있으면 원본 행 버림 (정본 실사 우선)
                    con.execute("DELETE FROM material_daily WHERE id=?", (d["id"],))
                else:
                    con.execute("UPDATE material_daily SET material_id=? WHERE id=?", (twin, d["id"]))
            # 이관된 auto 행 재계산: 전일 = 정본 직전 실재고
            for d in con.execute("SELECT * FROM material_daily WHERE material_id=? AND src='auto' ORDER BY date",
                                 (twin,)).fetchall():
                p = con.execute("""SELECT real_qty FROM material_daily WHERE material_id=? AND date<?
                    ORDER BY date DESC LIMIT 1""", (twin, d["date"])).fetchone()
                prev = p["real_qty"] if p else 0.0
                con.execute("UPDATE material_daily SET prev_qty=?, real_qty=? WHERE id=?",
                            (prev, prev + d["in_qty"] - d["used_qty"], d["id"]))
            for t in ["material_in", "material_usage_type", "bom"]:
                con.execute(f"UPDATE {t} SET material_id=? WHERE material_id=?", (twin, rid))
            moved.append((name, twin))
        con.execute("DELETE FROM opening_stock WHERE kind='material' AND ref_id=?", (rid,))
        con.execute("DELETE FROM material WHERE id=?", (rid,))
        deleted.append(name)

    con.execute("INSERT INTO audit_log(action,detail) VALUES(?,?)",
                ("cleanup_raw_packaging", f"삭제 {len(deleted)}종 (이관 {len(moved)}종, 보류 {len(held)}종)"))
    con.commit()

    print(f"삭제 {len(deleted)}종 · 기록 이관 {len(moved)}종 · 보류 {len(held)}종")
    for n, t in moved:
        print(f"  이관: {n} → 부재료 #{t}")
    for n, c in held:
        print(f"  ⚠ 보류(부재료판 없음+기록 {c}건): {n}")
    print("남은 자재:", dict(con.execute("SELECT kind, COUNT(*) FROM material GROUP BY kind").fetchall()))
    con.close()


if __name__ == "__main__":
    main()
