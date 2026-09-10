#!/usr/bin/env python3
"""Authenticated Quantus mining monitor and local stack controller.

The control plane talks to localhost services and starts the official node/miner
binaries without placing the wallet mnemonic in command-line arguments.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.cookies
import json
import math
import os
import re
import secrets
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DATA_DIR = Path(os.environ.get("MINING_CONTROL_DATA_DIR", str(ROOT / "data")))
STATE_FILE = DATA_DIR / "state.json"
ENV_FILE = Path(os.environ.get("MINING_CONTROL_ENV_FILE", str(ROOT / ".env")))

METRICS_TIMEOUT_SECONDS = 2.5
RPC_TIMEOUT_SECONDS = 3.5
REWARD_REFRESH_SECONDS = 60
WALLET_REFRESH_SECONDS = 15
SESSION_TTL_SECONDS = 12 * 60 * 60
MAX_BODY_BYTES = 64 * 1024
COOKIE_NAME = "quantus_mining_control_session"

MINING_DIR = Path(os.environ.get("MINING_CONTROL_MINING_DIR", "/root/quantus-mining"))
BIN_DIR = MINING_DIR / "bin"
NODE_BIN = Path(os.environ.get("MINING_CONTROL_NODE_BIN", str(BIN_DIR / "quantus-node")))
MINER_BIN = Path(os.environ.get("MINING_CONTROL_MINER_BIN", str(BIN_DIR / "quantus-miner")))
NODE_BASE_PATH = Path(
    os.environ.get("MINING_CONTROL_NODE_BASE_PATH", "/root/.local/share/quantus-node")
)
NODE_KEY_FILE = Path(
    os.environ.get("MINING_CONTROL_NODE_KEY_FILE", str(MINING_DIR / "node_key.p2p"))
)
NODE_LOG = Path(os.environ.get("MINING_CONTROL_NODE_LOG", str(MINING_DIR / "logs/node.log")))
MINER_LOG = Path(os.environ.get("MINING_CONTROL_MINER_LOG", str(MINING_DIR / "logs/miner.log")))
NODE_PID_FILE = Path(os.environ.get("MINING_CONTROL_NODE_PID_FILE", str(MINING_DIR / "node.pid")))
MINER_PID_FILE = Path(os.environ.get("MINING_CONTROL_MINER_PID_FILE", str(MINING_DIR / "miner.pid")))
WALLET_FILE = Path(
    os.environ.get("MINING_CONTROL_WALLET_FILE", str(MINING_DIR / "wallet-address"))
)
CHAIN = os.environ.get("MINING_CONTROL_CHAIN", "mainnet")
NODE_NAME_DEFAULT = os.environ.get("MINING_CONTROL_NODE_NAME", "quantus-miner")
MINER_LISTEN_PORT = int(os.environ.get("MINING_CONTROL_MINER_LISTEN_PORT", "9833"))
MINER_METRICS_PORT = int(os.environ.get("MINING_CONTROL_MINER_METRICS_PORT", "9900"))
NODE_METRICS_PORT = int(os.environ.get("MINING_CONTROL_NODE_METRICS_PORT", "9615"))
NODE_RPC_PORT = int(os.environ.get("MINING_CONTROL_NODE_RPC_PORT", "9944"))
DEFAULT_CPU_WORKERS = int(os.environ.get("MINING_CONTROL_CPU_WORKERS", "8"))
DEFAULT_GPU_DEVICES = int(os.environ.get("MINING_CONTROL_GPU_DEVICES", "1"))
DEFAULT_GPU_BATCH_SIZE = int(
    os.environ.get("MINING_CONTROL_GPU_BATCH_SIZE", "16777216")
)
DEFAULT_GPU_THROTTLE_MS = int(os.environ.get("MINING_CONTROL_GPU_THROTTLE_MS", "0"))
CUDA_GPU = os.environ.get("MINING_CONTROL_CUDA_GPU", "1") != "0"
TLS_ENABLED = os.environ.get("MINING_CONTROL_TLS", "1") != "0"
REQUIRE_HTTPS = os.environ.get("MINING_CONTROL_REQUIRE_HTTPS", "1") != "0"
TLS_CERT_FILE = Path(
    os.environ.get("MINING_CONTROL_TLS_CERT", str(ROOT / "tls" / "cert.pem"))
)
TLS_KEY_FILE = Path(
    os.environ.get("MINING_CONTROL_TLS_KEY", str(ROOT / "tls" / "key.pem"))
)


def load_env_file() -> None:
    if not ENV_FILE.is_file():
        return
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()

BIND_HOST = os.environ.get("MINING_CONTROL_BIND", "0.0.0.0")
BIND_PORT = int(os.environ.get("MINING_CONTROL_PORT", "10200"))
MINER_METRICS_URL = f"http://127.0.0.1:{MINER_METRICS_PORT}/metrics"
NODE_METRICS_URL = f"http://127.0.0.1:{NODE_METRICS_PORT}/metrics"
NODE_RPC_URL = f"http://127.0.0.1:{NODE_RPC_PORT}"


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def password_config() -> tuple[int, bytes, bytes]:
    raw = os.environ.get("MINING_CONTROL_PASSWORD_HASH", "")
    parts = raw.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        raise RuntimeError("MINING_CONTROL_PASSWORD_HASH is missing or invalid")
    iterations = int(parts[1])
    salt = _b64decode(parts[2])
    digest = _b64decode(parts[3])
    if not (100_000 <= iterations <= 2_000_000) or len(salt) < 16 or len(digest) < 32:
        raise RuntimeError("MINING_CONTROL_PASSWORD_HASH parameters are invalid")
    return iterations, salt, digest


PASSWORD_ITERATIONS, PASSWORD_SALT, PASSWORD_DIGEST = password_config()


def verify_password(candidate: str) -> bool:
    if not isinstance(candidate, str) or not candidate or len(candidate) > 256:
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        candidate.encode("utf-8"),
        PASSWORD_SALT,
        PASSWORD_ITERATIONS,
        dklen=len(PASSWORD_DIGEST),
    )
    return hmac.compare_digest(derived, PASSWORD_DIGEST)


def now_ms() -> int:
    return int(time.time() * 1000)


def safe_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def safe_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def qtc_from_planck(value: int | None) -> float | None:
    if value is None:
        return None
    return float(Decimal(value) / Decimal(1_000_000_000_000))


def read_http(url: str, method: str = "GET", body: bytes | None = None, timeout: float = 3.0) -> bytes:
    request = Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": "quantus-mining-monitor/1.0",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        content = response.read(2_000_000)
        if len(content) >= 2_000_000:
            raise RuntimeError("response too large")
        return content


def fetch_json(url: str, timeout: float = 5.0) -> object:
    return json.loads(read_http(url, timeout=timeout).decode("utf-8"))


def rpc_call(method: str, params: list[object] | None = None) -> object:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []},
        separators=(",", ":"),
    ).encode("utf-8")
    response = json.loads(read_http(NODE_RPC_URL, "POST", body, RPC_TIMEOUT_SECONDS).decode("utf-8"))
    if "error" in response:
        error = response["error"]
        message = error.get("message", "RPC error") if isinstance(error, dict) else "RPC error"
        raise RuntimeError(str(message))
    if "result" not in response:
        raise RuntimeError("RPC result missing")
    return response["result"]


def parse_prometheus(text: str) -> list[tuple[str, dict[str, str], float]]:
    rows: list[tuple[str, dict[str, str], float]] = []
    pattern = re.compile(
        r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+"
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|NaN|[+-]?Inf)\s*$"
    )
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        value = safe_float(match.group(3))
        if value is None:
            continue
        labels: dict[str, str] = {}
        label_text = match.group(2) or ""
        for label in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"])*)"', label_text):
            labels[label.group(1)] = (
                label.group(2)
                .replace(r"\\", "\\")
                .replace(r"\"", '"')
                .replace(r"\n", "\n")
            )
        rows.append((match.group(1), labels, value))
    return rows


def metric_value(
    rows: list[tuple[str, dict[str, str], float]],
    name: str,
    labels: dict[str, str] | None = None,
) -> float | None:
    expected = labels or {}
    for row_name, row_labels, value in rows:
        if row_name == name and all(row_labels.get(key) == val for key, val in expected.items()):
            return value
    return None


# xxHash64 is used by Substrate's twox128 storage prefixes.
MASK64 = (1 << 64) - 1
XX_PRIME1 = 11400714785074694791
XX_PRIME2 = 14029467366897019727
XX_PRIME3 = 1609587929392839161
XX_PRIME4 = 9650029242287828579
XX_PRIME5 = 2870177450012600261


def rotl64(value: int, bits: int) -> int:
    return ((value << bits) | (value >> (64 - bits))) & MASK64


def xxh64(data: bytes, seed: int = 0) -> int:
    length = len(data)
    index = 0
    if length >= 32:
        v1 = (seed + XX_PRIME1 + XX_PRIME2) & MASK64
        v2 = (seed + XX_PRIME2) & MASK64
        v3 = seed & MASK64
        v4 = (seed - XX_PRIME1) & MASK64
        limit = length - 32
        while index <= limit:
            for name in ("v1", "v2", "v3", "v4"):
                value = locals()[name]
                chunk = int.from_bytes(data[index:index + 8], "little")
                value = (value + chunk * XX_PRIME2) & MASK64
                value = rotl64(value, 31)
                value = (value * XX_PRIME1) & MASK64
                if name == "v1":
                    v1 = value
                elif name == "v2":
                    v2 = value
                elif name == "v3":
                    v3 = value
                else:
                    v4 = value
                index += 8
        result = (
            rotl64(v1, 1)
            + rotl64(v2, 7)
            + rotl64(v3, 12)
            + rotl64(v4, 18)
        ) & MASK64
        for value in (v1, v2, v3, v4):
            value = (value * XX_PRIME2) & MASK64
            value = rotl64(value, 31)
            value = (value * XX_PRIME1) & MASK64
            result ^= value
            result = ((result * XX_PRIME1) + XX_PRIME4) & MASK64
    else:
        result = (seed + XX_PRIME5) & MASK64
    result = (result + length) & MASK64
    while index + 8 <= length:
        value = int.from_bytes(data[index:index + 8], "little")
        value = (value * XX_PRIME2) & MASK64
        value = rotl64(value, 31)
        value = (value * XX_PRIME1) & MASK64
        result ^= value
        result = (rotl64(result, 27) * XX_PRIME1 + XX_PRIME4) & MASK64
        index += 8
    if index + 4 <= length:
        result ^= (int.from_bytes(data[index:index + 4], "little") * XX_PRIME1) & MASK64
        result = (rotl64(result, 23) * XX_PRIME2 + XX_PRIME3) & MASK64
        index += 4
    while index < length:
        result ^= (data[index] * XX_PRIME5) & MASK64
        result = (rotl64(result, 11) * XX_PRIME1) & MASK64
        index += 1
    result ^= result >> 33
    result = (result * XX_PRIME2) & MASK64
    result ^= result >> 29
    result = (result * XX_PRIME3) & MASK64
    result ^= result >> 32
    return result & MASK64


def twox128(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return xxh64(encoded, 0).to_bytes(8, "little") + xxh64(encoded, 1).to_bytes(8, "little")


BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_INDEX = {char: index for index, char in enumerate(BASE58_ALPHABET)}


def base58_decode(value: str) -> bytes:
    number = 0
    for char in value:
        if char not in BASE58_INDEX:
            raise ValueError("invalid base58 address")
        number = number * 58 + BASE58_INDEX[char]
    decoded = number.to_bytes(max(1, (number.bit_length() + 7) // 8), "big")
    leading = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading + (b"" if number == 0 else decoded)


def decode_ss58(address: str, expected_format: int = 189) -> bytes:
    raw = base58_decode(address.strip())
    if len(raw) < 4:
        raise ValueError("short SS58 address")
    first = raw[0]
    if first & 0x40:
        if len(raw) < 2:
            raise ValueError("short SS58 prefix")
        second = raw[1]
        address_format = ((first & 0x3F) << 2) | (second >> 6) | ((second & 0x3F) << 8)
        prefix_length = 2
    else:
        address_format = first
        prefix_length = 1
    if address_format != expected_format:
        raise ValueError("unexpected SS58 format")
    account = raw[prefix_length:-2]
    checksum = raw[-2:]
    expected = hashlib.blake2b(b"SS58PRE" + raw[:-2], digest_size=64).digest()[:2]
    if len(account) != 32 or not hmac.compare_digest(checksum, expected):
        raise ValueError("invalid SS58 checksum")
    return account


def account_storage_key(address: str) -> str:
    account = decode_ss58(address)
    key = (
        twox128("System")
        + twox128("Account")
        + hashlib.blake2b(account, digest_size=16).digest()
        + account
    )
    return "0x" + key.hex()


def decode_account(raw: str | None) -> tuple[int, int, int]:
    if raw is None:
        return 0, 0, 0
    if not isinstance(raw, str) or not raw.startswith("0x"):
        raise ValueError("invalid account storage")
    data = bytes.fromhex(raw[2:])
    if len(data) != 80:
        raise ValueError("unsupported account storage length")
    free = int.from_bytes(data[16:32], "little")
    reserved = int.from_bytes(data[32:48], "little")
    frozen = int.from_bytes(data[48:64], "little")
    return free, reserved, frozen


def read_wallet() -> dict[str, object]:
    address = WALLET_FILE.read_text(encoding="utf-8").strip()
    if not address:
        raise ValueError("wallet address is empty")
    key = account_storage_key(address)
    head = rpc_call("chain_getFinalizedHead")
    raw = rpc_call("state_getStorage", [key, head])
    free, reserved, frozen = decode_account(raw if isinstance(raw, str) else None)
    return {
        "address": address,
        "available_planck": str(free),
        "reserved_planck": str(reserved),
        "frozen_planck": str(frozen),
        "balance_planck": str(free + reserved),
        "available_qtc": qtc_from_planck(free),
        "balance_qtc": qtc_from_planck(free + reserved),
        "checked_at": now_ms(),
    }


def read_gpu() -> dict[str, object]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,power.limit",
        "--format=csv,noheader,nounits",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError((result.stderr or "nvidia-smi unavailable").strip()[:200])
    values = [item.strip() for item in result.stdout.strip().splitlines()[0].split(",")]
    if len(values) < 7:
        raise RuntimeError("unexpected nvidia-smi output")
    return {
        "name": values[0],
        "temperature_c": safe_float(values[1]),
        "utilization_percent": safe_float(values[2]),
        "memory_used_mb": safe_float(values[3]),
        "memory_total_mb": safe_float(values[4]),
        "power_w": safe_float(values[5]),
        "power_limit_w": safe_float(values[6]),
    }


def process_details(pid_value: object) -> dict[str, object]:
    pid = safe_int(pid_value)
    if pid is None or pid <= 0:
        return {"pid": None, "running": False}
    proc = Path("/proc") / str(pid)
    try:
        command = (proc / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return {"pid": pid, "running": False}
    return {
        "pid": pid,
        "running": proc.is_dir() and "quantus-miner" in command,
        "command": command[:180],
    }


def read_miner_process() -> dict[str, object]:
    pid = read_pid_file(MINER_PID_FILE)
    details = process_details(pid)
    if details["running"]:
        return details
    found = find_process("quantus-miner")
    return process_details(found[0] if found else None)


def read_activity() -> dict[str, object]:
    try:
        with MINER_LOG.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 512_000))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return {"recent": [], "jobs_in_log_window": 0, "solutions_in_log_window": 0}

    recent: list[dict[str, object]] = []
    jobs = 0
    solutions = 0
    for line in text.splitlines():
        lower = line.lower()
        if "received job:" in lower:
            jobs += 1
            label = "收到新区块任务"
        elif "found solution" in lower or "accepted" in lower or "submitted" in lower:
            solutions += 1
            label = "发现或提交候选解"
        elif "disconnected" in lower or "connection error" in lower:
            label = "节点连接异常"
        elif "error" in lower or "warn" in lower:
            label = "矿工日志出现警告"
        else:
            continue
        timestamp = line[1:21] if line.startswith("[") and len(line) >= 21 else ""
        recent.append({"time": timestamp, "label": label})
    return {
        "recent": recent[-8:][::-1],
        "jobs_in_log_window": jobs,
        "solutions_in_log_window": solutions,
    }


class MiningControlError(RuntimeError):
    """An expected, user-visible control-plane error."""


def effective_cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def normalize_mnemonic(value: object) -> str:
    if not isinstance(value, str):
        raise MiningControlError("请填写 24 个单词的 Quantus 助记词。")
    phrase = " ".join(value.strip().split())
    words = phrase.split(" ") if phrase else []
    if len(words) != 24:
        raise MiningControlError(f"助记词必须正好包含 24 个单词，当前为 {len(words)} 个。")
    if len(phrase) > 1024:
        raise MiningControlError("助记词长度无效。")
    return phrase


def parse_wormhole_keygen_output(output: str) -> tuple[str, str]:
    address_match = re.search(
        r"(?m)^Address:\s*(qz[1-9A-HJ-NP-Za-km-z]{40,62})\s*$",
        output,
    )
    inner_match = re.search(
        r"(?mi)^Inner\s+Hash:\s*(0x[0-9a-f]{64})\s*$",
        output,
    )
    if not address_match or not inner_match:
        # Older releases used a lowercase, underscore-separated label.
        inner_match = re.search(
            r"(?mi)^inner_hash:\s*(0x[0-9a-f]{64})\s*$",
            output,
        )
    if not address_match or not inner_match:
        raise MiningControlError("助记词派生失败，请确认是 Quantus 的有效 24 词助记词。")
    return address_match.group(1), inner_match.group(1)


def read_process_argv(pid: int) -> list[str]:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [
        chunk.decode("utf-8", "replace")
        for chunk in raw.split(b"\x00")
        if chunk
    ]


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    try:
        state = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").split()[2]
    except (OSError, IndexError):
        return False
    return state != "Z"


def find_process(executable_name: str) -> tuple[int, list[str]] | None:
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        argv = read_process_argv(pid)
        if argv and Path(argv[0]).name == executable_name and process_is_running(pid):
            return pid, argv
    return None


def argument_value(argv: list[str], name: str) -> str | None:
    try:
        index = argv.index(name)
    except ValueError:
        return None
    return argv[index + 1] if index + 1 < len(argv) else None


def read_pid_file(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def process_matches(pid: int | None, executable_name: str) -> bool:
    if pid is None or not process_is_running(pid):
        return False
    argv = read_process_argv(pid)
    return bool(argv and Path(argv[0]).name == executable_name)


def stop_process(pid: int, executable_name: str) -> None:
    if not process_matches(pid, executable_name):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 15
    while time.time() < deadline and process_is_running(pid):
        time.sleep(0.25)
    if process_is_running(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_for_port(port: int, timeout_seconds: float) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if port_is_open(port):
            return True
        time.sleep(0.5)
    return port_is_open(port)


class MiningControl:
    """Derive a reward identity and manage the official node/miner pair."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_action_at = 0.0

    def _auth_supported(self) -> bool:
        try:
            result = subprocess.run(
                [str(NODE_BIN), "--help"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        help_text = f"{result.stdout}\n{result.stderr}"
        return "miner-auth-token-file" in help_text

    def _auth_paths(self) -> tuple[Path, Path]:
        chain_dir = NODE_BASE_PATH / "chains" / CHAIN
        return chain_dir / "miner-auth-token", chain_dir / "miner-tls-cert-sha256"

    def _derive_wormhole(self, phrase: str, wallet_index: int) -> tuple[str, str]:
        if not NODE_BIN.is_file():
            raise MiningControlError("未找到 quantus-node，请先执行一键部署命令。")
        command = [
            str(NODE_BIN),
            "key",
            "quantus",
            "--scheme",
            "wormhole",
            "--words",
            "--wallet-index",
            str(wallet_index),
        ]
        try:
            result = subprocess.run(
                command,
                input=f"{phrase}\n",
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise MiningControlError("助记词派生超时，请稍后重试。") from error
        except OSError as error:
            raise MiningControlError("无法运行 quantus-node 密钥派生命令。") from error
        output = f"{result.stdout}\n{result.stderr}"
        if result.returncode != 0:
            raise MiningControlError("助记词无效或当前版本无法派生该钱包。")
        return parse_wormhole_keygen_output(output)

    def _ensure_node_key(self) -> None:
        if NODE_KEY_FILE.is_file():
            return
        MINING_DIR.mkdir(parents=True, exist_ok=True)
        NODE_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                [str(NODE_BIN), "key", "generate-node-key", "--file", str(NODE_KEY_FILE)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise MiningControlError("无法生成节点 P2P 密钥。") from error
        if result.returncode != 0 or not NODE_KEY_FILE.is_file():
            raise MiningControlError("节点 P2P 密钥生成失败。")
        os.chmod(NODE_KEY_FILE, 0o600)

    def _write_wallet_address(self, address: str) -> None:
        WALLET_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = WALLET_FILE.with_name(f".{WALLET_FILE.name}.{os.getpid()}.tmp")
        try:
            temp_path.write_text(f"{address}\n", encoding="utf-8")
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, WALLET_FILE)
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass

    def _stop_named(self, executable_name: str, pid_file: Path) -> bool:
        pid = read_pid_file(pid_file)
        if not process_matches(pid, executable_name):
            found = find_process(executable_name)
            pid = found[0] if found else None
        if pid is None:
            try:
                pid_file.unlink()
            except OSError:
                pass
            return False
        stop_process(pid, executable_name)
        try:
            pid_file.unlink()
        except OSError:
            pass
        return True

    def _start_node(self, node_name: str, inner_hash: str) -> int:
        self._ensure_node_key()
        NODE_LOG.parent.mkdir(parents=True, exist_ok=True)
        command = [
            str(NODE_BIN),
            "--name",
            node_name,
            "--validator",
            "--base-path",
            str(NODE_BASE_PATH),
            "--miner-listen-port",
            str(MINER_LISTEN_PORT),
            "--chain",
            CHAIN,
            "--node-key-file",
            str(NODE_KEY_FILE),
            "--rewards-inner-hash",
            inner_hash,
            "--max-blocks-per-request",
            "64",
            "--sync",
            "full",
        ]
        try:
            log_handle = NODE_LOG.open("ab", buffering=0)
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                cwd=str(MINING_DIR),
                start_new_session=True,
            )
        except OSError as error:
            raise MiningControlError("节点启动失败，请检查 node.log。") from error
        finally:
            try:
                log_handle.close()
            except (UnboundLocalError, AttributeError):
                pass
        NODE_PID_FILE.write_text(f"{process.pid}\n", encoding="utf-8")
        os.chmod(NODE_PID_FILE, 0o600)
        try:
            if self._auth_supported():
                token_path, pin_path = self._auth_paths()
                # Current Quantus releases expose the miner listener over UDP,
                # so TCP probing port 9833 cannot be used as a readiness check.
                deadline = time.time() + 120
                while time.time() < deadline and process_is_running(process.pid):
                    if token_path.is_file() and pin_path.is_file():
                        break
                    time.sleep(0.5)
                if not process_is_running(process.pid):
                    raise MiningControlError("节点启动后退出，请检查 node.log。")
                if not (token_path.is_file() and pin_path.is_file()):
                    raise MiningControlError("节点认证文件未生成，请检查 node.log。")
            elif not process_is_running(process.pid):
                raise MiningControlError("节点启动后退出，请检查 node.log。")
        except Exception:
            stop_process(process.pid, "quantus-node")
            raise
        return process.pid

    def _start_miner(self, cpu_workers: int, gpu_devices: int) -> int:
        if not MINER_BIN.is_file():
            raise MiningControlError("未找到 quantus-miner，请先执行一键部署命令。")
        command = [
            str(MINER_BIN),
            "serve",
            "--cpu-workers",
            str(cpu_workers),
            "--gpu-devices",
            str(gpu_devices),
            "--node-addr",
            f"127.0.0.1:{MINER_LISTEN_PORT}",
            "--metrics-port",
            str(MINER_METRICS_PORT),
        ]
        if gpu_devices > 0:
            command.extend(["--gpu-batch-size", str(DEFAULT_GPU_BATCH_SIZE)])
            command.extend(["--gpu-throttle-ms", str(DEFAULT_GPU_THROTTLE_MS)])
            if CUDA_GPU:
                command.append("--cuda-gpu")
        if self._auth_supported():
            token_path, pin_path = self._auth_paths()
            if not (token_path.is_file() and pin_path.is_file()):
                raise MiningControlError("节点认证文件不存在，无法安全连接矿工。")
            command.extend(
                [
                    "--auth-token-file",
                    str(token_path),
                    "--tls-cert-sha256-file",
                    str(pin_path),
                ]
            )
        MINER_LOG.parent.mkdir(parents=True, exist_ok=True)
        try:
            log_handle = MINER_LOG.open("ab", buffering=0)
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                cwd=str(MINING_DIR),
                start_new_session=True,
            )
        except OSError as error:
            raise MiningControlError("矿工启动失败，请检查 miner.log。") from error
        finally:
            try:
                log_handle.close()
            except (UnboundLocalError, AttributeError):
                pass
        MINER_PID_FILE.write_text(f"{process.pid}\n", encoding="utf-8")
        os.chmod(MINER_PID_FILE, 0o600)
        if not wait_for_port(MINER_METRICS_PORT, 30):
            stop_process(process.pid, "quantus-miner")
            raise MiningControlError("矿工未在 30 秒内开启指标接口，请检查 miner.log。")
        return process.pid

    def snapshot(self) -> dict[str, object]:
        node = find_process("quantus-node")
        miner = find_process("quantus-miner")
        node_argv = node[1] if node else []
        miner_argv = miner[1] if miner else []
        try:
            address = WALLET_FILE.read_text(encoding="utf-8").strip() or None
        except OSError:
            address = None
        return {
            "node_running": node is not None,
            "miner_running": miner is not None,
            "node_pid": node[0] if node else None,
            "miner_pid": miner[0] if miner else None,
            "node_name": argument_value(node_argv, "--name"),
            "cpu_workers": safe_int(argument_value(miner_argv, "--cpu-workers")),
            "gpu_devices": safe_int(argument_value(miner_argv, "--gpu-devices")),
            "wallet_address": address,
        }

    def start(self, payload: object) -> dict[str, object]:
        if not self.lock.acquire(timeout=5):
            raise MiningControlError("已有控制操作正在执行，请稍后重试。")
        try:
            return self._start_unlocked(payload)
        finally:
            self.lock.release()

    def _start_unlocked(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise MiningControlError("启动参数格式无效。")
        phrase = normalize_mnemonic(payload.get("mnemonic"))
        try:
            raw_name = payload.get("node_name", NODE_NAME_DEFAULT)
            node_name = str(raw_name).strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", node_name):
                raise MiningControlError("节点名只能使用字母、数字、点、下划线和短横线。")

            available_cpus = effective_cpu_count()
            raw_cpu = payload.get("cpu_workers", DEFAULT_CPU_WORKERS)
            raw_gpu = payload.get("gpu_devices", DEFAULT_GPU_DEVICES)
            raw_index = payload.get("wallet_index", 0)
            try:
                cpu_workers = int(raw_cpu)
                gpu_devices = int(raw_gpu)
                wallet_index = int(raw_index)
            except (TypeError, ValueError):
                raise MiningControlError("并发参数必须是整数。")
            if cpu_workers < 0 or cpu_workers > available_cpus:
                raise MiningControlError(f"CPU worker 必须在 0 到 {available_cpus} 之间。")
            if gpu_devices < 0 or gpu_devices > 16:
                raise MiningControlError("GPU 设备数必须在 0 到 16 之间。")
            if wallet_index < 0 or wallet_index >= (1 << 31):
                raise MiningControlError("账户索引无效。")
            if cpu_workers == 0 and gpu_devices == 0:
                raise MiningControlError("至少启用一个 CPU worker 或 GPU 设备。")

            address, inner_hash = self._derive_wormhole(phrase, wallet_index)
            current_node = find_process("quantus-node")
            current_inner = argument_value(current_node[1], "--rewards-inner-hash") if current_node else None
            current_name = argument_value(current_node[1], "--name") if current_node else None
            reuse_node = (
                current_node is not None
                and current_inner is not None
                and current_inner.lower() == inner_hash.lower()
                and current_name == node_name
                and port_is_open(MINER_LISTEN_PORT)
            )

            if not reuse_node:
                self._stop_named("quantus-miner", MINER_PID_FILE)
                self._stop_named("quantus-node", NODE_PID_FILE)
                self._start_node(node_name, inner_hash)
            else:
                if self._auth_supported():
                    token_path, pin_path = self._auth_paths()
                    if not (token_path.is_file() and pin_path.is_file()):
                        raise MiningControlError("节点认证文件不存在，无法安全启动矿工。")

            self._write_wallet_address(address)
            self._stop_named("quantus-miner", MINER_PID_FILE)
            miner_pid = self._start_miner(cpu_workers, gpu_devices)
            self.last_action_at = time.time()
            return {
                "ok": True,
                "action": "started",
                "address": address,
                "node_name": node_name,
                "cpu_workers": cpu_workers,
                "gpu_devices": gpu_devices,
                "miner_pid": miner_pid,
                "control": self.snapshot(),
            }
        finally:
            # Drop the local reference as soon as the child process has consumed stdin.
            phrase = ""

    def stop(self) -> dict[str, object]:
        if not self.lock.acquire(timeout=5):
            raise MiningControlError("已有控制操作正在执行，请稍后重试。")
        try:
            self._stop_named("quantus-miner", MINER_PID_FILE)
            self._stop_named("quantus-node", NODE_PID_FILE)
            self.last_action_at = time.time()
            return {"ok": True, "action": "stopped", "control": self.snapshot()}
        finally:
            self.lock.release()


CONTROL = MiningControl()


class RewardCache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fetched_at = 0.0
        self.reward_qtc: float | None = None
        self.source = "unavailable"
        self.error: str | None = None

    def get(self) -> dict[str, object]:
        with self.lock:
            if time.time() - self.fetched_at < REWARD_REFRESH_SECONDS:
                return {
                    "reward_qtc": self.reward_qtc,
                    "source": self.source,
                    "fetched_at": int(self.fetched_at * 1000) if self.fetched_at else None,
                    "error": self.error,
                }
            try:
                terms = fetch_json("https://quanpool.com/api/terms")
                rounds = fetch_json("https://quanpool.com/api/rounds/mainnet")
                terms_object = terms if isinstance(terms, dict) else {}
                rounds_list = rounds if isinstance(rounds, list) else []
                candidate: object = None
                source = "terms"
                if rounds_list and isinstance(rounds_list[0], dict):
                    candidate = rounds_list[0].get("reward")
                    source = "latest confirmed round"
                if candidate is None:
                    candidate = terms_object.get("block_reward_planck")
                if not isinstance(candidate, str) or not re.fullmatch(r"\d{1,24}", candidate):
                    raise ValueError("invalid reward response")
                reward = float(Decimal(candidate) / Decimal(1_000_000_000_000))
                if not math.isfinite(reward) or reward <= 0:
                    raise ValueError("invalid reward value")
                self.reward_qtc = reward
                self.source = source
                self.error = None
            except (OSError, ValueError, TypeError, HTTPError, URLError, json.JSONDecodeError) as error:
                self.error = str(error)[:200]
                self.source = "unavailable"
            self.fetched_at = time.time()
            return {
                "reward_qtc": self.reward_qtc,
                "source": self.source,
                "fetched_at": int(self.fetched_at * 1000),
                "error": self.error,
            }


class WalletCache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.checked_at = 0.0
        self.value: dict[str, object] | None = None
        self.error: str | None = None

    def get(self) -> tuple[dict[str, object] | None, str | None]:
        with self.lock:
            if self.value is not None and time.time() - self.checked_at < WALLET_REFRESH_SECONDS:
                return self.value, self.error
            try:
                self.value = read_wallet()
                self.error = None
            except (OSError, ValueError, RuntimeError, HTTPError, URLError, json.JSONDecodeError) as error:
                self.error = str(error)[:200]
            self.checked_at = time.time()
            return self.value, self.error


def load_tracking_state() -> dict[str, object]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


TRACKING_LOCK = threading.Lock()
TRACKING_STATE = load_tracking_state()


def persist_tracking_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=DATA_DIR, delete=False) as handle:
        json.dump(TRACKING_STATE, handle, ensure_ascii=True, separators=(",", ":"))
        temp_name = handle.name
    os.replace(temp_name, STATE_FILE)


def update_tracking(hashes_total: int | None, balance_planck: int | None) -> dict[str, object]:
    now = time.time()
    with TRACKING_LOCK:
        started_at = safe_float(TRACKING_STATE.get("started_at"))
        if started_at is None or started_at <= 0:
            started_at = now
            TRACKING_STATE["started_at"] = started_at

        initial_hashes = safe_int(TRACKING_STATE.get("initial_hashes"))
        if hashes_total is not None:
            if initial_hashes is None or hashes_total < initial_hashes:
                initial_hashes = hashes_total
                TRACKING_STATE["initial_hashes"] = initial_hashes
            TRACKING_STATE["last_hashes"] = hashes_total

        initial_balance = safe_int(TRACKING_STATE.get("initial_balance_planck"))
        if balance_planck is not None:
            if initial_balance is None:
                initial_balance = balance_planck
                TRACKING_STATE["initial_balance_planck"] = str(initial_balance)
            TRACKING_STATE["last_balance_planck"] = str(balance_planck)

        elapsed = max(1.0, now - started_at)
        session_hashes = None
        average_hash_rate = None
        if hashes_total is not None and initial_hashes is not None and hashes_total >= initial_hashes:
            session_hashes = hashes_total - initial_hashes
            average_hash_rate = session_hashes / elapsed

        observed_planck = None
        if balance_planck is not None and initial_balance is not None:
            observed_planck = balance_planck - initial_balance

        last_persist = safe_float(TRACKING_STATE.get("last_persisted_at")) or 0
        if now - last_persist >= 10:
            TRACKING_STATE["last_persisted_at"] = now
            try:
                persist_tracking_state()
            except OSError:
                pass

        return {
            "started_at": int(started_at * 1000),
            "elapsed_seconds": int(elapsed),
            "session_hashes": session_hashes,
            "average_hash_rate_hs": average_hash_rate,
            "observed_balance_delta_qtc": qtc_from_planck(observed_planck),
        }


REWARD_CACHE = RewardCache()
WALLET_CACHE = WalletCache()
COLLECT_LOCK = threading.Lock()


def build_snapshot() -> dict[str, object]:
    with COLLECT_LOCK:
        errors: list[str] = []
        miner_rows: list[tuple[str, dict[str, str], float]] = []
        node_rows: list[tuple[str, dict[str, str], float]] = []

        try:
            miner_rows = parse_prometheus(read_http(MINER_METRICS_URL, timeout=METRICS_TIMEOUT_SECONDS).decode("utf-8"))
        except (OSError, HTTPError, URLError, RuntimeError) as error:
            errors.append(f"矿工指标：{str(error)[:120]}")
        try:
            node_rows = parse_prometheus(read_http(NODE_METRICS_URL, timeout=METRICS_TIMEOUT_SECONDS).decode("utf-8"))
        except (OSError, HTTPError, URLError, RuntimeError) as error:
            errors.append(f"节点指标：{str(error)[:120]}")

        miner_hash_rate = metric_value(miner_rows, "miner_hash_rate")
        gpu_hash_rate = metric_value(miner_rows, "miner_gpu_hash_rate")
        cpu_hash_rate = metric_value(miner_rows, "miner_cpu_hash_rate")
        hashes_total_float = metric_value(miner_rows, "miner_hashes_total")
        hashes_total = int(hashes_total_float) if hashes_total_float is not None and hashes_total_float >= 0 else None
        active_jobs = metric_value(miner_rows, "miner_active_jobs")

        height = metric_value(node_rows, "qpow_metrics", {"data_group": "chain_height", "chain": "mainnet"})
        difficulty = metric_value(node_rows, "qpow_metrics", {"data_group": "difficulty", "chain": "mainnet"})
        last_block_duration_ms = metric_value(
            node_rows,
            "qpow_metrics",
            {"data_group": "last_block_duration", "chain": "mainnet"},
        )
        block_time_seconds = (
            last_block_duration_ms / 1000
            if last_block_duration_ms is not None and last_block_duration_ms > 0
            else None
        )

        try:
            health = rpc_call("system_health")
        except (OSError, HTTPError, URLError, RuntimeError, json.JSONDecodeError) as error:
            health = {}
            errors.append(f"节点健康：{str(error)[:120]}")
        try:
            sync_state = rpc_call("system_syncState")
        except (OSError, HTTPError, URLError, RuntimeError, json.JSONDecodeError) as error:
            sync_state = {}
            errors.append(f"同步状态：{str(error)[:120]}")

        reward = REWARD_CACHE.get()
        wallet, wallet_error = WALLET_CACHE.get()
        if wallet_error:
            errors.append(f"钱包余额：{wallet_error}")

        gpu: dict[str, object]
        try:
            gpu = read_gpu()
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            gpu = {
                "name": "unavailable",
                "temperature_c": None,
                "utilization_percent": None,
                "memory_used_mb": None,
                "memory_total_mb": None,
                "power_w": None,
                "power_limit_w": None,
            }
            errors.append(f"GPU：{str(error)[:120]}")

        process = read_miner_process()
        tracking = update_tracking(
            hashes_total,
            safe_int(wallet.get("balance_planck")) if wallet else None,
        )

        network_hash_rate = None
        if difficulty is not None and block_time_seconds and block_time_seconds > 0:
            network_hash_rate = difficulty / block_time_seconds

        network_share_percent = None
        expected_blocks_day = None
        estimated_daily_qtc = None
        if miner_hash_rate and network_hash_rate and network_hash_rate > 0:
            network_share_percent = miner_hash_rate / network_hash_rate * 100
            if block_time_seconds and block_time_seconds > 0:
                expected_blocks_day = miner_hash_rate / network_hash_rate * 86_400 / block_time_seconds
                if reward["reward_qtc"] is not None:
                    estimated_daily_qtc = expected_blocks_day * float(reward["reward_qtc"])

        power_w = safe_float(gpu.get("power_w"))
        hashes_per_watt = miner_hash_rate / power_w if miner_hash_rate and power_w and power_w > 0 else None
        energy_kwh_day = power_w * 24 / 1000 if power_w and power_w > 0 else None

        online = bool(process.get("running")) and miner_hash_rate is not None and (active_jobs or 0) >= 1
        syncing = bool(sync_state.get("currentBlock") != sync_state.get("highestBlock")) if isinstance(sync_state, dict) else None
        if isinstance(health, dict) and "isSyncing" in health:
            syncing = bool(health.get("isSyncing"))

        if errors:
            state = "warning"
        elif online and syncing is False:
            state = "online"
        elif online:
            state = "syncing"
        else:
            state = "offline"

        activity = read_activity()
        return {
            "ok": True,
            "server_time": now_ms(),
            "status": {
                "state": state,
                "online": online,
                "syncing": syncing,
                "errors": errors,
            },
            "miner": {
                "hash_rate_hs": miner_hash_rate,
                "gpu_hash_rate_hs": gpu_hash_rate,
                "cpu_hash_rate_hs": cpu_hash_rate,
                "hashes_total": hashes_total,
                "active_jobs": active_jobs,
                "workers": metric_value(miner_rows, "miner_workers"),
                "gpu_devices": metric_value(miner_rows, "miner_gpu_devices"),
                "average_hash_rate_hs": tracking["average_hash_rate_hs"],
                "session_hashes": tracking["session_hashes"],
                "process": process,
                "network_share_percent": network_share_percent,
                "expected_blocks_day": expected_blocks_day,
                "estimated_daily_qtc": estimated_daily_qtc,
                "hashes_per_watt": hashes_per_watt,
                "energy_kwh_day": energy_kwh_day,
            },
            "node": {
                "height": height,
                "difficulty": difficulty,
                "last_block_duration_ms": last_block_duration_ms,
                "block_time_seconds": block_time_seconds,
                "peers": health.get("peers") if isinstance(health, dict) else None,
                "is_syncing": health.get("isSyncing") if isinstance(health, dict) else syncing,
                "current_block": sync_state.get("currentBlock") if isinstance(sync_state, dict) else None,
                "highest_block": sync_state.get("highestBlock") if isinstance(sync_state, dict) else None,
            },
            "gpu": gpu,
            "output": {
                "reward_qtc": reward["reward_qtc"],
                "reward_source": reward["source"],
                "reward_checked_at": reward["fetched_at"],
                "observed_balance_delta_qtc": tracking["observed_balance_delta_qtc"],
                "monitor_started_at": tracking["started_at"],
                "balance_qtc": wallet.get("balance_qtc") if wallet else None,
                "available_qtc": wallet.get("available_qtc") if wallet else None,
                "wallet_address": wallet.get("address") if wallet else None,
            },
            "control": CONTROL.snapshot(),
            "activity": activity,
        }


SESSION_LOCK = threading.Lock()
SESSIONS: dict[str, dict[str, object]] = {}
FAILED_LOGINS: dict[str, list[float]] = {}


def user_agent_key(handler: BaseHTTPRequestHandler) -> str:
    return hashlib.sha256(handler.headers.get("User-Agent", "").encode("utf-8")).hexdigest()


def client_key(handler: BaseHTTPRequestHandler) -> str:
    return handler.client_address[0] if handler.client_address else "unknown"


def session_token(handler: BaseHTTPRequestHandler) -> str | None:
    cookie = http.cookies.SimpleCookie()
    cookie.load(handler.headers.get("Cookie", ""))
    morsel = cookie.get(COOKIE_NAME)
    return morsel.value if morsel else None


def is_authenticated(handler: BaseHTTPRequestHandler) -> bool:
    token = session_token(handler)
    if not token:
        return False
    with SESSION_LOCK:
        record = SESSIONS.get(token)
        if not record:
            return False
        if safe_float(record.get("expires_at")) is None or float(record["expires_at"]) < time.time():
            SESSIONS.pop(token, None)
            return False
        if record.get("user_agent") != user_agent_key(handler):
            return False
        record["expires_at"] = time.time() + SESSION_TTL_SECONDS
        return True


def can_attempt_login(handler: BaseHTTPRequestHandler) -> bool:
    key = client_key(handler)
    cutoff = time.time() - 300
    with SESSION_LOCK:
        recent = [stamp for stamp in FAILED_LOGINS.get(key, []) if stamp >= cutoff]
        FAILED_LOGINS[key] = recent
        return len(recent) < 10


def record_failed_login(handler: BaseHTTPRequestHandler) -> None:
    key = client_key(handler)
    with SESSION_LOCK:
        FAILED_LOGINS.setdefault(key, []).append(time.time())


def create_session(handler: BaseHTTPRequestHandler) -> str:
    token = secrets.token_urlsafe(32)
    with SESSION_LOCK:
        SESSIONS[token] = {
            "expires_at": time.time() + SESSION_TTL_SECONDS,
            "user_agent": user_agent_key(handler),
        }
    return token


def delete_session(handler: BaseHTTPRequestHandler) -> None:
    token = session_token(handler)
    if token:
        with SESSION_LOCK:
            SESSIONS.pop(token, None)


def request_is_secure(handler: BaseHTTPRequestHandler) -> bool:
    forwarded = handler.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    return forwarded == "https" or isinstance(handler.request, ssl.SSLSocket)


def same_origin(handler: BaseHTTPRequestHandler) -> bool:
    origin = handler.headers.get("Origin", "").strip()
    if not origin:
        return True
    host = handler.headers.get("Host", "").strip()
    return origin in {f"http://{host}", f"https://{host}"}


def cookie_flags(handler: BaseHTTPRequestHandler, max_age: int) -> str:
    flags = f"Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict"
    if request_is_secure(handler):
        flags += "; Secure"
    return flags


def common_headers(content_type: str, cache_control: str = "no-store") -> dict[str, str]:
    headers = {
        "Content-Type": content_type,
        "Cache-Control": cache_control,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    }
    if TLS_ENABLED:
        headers["Strict-Transport-Security"] = "max-age=31536000"
    return headers


class MonitorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format_string: str, *args: object) -> None:
        print(f"{self.address_string()} - {format_string % args}", flush=True)

    def send_bytes(self, content: bytes, status: int, content_type: str, extra: dict[str, str] | None = None) -> None:
        headers = common_headers(content_type)
        if extra:
            headers.update(extra)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, payload: object, status: int = 200, extra: dict[str, str] | None = None) -> None:
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_bytes(content, status, "application/json; charset=utf-8", extra)

    def read_body(self) -> bytes:
        length = safe_int(self.headers.get("Content-Length", "0")) or 0
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        return self.rfile.read(length)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self.send_json({"ok": True, "service": "quantus-mining-control"})
            return
        if path == "/api/status":
            if not is_authenticated(self):
                self.send_json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                self.send_json(build_snapshot())
            except Exception as error:  # Keep the process alive if one sensor is unusual.
                self.send_json({"ok": False, "error": str(error)[:200]}, 500)
            return

        static_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "application/javascript; charset=utf-8"),
        }
        file_info = static_files.get(path)
        if not file_info:
            self.send_json({"ok": False, "error": "not found"}, 404)
            return
        filename, content_type = file_info
        try:
            content = (WEB_ROOT / filename).read_bytes()
        except OSError:
            self.send_json({"ok": False, "error": "asset unavailable"}, 500)
            return
        cache = "no-cache" if filename == "index.html" else "public, max-age=60"
        headers = common_headers(content_type, cache)
        self.send_response(200)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/login":
            if not can_attempt_login(self):
                self.send_json({"ok": False, "error": "登录尝试过多，请 5 分钟后再试。"}, 429)
                return
            try:
                body = json.loads(self.read_body().decode("utf-8"))
                password = body.get("password") if isinstance(body, dict) else None
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                password = None
            if not verify_password(password):
                record_failed_login(self)
                self.send_json({"ok": False, "error": "密码错误。"}, 401)
                return
            token = create_session(self)
            cookie = f"{COOKIE_NAME}={token}; {cookie_flags(self, SESSION_TTL_SECONDS)}"
            self.send_json({"ok": True}, extra={"Set-Cookie": cookie})
            return

        if path == "/api/logout":
            delete_session(self)
            cookie = f"{COOKIE_NAME}=; {cookie_flags(self, 0)}"
            self.send_json({"ok": True}, extra={"Set-Cookie": cookie})
            return

        if path in {"/api/start", "/api/stop"}:
            if not is_authenticated(self):
                self.send_json({"ok": False, "error": "unauthorized"}, 401)
                return
            if REQUIRE_HTTPS and not request_is_secure(self):
                self.send_json({"ok": False, "error": "启动控制只接受 HTTPS 连接。"}, 400)
                return
            if not same_origin(self):
                self.send_json({"ok": False, "error": "请求来源不受信任。"}, 403)
                return
            body: object = {}
            try:
                body = json.loads(self.read_body().decode("utf-8")) if path == "/api/start" else {}
                if path == "/api/start":
                    result = CONTROL.start(body)
                else:
                    result = CONTROL.stop()
                self.send_json(result)
            except MiningControlError as error:
                self.send_json({"ok": False, "error": str(error)}, 400)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self.send_json({"ok": False, "error": "请求参数格式无效。"}, 400)
            except Exception:
                # Do not reflect subprocess output or request data to the client.
                self.send_json({"ok": False, "error": "控制操作失败，请检查节点和矿工日志。"}, 500)
            finally:
                if isinstance(body, dict):
                    body["mnemonic"] = ""
            return

        self.send_json({"ok": False, "error": "not found"}, 404)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), MonitorHandler)
    if TLS_ENABLED:
        if not TLS_CERT_FILE.is_file() or not TLS_KEY_FILE.is_file():
            raise RuntimeError(
                f"TLS certificate files not found: {TLS_CERT_FILE} / {TLS_KEY_FILE}"
            )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(TLS_CERT_FILE), str(TLS_KEY_FILE))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    else:
        scheme = "http"
    print(f"Quantus mining control listening on {scheme}://{BIND_HOST}:{BIND_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
