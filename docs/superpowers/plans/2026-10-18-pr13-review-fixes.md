# PR #13 review fixes

目标：修复两轮审查复现的六个问题，保留工作区已有的上传限制和失败清理。

按已确认的修复方向在当前工作区执行，不提交用户未提交的改动。

- [x] 在 `backend/tests/unit/test_api.py` 补充 500 CORS、traceparent 优先级及嵌套路由依赖检查的回归用例。
- [x] 在 `backend/tests/unit/test_resume.py` 补充数据库失败日志、缺失角色、PATCH 联系方式清理的回归用例。
- [x] 在 `backend/tests/unit/test_logging.py` 验证 JSON 和 console 异常链不输出消息、参数或局部变量，保留异常类型和栈位置。
- [x] 运行新增用例，确认分别因已复现的问题失败。
- [x] 日志使用安全异常渲染器；数据库默认隐藏 SQL 参数。
- [x] CORS 包裹完整 HTTP 处理（包含兜底错误），保留 FastAPI 工厂返回类型与生命周期。
- [x] 使用与 OpenTelemetry 一致的传播器解析上游上下文；有效上游 ID 优先于 X-Request-ID。
- [x] 使用已安装 FastAPI 的 `iter_route_contexts` 检查包含前缀和继承依赖的有效路由。
- [x] 项目角色缺失时保留项目；POST/PATCH 均使用同一联系方式清理函数。
- [x] 运行新增用例、完整 `make check`，检查最终差异与用户已有改动。

验证命令：`NO_PROXY='*' UV_CACHE_DIR=/tmp/interview-review-uv-cache make check UV='uv run --no-sync --directory backend'`。
测试只使用假厂商、本机临时服务和 SQLite，不调用真实厂商或 VPS 数据库。

完成验证：格式检查、ruff、pyright 与 329 个单元测试通过（23 个 live/db 测试未运行）；git diff --check 通过。新增回归用例覆盖六项修复以及兜底 CORS 和异常组安全日志。
