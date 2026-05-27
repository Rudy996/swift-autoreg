import json
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
import requests
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ProxyError, Timeout
CONFIG_PATH = Path("config.json")
REGISTER_URL = "https://shift-airdrop-backend.onrender.com/api/airdrop/register"
REF_PAGE_BASE = "https://airdrop.shiftrwa.xyz/register?ref="
REGISTER_PAGE_URL = "https://airdrop.shiftrwa.xyz/register"
ERROR_LOG_FILE = "errors.txt"
REF_POOL_FILE = "ref_pool.txt"
_file_lock = threading.Lock()
_print_lock = threading.Lock()
_proxy_cache: dict[str, list[str]] = {}
LogCallback = Callable[[str, str], None]
PoolSizeCallback = Callable[[int], None]
@dataclass
class AppConfig:
    ref_code: Optional[str]
    save_file: str
    timeout_sec: int
    proxy_file: str
    cycles: int
    threads: int
    proxy_retries: int
    ref_pool_file: str
@dataclass
class MultiConfig:
    accounts: int
    chance_without_ref: int
    chance_with_ref: int
    save_file: str
    timeout_sec: int
    proxy_file: str
    proxy_retries: int
    ref_pool_file: str
    threads: int
@dataclass
class RunResult:
    success: int
    failed: int
    total: int
class RefPool:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._codes: list[str] = []
        self._load()
    def _load(self) -> None:
        if not self._path.exists():
            self._codes = []
            return
        rows = [row.strip() for row in self._path.read_text(encoding="utf-8").splitlines() if row.strip()]
        self._codes = rows
    def _save_unlocked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(self._codes)
        if text:
            text += "\n"
        self._path.write_text(text, encoding="utf-8")
    def size(self) -> int:
        with self._lock:
            return len(self._codes)
    def pick_random(self) -> Optional[str]:
        with self._lock:
            if not self._codes:
                return None
            return random.choice(self._codes)
    def add(self, code: str) -> int:
        code = code.strip()
        with self._lock:
            if code and code not in self._codes:
                self._codes.append(code)
                self._save_unlocked()
            return len(self._codes)
@dataclass
class CycleLogger:
    run_no: int
    total: int
    on_line: Optional[LogCallback] = None
    def flush_ok(self, summary: str) -> None:
        self._emit(f"[{self.run_no}] {summary}", "success")
    def flush_fail(self, summary: str, details: list[str]) -> None:
        self._emit(f"[{self.run_no}] FAIL | {summary}", "error")
        if self.on_line:
            return
        for line in details:
            self._emit(f"[{self.run_no}]   {line}", "error")
    def _emit(self, text: str, kind: str) -> None:
        if self.on_line:
            self.on_line(text, kind)
            return
        with _print_lock:
            print(text, file=sys.stderr if kind == "error" else sys.stdout)
def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    if not path.exists():
        raise FileNotFoundError(f"Файл {path} не найден. Создай его перед запуском.")
    raw = json.loads(path.read_text(encoding="utf-8"))
    ref_code_raw = str(raw.get("ref_code", "")).strip()
    ref_code = ref_code_raw or None
    save_file = str(raw.get("save_file", "registered_wallets.txt")).strip()
    timeout_sec = int(raw.get("timeout_sec", 30))
    proxy_file = str(raw.get("proxy_file", "")).strip()
    ref_pool_file = str(raw.get("ref_pool_file", REF_POOL_FILE)).strip()
    cycles = int(raw.get("cycles", 1))
    threads = int(raw.get("threads", 1))
    proxy_retries = int(raw.get("proxy_retries", 3))
    missing = [name for name, value in [("proxy_file", proxy_file)] if not value]
    if missing:
        raise ValueError(f"В config.json отсутствуют обязательные поля: {', '.join(missing)}")
    if cycles < 1:
        raise ValueError("Поле cycles должно быть >= 1")
    if threads < 1:
        raise ValueError("Поле threads должно быть >= 1")
    if proxy_retries < 1:
        raise ValueError("Поле proxy_retries должно быть >= 1")
    return AppConfig(
        ref_code=ref_code,
        save_file=save_file,
        timeout_sec=timeout_sec,
        proxy_file=proxy_file,
        cycles=cycles,
        threads=threads,
        proxy_retries=proxy_retries,
        ref_pool_file=ref_pool_file,
    )
def config_for_run(
    base: AppConfig,
    *,
    ref_code: Optional[str],
    cycles: int,
    threads: int,
) -> AppConfig:
    if cycles < 1:
        raise ValueError("Циклы должны быть >= 1")
    if threads < 1:
        raise ValueError("Потоки должны быть >= 1")
    code = ref_code.strip() if ref_code else ""
    return replace(
        base,
        ref_code=code or None,
        cycles=cycles,
        threads=threads,
    )
def multi_config_from_base(
    base: AppConfig,
    *,
    accounts: int,
    chance_without_ref: int,
    chance_with_ref: int,
    threads: int,
) -> MultiConfig:
    if accounts < 1:
        raise ValueError("Аккаунты должны быть >= 1")
    if threads < 1:
        raise ValueError("Потоки должны быть >= 1")
    if chance_without_ref < 0 or chance_with_ref < 0:
        raise ValueError("Шансы не могут быть отрицательными")
    if chance_without_ref + chance_with_ref != 100:
        raise ValueError("Сумма шансов должна быть 100")
    return MultiConfig(
        accounts=accounts,
        chance_without_ref=chance_without_ref,
        chance_with_ref=chance_with_ref,
        save_file=base.save_file,
        timeout_sec=base.timeout_sec,
        proxy_file=base.proxy_file,
        proxy_retries=base.proxy_retries,
        ref_pool_file=base.ref_pool_file,
        threads=threads,
    )
def generate_random_user_agent() -> str:
    chrome_major = random.randint(118, 140)
    chrome_build = random.randint(0, 9999)
    firefox_major = random.randint(115, 135)
    platforms = [
        "Windows NT 10.0; Win64; x64",
        "Windows NT 11.0; Win64; x64",
        "Macintosh; Intel Mac OS X 10_15_7",
        "X11; Linux x86_64",
    ]
    platform = random.choice(platforms)
    templates = [
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_major}.0.{chrome_build}.0 Safari/537.36",
        f"Mozilla/5.0 ({platform}; rv:{firefox_major}.0) Gecko/20100101 Firefox/{firefox_major}.0",
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_major}.0.{chrome_build}.0 Safari/537.36 Edg/{chrome_major}.0.{chrome_build}.0",
    ]
    return random.choice(templates)
def _b58encode(data: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    num = int.from_bytes(data, "big")
    chars = []
    while num > 0:
        num, rem = divmod(num, 58)
        chars.append(alphabet[rem])
    pad = 0
    for b in data:
        if b == 0:
            pad += 1
        else:
            break
    return ("1" * pad) + ("".join(reversed(chars)) if chars else "")
def generate_solana_wallet() -> tuple[str, str, str]:
    try:
        from mnemonic import Mnemonic
        from nacl.signing import SigningKey
    except ImportError as exc:
        raise RuntimeError(
            "Для генерации кошелька нужны пакеты mnemonic и pynacl. Установи: pip install mnemonic pynacl"
        ) from exc
    mnemo = Mnemonic("english")
    mnemonic = mnemo.generate(strength=128)
    seed_64 = mnemo.to_seed(mnemonic, passphrase="")
    seed_32 = seed_64[:32]
    signing_key = SigningKey(seed_32)
    verify_key = signing_key.verify_key
    priv_32 = signing_key.encode()
    pub_32 = bytes(verify_key)
    secret_64 = priv_32 + pub_32
    private_key_b58 = _b58encode(secret_64)
    public_key_b58 = _b58encode(pub_32)
    return public_key_b58, private_key_b58, mnemonic
def build_session(user_agent: str, proxy: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "accept": "*/*",
            "origin": "https://airdrop.shiftrwa.xyz",
            "content-type": "application/json",
            "user-agent": user_agent,
            "referer": "https://airdrop.shiftrwa.xyz/",
        }
    )
    s.proxies.update({"http": proxy, "https": proxy})
    return s
def _load_proxies(proxy_file_path: str) -> list[str]:
    if proxy_file_path not in _proxy_cache:
        path = Path(proxy_file_path)
        if not path.exists():
            raise FileNotFoundError(f"Файл с прокси не найден: {proxy_file_path}")
        rows = [row.strip() for row in path.read_text(encoding="utf-8").splitlines() if row.strip()]
        if not rows:
            raise ValueError(f"Файл с прокси пустой: {proxy_file_path}")
        _proxy_cache[proxy_file_path] = rows
    return _proxy_cache[proxy_file_path]
def pick_random_proxy(proxy_file_path: str) -> tuple[str, int]:
    rows = _load_proxies(proxy_file_path)
    idx = random.randrange(len(rows))
    return rows[idx], idx + 1
def _registration_ref_url(ref_code: Optional[str]) -> str:
    if ref_code:
        return f"{REF_PAGE_BASE}{ref_code}"
    return REGISTER_PAGE_URL
def _registration_payload(wallet: str, ref_code: Optional[str]) -> dict:
    payload: dict = {"wallet": wallet}
    if ref_code:
        payload["refCode"] = ref_code
    return payload
def _parse_earned_ref_code(register_resp: requests.Response) -> Optional[str]:
    content_type = register_resp.headers.get("content-type", "")
    if "application/json" not in content_type:
        return None
    body = register_resp.json()
    if not isinstance(body, dict):
        return None
    earned = body.get("referralCode") or body.get("referral_code")
    if not earned:
        return None
    earned = str(earned).strip()
    return earned or None
def save_success_record(
    path: str,
    wallet: str,
    private_key_b58: str,
    mnemonic: str,
    *,
    used_ref_code: Optional[str] = None,
    earned_ref_code: Optional[str] = None,
) -> None:
    out = Path(path)
    line: dict = {
        "wallet": wallet,
        "private_key_b58": private_key_b58,
        "mnemonic": mnemonic,
    }
    if used_ref_code:
        line["used_ref_code"] = used_ref_code
    if earned_ref_code:
        line["earned_ref_code"] = earned_ref_code
    with _file_lock:
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
            f.flush()
def log_error(run_no: Optional[int], message: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = f"[{run_no}] " if run_no is not None else ""
    line = f"{ts} {prefix}{message}\n"
    with _file_lock:
        with Path(ERROR_LOG_FILE).open("a", encoding="utf-8") as f:
            f.write(line)
def is_proxy_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, (ProxyError, RequestsConnectionError, Timeout)):
        return True
    msg = str(exc).lower()
    markers = (
        "socks",
        "proxy",
        "connection",
        "host unreachable",
        "max retries exceeded",
        "failed to establish",
        "timed out",
    )
    return any(marker in msg for marker in markers)
def _short_error(exc: BaseException) -> str:
    msg = str(exc)
    low = msg.lower()
    if "host unreachable" in low:
        return "Host unreachable"
    if "timed out" in low:
        return "Timeout"
    if "max retries exceeded" in low:
        return "Нет соединения"
    return msg if len(msg) <= 60 else msg[:57] + "..."
def _pick_ref_for_multi(chance_without_ref: int, pool: RefPool) -> tuple[Optional[str], str]:
    roll = random.randint(1, 100)
    if roll <= chance_without_ref:
        return None, "без рефа"
    picked = pool.pick_random()
    if picked:
        return picked, f"реф {picked}"
    return None, "без рефа (пул пуст)"
def _execute_registration(
    *,
    proxy_file: str,
    timeout_sec: int,
    proxy_retries: int,
    ref_code: Optional[str],
    wallet_address: str,
    log: CycleLogger,
) -> tuple[int, Optional[str]]:
    user_agent = generate_random_user_agent()
    ref_url = _registration_ref_url(ref_code)
    payload = _registration_payload(wallet_address, ref_code)
    max_attempts = proxy_retries
    last_exc: Optional[BaseException] = None
    fail_details: list[str] = []
    for attempt in range(1, max_attempts + 1):
        selected_proxy, proxy_line = pick_random_proxy(proxy_file)
        session = build_session(user_agent, selected_proxy)
        try:
            page_resp = session.get(ref_url, timeout=timeout_sec)
            page_resp.raise_for_status()
            register_resp = session.post(
                REGISTER_URL,
                json=payload,
                timeout=timeout_sec,
            )
            register_resp.raise_for_status()
            earned = _parse_earned_ref_code(register_resp)
            return proxy_line, earned
        except Exception as exc:
            if is_proxy_connection_error(exc) and attempt < max_attempts:
                last_exc = exc
                log_error(
                    log.run_no,
                    f"прокси #{proxy_line} недоступен ({attempt}/{max_attempts}): {selected_proxy} — {exc}",
                )
                fail_details.append(f"прокси #{proxy_line}: {selected_proxy}")
                continue
            log_error(
                log.run_no,
                f"Цикл {log.run_no}/{log.total}: {exc} | прокси #{proxy_line}: {selected_proxy}",
            )
            log.flush_fail(
                f"Кошелёк: {wallet_address} | Прокси: #{proxy_line} | {_short_error(exc)}",
                [str(exc), selected_proxy],
            )
            raise
    if last_exc:
        log_error(
            log.run_no,
            f"Цикл {log.run_no}/{log.total}: {last_exc} | попыток {max_attempts}",
        )
        for detail in fail_details:
            log_error(log.run_no, detail)
        log.flush_fail(
            f"Кошелёк: {wallet_address} | Прокси недоступен ({max_attempts} поп.)",
            fail_details,
        )
        raise last_exc
    raise RuntimeError("Регистрация не завершена")
def run_registration(cfg: AppConfig, log: CycleLogger) -> None:
    wallet_address, generated_private_key, generated_mnemonic = generate_solana_wallet()
    proxy_line, earned = _execute_registration(
        proxy_file=cfg.proxy_file,
        timeout_sec=cfg.timeout_sec,
        proxy_retries=cfg.proxy_retries,
        ref_code=cfg.ref_code,
        wallet_address=wallet_address,
        log=log,
    )
    save_success_record(
        cfg.save_file,
        wallet_address,
        generated_private_key,
        generated_mnemonic,
        used_ref_code=cfg.ref_code,
        earned_ref_code=earned,
    )
    log.flush_ok(f"Кошелёк: {wallet_address} | Прокси: #{proxy_line} | Регистрация OK")
def run_multi_registration(
    cfg: MultiConfig,
    pool: RefPool,
    log: CycleLogger,
    on_pool_size: Optional[PoolSizeCallback] = None,
) -> None:
    wallet_address, generated_private_key, generated_mnemonic = generate_solana_wallet()
    used_ref, mode = _pick_ref_for_multi(cfg.chance_without_ref, pool)
    proxy_line, earned = _execute_registration(
        proxy_file=cfg.proxy_file,
        timeout_sec=cfg.timeout_sec,
        proxy_retries=cfg.proxy_retries,
        ref_code=used_ref,
        wallet_address=wallet_address,
        log=log,
    )
    save_success_record(
        cfg.save_file,
        wallet_address,
        generated_private_key,
        generated_mnemonic,
        used_ref_code=used_ref,
        earned_ref_code=earned,
    )
    if earned:
        size = pool.add(earned)
        if on_pool_size:
            on_pool_size(size)
    pool_note = f" | +реф {earned}" if earned else ""
    log.flush_ok(
        f"Кошелёк: {wallet_address} | {mode} | Прокси: #{proxy_line} | OK{pool_note}"
    )
def _run_cycle(cfg: AppConfig, run_no: int, on_line: Optional[LogCallback]) -> bool:
    log = CycleLogger(run_no, cfg.cycles, on_line=on_line)
    try:
        run_registration(cfg, log)
        return True
    except Exception:
        return False
def _run_multi_cycle(
    cfg: MultiConfig,
    pool: RefPool,
    run_no: int,
    on_line: Optional[LogCallback],
    on_pool_size: Optional[PoolSizeCallback] = None,
) -> bool:
    log = CycleLogger(run_no, cfg.accounts, on_line=on_line)
    try:
        run_multi_registration(cfg, pool, log, on_pool_size=on_pool_size)
        return True
    except Exception:
        return False
def run_all(cfg: AppConfig, on_line: Optional[LogCallback] = None) -> RunResult:
    success = 0
    failed = 0
    if cfg.threads > 1:
        workers = min(cfg.threads, cfg.cycles)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(_run_cycle, cfg, i + 1, on_line) for i in range(cfg.cycles)
            ]
            for future in as_completed(futures):
                if future.result():
                    success += 1
                else:
                    failed += 1
    else:
        for i in range(cfg.cycles):
            if _run_cycle(cfg, i + 1, on_line):
                success += 1
            else:
                failed += 1
    if on_line:
        on_line(f"Итого: успешно {success}, неуспешно {failed}, всего {cfg.cycles}", "info")
    return RunResult(success=success, failed=failed, total=cfg.cycles)
def run_multi_all(
    cfg: MultiConfig,
    pool: RefPool,
    on_line: Optional[LogCallback] = None,
    on_pool_size: Optional[PoolSizeCallback] = None,
) -> RunResult:
    success = 0
    failed = 0
    if cfg.threads > 1:
        workers = min(cfg.threads, cfg.accounts)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(_run_multi_cycle, cfg, pool, i + 1, on_line, on_pool_size)
                for i in range(cfg.accounts)
            ]
            for future in as_completed(futures):
                if future.result():
                    success += 1
                else:
                    failed += 1
    else:
        for i in range(cfg.accounts):
            if _run_multi_cycle(cfg, pool, i + 1, on_line, on_pool_size):
                success += 1
            else:
                failed += 1
    if on_line:
        on_line(
            f"Итого: успешно {success}, неуспешно {failed}, всего {cfg.accounts} | пул: {pool.size()}",
            "info",
        )
    return RunResult(success=success, failed=failed, total=cfg.accounts)
