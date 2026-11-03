"""
LivingMemory核心模块
提供统一的记忆管理引擎

目录结构:
- base/: 基础组件（异常、配置、常量）
- models/: 数据模型
- managers/: 管理器（会话管理、记忆引擎）
- processors/: 处理器（记忆处理、文本处理）
- validators/: 验证器（索引验证）
- retrieval/: 检索系统
- utils/: 工具函数
"""

# 基础组件
from .base import (
    ConfigManager,
    ConfigurationError,
    InitializationError,
    LivingMemoryException,
    ProviderNotReadyError,
)

# Runtime engines are lazy: storage/model imports must not initialize providers
# or recurse through ConversationManager while ConversationStore is importing.
def __getattr__(name):
    import importlib
    groups = {
        "ConversationManager": "managers", "GraphMemoryManager": "managers", "MemoryEngine": "managers",
        "ChatroomContextParser": "processors", "EntityResolver": "processors", "GraphExtractor": "processors",
        "MemoryProcessor": "processors", "TextProcessor": "processors", "IndexValidator": "validators",
    }
    if name not in groups:
        raise AttributeError(name)
    value = getattr(importlib.import_module("."+groups[name],__name__),name)
    globals()[name] = value
    return value

# 数据模型
from .models import (
    ExtractedGraph,
    GraphEdge,
    GraphEntry,
    GraphNode,
    MemoryEvent,
    Message,
    Session,
)


__all__ = [
    # 基础组件
    "ConfigManager",
    "ConfigurationError",
    "InitializationError",
    "LivingMemoryException",
    "ProviderNotReadyError",
    # 数据模型
    "MemoryEvent",
    "Message",
    "Session",
    "GraphNode",
    "GraphEdge",
    "GraphEntry",
    "ExtractedGraph",
    # 管理器
    "ConversationManager",
    "GraphMemoryManager",
    "MemoryEngine",
    # 处理器
    "ChatroomContextParser",
    "EntityResolver",
    "GraphExtractor",
    "MemoryProcessor",
    "TextProcessor",
    # 验证器
    "IndexValidator",
]
