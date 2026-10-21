"""Explicit CPA image check; listing is free of generation, --generate makes one image."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from fullish.errors import ModelError
from fullish.generation.gemini import GeminiImageGenerator
from fullish.media import sniff_image_type
from fullish.settings import ImageGenerationSettings, ModelSettings


async def check(*, generate: bool = False, output: Path | None = None) -> None:
    connection = ModelSettings.from_env()
    settings = ImageGenerationSettings.from_env()
    if connection is None:
        raise SystemExit("Configure AI_BASE_URL and AI_API_KEY first")
    if settings is None:
        raise SystemExit("Image generation is disabled by AI_IMAGE_MODEL=off")
    generator = GeminiImageGenerator(connection, settings)
    try:
        model = await generator.select_model()
        result: dict[str, object] = {"model": model}
        if generate:
            image = await generator.generate("生成一张白底蓝色圆形的简洁图标，只需一张图。")
            result.update(
                bytes=len(image.image),
                media_type=sniff_image_type(image.image),
                sha256=hashlib.sha256(image.image).hexdigest(),
            )
            if output:
                output.write_bytes(image.image)
        print(json.dumps(result, ensure_ascii=False))
    except ModelError as error:
        raise SystemExit(str(error)) from None
    except TimeoutError:
        raise SystemExit("image_connection_or_timeout") from None
    finally:
        await generator.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", action="store_true", help="Generate one tiny test image")
    parser.add_argument("--output", type=Path, help="Optionally save the test image bytes")
    args = parser.parse_args()
    if args.output and not args.generate:
        parser.error("--output requires --generate")
    asyncio.run(check(generate=args.generate, output=args.output))


if __name__ == "__main__":
    main()
