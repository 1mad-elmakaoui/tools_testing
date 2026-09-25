"""Work out which price applies to a model ID.

The AWS Price List keys Bedrock prices by marketing name — "Claude Sonnet 4 (Amazon
Bedrock Edition)" — while a log record carries a model ID like
``anthropic.claude-sonnet-4-20250514-v1:0``. AWS publishes no mapping between the two.

Rather than hand-type a table of model IDs, which would mean typing from memory numbers
that decide what a report says something cost, this module states a rule and applies it.
Both sides are reduced to a list of lowercase tokens, and a price applies only when the two
lists are **equal**. Near-misses are not resolved by similarity: an ID that does not match
exactly is reported as unpriced, which is visible, rather than priced from the closest
guess, which is not.

``model_name_overrides`` in prices.yaml exists for the cases the rule cannot reach, and each
entry carries its own source.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

#: Cross-region inference profile prefixes. They select where a request may run, which
#: changes the rate that applies, and they are not part of the model's identity.
_GEO_PREFIXES = ("us", "eu", "apac", "us-gov", "global")

#: Suffix the price list appends to every Bedrock entry.
_NAME_SUFFIX = "(amazon bedrock edition)"

#: An eight-digit release date inside a model ID. It distinguishes snapshots of one model,
#: which the price list does not price separately.
_DATE = re.compile(r"^\d{8}$")

#: The trailing API version. v1 is the base and is left off the marketing name; a later one
#: is part of it, as in "Claude 3.5 Sonnet v2".
_VERSION = re.compile(r"^v(\d+)$")


class InferenceScope(StrEnum):
    """Which rate column applies to a call."""

    REGIONAL = "regional"
    GLOBAL = "global"


@dataclass(frozen=True)
class ModelKey:
    """A model ID reduced to the parts that decide its price."""

    tokens: tuple[str, ...]
    scope: InferenceScope
    #: The prefix that was stripped, kept so a report can say which profile was used.
    geo_prefix: str | None = None


def model_key(model_id: str) -> ModelKey:
    """Reduce a Bedrock model ID to comparable tokens and an inference scope.

    ``global.anthropic.claude-sonnet-4-20250514-v1:0`` becomes the tokens
    ``("claude", "sonnet", "4")`` at global scope: the provider, the release date and the
    base API version say nothing about which price applies.
    """
    text = model_id.strip().lower()
    if not text:
        return ModelKey((), InferenceScope.REGIONAL)

    # An inference profile ID starts with a geography. "global" means the global rate.
    geo: str | None = None
    for prefix in sorted(_GEO_PREFIXES, key=len, reverse=True):
        if text.startswith(f"{prefix}."):
            geo = prefix
            text = text[len(prefix) + 1 :]
            break

    # Drop the ARN wrapper an inference profile ARN carries.
    text = text.rsplit("/", 1)[-1]
    # Drop the ":0" model version qualifier.
    text = text.split(":", 1)[0]

    parts = [part for part in re.split(r"[.\-_]", text) if part]
    tokens: list[str] = []
    for index, part in enumerate(parts):
        if index == 0:
            # The provider stays: "Cohere Command R" and "Meta Llama" carry it in the name.
            tokens.append(part)
            continue
        if _DATE.match(part):
            continue
        version = _VERSION.match(part)
        if version:
            if version.group(1) != "1":
                tokens.append(part)
            continue
        tokens.append(part)

    # "anthropic" is the provider in the ID but never appears in the marketing name.
    if tokens and tokens[0] == "anthropic":
        tokens = tokens[1:]

    return ModelKey(
        tokens=tuple(tokens),
        scope=InferenceScope.GLOBAL if geo == "global" else InferenceScope.REGIONAL,
        geo_prefix=geo,
    )


def service_name_key(service_name: str) -> tuple[str, ...]:
    """Reduce a price list service name to the same token form.

    "Claude 3.5 Sonnet v2 (Amazon Bedrock Edition)" becomes
    ``("claude", "3", "5", "sonnet", "v2")``.
    """
    text = service_name.strip().lower()
    if text.endswith(_NAME_SUFFIX):
        text = text[: -len(_NAME_SUFFIX)].strip()
    # "Command R+" and "Command-Light" have to land on the same tokens as their IDs.
    text = text.replace("+", " plus ")
    parts = [part for part in re.split(r"[\s.\-_/()]+", text) if part]
    return tuple(part for part in parts if part not in {"model", "-"})


def resolve_service_name(
    model_id: str,
    service_names: Mapping[str, object] | tuple[str, ...],
    overrides: Mapping[str, str] | None = None,
) -> str | None:
    """The price list name for `model_id`, or None when nothing matches exactly.

    None is a result, not a failure: the caller reports the model as unpriced rather than
    attaching a number that came from the nearest-looking entry.
    """
    if overrides:
        direct = overrides.get(model_id) or overrides.get(model_id.strip().lower())
        if direct:
            return direct

    key = model_key(model_id)
    if not key.tokens:
        return None

    names = tuple(service_names)
    for name in names:
        if service_name_key(name) == key.tokens:
            return name
    return None
