# -*- coding: utf-8 -*-
"""快速验证: 解析 EN54188 频道实体"""
import asyncio
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import downloader as D
from telethon import TelegramClient


async def main():
    cfg = json.load(open(D.MY_CONFIG, encoding='utf-8'))
    rcfg = json.load(open(D.REPO_CFG, encoding='utf-8'))
    proxy = D.resolve_proxy(rcfg)
    print('代理:', proxy if proxy else '直连')
    D.prepare_session()
    client = TelegramClient(D.SESS, int(rcfg['api_id']), rcfg['api_hash'],
                            proxy=proxy, timeout=30, request_retries=5,
                            connection_retries=10)
    await client.connect()
    if not await client.is_user_authorized():
        print('会话未授权')
        return
    txt = cfg.get('target', '')
    print('配置目标:', txt, '->解析为:', D.resolve_entity_text(txt))
    try:
        ent = await client.get_entity(D.resolve_entity_text(txt))
        print('解析成功:', getattr(ent, 'title', '?'), '| id =', ent.id,
              '| 用户名 =', getattr(ent, 'username', '?'))
    except Exception as e:
        print('解析失败:', type(e).__name__, ':', str(e)[:200])
    await client.disconnect()


if __name__ == '__main__':
    asyncio.run(main())