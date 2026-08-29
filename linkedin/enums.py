from enum import StrEnum


class ProfileState(StrEnum):
    QUALIFIED = "Qualified"
    READY_TO_CONNECT = "Ready to Connect"
    PENDING = "Pending"
    CONNECTED = "Connected"
    # Terminal until a human acts: the bot has stopped writing and the deal
    # waits in the admin queue. Deliberately absent from the reconcile
    # active-states tuple — that omission is what keeps the bot silent and
    # makes the handoff alert fire exactly once.
    HANDOFF = "Handoff"
    COMPLETED = "Completed"
    FAILED = "Failed"

