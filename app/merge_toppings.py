# -*- coding: utf-8 -*-
"""토핑 별도 제품 → 원제품 '토핑 배합 블록'으로 통합 (2026-07-10 사용자 확정).

- 각 토핑 제품(이름 끝 '토핑')을 이름으로 원제품에 매칭(괄호·공백 무시)
- 토핑 제품의 배합(bom) → 원제품으로 이관, block='토핑'
- 토핑 제품의 자재사용(material_usage) → 원제품 block='토핑'으로 이관(충돌 시 합산)
- 토핑 제품의 생산/출고/LOT/폐기/기초재고 → 삭제 (이중집계 원인)
- 토핑 제품 삭제
"""
import sys, io, re, shutil, sqlite3, datetime as dt
from pathlib import Path

if hasattr(sys.stdout, "buffer") and (sys.stdout.encoding or "").lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "martin_stock.db"


def nrm(s):
    s = re.sub(r"\([^)]*\)", "", str(s))
    return re.sub(r"\s+", "", s).lower()


def main():
    bak = BASE / "백업" / f"백업_{dt.date.today():%Y%m%d}_토핑통합전.db"
    shutil.copy2(DB, bak)
    print("백업:", bak.name)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    tops = con.execute("SELECT id, name FROM product WHERE name LIKE '%토핑%'").fetchall()
    parents = [dict(r) for r in con.execute("SELECT id, name FROM product WHERE name NOT LIKE '%토핑%'")]
    pn = {nrm(p["name"]): p for p in parents}

    pairs, unmatched = [], []
    for t in tops:
        key = nrm(re.sub(r"\s*토핑\s*$", "", t["name"]))
        par = pn.get(key)
        if not par:
            cands = [p for k, p in pn.items() if k.startswith(key) or key.startswith(k)]
            par = cands[0] if len(cands) == 1 else None
        (pairs if par else unmatched).append((t, par))
    if unmatched:
        print("⚠ 매칭 실패 — 중단:", [t["name"] for t, _ in unmatched])
        return

    n_bom = n_use = n_del_prod = n_del_ship = 0
    for t, par in pairs:
        tid, pid = t["id"], par["id"]
        # 배합 이관 (block=토핑)
        n_bom += con.execute("UPDATE bom SET product_id=?, block='토핑' WHERE product_id=?",
                             (pid, tid)).rowcount
        # 자재사용 이관 (충돌 시 합산 후 원본 삭제)
        for u in con.execute("SELECT * FROM material_usage WHERE product_id=?", (tid,)).fetchall():
            dup = con.execute("""SELECT id FROM material_usage
                WHERE date=? AND material_id=? AND product_id=? AND block='토핑'""",
                              (u["date"], u["material_id"], pid)).fetchone()
            if dup:
                con.execute("UPDATE material_usage SET qty=qty+? WHERE id=?", (u["qty"], dup["id"]))
                con.execute("DELETE FROM material_usage WHERE id=?", (u["id"],))
            else:
                con.execute("UPDATE material_usage SET product_id=?, block='토핑' WHERE id=?", (pid, u["id"]))
            n_use += 1
        # 이중집계/오류 기록 삭제
        n_del_prod += con.execute("DELETE FROM production WHERE product_id=?", (tid,)).rowcount
        n_del_ship += con.execute("DELETE FROM shipment WHERE product_id=?", (tid,)).rowcount
        for tbl in ("lot_snapshot", "lot_plan", "lot_expiry", "disposal"):
            con.execute(f"DELETE FROM {tbl} WHERE product_id=?", (tid,))
        con.execute("DELETE FROM opening_stock WHERE kind='product' AND ref_id=?", (tid,))
        con.execute("DELETE FROM product WHERE id=?", (tid,))

    con.execute("INSERT INTO audit_log(action,detail) VALUES(?,?)",
                ("merge_toppings", f"토핑 {len(pairs)}종 통합 (배합 {n_bom}행·사용 {n_use}행 이관, "
                 f"생산 {n_del_prod}·출고 {n_del_ship} 삭제)"))
    con.commit()
    print(f"통합 완료: 토핑 {len(pairs)}종 → 원제품 토핑 블록")
    print(f"  배합 이관 {n_bom}행 · 자재사용 이관 {n_use}행 · 생산삭제 {n_del_prod} · 출고삭제 {n_del_ship}")
    print("남은 제품 수:", con.execute("SELECT COUNT(*) FROM product").fetchone()[0])
    print("남은 토핑 제품:", con.execute("SELECT COUNT(*) FROM product WHERE name LIKE '%토핑%'").fetchone()[0])
    # 검증: 트레이더스 도넛 배합 블록
    tr = con.execute("SELECT id,name FROM product WHERE name LIKE '%트레이더스%' AND name NOT LIKE '%토핑%'").fetchone()
    if tr:
        bl = con.execute("SELECT block, COUNT(*) FROM bom WHERE product_id=? GROUP BY block", (tr["id"],)).fetchall()
        print(f"  {tr['name'][:30]} 배합 블록: {[(b[0] or '무')+':'+str(b[1]) for b in bl]}")
    con.close()


if __name__ == "__main__":
    main()
