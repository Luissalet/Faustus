"""Keep the same observed source list through streaming and saved history."""


def merge_web_sources(previous, incoming):
    """First observation wins; malformed rows cannot erase earlier sources."""
    result = []
    seen = set()
    for batch in (previous, incoming):
        if not isinstance(batch, list):
            continue
        for source in batch:
            if not isinstance(source, dict) or not isinstance(source.get('url'), str):
                continue
            if source['url'] not in seen:
                seen.add(source['url'])
                result.append(source)
    return result
