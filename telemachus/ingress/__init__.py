"""Telemachus Ingress Decision Gate and Pruning Proxy.

Sits transparently between OpenCode / agent clients and local LLMs (LM Studio),
dynamically pruning 18+ MCP tool schemas down to only relevant tools using
Nomic Embed v1.5 Q8_0 and fast domain routing.
"""
from __future__ import annotations

from telemachus.ingress.proxy import IngressProxyServer, run_proxy, start_ingress_proxy_background
from telemachus.ingress.router import IngressToolRouter

__all__ = ["IngressToolRouter", "IngressProxyServer", "run_proxy", "start_ingress_proxy_background"]
