"""VK Teams (Среда) platform adapter for Hermes Agent.

Ported from the OpenClaw vkteams channel plugin (TypeScript) to the
Hermes platform-plugin contract.  The OpenClaw plugin is untouched;
this is an independent Python implementation of the same VK Teams Bot API
integration (long polling, format-styles, edit/delete, typing).
"""

from .adapter import register

__all__ = ["register"]