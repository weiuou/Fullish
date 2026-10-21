"""Parser contract and an explicit allowlist for retained segment metadata."""

from collections.abc import Callable, Mapping

from fullish.collection.schema import MessagePart, MetadataValue

type SegmentParser = Callable[[Mapping[str, object]], MessagePart]


def metadata(data: Mapping[str, object], *keys: str) -> dict[str, MetadataValue]:
    return {
        key: value for key in keys if isinstance(value := data.get(key), (str, int, float, bool))
    }
