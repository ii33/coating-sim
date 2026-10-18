"""Optional Pillow raster tools, using bounded in-memory codecs and typed artifacts."""
from __future__ import annotations

from contextlib import contextmanager
import io
import warnings

from .contracts import ToolSpec, ToolkitError
from .fs import atomic_write, digest, read_bytes
from .policy import check_cancelled, resolve_path

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_DIMENSION = 4096
MAX_PIXELS = 4_000_000
FORMATS = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}


def _dimensions(width, height):
    if (width < 1 or height < 1 or width > MAX_DIMENSION or height > MAX_DIMENSION
            or width * height > MAX_PIXELS):
        raise ToolkitError('limit_exceeded', 'Image exceeds dimension or pixel limit')


@contextmanager
def _open(context, path):
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise ToolkitError('capability_unavailable', 'Pillow is not installed; automatic installation is disabled') from exc
    raw = read_bytes(context, path, MAX_IMAGE_BYTES)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw), formats=list(FORMATS)) as image:
                _dimensions(*image.size)
                if getattr(image, 'n_frames', 1) != 1:
                    raise ToolkitError('unsupported_image', 'Only single-frame raster images are supported')
                image.load()
                check_cancelled(context)
                yield image, raw
    except (UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ToolkitError('unsupported_image', 'Unsupported, malformed or oversized image') from exc


def _metadata(image, raw):
    return {'width': image.width, 'height': image.height, 'mode': image.mode,
            'media_type': FORMATS[image.format], 'bytes': len(raw), 'sha256': digest(raw)}


def image_info(context, args):
    with _open(context, args['path']) as (image, raw):
        return {'type': 'image_info', **_metadata(image, raw)}


def read_image(context, args):
    with _open(context, args['path']) as (image, raw):
        return {'type': 'image_artifact', 'path': str(resolve_path(context, args['path'])),
                **_metadata(image, raw)}


class _BoundedBuffer(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_IMAGE_BYTES:
            raise ToolkitError('limit_exceeded', 'Encoded image exceeds byte limit')
        return super().write(data)


def _save(context, image, args):
    _dimensions(*image.size)
    output = resolve_path(context, args['output_path'])
    # Deliberately encode PNG only, strip source metadata, and never pass a path to a codec.
    buffer = _BoundedBuffer()
    image.info.clear()
    image.save(buffer, format='PNG')
    raw = buffer.getvalue()
    atomic_write(context, output, raw, expected_hash=args.get('expected_hash', ''), max_bytes=MAX_IMAGE_BYTES)
    return {'type': 'image_artifact', 'path': str(output), 'media_type': 'image/png',
            'bytes': len(raw), 'sha256': digest(raw), 'width': image.width, 'height': image.height}


def crop_image(context, args):
    with _open(context, args['path']) as (image, _):
        left, top, right, bottom = args['box']
        if not 0 <= left < right <= image.width or not 0 <= top < bottom <= image.height:
            raise ToolkitError('invalid_arguments', 'Crop box must be nonempty and inside the image')
        with image.crop((left, top, right, bottom)) as result:
            return _save(context, result, args)


def resize_image(context, args):
    _dimensions(args['width'], args['height'])
    with _open(context, args['path']) as (image, _):
        from PIL import Image
        with image.resize((args['width'], args['height']), Image.Resampling.LANCZOS) as result:
            return _save(context, result, args)


def register_image_tools(registry):
    path = {'type': 'string', 'minLength': 1, 'maxLength': 4096}
    dimension = {'type': 'integer', 'minimum': 1, 'maximum': MAX_DIMENSION}
    output = {'output_path': path, 'expected_hash': {'type': 'string', 'maxLength': 64}}
    schemas = {
        'image_info': (image_info, 'read', {'path': path}, ['path']),
        'read_image': (read_image, 'read', {'path': path}, ['path']),
        'crop_image': (crop_image, 'write', {'path': path, **output,
            'box': {'type': 'array', 'minItems': 4, 'maxItems': 4,
                    'items': {'type': 'integer', 'minimum': 0, 'maximum': MAX_DIMENSION}}},
            ['path', 'output_path', 'box']),
        'resize_image': (resize_image, 'write', {'path': path, **output,
            'width': dimension, 'height': dimension}, ['path', 'output_path', 'width', 'height']),
    }
    for name, (handler, effect, properties, required) in schemas.items():
        registry.register(ToolSpec(name, 'img', f'Bounded raster operation: {name}',
            {'type': 'object', 'properties': properties, 'required': required,
             'additionalProperties': False}, effect), handler)
