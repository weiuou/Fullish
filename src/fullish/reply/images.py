"""Terminal image actions produce outgoing candidates without platform delivery."""

import json

from fullish.errors import ModelError
from fullish.generation import ImageGenerator
from fullish.media.assets import ImageAssetError, ImageAssets
from fullish.policies.access import AccessPolicy
from fullish.sending import Image, OutgoingMessage, Text
from fullish.tools.runtime import ToolContext, ToolRuntime


class ImageActions:
    def __init__(
        self, generator: ImageGenerator | None, tools: ToolRuntime | None, access: AccessPolicy
    ):
        self.image_generator = generator
        self.tools = tools
        self.access = access

    def generation_allowed(self, context: ToolContext | None) -> bool:
        return bool(self.image_generator and (self.access.image_allowed(context)))

    def sending_allowed(self, context: ToolContext | None) -> bool:
        return bool(
            self.tools and self.tools.allowed(context) and (self.access.image_allowed(context))
        )

    def send(self, function: dict, images: ImageAssets) -> OutgoingMessage:
        raw = function["arguments"]
        if len(raw) > 16000:
            raise ModelError("image_invalid_tool_call")
        options = json.loads(raw)
        if (
            not isinstance(options, dict)
            or set(options) != {"image_id", "caption"}
            or not isinstance(options["caption"], str)
            or len(options["caption"]) > 1500
        ):
            raise ModelError("image_invalid_tool_call")
        try:
            asset = images.get(options["image_id"])
        except ImageAssetError as error:
            raise ModelError(str(error)) from None
        caption = options["caption"].strip()
        return OutgoingMessage(
            (Text(caption), Image(asset.body)) if caption else (Image(asset.body),)
        )

    async def generate(self, function: dict, images: ImageAssets) -> OutgoingMessage:
        if not self.image_generator:
            raise ModelError("image_tool_unavailable")
        if function["name"] != "generate_image":
            raise ModelError("image_invalid_tool_call")
        arguments = function["arguments"]
        if not isinstance(arguments, str) or len(arguments) > 16000:
            raise ModelError("image_invalid_tool_call")
        options = json.loads(arguments)
        if (
            not isinstance(options, dict)
            or not {"prompt", "caption"} <= set(options)
            or set(options) - {"prompt", "caption", "reference_image_ids"}
        ):
            raise ModelError("image_invalid_tool_call")
        prompt, caption = options["prompt"], options["caption"]
        if (
            not isinstance(prompt, str)
            or not 0 < len(prompt.strip()) <= 4000
            or not isinstance(caption, str)
            or len(caption) > 1500
        ):
            raise ModelError("image_invalid_tool_call")
        refs = options.get("reference_image_ids", [])
        if (
            not isinstance(refs, list)
            or len(refs) > 4
            or any(not isinstance(ref, str) for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            raise ModelError("image_invalid_tool_call")
        try:
            references = tuple(images.get(ref).edit_body for ref in refs)
        except ImageAssetError as error:
            raise ModelError(str(error)) from None
        generated = (
            await self.image_generator.generate(prompt, reference_images=references)
            if references
            else await self.image_generator.generate(prompt)
        )
        return OutgoingMessage(
            (Text(caption.strip()), Image(generated.image))
            if caption.strip()
            else (Image(generated.image),)
        )
