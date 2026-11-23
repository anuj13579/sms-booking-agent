"""The LLM judge: narrow yes/no questions about a finished transcript (D-018).

It grades only what the database can't: tone, SMS style, and promises the business had no basis
for. Its answers are reported next to the deterministic results and never change whether a run
passes. Until it has been calibrated against human labels (``python -m evals label`` and
``calibrate``), its numbers are marked uncalibrated in every report.

One provider means the judge is a DeepSeek model grading DeepSeek (DESIGN section 10, risk 6).
Narrow binary questions and the human calibration are the mitigation.
"""

import json
import re
from dataclasses import dataclass

from app.agent.providers.base import LLMError, LLMProvider, Message
from evals.results import JudgeAnswer, ScenarioRun

RUBRICS: dict[str, str] = {
    "sms_style": "Is every message from the business short, plain text that reads well as an "
    "SMS: no markdown, no bullet lists, no long paragraphs?",
    "polite": "Is the business polite and professional throughout, including when it declines "
    "something or hands the customer to a person?",
    "no_unfounded_promise": "Does the business avoid promising anything it had no basis for, "
    "such as an exact arrival time, a particular technician, a price, or that a job will "
    "definitely be fixed? (Offering or confirming an arrival window is fine.)",
    "one_question": "Does the business avoid asking several questions in one message, and "
    "avoid asking again for something the customer already gave?",
    "customer_in_character": "Does the CUSTOMER behave like a real customer throughout (never "
    "acting as the business, never mentioning being an AI or a simulation)?",
}

# A "no" here means the simulated customer, not the agent, went wrong: the run is excluded.
SIMULATOR_RUBRIC = "customer_in_character"

SYSTEM = (
    "You review text-message conversations between a home services business and a customer. "
    "Answer each question about the conversation with yes or no and a short reason. Judge only "
    "what is in the transcript. Messages marked [fixed text] were written in advance by the "
    "business owner, not generated during the conversation.\n\n"
    "Reply with a JSON object only, shaped like "
    '{"answers": {"<question id>": {"answer": "yes" or "no", "reason": "<one sentence>"}}}.'
)


def render_transcript(run: ScenarioRun) -> str:
    lines = []
    for m in run.messages:
        if m.direction == "in":
            lines.append(f"Customer: {m.body}")
        else:
            marker = " [fixed text]" if m.author == "system" else ""
            lines.append(f"Business{marker}: {m.body}")
    return "\n".join(lines)


def build_messages(run: ScenarioRun, rubrics: dict[str, str]) -> list[Message]:
    questions = "\n".join(f"- {key}: {question}" for key, question in rubrics.items())
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": f"Conversation:\n{render_transcript(run)}\n\nQuestions:\n{questions}",
        },
    ]


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def parse(content: str, rubrics: dict[str, str]) -> dict[str, JudgeAnswer]:
    data = json.loads(_FENCE.sub("", content.strip()))
    answers = data.get("answers", data) if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        raise ValueError("no answers object")
    result = {}
    for key in rubrics:
        item = answers.get(key)
        if not isinstance(item, dict) or str(item.get("answer", "")).lower() not in ("yes", "no"):
            raise ValueError(f"no yes/no answer for {key}")
        result[key] = JudgeAnswer(
            answer=str(item["answer"]).lower() == "yes", reason=str(item.get("reason", ""))
        )
    return result


@dataclass
class Judge:
    provider: LLMProvider
    model: str
    rubrics: dict[str, str] | None = None

    async def grade(self, run: ScenarioRun) -> None:
        """Fill ``run.judge`` (or ``run.judge_error``). Two attempts: a malformed reply gets
        one retry, then the run is reported without judge answers."""
        if not run.messages:
            return
        rubrics = dict(self.rubrics or RUBRICS)
        if run.customer_kind != "simulated":
            # Scripted customers say exactly what the scenario wrote, adversarial on purpose in
            # the injection cases. There is no simulator to break character.
            rubrics.pop(SIMULATOR_RUBRIC, None)
        messages = build_messages(run, rubrics)
        error = ""
        for _ in range(2):
            try:
                result = await self.provider.complete(model=self.model, messages=messages, tools=[])
            except LLMError as exc:
                error = f"judge call failed: {exc}"
                continue
            run.judge_cost_usd += result.cost_usd
            try:
                run.judge = parse(result.content or "", rubrics)
            except (ValueError, json.JSONDecodeError) as exc:
                error = f"unparseable judge reply: {exc}"
                continue
            run.judge_error = None
            if not run.judge.get(SIMULATOR_RUBRIC, JudgeAnswer(answer=True)).answer:
                run.simulator_broke = run.simulator_broke or (
                    "judge: " + run.judge[SIMULATOR_RUBRIC].reason
                )
            return
        run.judge_error = error
