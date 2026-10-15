"""Model router 2.0: which local model answers a request, and why.

* fast    - short everyday requests (AI_FAST_MODEL)
* normal  - everything else (AI_MODEL)
* smart   - explanations, writing, analysis, plans, multi-step tasks (AI_SMART_MODEL)
* coding  - code, errors, tests, builds, stack traces - or "fix it" while you're in an editor (AI_CODING_MODEL,
            else the smart model, else AI_MODEL)
* vision  - screens and images (VISION_MODEL; chosen by the vision tools)

A role without a configured, installed model falls back to the next one, and a model that fails at request time
falls back to AI_MODEL once (app/ai/manager.py); both are recorded and shown by "which model are you using".

Private requests (Private mode, or words like password/medical/bank) are marked ``sensitive``: they may only go to
an Ollama on this PC or your own network - never to an Ollama host on the internet.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

_CODING = re.compile(r"\b(?:code|coding|function|method|class|bug|debug|error|exception|stack\s*trace|traceback|"
                     r"compile|compil\w+|build\s+(?:is\s+|are\s+)?(?:fails?|failed|failing|error)|tests?\s+(?:are\s+|is\s+)?(?:fail\w*|pass\w*|broken)|failing\s+tests?|"
                     r"refactor|regex|sql\s+query|api|endpoint|null\s*pointer|segfault|typescript|javascript|python|"
                     r"php|java|golang|rust|c\+\+|c#|html|css|react|laravel|django|npm|pip|git\s+(?:merge|rebase|"
                     r"conflict)|syntax|variable|import\s+error)\b|```|\.(?:py|js|ts|tsx|php|java|go|rs|cpp|cs)\b", re.I)
_EDITOR_FOLLOW_UP = re.compile(r"^(?:fix\s+(?:it|this|that)|why\s+(?:is\s+)?(?:it|this)\s+(?:failing|broken|not\s+"
                               r"working)|what'?s\s+wrong(?:\s+here)?|make\s+it\s+work)\b", re.I)
_SENSITIVE = re.compile(r"\b(?:password|passcode|pin\s+code|bank|salary|medical|diagnos\w+|lawyer|legal\s+case|"
                        r"private|confidential|secret)\b", re.I)


@dataclass(frozen=True)
class Route:
    kind: str  # fast | normal | smart | coding
    reason: str
    sensitive: bool = False


def route(text: str, *, simple: bool, hard: bool, task: bool, in_editor: bool = False,
          private_mode: bool = False) -> Route:
    sensitive = private_mode or bool(_SENSITIVE.search(text or ""))
    command = len((text or "").split()) <= 6 and re.match(r"^(?:please\s+)?(?:open|close|launch|start|run|show|play|switch|minimi[sz]e|"
                                  r"maximi[sz]e|focus)\b", (text or "").strip(), re.I)
    if _CODING.search(text or "") and not command:  # "open VS Code" is a command, not a coding question
        return Route("coding", "a coding question", sensitive)
    if in_editor and _EDITOR_FOLLOW_UP.search((text or "").strip()):
        return Route("coding", "a follow-up while you're in your editor", sensitive)
    if hard or task:
        return Route("smart", "a task" if task else "an explanation / writing / analysis", sensitive)
    if simple:
        return Route("fast", "a short everyday request", sensitive)
    return Route("normal", "a normal request", sensitive)


def is_local_host(url: str) -> bool:
    """True for an Ollama on this PC or a private network (LAN, Tailscale, VPN) - not the public internet."""
    host = (urlparse(url if "://" in url else f"http://{url}").hostname or "").strip("[]")
    if host in ("localhost", ""):
        return True
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(host, None)]
        except OSError:
            return False
    shared = ipaddress.ip_network("100.64.0.0/10")  # Tailscale / carrier-grade NAT
    return all(a.is_loopback or a.is_private or a.is_link_local or (a.version == 4 and a in shared)
               for a in addresses)
