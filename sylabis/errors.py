"""
Typed error hierarchy (Primetime plan, Phase 1 WS1a). Library code raises
these instead of SystemExit so the compiler/grader stay usable behind the
web and MCP surfaces; the CLI catches SylabisError at the top and exits.
"""


class SylabisError(Exception):
    """Base for every error sylabis raises deliberately."""


class ModelError(SylabisError):
    """A model call failed after retries (network, rate limit, overload)."""


class TruncationError(ModelError):
    """A response hit max_tokens — never ship a silently truncated doc."""


class SchemaError(ModelError):
    """Model output failed the per-stage schema check after one repair round."""


class CompileError(SylabisError):
    """A compile stage failed for a non-model reason."""


class CompileDeclined(CompileError):
    """Intake's viability verdict declined to compile the course.
    Carries the verdict and the intake stage's notes so surfaces can
    render the reason without parsing the message string."""

    def __init__(self, message: str, verdict: str = "decline", notes: str = ""):
        super().__init__(message)
        self.verdict = verdict
        self.notes = notes


class GradeError(SylabisError):
    """Grading could not run (missing checkpoint, bad bundle state)."""


class AttachError(SylabisError):
    """A bundle could not be attached (bad path/URL, clone failure)."""


class SandboxError(SylabisError):
    """Rubric-script sandbox setup or containment failure."""


class PublishError(SylabisError):
    """The publish scrubber refused to ship a bundle (leak, gate failure)."""


class RegistryError(SylabisError):
    """Registry index fetch/parse failure or SHA mismatch on get."""
