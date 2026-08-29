# linkedin/agents/follow_up_defaults.py
"""Default copy for the follow-up agent's qualifying pivot.

Kept in a tiny, dependency-free module for the same reason as
``post_prompt_defaults``: a migration or an admin form can import it
without dragging in the LLM client stack.

``Campaign.qualifying_question`` is stored blank by default, so this
string is what every campaign actually uses until someone overrides it.
Editing it here reaches all accounts on the next image build — a
campaign row is only consulted when an operator deliberately filled it.
"""
from __future__ import annotations


# The one question that decides whether a lead is worth a human's time.
# Written as an intent, not a script: the agent is told to rewrite it in
# the lead's language and anchor it to what they just said.
DEFAULT_QUALIFYING_QUESTION = (
    "Как у них сейчас устроена работа с зарубежными поставщиками: ввозят "
    "оборудование и проводят платежи напрямую сами или привлекают "
    "специализированных подрядчиков под импорт и расчёты?"
)
