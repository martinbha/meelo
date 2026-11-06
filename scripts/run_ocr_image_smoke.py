"""Run the offline OCR image smoke in a bounded, disposable container."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress

TIMEOUT_SECONDS = 180
REMOVE_TIMEOUT_SECONDS = 15


def _remove_container(name: str) -> None:
    with suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["docker", "rm", "--force", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=REMOVE_TIMEOUT_SECONDS,
        )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        print("usage: run_ocr_image_smoke.py IMAGE[:TAG] CONTAINER_NAME", file=sys.stderr)
        return 2

    image, container_name = arguments
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--network",
        "none",
        "--read-only",
        "--cpus",
        "2",
        "--memory",
        "2g",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777",
        "--env",
        "PADDLE_OCR_MODEL_ROOT=/opt/meelo/paddle-models",
        "--env",
        "PADDLE_PDX_CACHE_HOME=/tmp/paddlex-cache",
        "--env",
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True",
        "--env",
        "TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata",
        "--env",
        "XDG_CACHE_HOME=/tmp",
        "--env",
        "OMP_NUM_THREADS=2",
        "--env",
        "MKL_NUM_THREADS=2",
        "--entrypoint",
        "python",
        image,
        "scripts/smoke_test_ocr.py",
    ]
    try:
        result = subprocess.run(command, check=False, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _remove_container(container_name)
        print(
            f"The offline OCR smoke exceeded {TIMEOUT_SECONDS} seconds; the container was removed.",
            file=sys.stderr,
        )
        return 124
    except KeyboardInterrupt:
        _remove_container(container_name)
        return 130
    except OSError as error:
        _remove_container(container_name)
        print(f"Could not start the offline OCR smoke: {error}", file=sys.stderr)
        return 1

    if result.returncode != 0:
        _remove_container(container_name)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
