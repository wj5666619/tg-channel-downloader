# -*- coding: utf-8 -*-
"""Telegram 频道/群聊 视频图片下载工具

用法：双击 下载工具.bat
- 复用 tg-channel-reposter 的登录会话(自动复制副本, 搬运机器人运行中也不冲突)
- 按日期范围扫描指定频道/群聊, 下载视频+图片到本地
- 断点续传: 已下载且大小一致的自动跳过; 中断后重跑即可续传
- 大文件(>4MB)走 8MB 分片 x4 连接并行下载
"""
import asyncio
import json
import logging
import mimetypes
import msvcrt
import os
import re
import shutil
import sys
import threading
import time

# 静音 Telethon/网络层噪音(代理断流自愈是常态, 打日志反而刷屏淹没关键信息)
logging.getLogger('telethon').setLevel(logging.ERROR)

import winreg

from telethon import TelegramClient, types
from telethon import utils as tl_utils

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# 搬运机器人项目位置(存在则复用其 api_id/api_hash 与登录会话; 不存在则用独立模式)
REPO_DIR = os.path.join(os.path.dirname(BASE_DIR), 'tg-channel-reposter')
REPO_CFG = os.path.join(REPO_DIR, 'config.json')
REPO_SESSION = os.path.join(REPO_DIR, 'data', 'reposter.session')

MY_CONFIG = os.path.join(BASE_DIR, 'config.json')
SESS = os.path.join(BASE_DIR, 'data', 'downloader.session')


def load_credentials():
    """返回 (api_id, api_hash, proxy) 或 (None, None, None)。
    优先复用搬运机器人配置; 否则读本工具 config.json 的 api_id/api_hash(发布独立模式)"""
    if os.path.exists(REPO_CFG):
        try:
            rcfg = json.load(open(REPO_CFG, encoding='utf-8'))
            if rcfg.get('api_id') and rcfg.get('api_hash'):
                return int(rcfg['api_id']), rcfg['api_hash'], resolve_proxy(rcfg)
        except Exception:
            pass
    try:
        cfg = json.load(open(MY_CONFIG, encoding='utf-8'))
        if cfg.get('api_id') and cfg.get('api_hash'):
            return int(cfg['api_id']), cfg['api_hash'], resolve_proxy(cfg)
        # 兼容 config.json 里没有 proxy 字段
        return None, None, None
    except Exception:
        return None, None, None

PART = 8 * 1024 * 1024        # 分片大小 8MB
CONCURRENT = 4                # 分片并行连接数
RETRIES = 4                   # 单条消息重试次数
MAX_CONN_CHUNKS = 12          # 大文件分片上限: 超过则提示改小范围(避免开太多连接)


def log(msg):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}')


def resolve_proxy(cfg):
    """解析代理配置: None=直连 / 'system'=读注册表 / 元组=(type,host,port)"""
    p = cfg.get('proxy')
    if not p:
        return None
    if isinstance(p, str):
        if p.lower() != 'system':
            return None
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r'Software\Microsoft\Windows\CurrentVersion\Internet Settings')
            try:
                enabled, _ = winreg.QueryValueEx(k, 'ProxyEnable')
                server, _ = winreg.QueryValueEx(k, 'ProxyServer')
            finally:
                winreg.CloseKey(k)
        except OSError:
            return None
        if not enabled or not server:
            return None
        host = port = None
        if ';' in server:
            for item in server.split(';'):
                if item.lower().startswith('socks='):
                    host, port = item[6:].rsplit(':', 1)
                    break
            if not host:
                for pref in ('https=', 'http='):
                    for item in server.split(';'):
                        if item.lower().startswith(pref):
                            host, port = item[len(pref):].rsplit(':', 1)
                            break
                    if host:
                        break
        else:
            host, port = server.rsplit(':', 1)
        if not host:
            return None
        return ('socks5', host, int(port))
    if isinstance(p, (list, tuple)) and len(p) in (2, 3):
        return tuple(p)
    return None


def prepare_session():
    """优先复制搬运机器人会话(避免抢锁); 独立模式则直接使用本地会话"""
    os.makedirs(os.path.dirname(SESS), exist_ok=True)
    if not os.path.exists(REPO_SESSION):
        return  # 独立模式: 本地 session 已有则复用, 没有则走登录流程
    for suffix in ('', '-journal', '-wal', '-shm'):
        s = REPO_SESSION + suffix
        d = SESS + suffix
        if os.path.exists(d):
            try:
                os.remove(d)
            except OSError:
                pass
        if os.path.exists(s):
            try:
                shutil.copy(s, d)
            except OSError as e:
                print(f'复制会话失败({e})。请先完全退出搬运机器人再运行本工具。')
                sys.exit(1)


async def interactive_login(client):
    """首次运行的交互登录(手机号+验证码, 支持两步验证)"""
    from telethon.errors import SessionPasswordNeededError
    print('\n需要登录 Telegram 账号(只此一次, 登录状态会保存在本地)')
    phone = input('手机号(带国家区号, 如 +8613800138000): ').strip()
    try:
        await client.send_code_request(phone)
    except Exception as e:
        print(f'发送验证码失败: {type(e).__name__}: {str(e)[:120]}')
        return False
    code = input('验证码(Telegram 应用里收到的数字): ').strip()
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        pwd = input('该账号开启了两步验证, 请输入密码: ').strip()
        try:
            await client.sign_in(password=pwd)
        except Exception as e:
            print(f'两步验证失败: {type(e).__name__}: {str(e)[:120]}')
            return False
    except Exception as e:
        print(f'登录失败: {type(e).__name__}: {str(e)[:120]}')
        return False
    print('登录成功!')
    return True


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(s)).strip() or 'media'


def photo_bytes(m):
    """照片原图字节数(photo 的 msg.file.size 通常为空)"""
    try:
        sizes = sorted(s.size for s in m.photo.sizes if getattr(s, 'size', 0))
        return sizes[-1] if sizes else 0
    except Exception:
        return 0


def media_kind(m):
    """返回 'photo' / 'video' / 'gif' / None"""
    if not m.media:
        return None
    if isinstance(m.media, types.MessageMediaPhoto):
        return 'photo'
    if isinstance(m.media, types.MessageMediaDocument):
        doc = m.media.document
        for a in doc.attributes:
            if isinstance(a, types.DocumentAttributeVideo):
                return 'video'
            if isinstance(a, types.DocumentAttributeAnimated):
                return 'gif'
        mt = getattr(doc, 'mime_type', '') or ''
        if mt.startswith('video/'):
            return 'video'
        if mt.startswith('image/') and mt != 'image/webp':
            return 'photo'
    return None


def target_ext(m, kind):
    """按消息内容推导目标文件扩展名"""
    if kind == 'photo':
        name = getattr(m.file, 'name', '') or ''
        ext = os.path.splitext(name)[1]
        if ext and len(ext) <= 5:
            return ext.lower()
        return '.jpg'
    name = getattr(m.file, 'name', '') or ''
    ext = os.path.splitext(name)[1]
    if ext and len(ext) <= 5:
        return ext.lower()
    doc = getattr(m.media, 'document', None)
    mt = (getattr(doc, 'mime_type', '') or '') if doc else ''
    ext = mimetypes.guess_extension(mt) if mt else ''
    if not ext or len(ext) > 5:
        ext = '.gif' if kind == 'gif' else '.mp4'
    return ext.lower()


def resolve_entity_text(text):
    """把用户输入(链接/@用户名/数字ID)规范成 get_entity 可用的参数"""
    text = text.strip().split()[0]
    if not text:
        raise ValueError('为空')
    if re.fullmatch(r'-?\d{6,}', text):
        return int(text)
    m = re.search(r't\.me/([A-Za-z0-9_]+)', text)
    if m:
        name = m.group(1)
        if name == 'c' or name.startswith('c/'):
            m2 = re.search(r'/c/(\d+)', text)
            if m2:
                return int(m2.group(1)) + 1000000000000
        if re.fullmatch(r'\d+', name):
            return int(name) + 1000000000000
        return name
    if text.startswith('@'):
        return text[1:]
    # 裸用户名(如配置里存的 EN54188): 3+ 位字母数字下划线且非纯数字
    if re.fullmatch(r'[A-Za-z0-9_]{3,}', text) and not re.fullmatch(r'-?\d+', text):
        return text
    raise ValueError('无法识别的链接或ID')


def parse_date(s, default_end=False):
    """解析 YYYY-MM-DD (允许留空)"""
    s = (s or '').strip()
    if not s:
        return None
    import datetime
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y%m%d'):
        try:
            d = datetime.datetime.strptime(s, fmt)
            return d
        except ValueError:
            continue
    raise ValueError(f'日期格式不对: {s} (示例 2026-01-01)')


def resolve_link(text):
    """解析 t.me 链接 → (entity_id, message_id)

    支持格式:
      https://t.me/channel/123
      https://t.me/channel/post/123
      https://t.me/username/456
      https://t.me/c/123456/789  (私聊频道)
      t.me/channel/789
    """
    import re as _re
    text = text.strip()
    # 提取 t.me 域名后的部分
    m = _re.search(r't\.me[/?](.+)', text)
    if not m:
        raise ValueError('不是有效的 t.me 链接')
    path = m.group(1).split('?')[0].strip('/')
    parts = path.split('/')
    if len(parts) < 2:
        raise ValueError('链接格式不完整, 需要包含频道和消息ID')
    # 处理 c/ID 格式 (私聊频道)
    if parts[0] == 'c' and len(parts) >= 2:
        entity = int(parts[1]) + 1000000000000
        msg_str = parts[-1]
    else:
        entity = parts[0]
        msg_str = parts[-1]
    if not _re.fullmatch(r'\d+', msg_str):
        raise ValueError(f'无法从链接提取消息ID: {msg_str}')
    msg_id = int(msg_str)
    # entity 可能是 @用户名 或数值ID
    if isinstance(entity, str) and entity.startswith('@'):
        entity = entity[1:]
    return entity, msg_id


def ask_config():
    """首次运行或用户要求更改时, 交互收集目标/日期/输出目录, 存入 config.json"""
    import datetime
    cfg = {}
    if os.path.exists(MY_CONFIG):
        try:
            cfg = json.load(open(MY_CONFIG, encoding='utf-8'))
        except Exception:
            cfg = {}

    print('=' * 56)
    print('  Telegram 频道/群聊 视频图片下载工具')
    print('=' * 56)
    api_id, _, _ = load_credentials()
    if not api_id:
        print('! 未检测到 API 凭证: 本机没有搬运机器人时,')
        print('! 请先编辑 config.json 填入 api_id / api_hash')
        print('! (https://my.telegram.org 申请, 详见 README)\n')
    cur = cfg.get('target', '')
    mode = cfg.get('mode', 'date')
    print(f'当前目标: {cur or "(未设置)"}  |  当前模式: {mode}')
    if input('直接回车用当前配置, 输入 y 重新设置: ').strip().lower() == 'y':
        cfg = {}

    # 模式选择
    if 'mode' not in cfg:
        print('\n[下载模式]')
        print('  1 - 按日期范围 (原有模式)')
        print('  2 - 从链接到最新  (单条消息链接 → 频道最新)')
        print('  3 - 从链接到链接  (两条消息链接区间)')
        print('  4 - 单条链接      (只下指定消息)')
        while True:
            try:
                ch = input('\n选择模式 [1-4] (直接回车=1): ').strip() or '1'
                if ch in ('1', '2', '3', '4'):
                    cfg['mode'] = ch
                    break
                print('  请输入 1-4')
            except EOFError:
                cfg['mode'] = '1'
                break

    # --- 日期范围模式 (mode=1) ---
    if cfg.get('mode') == '1':
        if 'target' not in cfg or not cfg['target']:
            while True:
                txt = input('\n请输入频道/群聊链接或ID\n(t.me/xxx 或 -100xxxx 或 @用户名): ').strip()
                try:
                    cfg['target'] = str(resolve_entity_text(txt))
                    break
                except ValueError as e:
                    print(f'  {e}, 请重新输入')

        if 'start_date' not in cfg or 'end_date' not in cfg:
            today = datetime.date.today().isoformat()
            print('\n日期范围 (直接回车表示不限制)')
            while True:
                try:
                    sd = parse_date(input(f'开始日期(含, 示例 2026-01-01) [{cfg.get("start_date","")}]: ')) or None
                    break
                except ValueError as e:
                    print(f'  {e}')
            while True:
                try:
                    ed = parse_date(input(f'结束日期(含) [{cfg.get("end_date", today)}]: ')) or None
                    break
                except ValueError as e:
                    print(f'  {e}')
            if sd and ed and sd > ed:
                print('  开始日期晚于结束日期, 已交换')
                sd, ed = ed, sd
            cfg['start_date'] = sd.isoformat() if sd else ''
            cfg['end_date'] = ed.isoformat() if ed else ''

    # --- 链接→最新模式 (mode=2) ---
    elif cfg.get('mode') == '2':
        print('\n[链接→最新] 从指定消息开始下载, 直到频道最新')
        if 'target_link' not in cfg or not cfg['target_link']:
            while True:
                txt = input('起始消息链接 (t.me/.../编号): ').strip()
                try:
                    cfg['target_link'] = txt
                    break
                except ValueError as e:
                    print(f'  {e}, 请重新输入')
        if 'target' not in cfg or not cfg['target']:
            # 从链接提取实体
            try:
                ent_id, _ = resolve_link(cfg['target_link'])
                cfg['target'] = str(ent_id)
            except ValueError as e:
                print(f'  无法从链接解析实体: {e}')
                while True:
                    txt = input('或手动输入频道/群聊链接或ID: ').strip()
                    try:
                        cfg['target'] = str(resolve_entity_text(txt))
                        break
                    except ValueError as ex:
                        print(f'  {ex}, 请重新输入')
        if 'out_dir' not in cfg or not cfg['out_dir']:
            cfg['out_dir'] = input(f'\n保存目录 [downloads]: ').strip() or 'downloads'

    # --- 链接→链接模式 (mode=3) ---
    elif cfg.get('mode') == '3':
        print('\n[链接→链接] 下载两条消息之间的所有媒体')
        if 'start_link' not in cfg or not cfg['start_link']:
            while True:
                txt = input('起始消息链接 (t.me/.../编号): ').strip()
                try:
                    cfg['start_link'] = txt
                    break
                except ValueError as e:
                    print(f'  {e}, 请重新输入')
        if 'end_link' not in cfg or not cfg['end_link']:
            while True:
                txt = input('结束消息链接 (t.me/.../编号): ').strip()
                try:
                    cfg['end_link'] = txt
                    break
                except ValueError as e:
                    print(f'  {e}, 请重新输入')
        if 'target' not in cfg or not cfg['target']:
            try:
                ent_id, _ = resolve_link(cfg['start_link'])
                cfg['target'] = str(ent_id)
            except ValueError as e:
                print(f'  无法从起始链接解析实体: {e}')
                while True:
                    txt = input('或手动输入频道/群聊链接或ID: ').strip()
                    try:
                        cfg['target'] = str(resolve_entity_text(txt))
                        break
                    except ValueError as ex:
                        print(f'  {ex}, 请重新输入')
        if 'out_dir' not in cfg or not cfg['out_dir']:
            cfg['out_dir'] = input(f'\n保存目录 [downloads]: ').strip() or 'downloads'

    # --- 单条链接模式 (mode=4) ---
    elif cfg.get('mode') == '4':
        print('\n[单条链接] 只下载指定的一条消息')
        if 'target_link' not in cfg or not cfg['target_link']:
            while True:
                txt = input('消息链接 (t.me/.../编号): ').strip()
                try:
                    cfg['target_link'] = txt
                    break
                except ValueError as e:
                    print(f'  {e}, 请重新输入')
        if 'target' not in cfg or not cfg['target']:
            try:
                ent_id, _ = resolve_link(cfg['target_link'])
                cfg['target'] = str(ent_id)
            except ValueError as e:
                print(f'  无法从链接解析实体: {e}')
                while True:
                    txt = input('或手动输入频道/群聊链接或ID: ').strip()
                    try:
                        cfg['target'] = str(resolve_entity_text(txt))
                        break
                    except ValueError as ex:
                        print(f'  {ex}, 请重新输入')
        if 'out_dir' not in cfg or not cfg['out_dir']:
            cfg['out_dir'] = input(f'\n保存目录 [downloads]: ').strip() or 'downloads'

    if 'out_dir' not in cfg and cfg.get('mode') == '1':
        cfg['out_dir'] = input(f'\n保存目录 [downloads]: ').strip() or 'downloads'

    json.dump(cfg, open(MY_CONFIG, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=2)
    print(f'\n配置已保存到 config.json\n')
    return cfg


def to_dt(iso, end_of_day=False, fallback=None):
    """配置里的日期字符串 → datetime(兼容 '2026-01-01' 和 '2026-01-01T00:00:00')"""
    import datetime
    if not iso:
        return fallback
    d = datetime.datetime.strptime(str(iso)[:10], '%Y-%m-%d')
    if end_of_day:
        d = d.replace(hour=23, minute=59, second=59)
    return d


async def collect(client, ent, start_dt, end_dt):
    """扫描频道在 [start_dt, end_dt] 内的视频/图片消息, 返回旧→新排序的列表

    iter_messages(offset_date=end次日) 从结束日期位置往回扫,
    遇到 date < start_dt 立即 break —— 不会把整个频道历史拉下来。
    """
    import datetime
    items = []
    until = (end_dt + datetime.timedelta(days=1)
             if end_dt else datetime.datetime.now(datetime.timezone.utc))
    async for m in client.iter_messages(ent, offset_date=until):
        if m.action is not None:
            continue
        if start_dt and (m.date.replace(tzinfo=None) if m.date.tzinfo else m.date) < start_dt:
            break
        if m.media and media_kind(m):
            items.append(m)
    items.reverse()  # 旧 → 新
    return items


async def collect_from_link_to_latest(client, ent, start_msg_id):
    """从指定消息ID开始, 下载到该频道最新 (mode=2)"""
    items = []
    async for m in client.iter_messages(ent, reverse=True, offset_id=start_msg_id):
        if m.action is not None:
            continue
        if m.media and media_kind(m):
            items.append(m)
    items.reverse()  # 旧 → 新 (从起始消息到最新)
    return items


async def collect_from_link_to_link(client, ent, start_msg_id, end_msg_id):
    """在两个消息ID之间扫描媒体 (mode=3)

    注意: iter_messages 从前往后扫, start < end 时直接用;
    若 start > end (用户填反了), 自动交换。
    """
    import datetime
    # 确保 start <= end
    if start_msg_id > end_msg_id:
        start_msg_id, end_msg_id = end_msg_id, start_msg_id
    items = []
    # 使用 start_msg_id 作为 offset, 向前扫到 end_msg_id
    async for m in client.iter_messages(ent, reverse=True, offset_id=start_msg_id):
        if m.id <= end_msg_id:
            break
        if m.action is not None:
            continue
        if m.media and media_kind(m):
            items.append(m)
    items.reverse()  # 旧 → 新
    return items


class PauseManager:
    """后台线程监听键盘: P=暂停 R=继续 Q=退出(当前文件完成后停)"""
    def __init__(self):
        self.paused = threading.Event()
        self.quit = threading.Event()

    def start(self):
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        while not self.quit.is_set():
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getch().decode('latin-1', errors='ignore').lower()
                    if ch == 'p':
                        self.paused.set()
                        print('\n[⏸ 已请求暂停] 等待当前下载点暂停 (按 R 继续, Q 结束)')
                    elif ch == 'r':
                        self.paused.clear()
                        print('\n[▶ 继续] 恢复下载')
                    elif ch == 'q':
                        self.quit.set()
                        print('\n[⏹ 已请求退出] 将在暂停点停止 (已下载文件保留)')
            except Exception:
                pass
            time.sleep(0.15)


class DlStat:
    """单文件下载进度统计 + 停滞检测"""
    def __init__(self, msg_id):
        self.msg_id = msg_id
        self.got = 0
        self.t0 = time.time()
        self.last_bytes = 0
        self.last_ts = self.t0

    def feed(self, current, total):
        now = time.time()
        self.got = current
        self.last_bytes = current
        self.last_ts = now
        if total and now - self.t0 >= 5:
            speed = current / max(1e-9, now - self.t0) / 1048576
            log(f'[下载] #{self.msg_id} 进度 {current / 1048576:.1f}/{total / 1048576:.1f}MB ({speed:.1f} MB/s)')
            self.t0 = now

    def check_stall(self):
        """90 秒无任何进度 → 视为连接僵死"""
        if time.time() - self.last_ts > 90:
            raise TimeoutError(f'#{self.msg_id} 下载停滞 {time.time() - self.last_ts:.0f}s, 判定连接僵死')


async def parallel_download(client, m, target, size):
    """大文件(>4MB, 大小已知): 8MB 分片 x4 连接并行下载, 分片落盘支持续传"""
    ranges = [(off, min(PART, size - off)) for off in range(0, size, PART)]
    if size > 4 * 1024 * 1024 * 1024:
        raise IOError(f'文件 {size / 1073741824:.1f}GB 超过 Telegram 单文件上限')
    # 并发由下方 Semaphore(CONCURRENT) 控制, 分片再多也只是排队, 无副作用

    sem = asyncio.Semaphore(CONCURRENT)
    st = DlStat(m.id)
    seg_files = []
    timers = [time.time()]  # 进度日志节流

    def progress_hook():
        now = time.time()
        if now - timers[0] >= 5:
            done = sum(os.path.getsize(f) for f in seg_files) if seg_files else 0
            speed = done / max(1e-9, now - st.t0) / 1048576
            log(f'[下载] #{m.id} 已落盘 {done / 1048576:.1f}/{size / 1048576:.1f}MB ({speed:.1f} MB/s)')
            timers[0] = now

    async def grab(off, ln, idx):
        seg = f'{target}.part{idx}'
        # 分片级续传: 已完整落盘的片直接复用
        if os.path.exists(seg) and os.path.getsize(seg) == ln:
            seg_files.append(seg)
            return seg
        got = 0
        async with sem:
            it = client.iter_download(m.media, offset=off,
                                      request_size=512 * 1024,
                                      file_size=size).__aiter__()
            with open(seg, 'wb') as f:
                while True:
                    try:
                        chunk = await asyncio.wait_for(it.__anext__(), timeout=90)
                    except StopAsyncIteration:
                        break
                    st.feed(st.got + len(chunk), size)
                    take = min(len(chunk), ln - got)
                    if take > 0:
                        f.write(chunk[:take])
                        got += take
                    if got >= ln:
                        break
        if got < ln:
            raise IOError(f'#{m.id} 分片{idx} 不完整: {got}/{ln}')
        seg_files.append(seg)
        progress_hook()
        return seg

    success = False
    try:
        results = await asyncio.gather(
            *(grab(off, ln, i) for i, (off, ln) in enumerate(ranges)))
        # 按序拼接
        with open(target, 'wb') as out:
            for seg in results:
                with open(seg, 'rb') as f:
                    shutil.copyfileobj(f, out, 1024 * 1024)
        if os.path.getsize(target) != size:
            raise IOError(f'#{m.id} 拼接后大小不符: {os.path.getsize(target)}/{size}')
        success = True
    finally:
        # 只有完整成功才清理分片; 取消/失败保留 .partN 供断点续传
        if success:
            for i in range(len(ranges)):
                p = f'{target}.part{i}'
                try:
                    os.remove(p)
                except OSError:
                    pass
    return target


async def simple_download(client, m, target, size):
    """小文件/未知大小: 普通 download_media + 停滞检测"""
    st = DlStat(m.id)

    def prog(current, total):
        st.feed(current, total)

    async def _dl():
        await client.download_media(m, file=target, progress_callback=prog)

    try:
        await asyncio.wait_for(_dl(), timeout=1800)
    except asyncio.TimeoutError:
        raise TimeoutError(f'#{m.id} 下载超总预算 30 分钟')

    if os.path.exists(target) and size and os.path.getsize(target) != size:
        raise IOError(f'#{m.id} 大小不符: {os.path.getsize(target)}/{size}')
    return target


async def download_with_retry(client, m, target, kind):
    """单条消息下载 + 指数退避重试; 返回 'ok'/'skip'/'fail'"""
    size = getattr(m.file, 'size', 0) or photo_bytes(m) or 0

    # 断点续传: 文件已存在且大小一致 → 跳过
    if os.path.exists(target):
        if size and os.path.getsize(target) == size:
            log(f'[跳过] #{m.id} 已存在 ({size / 1048576:.1f}MB)')
            return 'skip'
        try:
            os.remove(target)
        except OSError:
            pass
    # 注: .partN 分片不清除 —— 交给 parallel_download 内部按片核验复用

    delays = [3, 6, 12, 24]
    total = size / 1048576 if size else 0
    name = os.path.basename(target)
    log(f'[下载] #{m.id} {name} ({total:.1f}MB)...')

    for attempt in range(RETRIES):
        try:
            # 分片并行阈值: 默认 >4MB, 可用环境变量 TGDL_PARALLEL_MB 覆盖
            parallel_mb = float(os.environ.get('TGDL_PARALLEL_MB', '4'))
            if size > parallel_mb * 1048576:
                await asyncio.wait_for(
                    parallel_download(client, m, target, size), timeout=2400)
            else:
                await asyncio.wait_for(
                    simple_download(client, m, target, size), timeout=1800)
            log(f'[完成] #{m.id} {name}')
            return 'ok'
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if attempt == RETRIES - 1:
                log(f'[失败] #{m.id} {type(e).__name__}: {str(e)[:120]} (重试耗尽)')
                return 'fail'
            wait = delays[attempt]
            log(f'[重试] #{m.id} {type(e).__name__}: {str(e)[:100]} → {wait}s 后第 {attempt + 2} 次')
            await asyncio.sleep(wait)
    return 'fail'


def targets_for(m, kind, out_root):
    """计算消息的文件路径列表(相册成员各一条消息, 天然独立)"""
    title = out_root
    os.makedirs(out_root, exist_ok=True)
    name = getattr(m.file, 'name', '') or ''
    ext = target_ext(m, kind)
    if name and os.path.splitext(name)[1]:
        ext = ''  # 原文件名自带扩展名, 不再追加
    base = f'{m.id}_{safe_name(name)}' if name else f'{m.id}'
    return os.path.join(out_root, base + ext)


async def main():
    cfg = ask_config()
    target = cfg['target']
    mode = cfg.get('mode', '1')
    out_root = os.path.normpath(os.path.join(BASE_DIR, cfg.get('out_dir', 'downloads')))

    api_id, api_hash, proxy = load_credentials()
    if not api_id or not api_hash:
        print('未找到 Telegram API 凭证:')
        print('  - 本机装有搬运机器人时自动复用, 无需设置')
        print('  - 独立使用请在 config.json 填入 api_id 和 api_hash')
        print('    (前往 https://my.telegram.org 申请, 见 README)')
        sys.exit(1)
    log(f'代理: {proxy if proxy else "直连"}')

    prepare_session()
    client = TelegramClient(SESS, api_id, api_hash, proxy=proxy,
                            timeout=30, request_retries=5, connection_retries=10)
    await client.connect()
    if not await client.is_user_authorized():
        if not await interactive_login(client):
            print('登录未完成, 已退出。')
            await client.disconnect()
            sys.exit(1)

    try:
        ent = await client.get_entity(resolve_entity_text(target))
    except Exception as e:
        print(f'解析频道失败: {type(e).__name__}: {str(e)[:150]}')
        print('请确认链接正确且账号已加入该频道/群聊(可在搬运机器人里用 登录并列出频道ID.bat 查看)')
        await client.disconnect()
        sys.exit(1)

    title = safe_name(getattr(ent, 'title', None) or str(ent.id))
    media_dir = os.path.join(out_root, title)
    os.makedirs(media_dir, exist_ok=True)

    # 根据不同模式收集消息
    items = []
    if mode == '1':
        # 日期范围模式
        start_dt = to_dt(cfg.get('start_date', ''))
        end_dt = to_dt(cfg.get('end_date', ''), end_of_day=True)
        print(f'\n目标: {title} (ID {ent.id})')
        print(f'范围: {cfg.get("start_date") or "不限"} ~ {cfg.get("end_date") or "今天"}')
        print('正在扫描频道消息(只拉日期范围内, 稍候)...\n')
        items = await collect(client, ent, start_dt, end_dt)

    elif mode == '2':
        # 链接→最新模式
        try:
            _, start_msg_id = resolve_link(cfg['target_link'])
        except ValueError as e:
            print(f'链接解析失败: {e}')
            await client.disconnect()
            sys.exit(1)
        print(f'\n目标: {title} (ID {ent.id})')
        print(f'起始消息: {cfg["target_link"]}')
        print('正在扫描从该消息到最新, 稍候...\n')
        items = await collect_from_link_to_latest(client, ent, start_msg_id)

    elif mode == '3':
        # 链接→链接模式
        try:
            _, start_msg_id = resolve_link(cfg['start_link'])
            _, end_msg_id = resolve_link(cfg['end_link'])
        except ValueError as e:
            print(f'链接解析失败: {e}')
            await client.disconnect()
            sys.exit(1)
        display_start = cfg['start_link'].split('/')[-1] if '/' in cfg['start_link'] else cfg['start_link']
        display_end = cfg['end_link'].split('/')[-1] if '/' in cfg['end_link'] else cfg['end_link']
        print(f'\n目标: {title} (ID {ent.id})')
        print(f'范围: 消息 #{display_start} ~ #{display_end}')
        print('正在扫描频道消息(稍候)...\n')
        items = await collect_from_link_to_link(client, ent, start_msg_id, end_msg_id)

    elif mode == '4':
        # 单条链接模式
        try:
            _, msg_id = resolve_link(cfg['target_link'])
        except ValueError as e:
            print(f'链接解析失败: {e}')
            await client.disconnect()
            sys.exit(1)
        print(f'\n目标: {title} (ID {ent.id})')
        print(f'消息: {cfg["target_link"]}')
        print('正在获取消息...\n')
        try:
            m = await client.get_messages(ent, ids=msg_id)
            if m and m.media and media_kind(m):
                items = [m]
            else:
                print('该消息没有视频或图片媒体。')
                await client.disconnect()
                return
        except Exception as e:
            print(f'获取消息失败: {type(e).__name__}: {str(e)[:150]}')
            await client.disconnect()
            sys.exit(1)

    if not items:
        print('没有找到符合条件的媒体消息。')
        await client.disconnect()
        return

    kinds = [media_kind(m) for m in items]
    total_bytes = sum(getattr(m.file, 'size', 0) or photo_bytes(m) or 0 for m in items)
    print(f'共扫描到 {len(items)} 条媒体消息, 合计 {total_bytes / 1073741824:.2f}GB')
    print(f'保存到: {media_dir}\n')

    ok = skip = fail = 0
    fail_list = []
    pm = PauseManager()
    pm.start()
    print('[按键] 下载中随时可按:  P=暂停   R=继续   Q=结束(已下载的保留, 重跑续传)\n')
    try:
        for i, (m, kind) in enumerate(zip(items, kinds), 1):
            if pm.quit.is_set():
                break
            tgt = targets_for(m, kind, media_dir)
            print(f'--- [{i}/{len(items)}] 消息 #{m.id} ({m.date.strftime("%m-%d %H:%M")}) ---')
            dl = asyncio.create_task(download_with_retry(client, m, tgt, kind))
            while not dl.done():
                if pm.paused.is_set():
                    dl.cancel()
                    try:
                        await dl
                    except asyncio.CancelledError:
                        pass
                    print(f'[已暂停] #{m.id} 下载已停止 (分片保留, 恢复后自动续传)')
                    while pm.paused.is_set() and not pm.quit.is_set():
                        await asyncio.sleep(0.3)
                    if pm.quit.is_set():
                        break
                    print(f'[恢复] 继续 #{m.id} ...')
                    dl = asyncio.create_task(download_with_retry(client, m, tgt, kind))
                    continue
                await asyncio.sleep(0.2)
            if pm.quit.is_set():
                if not dl.done():
                    dl.cancel()
                    try:
                        await dl
                    except asyncio.CancelledError:
                        pass
                break
            try:
                r = dl.result()
            except asyncio.CancelledError:
                r = 'fail'
            except Exception as e:
                r = 'fail'
                log(f'[失败] #{m.id} 意外异常: {type(e).__name__}: {str(e)[:120]}')
            if r == 'ok':
                ok += 1
            elif r == 'skip':
                skip += 1
            else:
                fail += 1
                fail_list.append(f'#{m.id} {m.date.isoformat()}')
    except KeyboardInterrupt:
        print('\n\n用户中断(Ctrl+C), 已下载的文件保留, 重跑自动续传。')

    if fail_list:
        fp = os.path.join(BASE_DIR, '失败清单.txt')
        with open(fp, 'a', encoding='utf-8') as f:
            f.write(f'\n--- {time.strftime("%Y-%m-%d %H:%M:%S")} 目标 {title} ---\n')
            f.write('\n'.join(fail_list) + '\n')
        log(f'失败 {fail} 条, 已记录到 {fp}')

    print('\n' + '=' * 56)
    print(f'  下载完成: 新下载 {ok}  |  跳过(已存在) {skip}  |  失败 {fail}')
    print(f'  保存位置: {media_dir}')
    print('=' * 56)
    print('关闭窗口即可退出。')
    await client.disconnect()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n已退出。')