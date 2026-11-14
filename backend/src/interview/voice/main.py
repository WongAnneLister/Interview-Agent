"""make worker 的入口：LiveKit CLI 在这个模块里找名为 server 的 AgentServer。

单独成文件是为了让 voice/worker.py 导入时不读 .env（challenges C12）。
"""

from interview.config import get_settings
from interview.voice.worker import build_server

server = build_server(get_settings())
