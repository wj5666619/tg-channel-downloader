# -*- coding: utf-8 -*-
"""真实链路验收: 会话复制 -> 连接 -> 解析频道 -> 扫描 -> 实下 1 条 -> 清理"""
import asyncio
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import downloader as D
from telethon import TelegramClient


async def main():
    rcfg = json.load(open(D.REPO_CFG, encoding='utf-8'))
    proxy = D.resolve_proxy(rcfg)
    print('代理:', proxy if proxy else '直连')
    D.prepare_session()
    client = TelegramClient(D.SESS, int(rcfg['api_id']), rcfg['api_hash'],
                            proxy=proxy, timeout=30, request_retries=5,
                            connection_retries=10)
    await client.connect()
    me = await client.get_me()
    print('登录账号 OK:', me.username or me.first_name, 'id=' + str(me.id))
    src = rcfg['pairs'][0]['source']
    ent = await client.get_entity(src)
    print('频道解析 OK:', getattr(ent, 'title', '?'), ent.id)

    chosen = None
    cnt = 0
    async for m in client.iter_messages(ent, limit=10):
        k = D.media_kind(m)
        if k:
            sz = getattr(m.file, 'size', 0) or D.photo_bytes(m)
            print(f'  #{m.id} {m.date.strftime("%Y-%m-%d")} {k} {sz / 1048576:.2f}MB')
            if chosen is None:
                chosen = (m, k)
            cnt += 1
    print('最近10条中媒体:', cnt, '条')

    if chosen:
        m, k = chosen
        print(f'\n实下 1 条验证: #{m.id} ({k})...')
        tmp = os.path.join(BASE, '_test_dl')
        os.makedirs(tmp, exist_ok=True)
        tgt = os.path.join(tmp, 'test' + D.target_ext(m, k))
        r = await D.download_with_retry(client, m, tgt, k)
        if os.path.exists(tgt):
            print('真实文件大小:', os.path.getsize(tgt), '字节 ===> 下载引擎 OK')
        print('结果:', r)
        shutil.rmtree(tmp, ignore_errors=True)
    await client.disconnect()
    print('\n===== 全链路验收完成 =====')


if __name__ == '__main__':
    asyncio.run(main())