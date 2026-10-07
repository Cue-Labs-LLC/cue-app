"""Instagram DM support agent services.

A ReAct answer agent (``InstagramSupportAgentService``) drafts a customer-facing reply
grounded in the org's published FAQs and live public event data via a dedicated,
customer-safe tool set. A structured classifier (``classify_escalation``) then triages
the thread, and ``decide_autosend`` applies the groundedness + confidence gate that
decides whether the draft can be auto-sent or must be queued for a human.
"""

from .agent import AnswerResult, InstagramAgentError, InstagramSupportAgentService
from .classifier import EscalationDecision, classify_escalation, decide_autosend
from .inbound import NormalizedInbound, normalize_meta_payload, verify_meta_signature
from .sender import InstagramSender, SendResult, StubSender, get_sender
from .tools import build_ig_tools

__all__ = [
    'AnswerResult',
    'InstagramAgentError',
    'InstagramSupportAgentService',
    'EscalationDecision',
    'classify_escalation',
    'decide_autosend',
    'build_ig_tools',
    'NormalizedInbound',
    'normalize_meta_payload',
    'verify_meta_signature',
    'InstagramSender',
    'SendResult',
    'StubSender',
    'get_sender',
]
