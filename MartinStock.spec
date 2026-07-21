# -*- mode: python ; coding: utf-8 -*-
# 빌드: python -m PyInstaller --noconfirm MartinStock.spec
# 결과: dist\재고관리.exe (DB는 exe와 같은 폴더의 martin_stock.db 사용)

a = Analysis(
    ['app\\main.py'],
    pathex=['app'],
    binaries=[],
    datas=[('app\\static', 'static')],
    hiddenimports=[
        'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto',
        'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan', 'uvicorn.lifespan.on',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'PIL', 'numpy', 'pandas'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='재고관리',
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon='app\\logo.ico',   # Logo.png에서 생성 (작은 크기=셰브론, 256px=전체 로고)
)
