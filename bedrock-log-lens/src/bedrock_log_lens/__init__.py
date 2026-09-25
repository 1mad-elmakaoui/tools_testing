"""Analyse Amazon Bedrock model invocation logs without reading what was said.

These logs contain, in full, every prompt sent to a model and every response it gave. That
is the most sensitive data most organisations will ever put in an S3 bucket, and it is
sitting in a format that invites casual grepping.

This tool answers the operational questions — how many calls, how many tokens, what it
cost, who is doing it, and is something stuck in a loop — from the metadata alone. The
prompt and response fields are discarded the moment a record is parsed, before any analysis
sees them, and the type that flows through the rest of the program has nowhere to put them.
"""

__version__ = "0.1.0"
