"""Testes do núcleo (não exigem rede nem GPU)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from videoupscale import core, pipeline
from videoupscale import video_io as vio
from videoupscale.backends import BACKENDS
from videoupscale.config import load as load_config


def make_video(path: Path, w: int = 320, h: int = 180, seconds: int = 2, fps: int = 24,
               with_audio: bool = True) -> Path:
    cmd = [vio.ffmpeg_bin(), "-y", "-v", "error",
           "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate={fps}:duration={seconds}"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def sample(tmp_path_factory) -> Path:
    return make_video(tmp_path_factory.mktemp("in") / "sample.mp4")


def test_target_size_preserves_aspect():
    info = vio.VideoInfo("x", 1280, 720, "30/1", 30.0, 60, 2.0)
    assert vio.target_size(info, None, "4k") == (3840, 2160)
    assert vio.target_size(info, None, "1080p") == (1920, 1080)
    w, h = vio.target_size(info, None, "720p")
    assert (w, h) == (1280, 720) and w % 2 == 0 and h % 2 == 0


def test_target_size_fits_inside_target_for_4_3():
    info = vio.VideoInfo("x", 640, 480, "30/1", 30.0, 60, 2.0)
    w, h = vio.target_size(info, None, "1080p")
    assert (w, h) == (1440, 1080) and abs(w / h - 4 / 3) < 0.01


def test_target_size_scale_and_errors():
    info = vio.VideoInfo("x", 100, 50, "30/1", 30.0, 30, 1.0)
    assert vio.target_size(info, 3.0, None) == (300, 150)
    with pytest.raises(ValueError):
        vio.target_size(info, None, "besouro")
    with pytest.raises(ValueError):
        vio.target_size(info, -1, None)


def test_encoder_args():
    assert vio.encoder_args("libx265", 20) == ["-c:v", "libx265", "-crf", "20", "-preset", "medium", "-tag:v", "hvc1"]
    assert vio.encoder_args("libx264", 18, "fast", "10M") == ["-c:v", "libx264", "-b:v", "10M"]
    assert "-cq" in vio.encoder_args("hevc_nvenc", 22)


def test_pix_fmt_auto():
    sdr = vio.VideoInfo("x", 640, 360, "30/1", 30.0, 30, 1.0, bit_depth=8)
    hdr = vio.VideoInfo("x", 640, 360, "30/1", 30.0, 30, 1.0, bit_depth=10)
    assert vio.output_pix_fmt("auto", sdr, "libx264") == "yuv420p"
    assert vio.output_pix_fmt("auto", hdr, "libx265") == "yuv420p10le"


def test_probe_and_plan(sample):
    info = vio.probe(str(sample))
    assert (info.width, info.height) == (320, 180)
    assert info.has_audio
    assert info.frames > 0
    opts = core.Options(target="1080p", backend="ffmpeg")
    _, w, h = core.plan(str(sample), opts)
    assert (w, h) == (1920, 1080)


def test_pipeline_end_to_end(sample, tmp_path):
    """Roda o pipeline completo com um upscaler falso (resize) e confere saída + áudio."""
    import cv2

    def make_upscaler():
        return lambda img: cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_LANCZOS4)

    info = vio.probe(str(sample))
    dst = tmp_path / "out.mp4"
    seen: list[int] = []
    opts = pipeline.PipelineOptions(width=640, height=360, workers=2, frame_ext="png", codec="libx264")
    pipeline.run(str(sample), str(dst), info, opts, make_upscaler, lambda d, t: seen.append(d))
    assert dst.is_file() and dst.stat().st_size > 0
    out = vio.probe(str(dst))
    assert (out.width, out.height) == (640, 360)
    assert out.has_audio, "o áudio original deve ser preservado"
    assert seen and max(seen) == len(seen), "o progresso deve chegar ao total de frames"


class _ResizeFactory:
    """Factory de nível de módulo (precisa ser serializável para o multiprocessing)."""

    def __call__(self):
        import cv2

        return lambda img: cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_LANCZOS4)


def test_pipeline_cancels(sample, tmp_path):
    info = vio.probe(str(sample))
    opts = pipeline.PipelineOptions(width=640, height=360, workers=1)
    with pytest.raises(pipeline.Cancelled):
        pipeline.run(str(sample), str(tmp_path / "x.mp4"), info, opts, _ResizeFactory(), None, lambda: True)


def test_registry_has_local_and_paid_backends():
    assert set(BACKENDS) >= {"ffmpeg", "opencv", "torch", "realesrgan", "replicate", "topaz"}
    free = {n for n, c in BACKENDS.items() if c.capabilities.free}
    paid = set(BACKENDS) - free
    assert free and paid == {"replicate", "topaz"}
    for cls in BACKENDS.values():
        assert cls.description and 1 <= cls.capabilities.quality <= 5
        if cls.capabilities.default_model:
            assert cls.capabilities.default_model in cls.capabilities.models


def test_default_output_and_batch(tmp_path):
    out = core.default_output("/pasta/video.mp4", 3840, 2160, "torch")
    assert out == "/pasta/video_3840x2160_torch.mp4"
    make_video(tmp_path / "a.mp4", seconds=1)
    (tmp_path / "nota.txt").write_text("x")
    found = core.find_videos(tmp_path, (".mp4",))
    assert [p.name for p in found] == ["a.mp4"]


def test_config_file(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('backend = "torch"\ncrf = 16\n# comentário\n')
    data = load_config(cfg)
    assert data["backend"] == "torch" and data["crf"] == 16
