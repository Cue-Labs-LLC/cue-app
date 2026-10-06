"""Instagram DM support agent services.

The agent answers from an org's published FAQs (sent near-verbatim). Questions it
can't ground in a FAQ — refunds, complaints, event specifics, etc. — are declined
(answered=False) and deferred to a human.
"""

from .agent import FaqAnswer, InstagramAgentError, answer_faq

__all__ = ['FaqAnswer', 'InstagramAgentError', 'answer_faq']
