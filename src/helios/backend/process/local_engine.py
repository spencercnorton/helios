import os
import shutil
import stat
import subprocess
import urllib.request
from pathlib import Path

from helios.log import get_logger
from helios.paths import state_dir

_log = get_logger("local_engine")

MODELS = {
    "qwen2.5-0.5b": {
        "url": "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "filename": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
    },
    "llama-3.2-1b": {
        "url": "https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        "filename": "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
    },
    "phi-3-mini": {
        "url": "https://huggingface.co/microsoft/Phi-3-mini-4k-instruct-gguf/resolve/main/Phi-3-mini-4k-instruct-q4.gguf",
        "filename": "Phi-3-mini-4k-instruct-q4.gguf",
    },
}

_LEGACY_MODEL_ALIASES = {
    "qwen2.5:0.5b": "qwen2.5-0.5b",
    "llama3.2:1b": "llama-3.2-1b",
    "phi3:mini": "phi-3-mini",
}


def _llm_dir() -> Path:
    return state_dir() / "llm"


def ensure_binaries() -> Path:
    """Locate an existing llama-cli binary without downloading unverified executables."""
    base_dir = _llm_dir()
    base_dir.mkdir(parents=True, exist_ok=True)

    which_cli = shutil.which("llama-cli")
    if which_cli:
        p = Path(which_cli)
        if p.is_file() and os.access(p, os.X_OK):
            return p

    for candidate in (
        base_dir / "build" / "bin" / "llama-cli",
        base_dir / "bin" / "llama-cli",
        base_dir / "llama-cli",
    ):
        if candidate.exists():
            if not os.access(candidate, os.X_OK):
                os.chmod(candidate, os.stat(candidate).st_mode | stat.S_IEXEC)
            return candidate

    raise FileNotFoundError(
        f"llama-cli not found on PATH or in {base_dir}. "
        "Install llama.cpp (llama-cli) to use the built-in local title engine."
    )


def ensure_model(model_key: str) -> Path:
    model_key = _LEGACY_MODEL_ALIASES.get(model_key, model_key)
    if model_key not in MODELS:
        model_key = "qwen2.5-0.5b"

    cfg = MODELS[model_key]
    base_dir = _llm_dir()
    base_dir.mkdir(parents=True, exist_ok=True)

    model_path = base_dir / cfg["filename"]
    if not model_path.exists():
        _log.info("Downloading model %s...", model_key)
        urllib.request.urlretrieve(cfg["url"], str(model_path))

    return model_path


def generate(model_key: str, prompt: str, timeout: int = 60) -> str:
    cli_path = ensure_binaries()
    model_path = ensure_model(model_key)

    cmd = [
        str(cli_path),
        "-m", str(model_path),
        "-p", prompt,
        "-n", "30",
        "--temp", "0.3",
        "--log-disable",
    ]

    from helios.backend.process.env_scrub import scrubbed_child_env

    _log.debug("Running local engine: %s", cmd)
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=scrubbed_child_env(),
    )
    if res.returncode != 0:
        raise RuntimeError(f"llama-cli failed: {res.stderr}")

    out = res.stdout
    if prompt in out:
        out = out.split(prompt, 1)[1]
    return out.strip()
