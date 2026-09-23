"""Profile a CSV or Parquet file and generate a starter AWS Glue Data Quality ruleset.

The core of this package makes no AWS API calls. Only :mod:`dqdl_gen.push` talks to AWS, it
is an optional install, and ``tests/test_no_aws_calls.py`` fails the build if any other
module gains an AWS import.
"""

from dqdl_gen.catalog import Catalog, CatalogError, load_catalog
from dqdl_gen.emit import emit
from dqdl_gen.generate import generate
from dqdl_gen.models import (
    ColumnKind,
    ColumnProfile,
    DatasetProfile,
    Rule,
    Ruleset,
    Strictness,
)
from dqdl_gen.profile import profile_table
from dqdl_gen.reader import ReadError, SourceFormat, read_table
from dqdl_gen.validate import DqdlSyntaxError, validate

__version__ = "0.1.0"

__all__ = [
    "Catalog",
    "CatalogError",
    "ColumnKind",
    "ColumnProfile",
    "DatasetProfile",
    "DqdlSyntaxError",
    "ReadError",
    "Rule",
    "Ruleset",
    "SourceFormat",
    "Strictness",
    "__version__",
    "emit",
    "generate",
    "load_catalog",
    "profile_table",
    "read_table",
    "validate",
]
