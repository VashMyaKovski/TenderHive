from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from dataclasses import dataclass, field
from io import BytesIO

from PIL import Image

from modelgw.errors import Failure, GatewayError
from modelgw.privacy import mask_public_text
from modelgw.schemas import (
    InferenceRequest,
    Knowledge,
    Penalty,
    PublicImage,
    PublicText,
    SensitiveText,
    SyntheticText,
)


@dataclass(frozen=True, slots=True)
class Replacement:
    fragment: int
    token: str
    value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedContext:
    texts: tuple[str, ...]
    image_url: str | None
    knowledge: Knowledge | None
    penalty: Penalty | None
    replacements: tuple[Replacement, ...] = field(repr=False)


def prepare_context(request: InferenceRequest) -> PreparedContext:
    public_texts: list[str] = []
    replacements: list[Replacement] = []
    for index, item in enumerate(request.context, start=1):
        if isinstance(item, PublicText | SyntheticText):
            safe_text, matches = mask_public_text(item.text)
            public_texts.append(safe_text)
            replacements.extend(Replacement(index, token, value) for token, value in matches)
        if isinstance(item, SensitiveText):
            replacements.append(
                Replacement(index, f"[REDACTED_{item.category.upper()}]", item.text)
            )
    images = [item for item in request.context if isinstance(item, PublicImage)]
    rules = [item for item in request.context if isinstance(item, Knowledge)]
    penalties = [item for item in request.context if isinstance(item, Penalty)]

    if request.task == "risk_assessment":
        if len(rules) != 1 or len(penalties) != 1:
            raise GatewayError(Failure.INVALID_CONTEXT)
    elif rules or penalties:
        raise GatewayError(Failure.INVALID_CONTEXT)

    if request.task == "vision":
        if len(images) != 1:
            raise GatewayError(Failure.INVALID_CONTEXT)
    elif images or not public_texts:
        raise GatewayError(Failure.INVALID_CONTEXT)

    return PreparedContext(
        texts=tuple(public_texts),
        image_url=_normalize_image(images[0].data) if images else None,
        knowledge=rules[0] if rules else None,
        penalty=penalties[0] if penalties else None,
        replacements=tuple(replacements),
    )


def _normalize_image(encoded: str) -> str:
    try:
        data = b64decode(encoded, validate=True)
        if len(data) > 2_000_000:
            raise GatewayError(Failure.INVALID_CONTEXT)
        with Image.open(BytesIO(data), formats=["PNG", "JPEG"]) as image:
            if image.width * image.height > 4_000_000 or getattr(image, "n_frames", 1) != 1:
                raise GatewayError(Failure.INVALID_CONTEXT)
            # A fresh pixel buffer drops metadata. Compositing discards fully transparent colors.
            pixels = image.convert("RGBA")
            clean = Image.new("RGB", image.size, "white")
            clean.paste(pixels, (0, 0), pixels.getchannel("A"))
            output = BytesIO()
            clean.save(output, format="JPEG", quality=85)
    except Base64Error, ValueError, OSError, Image.DecompressionBombError:
        raise GatewayError(Failure.INVALID_CONTEXT) from None
    if output.tell() > 2_000_000:
        raise GatewayError(Failure.INVALID_CONTEXT)
    return "data:image/jpeg;base64," + b64encode(output.getvalue()).decode("ascii")
