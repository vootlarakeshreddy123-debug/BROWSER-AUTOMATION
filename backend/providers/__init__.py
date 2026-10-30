"""Provider adapters (OpenAI, Anthropic Claude, xAI Grok) + the central registry.

Gemini and Ollama keep their implementations in services/ai_provider.py and are registered here too, so the
rest of NEXA talks to ONE registry instead of per-provider conditionals.
"""
