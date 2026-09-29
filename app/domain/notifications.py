"""Owner alerts: the port, plus two adapters that need no external service.

The real adapter (an SMS to the owner's phone) arrives with Twilio in Phase 5. Alerts are sent
*after* the database transaction commits, so the owner is never paged about something that was
rolled back. The cost: a crash between commit and send loses the page, but not the escalation
row, which the owner dashboard lists.
"""

import logging
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

logger = logging.getLogger(__name__)


class AlertKind(StrEnum):
    ESCALATION = "escalation"  # a new escalation was raised
    MESSAGE_WHILE_HANDED_OFF = "message_while_handed_off"  # customer wrote; a human owns the chat
    MESSAGE_FROM_OPTED_OUT = "message_from_opted_out"  # we can't text them; the owner may call


@dataclass(frozen=True)
class OwnerAlert:
    kind: AlertKind
    business_id: uuid.UUID
    conversation_id: uuid.UUID
    text: str
    escalation_id: uuid.UUID | None = None


class OwnerNotifier(Protocol):
    async def notify(self, alert: OwnerAlert) -> None: ...


class LoggingNotifier:
    async def notify(self, alert: OwnerAlert) -> None:
        logger.warning(
            "owner alert [%s] conversation=%s: %s", alert.kind, alert.conversation_id, alert.text
        )


@dataclass
class RecordingNotifier:
    """Keeps alerts in memory. Used by tests, and by the eval harness to grade escalations."""

    alerts: list[OwnerAlert] = field(default_factory=list)

    async def notify(self, alert: OwnerAlert) -> None:
        self.alerts.append(alert)
