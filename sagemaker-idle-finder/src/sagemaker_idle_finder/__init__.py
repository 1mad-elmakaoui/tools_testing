"""Find SageMaker endpoints that cost money while receiving little or no traffic.

Every AWS call this package can make is a read. The allowlist lives in ``policy.yaml`` and
is enforced at runtime by :class:`~sagemaker_idle_finder.aws.ReadOnlyClient`, so an
operation outside it raises before it reaches the network.
"""

from sagemaker_idle_finder.catalog import CatalogError, Policy, Prices, load_policy, load_prices
from sagemaker_idle_finder.models import (
    Endpoint,
    EndpointStatus,
    Finding,
    MetricWindow,
    Remedy,
    ScanResult,
    Variant,
    Verdict,
)

__version__ = "0.1.0"

__all__ = [
    "CatalogError",
    "Endpoint",
    "EndpointStatus",
    "Finding",
    "MetricWindow",
    "Policy",
    "Prices",
    "Remedy",
    "ScanResult",
    "Variant",
    "Verdict",
    "__version__",
    "load_policy",
    "load_prices",
]
