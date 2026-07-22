# -*- coding: utf-8 -*-
"""martin_stock DB — SQLite 스키마 및 연결.

설계 원칙:
- 자체 DB가 데이터 원본 (엑셀은 최초 1회 임포트 + 이후 내보내기 전용)
- 트랜잭션 원장: 일일 기록(생산/출고/자재/인원)이 쌓이면 재고·수불부·집계는 전부 파생 계산
- 기준정보(제품/자재/거래처/인원/라인/배합비)는 마스터, 일일 기록은 마스터 id 참조
"""
import sys
import sqlite3
from pathlib import Path

# exe(PyInstaller)로 실행되면 DB는 exe 옆에, 소스 실행이면 프로젝트 루트에
if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent
else:
    BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "martin_stock.db"
# 사내 채팅은 별도 DB — 업무 데이터(재고·생산)와 분리해 백업·삭제·보관 주기를 따로 관리
CHAT_DB_PATH = BASE / "martin_chat.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

-- ── 기준정보 ──────────────────────────────
CREATE TABLE IF NOT EXISTS product (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  category TEXT DEFAULT '',            -- 빵류/냉동생지/기타가공품 …
  spec TEXT DEFAULT '',                -- 60g/EA 등
  pack_sizes TEXT DEFAULT '',          -- '135,30' (개입수, 완제품수불부 유래)
  line_id INTEGER,
  unit_price REAL DEFAULT 0,
  shelf_days INTEGER DEFAULT 0,        -- 소비일
  safety_stock REAL DEFAULT 0,
  batch_yield REAL DEFAULT 0,          -- 1배합(도우)당 생산수량(개) — 반죽량.xlsx
  image TEXT DEFAULT '',               -- 제품 이미지 파일명 (exe 옆 Image/ 폴더)
  status TEXT DEFAULT '판매중',         -- 판매중/단종
  note TEXT DEFAULT '',
  sort INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS material (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                  -- 'raw'(원재료) / 'sub'(부재료)
  name TEXT NOT NULL,
  spec TEXT DEFAULT '',
  unit TEXT DEFAULT 'kg',
  pack_count REAL DEFAULT 0,           -- 개입수 (개수 단위 자재: 소모 = 생산수량 ÷ 개입수, 예: 16개입 트레이=16)
  pack_set TEXT DEFAULT '',            -- (구) 단일 세트명 — 다대다 전환으로 pack_set_member가 정본, 표시 호환용으로만 유지
  unit_price REAL DEFAULT 0,
  partner_id INTEGER,                  -- 공급처
  safety_stock REAL DEFAULT 0,
  prod_mult REAL,                      -- 부재료: 단위당 수량 (롤당 500매)
  prod_per REAL,                       -- 부재료: 1회 생산 소요량
  status TEXT DEFAULT '사용중',
  note TEXT DEFAULT '',
  sort INTEGER DEFAULT 0,
  UNIQUE(kind, name)
);

CREATE TABLE IF NOT EXISTS partner (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  type TEXT DEFAULT '판매처',           -- 판매처/자재 공급처
  phone TEXT DEFAULT '',
  contact TEXT DEFAULT '',
  note TEXT DEFAULT '',
  status TEXT DEFAULT '활성',           -- 활성/중지
  biz_no TEXT DEFAULT '',              -- 사업자등록번호 (ERP 가져오기 매칭 키)
  ceo TEXT DEFAULT '',                 -- 대표자명
  mobile TEXT DEFAULT ''               -- 모바일
);

CREATE TABLE IF NOT EXISTS staff (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT DEFAULT '정직원',           -- 정직원/용역
  position TEXT DEFAULT '',            -- 직책 (사장/부장/과장/… — 직접 입력 또는 기존값 선택)
  process TEXT DEFAULT '',
  wage REAL DEFAULT 0,                 -- 시급
  join_date TEXT DEFAULT '',
  phone TEXT DEFAULT '',
  status TEXT DEFAULT '재직',           -- 재직/계약중/퇴사
  note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS line (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,               -- 동명 라인 허용 (공정으로 구분, 표시 시 번호 부여)
  process TEXT DEFAULT '',
  std_hours REAL DEFAULT 8,            -- 정상가동시간(h/일)
  parent_id INTEGER,                   -- 소속(대표) 라인 — 지정 시 이 행은 그 물리 라인의 한 공정 (집계는 대표 기준)
  note TEXT DEFAULT '',
  status TEXT DEFAULT '가동'
);

-- 배합비(BOM): 제품 1개당 자재 소요량 (적용 시작일로 버전 관리)
CREATE TABLE IF NOT EXISTS bom (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES product(id),
  material_id INTEGER NOT NULL REFERENCES material(id),
  qty_per_unit REAL NOT NULL,
  unit TEXT DEFAULT 'g',
  block TEXT DEFAULT '',               -- 반죽/토핑 (수율이 다른 배합 블록 — 엑셀 원본 구조)
  batch_qty REAL DEFAULT 0,            -- 그 블록 1배합당 소요량 g (엑셀 원본 수치 그대로)
  block_yield REAL DEFAULT 0,          -- 그 블록 1배합당 생산수량 (개)
  partner_id INTEGER,                  -- (구) 납품처 단일 지정 — partner_ids로 대체, 하위호환용
  partner_ids TEXT DEFAULT '',         -- 납품처 복수 지정 "1,3" (예: 이마트·급식용 BOX) — 계획 거래처 분배와 연동
  effective_from TEXT DEFAULT '',
  note TEXT DEFAULT ''
);

-- 생산 수량의 거래처별 분배 (생산 셀 분배 팝업 — 합계 = prod_qty. 납품처 부재료 계산 기준)
CREATE TABLE IF NOT EXISTS prod_split (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  product_id INTEGER NOT NULL REFERENCES product(id),
  partner_id INTEGER,
  qty REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_prodsplit ON prod_split(date, product_id);

-- ── 일일 기록 ─────────────────────────────
-- 날짜별 헤더(특이사항·상태). 존재하면 '그날 기록 있음'
CREATE TABLE IF NOT EXISTS day_record (
  date TEXT PRIMARY KEY,               -- 'YYYY-MM-DD'
  memo TEXT DEFAULT '',
  status TEXT DEFAULT 'saved',         -- draft/saved
  updated_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 생산실적 (제품×일)
CREATE TABLE IF NOT EXISTS production (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  product_id INTEGER NOT NULL REFERENCES product(id),
  line_id INTEGER,
  plan_qty REAL DEFAULT 0,
  prod_qty REAL DEFAULT 0,
  defect_qty REAL DEFAULT 0,
  batches REAL DEFAULT 0,              -- 배합 수 (계획 = 배합 × 제품 batch_yield)
  unit_price REAL DEFAULT 0,           -- 당시 단가 스냅샷
  note TEXT DEFAULT '',                -- 수불부 비고
  defect_reason TEXT DEFAULT '',       -- 불량 사유 (태움/모양불량/이물 등)
  UNIQUE(date, product_id)
);
CREATE INDEX IF NOT EXISTS idx_production_date ON production(date);

-- 완제품 출고 (제품×거래처×일)
CREATE TABLE IF NOT EXISTS shipment (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  product_id INTEGER NOT NULL REFERENCES product(id),
  partner_id INTEGER,
  qty REAL NOT NULL DEFAULT 0,
  prod_date TEXT DEFAULT '',           -- 출고한 재고의 생산일자 (빈값 = FIFO 자동)
  expiry TEXT DEFAULT '',              -- 이 출고분의 소비기한 (납품 표기용 — 기본값=LOT 소비기한, 재고 계산엔 무관)
  lot_no INTEGER DEFAULT 0,            -- 같은 (생산일,소비기한) LOT이 여럿일 때 구분 번호 (0=단일)
  note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_shipment_date ON shipment(date);

-- 원부자재 일일 (입고/실재고 입력, 사용량 = 전일+입고-실재고)
CREATE TABLE IF NOT EXISTS material_daily (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  material_id INTEGER NOT NULL REFERENCES material(id),
  prev_qty REAL DEFAULT 0,
  in_qty REAL DEFAULT 0,
  real_qty REAL DEFAULT 0,
  used_qty REAL DEFAULT 0,
  order_date TEXT DEFAULT '',          -- 발주/입고예정 메모
  order_qty REAL DEFAULT 0,            -- 발주량 (부재료)
  src TEXT DEFAULT 'manual',           -- manual=실사 입력 / auto=제품별 사용 합계 자동 차감
  UNIQUE(date, material_id)
);
CREATE INDEX IF NOT EXISTS idx_matdaily_date ON material_daily(date);

-- 원부자재 입고 (실사와 분리 — 입고만 기록해도 재고에 반영, 유통기한 기록)
CREATE TABLE IF NOT EXISTS material_in (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  material_id INTEGER NOT NULL REFERENCES material(id),
  qty REAL NOT NULL DEFAULT 0,
  made_date TEXT DEFAULT '',           -- 제조일자
  expiry TEXT DEFAULT '',              -- 유통기한
  note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_matin_date ON material_in(date);

-- 자재 사용처: 자재×제품×일 실측 사용량 (원료수불부 매트릭스)
-- product_id NULL = 기타 사용 (생산과 무관한 자재 사용 — 테스트/청소/타용도)
CREATE TABLE IF NOT EXISTS material_usage (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  material_id INTEGER NOT NULL REFERENCES material(id),
  product_id INTEGER REFERENCES product(id),
  qty REAL NOT NULL DEFAULT 0,
  block TEXT DEFAULT '',               -- 배합 구분: 반죽/토핑/''(구분 없음 — 실측 등)
  UNIQUE(date, material_id, product_id, block)
);
CREATE INDEX IF NOT EXISTS idx_matusage_date ON material_usage(date);
CREATE INDEX IF NOT EXISTS idx_matusage_mat ON material_usage(material_id, date);

-- 완제품 LOT 스냅샷 (수불부 우측 블록: 생산일자별 재고/출고 + 소비기한)
CREATE TABLE IF NOT EXISTS lot_snapshot (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  product_id INTEGER NOT NULL REFERENCES product(id),
  kind TEXT NOT NULL,                  -- 'stock' 재고 / 'out' 출고
  slot INTEGER DEFAULT 0,
  qty REAL NOT NULL DEFAULT 0,
  made_date TEXT DEFAULT '',           -- 생산일자
  expiry TEXT DEFAULT ''               -- 소비기한
);
CREATE INDEX IF NOT EXISTS idx_lot_date ON lot_snapshot(date);

-- 원재료 용도별 사용량 (도넛/소보로토핑/단백질/추가/테스트 배합)
CREATE TABLE IF NOT EXISTS material_usage_type (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  material_id INTEGER NOT NULL REFERENCES material(id),
  type TEXT NOT NULL,
  qty REAL NOT NULL DEFAULT 0,
  UNIQUE(date, material_id, type)
);
CREATE INDEX IF NOT EXISTS idx_mut_date ON material_usage_type(material_id, date);

-- 인원·가동 (라인×일)
CREATE TABLE IF NOT EXISTS staffing (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  line_id INTEGER REFERENCES line(id),
  headcount REAL DEFAULT 0,
  agency_count REAL DEFAULT 0,         -- 용역 인원수 (이름 없이 — 투입인원에서 '＋ 용역'으로 추가)
  agency_hours REAL DEFAULT 0,         -- 용역 총 투입시간 합 (노무비 = 용역 총시간 × 용역시급)
  agency_wage REAL DEFAULT 0,          -- 용역 시급
  target_hours REAL DEFAULT 0,         -- 그날의 목표가동 시간 (0이면 라인 정상가동시간 사용)
  work_hours REAL DEFAULT 0,
  stop_reason TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_staffing_date ON staffing(date);

CREATE TABLE IF NOT EXISTS staffing_member (
  staffing_id INTEGER NOT NULL REFERENCES staffing(id) ON DELETE CASCADE,
  staff_id INTEGER NOT NULL REFERENCES staff(id),
  hours REAL DEFAULT 0,                -- 개인별 투입 시간 (노무비 = 시급 × 시간)
  PRIMARY KEY(staffing_id, staff_id)
);

-- 용역 개인별 투입 (용역마다 시급이 다름 — staffing의 agency_* 집계 컬럼은 하위호환용 유지)
CREATE TABLE IF NOT EXISTS staffing_agency (
  staffing_id INTEGER NOT NULL REFERENCES staffing(id) ON DELETE CASCADE,
  seq INTEGER DEFAULT 0,
  hours REAL DEFAULT 0,
  wage REAL DEFAULT 0,
  gender TEXT DEFAULT '',              -- 남/여
  partner_id INTEGER                   -- 용역 업체 (partner.type='용역업체') — 업체별 정산용
);
CREATE INDEX IF NOT EXISTS idx_staffagency ON staffing_agency(staffing_id);

-- LOT별 소비기한 (생산일마다 개별 지정 — 미입력 시 생산일 + 제품 소비일로 폴백)
CREATE TABLE IF NOT EXISTS lot_expiry (
  product_id INTEGER NOT NULL REFERENCES product(id),
  made TEXT NOT NULL DEFAULT '',       -- 생산일 ('' = 이월/생산일 미상 LOT)
  expiry TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(product_id, made)
);

-- 포장 세트 구성원 (다대다) — 한 부재료가 여러 세트에 동시에 속할 수 있다
-- 예: '리바이 무지 135개입 BOX'가 A제품 세트와 B제품 세트 양쪽에 포함
CREATE TABLE IF NOT EXISTS pack_set_member (
  set_name TEXT NOT NULL,
  material_id INTEGER NOT NULL REFERENCES material(id) ON DELETE CASCADE,
  PRIMARY KEY (set_name, material_id)
);
CREATE INDEX IF NOT EXISTS idx_psm_mat ON pack_set_member(material_id);

-- 거래처별 판매 단가 — 없으면 product.unit_price(기본 단가) 사용
CREATE TABLE IF NOT EXISTS product_price (
  product_id INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
  partner_id INTEGER NOT NULL REFERENCES partner(id) ON DELETE CASCADE,
  price REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (product_id, partner_id)
);

-- 생산 LOT 소비기한 분할 (한 생산분을 수량별로 여러 소비기한으로 나눔)
-- 예: 7/9 생산 1,000개 → 300개(07-11) + 700개(07-12). lot_expiry(단일)보다 우선.
CREATE TABLE IF NOT EXISTS lot_plan (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES product(id),
  made TEXT NOT NULL DEFAULT '',        -- 생산일
  seq INTEGER DEFAULT 0,
  qty REAL NOT NULL DEFAULT 0,
  expiry TEXT NOT NULL DEFAULT '',
  pack_mid INTEGER,                     -- 이 구간의 포장 부재료(material.id) — 단일 자재 선택 시
  pack_set TEXT DEFAULT '',             -- 이 구간의 포장 세트명 (세트 선택 시 — 세트 전원 소모)
  partner_id INTEGER                    -- 이 구간의 납품처 (거래처 분배와 연동 — 출고 시 자동 선택)
);
CREATE INDEX IF NOT EXISTS idx_lotplan ON lot_plan(product_id, made);

-- 완제품 폐기 (만료/불량 — LOT 단위 기록, 재고 = 기초 + 생산 − 출고 − 폐기)
CREATE TABLE IF NOT EXISTS disposal (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,                  -- 폐기일
  product_id INTEGER NOT NULL REFERENCES product(id),
  qty REAL NOT NULL DEFAULT 0,
  prod_date TEXT DEFAULT '',           -- 폐기한 LOT의 생산일자 ('' = 미상)
  reason TEXT DEFAULT '',              -- 소비기한 만료/불량/파손/기타
  note TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_disposal_date ON disposal(date);

-- 재고 시작점 (임포트 시 첫 시트의 전일재고 = 기초재고)
CREATE TABLE IF NOT EXISTS opening_stock (
  kind TEXT NOT NULL,                  -- 'product' / 'material'
  ref_id INTEGER NOT NULL,
  date TEXT NOT NULL,                  -- 이 날짜 이전 재고
  qty REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(kind, ref_id)
);

-- 사용자 (admin=전체 / op=시급 제외 전체 / guest=보기 전용)
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'guest',
  duty TEXT NOT NULL DEFAULT 'all',    -- 담당(복수 가능, 콤마): production,shipment,usage,staffing,stock,lot
                                       -- 'all'=전체 / 'none'=담당 없음 — 일일 입력 저장 범위 제한
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 일일 생산 현장 사진 (날짜별 첨부 — exe 옆 DayPhoto/ 폴더에 파일 저장)
CREATE TABLE IF NOT EXISTS day_photo (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  file TEXT NOT NULL,                  -- DayPhoto/ 폴더 내 파일명
  note TEXT DEFAULT '',
  at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_dayphoto_date ON day_photo(date);

-- 사내 채팅은 별도 DB(martin_chat.db)로 분리 — CHAT_SCHEMA 참고

-- 수정 이력
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  at TEXT DEFAULT (datetime('now','localtime')),
  action TEXT,                         -- save_day/update_master/…
  detail TEXT,
  username TEXT DEFAULT ''             -- 누가 (로그인 사용자)
);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


# ── 사내 채팅 전용 DB ─────────────────────────
# 업무 DB와 분리한 이유: 대화는 원장 데이터가 아니라 휘발성 소통 기록 —
# 백업/보관 주기가 다르고, 통째로 지워도 재고·생산 데이터에 영향이 없어야 한다.
CHAT_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS chat (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,                   -- 대화 날짜 — 채팅창은 이 값 기준으로 하루마다 갱신
  username TEXT NOT NULL,
  text TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'user',   -- user(사람) / system(감사로그 자동 기록)
  mentions TEXT DEFAULT '',            -- ',홍길동,admin,' 형태 — LIKE '%,이름,%'로 조회
  file TEXT DEFAULT '',                -- 첨부 저장 파일명 (ChatFile/)
  fname TEXT DEFAULT '',               -- 첨부 원본 파일명 (표시·다운로드용)
  fkind TEXT DEFAULT '',               -- image(인라인 표시) / file(다운로드 링크)
  at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_chat_day ON chat(day, id);

-- 읽음 표시 — 사용자마다 '어디까지 읽었는지'만 기록 (메시지×사용자 조합을 쌓지 않음)
CREATE TABLE IF NOT EXISTS chat_read (
  username TEXT PRIMARY KEY,
  last_id INTEGER NOT NULL DEFAULT 0,
  at TEXT DEFAULT (datetime('now','localtime'))
);
"""


def chat_connect() -> sqlite3.Connection:
    con = sqlite3.connect(CHAT_DB_PATH)
    con.row_factory = sqlite3.Row
    return con


CHAT_RETENTION_DAYS = 90     # 이 기간이 지난 대화는 자동 삭제 (0이면 영구 보관)


def purge_old_chat(file_dir=None) -> int:
    """보관 주기가 지난 대화 삭제 — 첨부 파일도 함께 정리. 삭제 건수 반환."""
    if CHAT_RETENTION_DAYS <= 0:
        return 0
    import datetime as _dt
    cut = (_dt.date.today() - _dt.timedelta(days=CHAT_RETENTION_DAYS)).isoformat()
    con = chat_connect()
    try:
        olds = [r["file"] for r in con.execute(
            "SELECT file FROM chat WHERE day<? AND COALESCE(file,'')!=''", (cut,))]
        n = con.execute("DELETE FROM chat WHERE day<?", (cut,)).rowcount
        con.commit()
    finally:
        con.close()
    if file_dir:
        for f in olds:
            try:
                (file_dir / f).unlink(missing_ok=True)
            except OSError:
                pass
    return n


def init_chat_db() -> None:
    """채팅 DB 생성 + (1회) 본 DB에 있던 기존 채팅 이관."""
    con = chat_connect()
    con.executescript(CHAT_SCHEMA)
    cols = [r[1] for r in con.execute("PRAGMA table_info(chat)")]
    if "day" not in cols:      # 예전 채팅 DB → day 컬럼 보강
        con.execute("ALTER TABLE chat ADD COLUMN day TEXT DEFAULT ''")
        con.execute("UPDATE chat SET day=substr(at,1,10) WHERE COALESCE(day,'')=''")
        con.execute("CREATE INDEX IF NOT EXISTS idx_chat_day ON chat(day, id)")
    for col, ddl in (("kind", "TEXT NOT NULL DEFAULT 'user'"), ("mentions", "TEXT DEFAULT ''"),
                     ("file", "TEXT DEFAULT ''"), ("fname", "TEXT DEFAULT ''"),
                     ("fkind", "TEXT DEFAULT ''")):
        if col not in cols:
            con.execute(f"ALTER TABLE chat ADD COLUMN {col} {ddl}")
    con.commit()
    # 본 DB(martin_stock.db)에 남아 있던 chat 테이블 → 채팅 DB로 옮기고 원본은 제거
    main = connect()
    try:
        if main.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chat'").fetchone():
            old = list(main.execute("SELECT username, text, at FROM chat ORDER BY id"))
            if old and con.execute("SELECT COUNT(*) c FROM chat").fetchone()["c"] == 0:
                con.executemany("INSERT INTO chat(day, username, text, at) VALUES(?,?,?,?)",
                                [((r["at"] or "")[:10], r["username"], r["text"], r["at"]) for r in old])
                con.commit()
            main.execute("DROP TABLE chat")
            main.commit()
    finally:
        main.close()
    con.close()


def init_db() -> None:
    con = connect()
    con.executescript(SCHEMA)
    # 기존 DB 마이그레이션 (CREATE IF NOT EXISTS는 기존 테이블에 새 컬럼을 안 붙임)
    cols = [r[1] for r in con.execute("PRAGMA table_info(shipment)")]
    if "prod_date" not in cols:
        con.execute("ALTER TABLE shipment ADD COLUMN prod_date TEXT DEFAULT ''")
    if "expiry" not in cols:
        con.execute("ALTER TABLE shipment ADD COLUMN expiry TEXT DEFAULT ''")
    if "lot_no" not in cols:
        con.execute("ALTER TABLE shipment ADD COLUMN lot_no INTEGER DEFAULT 0")
    bcols = [r[1] for r in con.execute("PRAGMA table_info(bom)")]
    for col, ddl in [("block", "TEXT DEFAULT ''"), ("batch_qty", "REAL DEFAULT 0"),
                     ("block_yield", "REAL DEFAULT 0"), ("partner_id", "INTEGER")]:
        if col not in bcols:
            con.execute(f"ALTER TABLE bom ADD COLUMN {col} {ddl}")
    if "partner_ids" not in bcols:
        con.execute("ALTER TABLE bom ADD COLUMN partner_ids TEXT DEFAULT ''")
        # 1회 이관: 단일 partner_id → 복수 partner_ids
        con.execute("""UPDATE bom SET partner_ids = CAST(partner_id AS TEXT)
            WHERE partner_id IS NOT NULL AND COALESCE(partner_ids,'')=''""")
    # (구) plan_split → prod_split 이관 후 제거 (분배 기준을 계획→생산으로 변경, 2026-07-14)
    if con.execute("SELECT name FROM sqlite_master WHERE name='plan_split'").fetchone():
        con.execute("""INSERT INTO prod_split(date, product_id, partner_id, qty)
            SELECT date, product_id, partner_id, qty FROM plan_split""")
        con.execute("DROP TABLE plan_split")
    smcols = [r[1] for r in con.execute("PRAGMA table_info(staffing_member)")]
    if "hours" not in smcols:
        con.execute("ALTER TABLE staffing_member ADD COLUMN hours REAL DEFAULT 0")
    # 출근·퇴근 시각(HH:MM) + 휴게(분) — 입력 시 근무시간(hours) 자동 계산. 옛 행은 빈값(수동 hours 사용).
    if "start_time" not in smcols:
        con.execute("ALTER TABLE staffing_member ADD COLUMN start_time TEXT DEFAULT ''")
    if "end_time" not in smcols:
        con.execute("ALTER TABLE staffing_member ADD COLUMN end_time TEXT DEFAULT ''")
    if "break_min" not in smcols:
        con.execute("ALTER TABLE staffing_member ADD COLUMN break_min REAL DEFAULT 0")
    # 출고 단가 스냅샷 — 저장 시점의 판매가를 기록해 나중에 단가를 바꿔도 과거 금액이 안 바뀌게
    # (0이면 스냅샷 없음 = 기존 행 → 계산 시 현재 제품 단가로 폴백)
    if "unit_price" not in [r[1] for r in con.execute("PRAGMA table_info(shipment)")]:
        con.execute("ALTER TABLE shipment ADD COLUMN unit_price REAL DEFAULT 0")
    # 포장 세트 다대다 전환: 기존 material.pack_set(단일) → pack_set_member (1회, 중복 무시라 재실행 안전)
    con.execute("""INSERT OR IGNORE INTO pack_set_member(set_name, material_id)
                   SELECT pack_set, id FROM material WHERE COALESCE(pack_set,'')!=''""")
    ucols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
    if "duty" not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN duty TEXT NOT NULL DEFAULT 'all'")
    # 담당 세분화(복수 지정) 이전의 'prod'(생산 담당) → 세부 담당들로 1회 확장.
    # 새 담당 코드에 'prod'가 없어 재실행해도 다시 걸리지 않는다 (idempotent).
    con.execute("UPDATE users SET duty='production,shipment,usage,staffing,lot' WHERE duty='prod'")
    if "money_perms" not in ucols:
        # 금액 열람 권한: "mat,prod,labor,cost" 콤마 목록 — admin은 항상 전체, 나머지는 admin이 체크한 것만
        con.execute("ALTER TABLE users ADD COLUMN money_perms TEXT NOT NULL DEFAULT ''")
    lpcols = [r[1] for r in con.execute("PRAGMA table_info(lot_plan)")]
    if "pack_mid" not in lpcols:
        # LOT 분할 구간별 포장 부재료 선택 (환산·부재료 소모 계산 근거)
        con.execute("ALTER TABLE lot_plan ADD COLUMN pack_mid INTEGER")
    if "partner_id" not in lpcols:
        con.execute("ALTER TABLE lot_plan ADD COLUMN partner_id INTEGER")
    if "position" not in [r[1] for r in con.execute("PRAGMA table_info(staff)")]:
        con.execute("ALTER TABLE staff ADD COLUMN position TEXT DEFAULT ''")
    if "pack_set" not in [r[1] for r in con.execute("PRAGMA table_info(material)")]:
        con.execute("ALTER TABLE material ADD COLUMN pack_set TEXT DEFAULT ''")
    if "pack_set" not in lpcols:
        con.execute("ALTER TABLE lot_plan ADD COLUMN pack_set TEXT DEFAULT ''")
    stcols = [r[1] for r in con.execute("PRAGMA table_info(staffing)")]
    if "target_hours" not in stcols:
        con.execute("ALTER TABLE staffing ADD COLUMN target_hours REAL DEFAULT 0")
    if "agency_count" not in stcols:
        con.execute("ALTER TABLE staffing ADD COLUMN agency_count REAL DEFAULT 0")
    if "agency_hours" not in stcols:
        con.execute("ALTER TABLE staffing ADD COLUMN agency_hours REAL DEFAULT 0")
    if "agency_wage" not in stcols:
        con.execute("ALTER TABLE staffing ADD COLUMN agency_wage REAL DEFAULT 0")
    mcols = [r[1] for r in con.execute("PRAGMA table_info(material)")]
    if "pack_count" not in mcols:
        con.execute("ALTER TABLE material ADD COLUMN pack_count REAL DEFAULT 0")
    pcols = [r[1] for r in con.execute("PRAGMA table_info(product)")]
    if "image" not in pcols:
        con.execute("ALTER TABLE product ADD COLUMN image TEXT DEFAULT ''")
    prcols = [r[1] for r in con.execute("PRAGMA table_info(production)")]
    if "defect_reason" not in prcols:
        con.execute("ALTER TABLE production ADD COLUMN defect_reason TEXT DEFAULT ''")
    sacols = [r[1] for r in con.execute("PRAGMA table_info(staffing_agency)")]
    if "gender" not in sacols:
        con.execute("ALTER TABLE staffing_agency ADD COLUMN gender TEXT DEFAULT ''")
    if "partner_id" not in sacols:
        con.execute("ALTER TABLE staffing_agency ADD COLUMN partner_id INTEGER")
    # 원부자재 입고 제조일자 (유통기한과 동일 구조)
    micols = [r[1] for r in con.execute("PRAGMA table_info(material_in)")]
    if "made_date" not in micols:
        con.execute("ALTER TABLE material_in ADD COLUMN made_date TEXT DEFAULT ''")
    # 거래처: 사업자등록번호·대표자·모바일 (ERP 거래처등록 엑셀 가져오기)
    pcols = [r[1] for r in con.execute("PRAGMA table_info(partner)")]
    for col in ("biz_no", "ceo", "mobile"):
        if col not in pcols:
            con.execute(f"ALTER TABLE partner ADD COLUMN {col} TEXT DEFAULT ''")
    # 용역도 출근·퇴근·휴게 입력 지원 (정직원 staffing_member와 동일)
    if "start_time" not in sacols:
        con.execute("ALTER TABLE staffing_agency ADD COLUMN start_time TEXT DEFAULT ''")
    if "end_time" not in sacols:
        con.execute("ALTER TABLE staffing_agency ADD COLUMN end_time TEXT DEFAULT ''")
    if "break_min" not in sacols:
        con.execute("ALTER TABLE staffing_agency ADD COLUMN break_min REAL DEFAULT 0")
    acols = [r[1] for r in con.execute("PRAGMA table_info(audit_log)")]
    if "username" not in acols:
        con.execute("ALTER TABLE audit_log ADD COLUMN username TEXT DEFAULT ''")
    lcols = [r[1] for r in con.execute("PRAGMA table_info(line)")]
    if "parent_id" not in lcols:
        con.execute("ALTER TABLE line ADD COLUMN parent_id INTEGER")
        # 1회 자동 연결: 라인명이 같은 기존 행들 = 한 물리 라인의 공정들 (가장 오래된 행이 대표)
        # 이후에는 이름과 무관하게 폼의 '소속 라인' 지정으로만 묶임 (오타 위험 제거)
        for r in con.execute("SELECT name, MIN(id) mid FROM line GROUP BY name HAVING COUNT(*)>1").fetchall():
            con.execute("UPDATE line SET parent_id=? WHERE name=? AND id!=?", (r[1], r[0], r[1]))
    # material_usage 재구성 마이그레이션: ①block 추가 ②product_id NOT NULL 해제(기타 사용)
    mu_info = list(con.execute("PRAGMA table_info(material_usage)"))
    mucols = [r[1] for r in mu_info]
    pid_notnull = any(r[1] == "product_id" and r[3] == 1 for r in mu_info)
    if "block" not in mucols or pid_notnull:
        has_block = "block" in mucols
        con.executescript(f"""
        CREATE TABLE material_usage_new (
          id INTEGER PRIMARY KEY,
          date TEXT NOT NULL,
          material_id INTEGER NOT NULL REFERENCES material(id),
          product_id INTEGER REFERENCES product(id),
          qty REAL NOT NULL DEFAULT 0,
          block TEXT DEFAULT '',
          UNIQUE(date, material_id, product_id, block)
        );
        INSERT INTO material_usage_new(id, date, material_id, product_id, qty, block)
          SELECT id, date, material_id, product_id, qty, {"block" if has_block else "''"}
          FROM material_usage;
        DROP TABLE material_usage;
        ALTER TABLE material_usage_new RENAME TO material_usage;
        CREATE INDEX IF NOT EXISTS idx_matusage_date ON material_usage(date);
        CREATE INDEX IF NOT EXISTS idx_matusage_mat ON material_usage(material_id, date);
        """)
    con.commit()
    con.close()


if __name__ == "__main__":
    init_db()
    print("DB initialized:", DB_PATH)
