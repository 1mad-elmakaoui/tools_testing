"""Recommend a SageMaker inference option for a workload, and explain the rejections."""

from sagemaker_inference_picker.engine import recommend
from sagemaker_inference_picker.limits import LimitsData, LimitsError, load_limits
from sagemaker_inference_picker.models import (
    Advice,
    Conflict,
    Elimination,
    Option,
    Preference,
    RankedOption,
    Recommendation,
    TrafficPattern,
    Workload,
)

__version__ = "0.1.0"

__all__ = [
    "Advice",
    "Conflict",
    "Elimination",
    "LimitsData",
    "LimitsError",
    "Option",
    "Preference",
    "RankedOption",
    "Recommendation",
    "TrafficPattern",
    "Workload",
    "__version__",
    "load_limits",
    "recommend",
]
