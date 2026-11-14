"""Error types for the dirty-data rules DSL."""


class RulesConfigError(ValueError):
    """Configuration-level error (bad YAML, missing keys)."""


class RuleFieldConfigError(RulesConfigError):
    """A single field spec is malformed."""


class UnknownFieldTypeError(RulesConfigError):
    """A field declares an unsupported 'type'."""
