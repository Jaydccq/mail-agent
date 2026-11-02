"""Lark 长连接线程：lark-oapi 的 ws/client.py 全部用模块级变量 loop。"""

import asyncio
import threading

import lark_oapi as lark
import lark_oapi.ws.client as ws_client

from mailagent.channels.lark import _WsRunner
from mailagent.config import LarkConfig


def test_old_and_new_runner_each_keep_their_own_loop(monkeypatch):
    # 重建长连接时旧线程可能还没退出（SDK 拿长连接地址是不带超时的同步 requests.post）。
    # 以前新线程把模块级 loop 换掉，旧线程里 SDK 的 loop.create_task 就跨线程排到新线程的循环上。
    monkeypatch.setattr(ws_client, "loop", ws_client.loop)  # 测完恢复
    old_running, new_running, old_checked = threading.Event(), threading.Event(), threading.Event()
    same_loop: dict[str, bool] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def start(self) -> None:
            me = threading.current_thread().name

            async def body() -> None:
                if me == "old":
                    old_running.set()
                    new_running.wait(5)  # 新线程已经起来、在跑它自己的循环
                    task = ws_client.loop.create_task(asyncio.sleep(0))
                    same_loop[me] = task.get_loop() is asyncio.get_running_loop()
                    old_checked.set()
                else:
                    new_running.set()
                    old_checked.wait(5)

            ws_client.loop.run_until_complete(body())

    monkeypatch.setattr(lark.ws, "Client", FakeClient)
    cfg = LarkConfig(app_id="a", app_secret="b")
    old = _WsRunner(cfg, lark.FEISHU_DOMAIN, None, name="old")
    old.start()
    assert old_running.wait(5)
    new = _WsRunner(cfg, lark.FEISHU_DOMAIN, None, name="new")
    new.start()
    old.done.result(10)  # 线程收尾（关掉自己的循环）之后才完成；异常 = 线程里出错
    new.done.result(10)
    assert same_loop == {"old": True}
