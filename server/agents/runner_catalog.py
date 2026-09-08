"""External executable catalog; these names are never Account roles."""

# Noninteractive stdin entry points; ChatGPT requires an operator-provided integration.
KINDS = {
    "openclaw": ("openclaw", "agent", "exec", "--message-file", "-"),
    "hermes": ("hermes", "chat", "--query-file", "-", "--quiet", "--source", "agent-colab-runner"),
    "chatgpt": ("agent-colab-chatgpt",),
    "codex": ("codex", "exec", "-"),
    "claude": ("claude", "-p"),
    "claude-code": ("claude", "-p"),
    "gemini": ("gemini",),
}
DEFAULT_KIND = "codex"
