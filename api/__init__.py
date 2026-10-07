"""api —— HTTP 接口层（FastAPI）。

职责：只做「把 HTTP 请求翻译成对 `services/` 的调用、把异常翻译成可读 JSON」。
`app.py` 是本包唯一入口（`create_app()` 工厂），路由集中注册，不散落 `@app.get`。

为什么不含业务逻辑：等时圈 / POI / 盲区的判定口径要在 `services/` 里被单测直接调用，
一旦写进路由函数就只能起服务才能测，违背 PRD §5「算法层为纯函数」。
"""
