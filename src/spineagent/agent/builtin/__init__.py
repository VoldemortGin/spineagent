"""预置 agent:用既有原语纯组合装配的开箱即用 agent(不新造机制)。

deep_research —— DeepResearchAgent:planner 分解 + Coordinator 并行检索 + LlmAgent 综合。
"""

from spineagent.agent.builtin.deep_research import DeepResearchAgent, default_planner

__all__ = ["DeepResearchAgent", "default_planner"]
