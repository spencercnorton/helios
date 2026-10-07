import urllib.request
import os
import stat
import platform
import subprocess
import zipfile
from pathlib import Path
from helios.log import get_logger

_log = get_logger("local_engine")

MODELS = {
    "qwen2.5-0.5b": {
        "url": "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "filename": "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    },
    "llama-3.2-1b": {
        "url": "https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        "filename": "Llama-3.2-1B-Instruct-Q4_K_M.gguf"
    },
    "phi-3-mini": {
        "url": "https://huggingface.co/microsoft/Phi-3-mini-4k-instruct-gguf/resolve/main/Phi-3-mini-4k-instruct-q4.gguf",
        "filename": "Phi-3-mini-4k-instruct-q4.gguf"
    }
}

def get_llama_url():
    sys = platform.system().lower()
    arch = platform.machine().lower()
    version = "b4000"
    base = f"https://github.com/ggerganov/llama.cpp/releases/download/{version}/llama-{version}-bin-"
    
    if sys == "darwin":
        if arch == "arm64":
            return base + "macos-arm64.zip"
        else:
            return base + "macos-x64.zip"
    elif sys == "linux":
        if arch in ("aarch64", "arm64"):
            return base + "ubuntu-aarch64.zip"
        else:
            return base + "ubuntu-x64.zip"
    else:
        raise Exception(f"Unsupported OS/Arch: {sys}/{arch}")

def ensure_binaries():
    base_dir = Path.home() / ".local" / "share" / "helios" / "llm"
    base_dir.mkdir(parents=True, exist_ok=True)
    
    cli_path = base_dir / "build" / "bin" / "llama-cli"
    alt_cli_path = base_dir / "llama-cli"
    
    found_path = None
    if cli_path.exists():
        found_path = cli_path
    elif alt_cli_path.exists():
        found_path = alt_cli_path
        
    if found_path:
        if not os.access(found_path, os.X_OK):
            os.chmod(found_path, os.stat(found_path).st_mode | stat.S_IEXEC)
        return found_path

    _log.info("Downloading llama-cli for local engine...")
    url = get_llama_url()
    zip_path = base_dir / "llama.zip"
    urllib.request.urlretrieve(url, str(zip_path))
    
    with zipfile.ZipFile(str(zip_path), 'r') as zip_ref:
        zip_ref.extractall(str(base_dir))
        
    try:
        zip_path.unlink()
    except Exception:
        pass
        
    if cli_path.exists():
        os.chmod(cli_path, os.stat(cli_path).st_mode | stat.S_IEXEC)
        return cli_path
    elif alt_cli_path.exists():
        os.chmod(alt_cli_path, os.stat(alt_cli_path).st_mode | stat.S_IEXEC)
        return alt_cli_path
    else:
        raise Exception("llama-cli not found in downloaded zip.")

def ensure_model(model_key: str):
    if model_key not in MODELS:
        model_key = "qwen2.5-0.5b"
    
    cfg = MODELS[model_key]
    base_dir = Path.home() / ".local" / "share" / "helios" / "llm"
    base_dir.mkdir(parents=True, exist_ok=True)
    
    model_path = base_dir / cfg["filename"]
    if not model_path.exists():
        _log.info(f"Downloading model {model_key}...")
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
        "--log-disable"
    ]
    
    from helios.backend.process.env_scrub import scrubbed_child_env
    
    _log.debug(f"Running local engine: {cmd}")
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=scrubbed_child_env()
    )
    if res.returncode != 0:
        raise Exception(f"llama-cli failed: {res.stderr}")
        
    out = res.stdout
    if prompt in out:
        out = out.split(prompt, 1)[1]
    return out.strip()
