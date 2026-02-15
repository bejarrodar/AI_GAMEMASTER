from __future__ import annotations

from pydantic import BaseModel, Field

from aigm.adapters.llm import LLMAdapter
from aigm.schemas.game import AIResponse, WorldState


class AgentStep(BaseModel):
    name: str
    role: str
    instructions: str


class AgentCrewDefinition(BaseModel):
    name: str = "default_game_master_crew"
    steps: list[AgentStep] = Field(default_factory=list)


def default_agent_crew_definition() -> AgentCrewDefinition:
    return AgentCrewDefinition(
        steps=[
            AgentStep(
                name="planner",
                role="Scene Planner",
                instructions=(
                    "Create a concise tactical plan for the next response. "
                    "Do not decide player actions; only project world-side outcomes and pressure."
                ),
            ),
            AgentStep(
                name="narrator",
                role="Narrator",
                instructions=(
                    "Write vivid but concise external-world narration that preserves player agency. "
                    "End with clear space for player input."
                ),
            ),
            AgentStep(
                name="state_commander",
                role="State Writer",
                instructions=(
                    "Propose only valid state mutation commands for explicit player-declared actions. "
                    "If no valid mutation exists, return only narration/no-op style output."
                ),
            ),
        ]
    )


class CrewOrchestrator:
    """Minimal crew-style multi-agent flow backed by the configured LLM adapter."""

    def __init__(self, llm: LLMAdapter):
        self.llm = llm

    @staticmethod
    def parse_definition(raw: str | None) -> AgentCrewDefinition:
        if not raw or not raw.strip():
            return default_agent_crew_definition()
        return AgentCrewDefinition.model_validate_json(raw)

    def run(
        self,
        user_input: str,
        state: WorldState,
        mode: str,
        context_json: str,
        system_prompt: str,
        crew_definition: AgentCrewDefinition,
    ) -> tuple[AIResponse, dict[str, str]]:
        outputs: dict[str, str] = {}
        planner_text = ""
        narration_result: AIResponse | None = None
        command_result: AIResponse | None = None

        for step in crew_definition.steps:
            step_prompt = (
                f"{system_prompt}\n\n"
                f"[AGENT_STEP]\n"
                f"name={step.name}\n"
                f"role={step.role}\n"
                f"instructions={step.instructions}\n"
                f"prior_outputs={outputs}\n"
            )
            response = self.llm.generate(
                user_input=user_input,
                state_json=state.model_dump_json(),
                mode=mode,
                context_json=context_json,
                system_prompt=step_prompt,
            )
            outputs[step.name] = response.narration
            if step.name == "planner":
                planner_text = response.narration
            elif step.name == "narrator":
                narration_result = response
            elif step.name == "state_commander":
                command_result = response

        if narration_result is None:
            narration_result = self.llm.generate(
                user_input=f"Plan: {planner_text}\nPlayer input: {user_input}",
                state_json=state.model_dump_json(),
                mode=mode,
                context_json=context_json,
                system_prompt=f"{system_prompt}\n\nFallback narrator step.",
            )
            outputs["narrator_fallback"] = narration_result.narration

        if command_result is None:
            command_result = self.llm.generate(
                user_input=f"Plan: {planner_text}\nPlayer input: {user_input}",
                state_json=state.model_dump_json(),
                mode=mode,
                context_json=context_json,
                system_prompt=f"{system_prompt}\n\nFallback state writer step.",
            )
            outputs["state_commander_fallback"] = command_result.narration

        final = AIResponse(narration=narration_result.narration, commands=command_result.commands)
        return final, outputs
