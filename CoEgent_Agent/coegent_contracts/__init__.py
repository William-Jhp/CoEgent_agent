"""Small shared robot contracts; no model or orchestration dependencies."""

from .config import RuntimeConfig
from .registry import SkillRegistry
from .state import StateContract

CONTRACT_VERSION = "coegent.contract.v1"
__all__ = ["CONTRACT_VERSION", "RuntimeConfig", "SkillRegistry", "StateContract"]
