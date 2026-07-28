"""The PRD is the microservice's only input. It is a hard contract: if it does not
validate here, nothing downstream runs. Every subagent reads the PRD; none may
mutate it."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

DART_IDENTIFIER = re.compile(r"^[a-z][a-zA-Z0-9]*$")
PACKAGE_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")

FieldType = Literal["text", "number", "bool", "date"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Field_(Strict):
    """A single data field. Named `Field_` to avoid colliding with pydantic.Field."""

    name: str
    label: str
    type: FieldType = "text"

    @field_validator("name")
    @classmethod
    def _dart_safe(cls, v: str) -> str:
        if not DART_IDENTIFIER.match(v):
            raise ValueError(f"{v!r} is not a lowerCamelCase Dart identifier")
        return v


class Action(Strict):
    name: str
    kind: Literal["create", "update", "delete", "navigate", "signIn", "signOut"]
    target: str | None = None

    @field_validator("name")
    @classmethod
    def _dart_safe(cls, v: str) -> str:
        if not DART_IDENTIFIER.match(v):
            raise ValueError(f"{v!r} is not a lowerCamelCase Dart identifier")
        return v


class Screen(Strict):
    id: str
    title: str
    kind: Literal["list", "form", "detail", "auth", "settings"]
    model: str | None = None
    fields: list[Field_] = []
    actions: list[Action] = []

    @field_validator("id")
    @classmethod
    def _dart_safe(cls, v: str) -> str:
        if not DART_IDENTIFIER.match(v):
            raise ValueError(f"{v!r} is not a lowerCamelCase Dart identifier")
        return v


class DataModel(Strict):
    name: str
    collection: str
    fields: list[Field_]

    @field_validator("name")
    @classmethod
    def _pascal(cls, v: str) -> str:
        if not re.match(r"^[A-Z][a-zA-Z0-9]*$", v):
            raise ValueError(f"model name {v!r} must be PascalCase")
        return v


class PRD(Strict):
    app_name: str
    package_name: str
    description: str = ""
    theme: Literal["material", "cupertino"] = "material"
    auth: bool = False
    models: list[DataModel] = []
    screens: list[Screen]

    # Set by the buyer-facing payment layer, not by the PRD author. The build
    # pipeline refuses to package an APK unless this is True.
    x402_payment_verified: bool = False

    @field_validator("package_name")
    @classmethod
    def _reverse_dns(cls, v: str) -> str:
        if not PACKAGE_NAME.match(v):
            raise ValueError(f"{v!r} must be reverse-DNS, e.g. com.example.todo")
        return v

    @model_validator(mode="after")
    def _referential_integrity(self) -> PRD:
        if not self.screens:
            raise ValueError("PRD must declare at least one screen")

        ids = [s.id for s in self.screens]
        if len(ids) != len(set(ids)):
            raise ValueError("screen ids must be unique")

        known_models = {m.name for m in self.models}
        for screen in self.screens:
            if screen.model is not None and screen.model not in known_models:
                raise ValueError(
                    f"screen {screen.id!r} references unknown model {screen.model!r}"
                )

        nav_targets = {a.target for s in self.screens for a in s.actions if a.kind == "navigate"}
        unknown = nav_targets - set(ids) - {None}
        if unknown:
            raise ValueError(f"navigate actions target unknown screens: {sorted(unknown)}")

        return self


def load_prd(path: str | Path) -> PRD:
    """Parse and validate a PRD file. Raises on any contract violation."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return PRD.model_validate(raw)
