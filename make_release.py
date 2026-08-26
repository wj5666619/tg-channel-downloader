# -*- coding: utf-8 -*-
"""打包发布: 制作 Windows 便携版 zip(内嵌 runtime), 脱敏审计 + 解压冒烟测试

用法: 运行本脚本 -> 输出 release/tg-channel-downloader-vX.Y.Z-windows-portable.zip
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
VERSION = '1.0.0'
RUNTIME_SRC = r'D:\myq\AI\tg-channel-reposter\runtime\python'
OUT_DIR = os.path.join(BASE, 'release')
ZIP_PATH = os.path.join(OUT_DIR, f'tg-channel-downloader-v{VERSION}-windows-portable.zip')

# 打包进 zip 的根目录文件
FILES = ['downloader.py', 'config.example.json', 'README.md', 'LICENSE',
         'test_downloader.py', 'test_parallel_offline.py']
LAUNCHER = r'''@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d %~dp0
"runtime\python\python.exe" downloader.py
pause
'''


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    if os.path.exists(ZIP_PATH):
        os.remove(ZIP_PATH)

    print('== 1/4 打包中...')
    n = 0
    with zipfile.ZipFile(ZIP_PATH, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in FILES:
            src = os.path.join(BASE, f)
            if not os.path.exists(src):
                print(f'  ! 缺少 {f}, 跳过')
                continue
            z.write(src, f)
            n += 1
        z.writestr('下载工具.bat', LAUNCHER)
        n += 1
        for root, dirs, files in os.walk(RUNTIME_SRC):
            dirs[:] = [d for d in dirs if d not in ('__pycache__',)]
            for f in files:
                if f.endswith(('.pyc', '.pyo')):
                    continue
                full = os.path.join(root, f)
                arc = os.path.join('runtime', 'python', os.path.relpath(full, RUNTIME_SRC))
                z.write(full, arc)
                n += 1
    size_mb = os.path.getsize(ZIP_PATH) / 1048576
    print(f'  完成: {n} 个文件, {size_mb:.1f}MB')

    print('== 2/4 脱敏审计...')
    bad = []
    with zipfile.ZipFile(ZIP_PATH) as z:
        names = z.namelist()
        for probe in ('config.json', 'downloader.session', 'reposter.session',
                      'data/', '失败清单', 'bot.py', 'control_bot.py'):
            hit = [x for x in names if probe in x]
            if hit:
                bad.append((probe, hit))
        if bad:
            print('  !! 发现敏感内容, 中止!')
            for probe, hit in bad:
                print(f'     {probe}: {hit}')
            sys.exit(1)
        # 确认模板存在且不含真实 api_hash
        cfg = z.read('config.example.json').decode('utf-8')
        assert '"api_id": ""' in cfg, 'config.example.json 不是空模板'
        print(f'  干净: 无 config.json/session/data, 共 {len(names)} 项')

    print('== 3/4 解压冒烟测试...')
    tmp = tempfile.mkdtemp(prefix='dl_release_')
    try:
        with zipfile.ZipFile(ZIP_PATH) as z:
            z.extractall(tmp)
        py = os.path.join(tmp, 'runtime', 'python', 'python.exe')
        assert os.path.exists(py), '包内缺少 runtime'
        assert not os.path.exists(os.path.join(tmp, 'config.json')), '包内不应有 config.json'
        assert not [x for x in os.listdir(tmp) if x.endswith('.session')], '包内不应有 session'
        # 冒烟: import + 离线逻辑测试
        env = dict(os.environ)
        for t in ('test_downloader.py', 'test_parallel_offline.py'):
            print(f'  跑 {t} ...')
            r = subprocess.run([py, t], cwd=tmp, capture_output=True,
                               text=True, encoding='utf-8', errors='replace',
                               timeout=180, env=env)
            if r.returncode != 0:
                print(r.stdout[-1500:])
                print(r.stderr[-1500:])
                print(f'  !! {t} 失败')
                sys.exit(1)
            print('    通过')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    h = hashlib.sha256(open(ZIP_PATH, 'rb').read()).hexdigest()
    print(f'== 4/4 完成 ==')
    print(f'  {ZIP_PATH}')
    print(f'  SHA256: {h}')


if __name__ == '__main__':
    main()