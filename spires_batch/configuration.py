"""The sole public configuration contract for the spipy v1.0.0 baseline.

Configuration versions describe operator-authored JSON, not scientific recipes,
checkpoint layouts or immutable plan formats. Development configurations are not
upgraded implicitly. Future contracts must be introduced deliberately.
"""

from typing import Literal, get_args

from pydantic import BaseModel, Field, model_validator


CONFIG_SCHEMA_VERSION = 1
HEADER_FIELDS = frozenset({"config_type", "schema_version"})


def require_configuration(value, config_type):
    """Reject missing, mismatched or superseded public configuration headers."""
    if (
        not isinstance(value, dict)
        or value.get("config_type") != config_type
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != CONFIG_SCHEMA_VERSION
    ):
        raise ValueError(
            f"Expected config_type={config_type!r} and schema_version=1 "
            "(spipy v1.0.0 baseline). Development configuration formats are "
            "not supported; use the current JSON template."
        )
    return value


class PublicConfiguration(BaseModel):
    config_type: str = Field(description="Configuration kind; selects the public interface, not a scientific algorithm.")
    schema_version: Literal[CONFIG_SCHEMA_VERSION] = Field(description="Required integer 1: the spipy v1.0.0 configuration baseline. Independent of saved-state formats and package versions.")

    @model_validator(mode="before")
    @classmethod
    def require_baseline(cls, value):
        if isinstance(value, cls):
            return value
        config_type = get_args(cls.model_fields["config_type"].annotation)[0]
        return require_configuration(value, config_type)
