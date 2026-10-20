from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Competency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=300)


class InterviewConfig(BaseModel):
    candidate_name: str = Field(max_length=100)
    profile: str = Field(max_length=8000)
    job_title: str = Field(max_length=200)
    job_description: str = Field(max_length=8000)
    interview_id: str
    # Screening interviews (a company's interview of an applicant). Practice
    # sessions send none of these and behave as before.
    mode: Literal["practice", "screening"] = "practice"
    company_name: str | None = Field(default=None, max_length=120)
    questions: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=8)
    competencies: list[Competency] = Field(default_factory=list, max_length=5)
    max_minutes: int = Field(default=5, ge=5, le=20)
    voice_session_ref: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _screening_needs_a_company(self):
        if self.mode == "screening" and not self.company_name:
            raise ValueError("company_name is required for a screening interview")
        return self
