"""Side-effect-free local clone of the SN55 validator scoring pipeline."""

from .artifacts import ArtifactBundle
from .evaluator import evaluate_submission

__all__ = ["ArtifactBundle", "evaluate_submission"]
__version__ = "0.1.0"
