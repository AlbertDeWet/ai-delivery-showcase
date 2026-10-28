"""SQL generation providers."""

from nl2sql.providers.base import Generation, Provider


def get_provider(name: str, tier: str | None = None, **kwargs) -> Provider:
    if name == "baseline":
        from nl2sql.providers.baseline import BaselineProvider

        return BaselineProvider()
    if name in ("claude-cli", "claude"):
        from nl2sql.providers.claude_cli import ClaudeCliProvider

        return ClaudeCliProvider(tier=tier or "haiku", **kwargs)
    raise ValueError(f"unknown provider: {name}")


__all__ = ["Generation", "Provider", "get_provider"]
