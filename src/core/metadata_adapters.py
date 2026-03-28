from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetadataContext:
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    track: str | None = None
    disc: str | None = None


class MetadataAdapter:
    """Container-aware ffmpeg metadata mapping contract."""

    def ffmpeg_output_args(self) -> list[str]:
        raise NotImplementedError

    def ffmpeg_metadata_args(self, context: MetadataContext) -> list[str]:
        raise NotImplementedError


class FlacMetadataAdapter(MetadataAdapter):
    def ffmpeg_output_args(self) -> list[str]:
        return ["-c:a", "flac"]

    def ffmpeg_metadata_args(self, context: MetadataContext) -> list[str]:
        mapping = {
            "title": context.title,
            "artist": context.artist,
            "album": context.album,
            "track": context.track,
            "disc": context.disc,
        }
        args: list[str] = []
        for key, value in mapping.items():
            if value:
                args.extend(["-metadata", f"{key}={value}"])
        return args


class Mp3MetadataAdapter(MetadataAdapter):
    def ffmpeg_output_args(self) -> list[str]:
        return ["-c:a", "libmp3lame", "-id3v2_version", "3"]

    def ffmpeg_metadata_args(self, context: MetadataContext) -> list[str]:
        # FFmpeg maps common fields to ID3 frames (e.g. title->TIT2, artist->TPE1).
        return FlacMetadataAdapter().ffmpeg_metadata_args(context)


class M4bMetadataAdapter(MetadataAdapter):
    def ffmpeg_output_args(self) -> list[str]:
        return ["-c:a", "aac", "-b:a", "96k", "-f", "ipod"]

    def ffmpeg_metadata_args(self, context: MetadataContext) -> list[str]:
        # MP4 atom names accepted by ffmpeg are still title/artist/album.
        args = FlacMetadataAdapter().ffmpeg_metadata_args(context)
        args.extend(["-movflags", "use_metadata_tags"])
        return args


def adapter_for_extension(output_filename: str) -> MetadataAdapter:
    ext = output_filename.lower().rsplit(".", 1)[-1] if "." in output_filename else "flac"
    if ext == "mp3":
        return Mp3MetadataAdapter()
    if ext == "m4b":
        return M4bMetadataAdapter()
    return FlacMetadataAdapter()
