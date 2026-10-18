"""The sole model-SDK boundary for portable artifact results."""
import asyncio
import hashlib
from .fs import read_bytes
from .images import MAX_IMAGE_BYTES
from .adapter import failure


def _image_bytes(data, context):
    raw = read_bytes(context, data['path'], MAX_IMAGE_BYTES)
    if len(raw) > MAX_IMAGE_BYTES or len(raw) != data.get('bytes') or hashlib.sha256(raw).hexdigest() != data.get('sha256'):
        raise ValueError('Image artifact changed or exceeds model budget')
    if data.get('media_type') not in ('image/png', 'image/jpeg', 'image/webp'):
        raise ValueError('Unsupported image media type')
    return raw


async def model_result(result, context):
    """Keep JSON envelope stable; attach only bounded verified image artifacts."""
    data = result.get('data')
    if not result.get('ok') or not isinstance(data, dict) or data.get('type') != 'image_artifact':
        return result
    try:
        raw = await asyncio.to_thread(_image_bytes, data, context)
    except (ValueError, OSError):
        return failure('artifact_unavailable', 'Image artifact is unavailable or changed')
    from pydantic_ai.messages import BinaryContent
    from coat_sim.agent.tool_dispatch import MCPMultimodalResult
    return MCPMultimodalResult(value=result, content=(BinaryContent(data=raw, media_type=data['media_type']),))
