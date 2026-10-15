# -*- coding: utf-8 -*-
"""爬虫适配器注册表：统一入口，引擎可插拔切换"""
from __future__ import annotations

from typing import Dict, List, Type

from .base import CrawlerAdapter

_REGISTRY: Dict[str, Type[CrawlerAdapter]] = {}


def register(cls: Type[CrawlerAdapter]) -> Type[CrawlerAdapter]:
    _REGISTRY[cls.name] = cls
    return cls


def get_adapter(name: str) -> CrawlerAdapter:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(f"未知采集引擎: {name}，可用: {', '.join(sorted(_REGISTRY))}")


def list_adapters() -> List[CrawlerAdapter]:
    return [_REGISTRY[k]() for k in sorted(_REGISTRY)]


def adapter_names() -> List[str]:
    return sorted(_REGISTRY)


# 注册内置适配器（导入即注册）
from . import cortech_browser  # noqa: E402,F401
from . import cortech_spider  # noqa: E402,F401
from . import cortech_downloader  # noqa: E402,F401
from . import cortech_direct  # noqa: E402,F401
