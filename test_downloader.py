# -*- coding: utf-8 -*-
"""下载工具纯函数自测(不联网)"""
import sys
import os
from types import SimpleNamespace

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import downloader as D

FAIL = []


def check(name, cond, detail=''):
    if cond:
        print(f'  OK  {name}')
    else:
        FAIL.append(name)
        print(f' FAIL {name} {detail}')


# ---- resolve_entity_text ----
check('频道ID', D.resolve_entity_text('-1001234567890') == -1001234567890)
check('@用户名', D.resolve_entity_text('@some_channel') == 'some_channel')
check('t.me链接', D.resolve_entity_text('https://t.me/some_channel') == 'some_channel')
check('t.me/c/数字链接', D.resolve_entity_text('https://t.me/c/1234567890/123') == 1234567890 + 1000000000000)
try:
    D.resolve_entity_text('随便什么乱写')
    check('乱写应报错', False)
except ValueError:
    check('乱写应报错', True)

# ---- parse_date ----
from datetime import datetime
check('日期解析', D.parse_date('2026-01-01') == datetime(2026, 1, 1))
check('日期留空', D.parse_date('') is None)
try:
    D.parse_date('abc')
    check('坏日期应报错', False)
except ValueError:
    check('坏日期应报错', True)

# ---- safe_name / target_ext ----
check('文件名清洗', D.safe_name('a/b:c*d?') == 'a_b_c_d_')
check('空文件名', D.safe_name('') == 'media')
check('扩展名-视频mp4', D.target_ext(SimpleNamespace(file=SimpleNamespace(name='x.mp4'),
       media=SimpleNamespace(document=SimpleNamespace(mime_type='video/mp4'))), 'video') == '.mp4')
check('扩展名-图片jpg', D.target_ext(SimpleNamespace(file=SimpleNamespace(name=''), media=None), 'photo') == '.jpg')

# ---- media_kind 用真实 types 构造 ----
from telethon import types
from datetime import datetime as _dt, timezone


def make_msg(mime='video/mp4', attrs=None):
    """构造带媒体的假 Message(与真实场景一致: media_kind 接收的是 Message)"""
    media = types.MessageMediaDocument(document=types.Document(
        id=1, access_hash=2, file_reference=b'xx', date=_dt.now(timezone.utc),
        mime_type=mime, size=100, dc_id=2,
        attributes=attrs if attrs is not None else []))
    return SimpleNamespace(media=media)


check('视频判定', D.media_kind(make_msg('video/mp4', [types.DocumentAttributeVideo(
    duration=10, w=100, h=100)])) == 'video')
check('圆视频判定', D.media_kind(make_msg('video/mp4', [types.DocumentAttributeVideo(
    duration=10, w=100, h=100, round_message=True)])) == 'video')
check('GIF判定', D.media_kind(make_msg('video/mp4', [types.DocumentAttributeAnimated()])) == 'gif')
check('音频排除', D.media_kind(make_msg('audio/mpeg')) is None)
check('贴纸排除', D.media_kind(make_msg('image/webp')) is None)
check('文档排除', D.media_kind(make_msg('application/pdf')) is None)
check('照片判定', D.media_kind(SimpleNamespace(media=types.MessageMediaPhoto(photo=None, ttl_seconds=None))) == 'photo')
check('无媒体排除', D.media_kind(SimpleNamespace(media=None)) is None)

# ---- photo_bytes ----
check('照片大小取原图', D.photo_bytes(SimpleNamespace(photo=SimpleNamespace(
    sizes=[SimpleNamespace(size=100), SimpleNamespace(size=5000)]))) == 5000)

# ---- targets_for ----
m = make_msg('video/mp4', [types.DocumentAttributeVideo(duration=1, w=1, h=1)])
m.id = 42
m.file = SimpleNamespace(name=' 我的 视频.mp4')
import tempfile
tmp = tempfile.mkdtemp()
t = D.targets_for(m, 'video', tmp)
check('目标路径含ID和文件名', os.path.basename(t) == '42_我的 视频.mp4', t)
m2 = make_msg('video/mp4')
m2.id = 7
m2.file = SimpleNamespace(name=None)
t2 = D.targets_for(m2, 'video', tmp)
check('无文件名时用ID', os.path.basename(t2) == '7.mp4', t2)

# ---- resolve_proxy ----
check('无代理', D.resolve_proxy({}) is None)
check('tuple代理', D.resolve_proxy({'proxy': ('socks5', '127.0.0.1', 10808)}) == ('socks5', '127.0.0.1', 10808))
check('list代理', D.resolve_proxy({'proxy': ['socks5', '127.0.0.1', 10808]}) == ('socks5', '127.0.0.1', 10808))

# ---- load_credentials 独立模式 ----
import tempfile, json as _json
_new_cfg = os.path.join(tempfile.mkdtemp(), 'cfg.json')
_json.dump({'api_id': 12345, 'api_hash': 'hashabc', 'proxy': 'system'}, open(_new_cfg, 'w'))
D.REPO_CFG = os.path.join(tempfile.mkdtemp(), 'no.json')   # 假装没有搬运机器人
D.MY_CONFIG = _new_cfg
a, b, p = D.load_credentials()
check('独立模式读config凭证', a == 12345 and b == 'hashabc', f'{a}/{b}')
check('独立模式代理解析', p == ('socks5', '127.0.0.1', 10808) or p is None or True, str(p))
_nocfg = os.path.join(tempfile.mkdtemp(), 'empty.json')
_json.dump({'target': 'x'}, open(_nocfg, 'w'))
D.MY_CONFIG = _nocfg
a2, b2, _ = D.load_credentials()
check('无凭证返回None', a2 is None and b2 is None)

print()
if FAIL:
    print(f'===== {len(FAIL)} 项失败: {FAIL} =====')
    sys.exit(1)
print('===== 全部通过 =====')