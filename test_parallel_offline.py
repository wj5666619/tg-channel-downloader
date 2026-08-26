# -*- coding: utf-8 -*-
"""parallel_download 全逻辑离线测试(不联网):
1. 正常分片下载 -> 拼接完整 + 分片清理
2. 中途取消 -> 分片保留(暂停核心)
3. 续传 -> 完整分片复用(不重新请求该 offset) + 拼接完整
"""
import asyncio
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import downloader as D

PART = D.PART  # 8MB
CHUNK = 512 * 1024
SIZE = 40 * 1024 * 1024  # 40MB -> 5 片 (8*4 + 4), 超过并发 4, 第 5 片排队


class FakeClient:
    """模拟 iter_download: 按 offset 产出数据, 记录每次请求, 可控速度"""
    def __init__(self, block_s=0.08):
        self.calls = []          # (offset, file_size)
        self.block_s = block_s

    def iter_download(self, media, offset=0, request_size=None, file_size=None):
        self.calls.append((offset, file_size))
        return self._gen(offset, file_size)

    async def _gen(self, offset, file_size):
        pos = offset
        while pos < file_size:
            await asyncio.sleep(self.block_s)
            n = min(CHUNK, file_size - pos)
            yield b'\xab' * n
            pos += n


async def step1_normal(client, msg, tgt):
    """正常下载: 5 片并行(4片并发+1排队), 拼接 40MB, 分片清理"""
    await D.parallel_download(client, msg, tgt, SIZE)
    assert os.path.getsize(tgt) == SIZE, f'拼接大小不符 {os.path.getsize(tgt)}/{SIZE}'
    left = [p for p in os.listdir(os.path.dirname(tgt)) if '.part' in p]
    assert not left, f'分片未清理: {left}'
    offsets = sorted(o for o, _ in client.calls)
    print(f'[1] 正常下载 OK: 拼接 {SIZE // 1048576}MB, 分片已清理')
    print(f'    请求的 offset: {offsets}')
    assert offsets == [0, PART, 2 * PART, 3 * PART, 4 * PART], f'offset 集合错误: {offsets}'


async def step2_cancel(client, msg, tgt):
    """中途取消: 4 片已完整 + 第5片半截 -> 全部保留, 不拼接"""
    client.calls.clear()
    task = asyncio.create_task(D.parallel_download(client, msg, tgt, SIZE))
    await asyncio.sleep(2.0)   # 前4片 1.28s 完成, 第5片 (2.0-1.28)/0.08=9块=4.5MB 半截
    task.cancel()
    try:
        await task
        ok_cancel = False
    except asyncio.CancelledError:
        ok_cancel = True
    print(f'[2] 取消触发: {ok_cancel}')
    assert ok_cancel, 'FAIL: 任务未被取消'
    parts = {}
    for name in os.listdir(os.path.dirname(tgt)):
        if '.part' in name:
            p = os.path.join(os.path.dirname(tgt), name)
            parts[name] = os.path.getsize(p)
    print(f'    取消后保留分片: {parts}')
    p0 = next((v for k, v in parts.items() if k.endswith('.part0')), 0)
    assert p0 == PART, f'FAIL: 完整分片0未保留: {parts}'
    assert not os.path.exists(tgt), 'FAIL: 取消后不应有拼接文件'
    print('    ✓ 完整分片 + 半截分片全部保留, 未拼接 —— 暂停-续传基础成立')
    return parts


async def step3_resume(client, msg, tgt, old_parts):
    """续传: 完整分片复用(不重复请求), 只补第5片, 拼接完整"""
    had_complete = sorted(k for k, v in old_parts.items() if v == PART)
    client.calls.clear()
    await D.parallel_download(client, msg, tgt, SIZE)
    assert os.path.getsize(tgt) == SIZE, f'续传拼接大小不符 {os.path.getsize(tgt)}/{SIZE}'
    offsets2 = sorted(o for o, _ in client.calls)
    print(f'[3] 续传完成: 请求的 offset = {offsets2}')
    print(f'    之前完整分片: {had_complete}; 续传只应请求缺失的第5片 offset=4*PART')
    assert offsets2 == [4 * PART], f'FAIL: 续传请求了多余的片: {offsets2}'
    left = [p for p in os.listdir(os.path.dirname(tgt)) if '.part' in p]
    assert not left, f'续传后分片未清理: {left}'
    print('    ✓ 完整分片零重复下载, 仅补缺失片, 拼接完整, 分片已清理')


async def main():
    tmp = tempfile.mkdtemp(prefix='dl_test_')
    try:
        msg = SimpleNamespace(id=99, media='fake-media')
        client = FakeClient()

        tgt1 = os.path.join(tmp, 'a.mp4')
        await step1_normal(client, msg, tgt1)
        assert client.calls, 'step1 无请求记录'

        tgt2 = os.path.join(tmp, 'b.mp4')
        parts = await step2_cancel(client, msg, tgt2)

        await step3_resume(client, msg, tgt2, parts)
        print('\n===== 分片下载/暂停/续传离线测试全部通过 =====')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    asyncio.run(main())