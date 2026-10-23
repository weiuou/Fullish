"""Configured media is validated before it becomes an expression choice."""

import json

import pytest
from nonebot.adapters.qq import MessageSegment
from PIL import Image as PILImage

from fullish.sending import Image, OutgoingMessage, QQFace, ReplyPlan, Text, reply_messages
from fullish.sending.adapters import ADAPTERS
from fullish.sending.assets import MAX_IMAGE_BYTES, MAX_MANIFEST_BYTES, ReplyAsset, ReplyAssets
from fullish.sending.qq import build_message


def manifest(tmp_path, assets):
    path = tmp_path / "assets.json"
    path.write_text(json.dumps({"assets": assets}), encoding="utf-8")
    return path


def face(**changes):
    return {
        "id": "smile",
        "kind": "qq_face",
        "description": "温和微笑",
        "face_id": "14",
        "fallback": "🙂",
        **changes,
    }


def picture(**changes):
    return {
        "id": "celebrate",
        "kind": "image",
        "description": "庆祝成功的小猫",
        "path": "./celebrate.png",
        **changes,
    }


def test_loads_relative_images_faces_and_https_urls(tmp_path):
    PILImage.new("RGB", (4, 4), "red").save(tmp_path / "celebrate.png")
    image_bytes = (tmp_path / "celebrate.png").read_bytes()
    path = manifest(
        tmp_path,
        [
            picture(),
            face(),
            {
                "id": "remote",
                "kind": "image",
                "description": "鼓励",
                "url": "https://assets.example/cheer.png",
            },
        ],
    )
    assets = ReplyAssets.from_file(path)
    assert assets.items == (
        ReplyAsset("celebrate", "庆祝成功的小猫", Image(image_bytes)),
        ReplyAsset("smile", "温和微笑", QQFace("14", "🙂")),
        ReplyAsset("remote", "鼓励", Image("https://assets.example/cheer.png")),
    )
    # Later filesystem changes cannot silently substitute a configured asset.
    (tmp_path / "celebrate.png").write_bytes(b"different")
    assert assets.items[0].part == Image(image_bytes)
    assert ReplyAssets.from_file(manifest(tmp_path, [])).items == ()


@pytest.mark.parametrize("face_id", ["", "-1", "14x", "1234567", "１４", 14, True])
def test_face_rejects_invalid_ids(face_id):
    with pytest.raises(ValueError, match="ASCII digits"):
        QQFace(face_id, "🙂")


@pytest.mark.parametrize("fallback", ["", " \t", None, "x" * 101])
def test_face_requires_bounded_readable_fallback(fallback):
    with pytest.raises(ValueError, match="fallback"):
        QQFace("14", fallback)


def test_face_default_adapter_and_history_use_fallback():
    part = QQFace("14", "🙂")
    reply = OutgoingMessage((Text("好呀"), part))
    assert reply.context_text == "好呀🙂"
    message = build_message(reply)
    assert [segment.type for segment in message] == ["text", "text"]
    assert message[1].data["text"] == "🙂"

    def native(part):
        return MessageSegment.text(f"verified:{part.id}")

    custom = build_message(reply, adapters={**ADAPTERS, "qq_face": native})
    assert custom[1].data["text"] == "verified:14"
    with pytest.raises(TypeError):
        ADAPTERS["qq_face"](Text("invalid"))


def test_plan_preserves_explicit_messages_and_single_reply_compatibility():
    one = OutgoingMessage((Text("终于过了"),))
    two = OutgoingMessage((QQFace("14", "🙂"),))
    plan = ReplyPlan((one, two))
    assert reply_messages(plan) == (one, two)
    assert reply_messages(one) == (one,)
    assert reply_messages("终于过了") == (one,)


@pytest.mark.parametrize("count", [0, 6])
def test_plan_rejects_empty_or_over_budget_messages(count):
    with pytest.raises(ValueError):
        ReplyPlan((OutgoingMessage((Text("ok"),)),) * count)


@pytest.mark.parametrize("messages", [[OutgoingMessage((Text("ok"),))], ("raw",)])
def test_plan_rejects_invalid_message_types(messages):
    with pytest.raises(TypeError):
        ReplyPlan(messages)


@pytest.mark.parametrize(
    "entries",
    [
        {},
        [None],
        [face(), face()],
        [face(id="../escape")],
        [face(id="x" * 65)],
        [face(description="")],
        [face(description="x" * 201)],
        [face(kind="audio")],
        [face(extra="typo")],
        [face(fallback=None)],
        [face(id=f"face_{index}") for index in range(25)],
        [picture(url="https://assets.example/image.png")],
        [picture(path=None)],
        [picture(path="")],
    ],
)
def test_invalid_catalogs_are_rejected(tmp_path, entries):
    with pytest.raises((ValueError, TypeError)):
        ReplyAssets.from_file(manifest(tmp_path, entries))


@pytest.mark.parametrize(
    "url",
    ["http://example/a", "file:///tmp/a", "https://", 42, "https://user:secret@example.com/a"],
)
def test_asset_url_requires_https_without_credentials(tmp_path, url):
    entry = {"id": "image", "kind": "image", "description": "图片", "url": url}
    with pytest.raises(ValueError):
        ReplyAssets.from_file(manifest(tmp_path, [entry]))


@pytest.mark.parametrize("contents", [b"not an image", b"\x89PNG\r\n\x1a\ntruncated", b""])
def test_invalid_local_image_bytes_are_rejected(tmp_path, contents):
    (tmp_path / "celebrate.png").write_bytes(contents)
    with pytest.raises(ValueError, match="valid supported image"):
        ReplyAssets.from_file(manifest(tmp_path, [picture()]))


def test_local_image_format_is_sniffed_and_unsupported_formats_rejected(tmp_path):
    PILImage.new("RGB", (2, 2)).save(tmp_path / "celebrate.png", format="BMP")
    with pytest.raises(ValueError, match="PNG, JPEG, GIF or WebP"):
        ReplyAssets.from_file(manifest(tmp_path, [picture()]))


@pytest.mark.parametrize("cut", [2, 10, 100])
def test_truncated_jpeg_payload_is_rejected_even_if_headers_verify(tmp_path, cut):
    path = tmp_path / "celebrate.jpg"
    PILImage.new("RGB", (100, 100), "red").save(path, format="JPEG")
    path.write_bytes(path.read_bytes()[:-cut])
    # Pillow's header verification alone accepts each of these broken images.
    with PILImage.open(path) as image:
        image.verify()
    with pytest.raises(ValueError, match="valid supported image"):
        ReplyAssets.from_file(manifest(tmp_path, [picture(path="celebrate.jpg")]))


def test_oversized_image_dimensions_are_rejected_before_decode(tmp_path, monkeypatch):
    PILImage.new("RGB", (10, 10)).save(tmp_path / "celebrate.png")
    monkeypatch.setattr(PILImage, "MAX_IMAGE_PIXELS", 60)
    with pytest.raises(ValueError, match="valid supported image"):
        ReplyAssets.from_file(manifest(tmp_path, [picture()]))


def test_missing_image_is_rejected_at_catalog_load(tmp_path):
    with pytest.raises(FileNotFoundError):
        ReplyAssets.from_file(manifest(tmp_path, [picture()]))


def test_large_inputs_are_rejected(tmp_path):
    path = manifest(tmp_path, [picture()])
    (tmp_path / "celebrate.png").write_bytes(b"x" * (MAX_IMAGE_BYTES + 1))
    with pytest.raises(ValueError, match="10 MiB"):
        ReplyAssets.from_file(path)
    path.write_bytes(b" " * (MAX_MANIFEST_BYTES + 1))
    with pytest.raises(ValueError, match="64 KiB"):
        ReplyAssets.from_file(path)


@pytest.mark.parametrize("raw", ["[]", "null", "{}", '{"assets": [], "extra": true}'])
def test_manifest_requires_exact_top_level_shape(tmp_path, raw):
    path = tmp_path / "assets.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        ReplyAssets.from_file(path)
