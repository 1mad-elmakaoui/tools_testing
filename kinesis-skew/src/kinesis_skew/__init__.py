"""Tell partition key skew apart from genuine under-provisioning in Kinesis Data Streams.

Throttled writes have two very different causes. One hot partition key pins a single shard
at its limit while the rest of the stream idles; adding shards does not help, because the
key still hashes to one shard. A stream that is simply too small throttles with its traffic
spread evenly across shards; there, more shards is exactly the answer. The two look
identical from the stream-level metrics, and are easy to tell apart from the shard-level
ones.
"""

__version__ = "0.1.0"
