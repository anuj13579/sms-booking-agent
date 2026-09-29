"""The five tools the model can call (D-001), as Pydantic models.

The JSON Schema the LLM sees is generated from these models, so the schema and the validation
can't drift apart. Two deliberate omissions (D-010): no tool takes a customer id, business id, or
phone number. Identity comes from the conversation on the server, so even a fully hijacked model
can only touch the texting customer's own bookings.

Descriptions are part of the prompt. Changing one changes the prompt version (see prompts.py).
"""

import copy
from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.availability import PartOfDay
from app.domain.enums import EscalationReason, Hazard, Urgency


class _Args(BaseModel):
    # Unknown keys are ignored rather than rejected: a model that adds a harmless extra field
    # shouldn't burn a step. The schema still advertises additionalProperties: false.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class GetAvailabilityArgs(_Args):
    service_type: str = Field(description="Service code from the business profile.")
    earliest_date: date | None = Field(
        default=None,
        description="First local date to search, YYYY-MM-DD, from the context dates. "
        "Omit to start today.",
    )
    latest_date: date | None = Field(
        default=None,
        description="Last local date to search, YYYY-MM-DD. Omit to search as far ahead as the "
        "business books.",
    )
    part_of_day: PartOfDay | None = Field(
        default=None, description="Only if the customer asked for mornings or afternoons."
    )


class CreateBookingArgs(_Args):
    service_type: str = Field(description="Service code from the business profile.")
    slot_id: str = Field(
        description="The slot_id of the window the customer chose, copied exactly from a "
        "get_availability or error result."
    )
    customer_name: str = Field(min_length=1, max_length=100)
    address: str = Field(
        min_length=3,
        max_length=200,
        description="Street address with town, e.g. 12 River St, Hoboken",
    )
    zip: str = Field(min_length=5, max_length=10, description="5-digit ZIP code.")
    problem_description: str = Field(
        min_length=2, max_length=500, description="The problem in a few words, for the technician."
    )
    urgency: Urgency = Field(
        default=Urgency.ROUTINE,
        description="urgent if the customer says so or is without heat, cooling, water, or "
        "power; otherwise routine.",
    )


class RescheduleBookingArgs(_Args):
    booking_ref: str = Field(min_length=1, max_length=20, description="Booking ref from context.")
    new_slot_id: str = Field(description="slot_id of the new window, copied exactly.")


class CancelBookingArgs(_Args):
    booking_ref: str = Field(min_length=1, max_length=20, description="Booking ref from context.")
    reason: str | None = Field(default=None, max_length=300, description="Why, if they said.")


class EscalateReason(StrEnum):
    """The reasons the model may give. ``system_error`` is raised by the server only."""

    EMERGENCY = "emergency"
    UPSET_CUSTOMER = "upset_customer"
    HUMAN_REQUESTED = "human_requested"
    UNCERTAIN = "uncertain"
    OUT_OF_SCOPE = "out_of_scope"
    PRICE_QUOTE = "price_quote"
    URGENT_NO_AVAILABILITY = "urgent_no_availability"

    def domain(self) -> EscalationReason:
        return EscalationReason(self.value)


class EscalateArgs(_Args):
    reason: EscalateReason
    summary: str = Field(
        min_length=1,
        max_length=500,
        description="One or two sentences for the owner: what the customer needs and why.",
    )
    hazard: Hazard | None = Field(
        default=None, description="Required when reason is emergency; 'other' if none fits."
    )


TOOLS: dict[str, tuple[type[_Args], str]] = {
    "get_availability": (
        GetAvailabilityArgs,
        "List open arrival windows for a service, earliest first (at most 6). Each window has a "
        "slot_id and a label to show the customer. Call this before offering any times.",
    ),
    "create_booking": (
        CreateBookingArgs,
        "Book a window the customer chose. Re-checks availability; if the window was just "
        "taken it returns slot_unavailable with alternatives. Safe to retry: a repeated call "
        "returns the existing booking.",
    ),
    "reschedule_booking": (
        RescheduleBookingArgs,
        "Move one of this customer's upcoming bookings to a new window.",
    ),
    "cancel_booking": (
        CancelBookingArgs,
        "Cancel one of this customer's upcoming bookings.",
    ),
    "escalate_to_owner": (
        EscalateArgs,
        "Bring in the owner. For emergency, upset_customer, human_requested, out_of_scope, and "
        "uncertain the owner takes over: the system texts the customer and you stop replying. "
        "For price_quote and urgent_no_availability the owner is notified and you carry on.",
    ),
}


def _simplify(schema: dict[str, Any]) -> dict[str, Any]:
    """Pydantic's schema -> the plain shape tool-calling models handle best: optional fields
    lose their ``anyOf [..., null]`` wrapper and leave ``required``, enums are inlined, titles
    and defaults go."""
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                target = copy.deepcopy(defs[node["$ref"].split("/")[-1]])
                target.pop("title", None)
                target.pop("description", None)
                merged = {**resolve(target), **{k: v for k, v in node.items() if k != "$ref"}}
                return resolve(merged)
            if "anyOf" in node:
                options = [o for o in node["anyOf"] if o != {"type": "null"}]
                if len(options) == 1:
                    rest = {k: v for k, v in node.items() if k != "anyOf"}
                    return resolve({**options[0], **rest})
            return {k: resolve(v) for k, v in node.items() if k not in ("title", "default")}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    result: dict[str, Any] = resolve(schema)
    result["additionalProperties"] = False
    return result


def tool_specs(service_codes: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Tool definitions in OpenAI format. With ``service_codes``, the ``service_type``
    arguments become an enum of this business's codes, so the model can't invent one."""
    specs = []
    for name, (model, description) in TOOLS.items():
        parameters = _simplify(model.model_json_schema())
        service = parameters["properties"].get("service_type")
        if service is not None and service_codes:
            service["enum"] = list(service_codes)
        specs.append(
            {
                "type": "function",
                "function": {"name": name, "description": description, "parameters": parameters},
            }
        )
    return specs
