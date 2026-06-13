"""
每日资讯生成器 · LLM 客户端
============================

职责：
1. 从 .secrets 读 provider 配置（apikey / base_url / model）
2. 从环境变量（AINEWS_LLM_*）读覆盖值（优先级最高）
3. 调 OpenAI 兼容协议的 chat 接口
4. 内置缓存（data/llm_cache/）+ 失败降级

设计原则：
- 配置 / 密钥完全分离：.secrets 不入 git（见 .gitignore）
- 环境变量解耦：AINEWS_LLM_<FIELD> 不带 provider 名
- 降级零侵入：缺 key / 调失败 → 返 None / 抛 LLMError
- 可被 generate_daily.py 调用，也可独立 import

环境变量优先级（高 → 低）：
  AINEWS_LLM_<FIELD>             通用环境变量（临时覆盖）
  AINEWS_LLM_<PROVIDER>_<FIELD>  .secrets 里的 provider-specific
  占位符 / 缺失                  → 降级（无 LLM）
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ============== 路径常量 ==============

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SECRETS_PATH = REPO_ROOT / ".secrets"
CACHE_DIR = REPO_ROOT / "data" / "llm_cache"

PROMPT_VERSION = "v1.0"  # 改 prompt 时升版本，旧缓存自动失效


# ============== 异常 ==============

class LLMError(Exception):
    """LLM 调用失败（网络 / 超时 / 鉴权 / 内容安全）"""


# ============== 配置加载 ==============

_PLACEHOLDER = re.compile(r"^<[A-Z_]+>$")  # <YOUR_KEY> / <your-key>


def _is_placeholder(value: str | None) -> bool:
    """判断是否仍是模板占位符（未替换的真实值）"""
    if not value:
        return True
    s = value.strip()
    return bool(_PLACEHOLDER.match(s)) or s.lower() in ("none", "null", "")


def parse_secrets_file(path: Path) -> dict[str, str]:
    """
    解析 key=value 格式的 .secrets 文件
    支持：# 注释 / 空行 / "..." 或 '...' 包裹 / 行尾  # 注释
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # 去引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        # 去行尾注释（" # xxx"）
        if " #" in value:
            value = value.split(" #", 1)[0].strip()
        out[key] = value
    return out


def _get_field(field: str, provider: str, secrets: dict[str, str]) -> str | None:
    """
    按优先级拿某个字段（API_KEY / BASE_URL / MODEL）：
      1. AINEWS_LLM_<FIELD>              （通用环境变量，最高）
      2. AINEWS_LLM_<PROVIDER>_<FIELD>   （.secrets provider-specific）
    """
    env_key = f"AINEWS_LLM_{field}"
    sec_key = f"AINEWS_LLM_{provider.upper()}_{field}"
    return os.environ.get(env_key) or secrets.get(sec_key)


def _get_param(name: str, secrets: dict[str, str], default: Any = None) -> Any:
    """读通用参数：env 优先 → secrets → 默认值"""
    env_key = f"AINEWS_LLM_{name}"
    sec_key = f"AINEWS_LLM_{name}"
    v = os.environ.get(env_key) or secrets.get(sec_key)
    if v is None or v == "":
        return default
    return v


def _resolve_provider(secrets: dict[str, str]) -> str:
    """决定激活哪个 provider：env 优先 → secrets → 默认 deepseek"""
    return (
        os.environ.get("AINEWS_LLM_PROVIDER")
        or secrets.get("AINEWS_LLM_PROVIDER")
        or "deepseek"
    )


# ============== 客户端 ==============

@dataclass
class LLMClient:
    """OpenAI 兼容协议的 LLM 客户端（任何兼容服务都可用）"""

    provider: str
    base_url: str
    model: str
    api_key: str
    max_tokens: int = 2000
    timeout: int = 30
    temperature: float = 0.3

    def __post_init__(self) -> None:
        # 延迟 import openai（保持本模块不依赖未装的包）
        from openai import OpenAI
        self._client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
        )

    def __repr__(self) -> str:
        return (
            f"LLMClient(provider={self.provider!r}, model={self.model!r}, "
            f"base_url={self.base_url!r})"
        )

    # ----- 缓存 -----
    def _cache_key(self, system: str, user: str) -> str:
        blob = f"{self.provider}|{self.model}|{PROMPT_VERSION}|{system}|{user}"
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]

    def _cache_get(self, key: str) -> str | None:
        if not CACHE_DIR.exists():
            return None
        p = CACHE_DIR / f"{key}.txt"
        if not p.exists():
            return None
        raw = p.read_text(encoding="utf-8")
        # 去掉 meta header（第一行 <!-- ... -->）
        return re.sub(r"^<!--.*?-->\s*\n", "", raw, count=1, flags=re.DOTALL).strip()

    def _cache_put(self, key: str, text: str) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = CACHE_DIR / f"{key}.txt"
        # 写一个简单的 meta header + 文本
        meta = f"<!-- provider={self.provider} model={self.model} v={PROMPT_VERSION} -->\n"
        p.write_text(meta + text, encoding="utf-8")

    # ----- 主调用 -----
    def chat(self, system: str, user: str, *, use_cache: bool = True) -> str:
        """
        单轮对话；带缓存 + 失败重试 1 次
        失败抛 LLMError（由调用方降级）
        """
        key = self._cache_key(system, user)
        if use_cache:
            cached = self._cache_get(key)
            if cached:
                return cached

        last_err: Exception | None = None
        for attempt in range(2):  # 失败重试 1 次
            try:
                r = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                )
                text = (r.choices[0].message.content or "").strip()
                if not text:
                    raise LLMError(f"[{self.provider}] 返回内容为空")
                self._cache_put(key, text)
                return text
            except Exception as e:
                last_err = e
                if attempt == 1:
                    break
        raise LLMError(f"[{self.provider}/{self.model}] 调用失败: {last_err}") from last_err


# ============== 顶层工厂 ==============

def load_llm_client(provider: str | None = None) -> LLMClient | None:
    """
    加载 LLM 客户端；任一缺失返回 None（让调用方降级到规则渲染）

    加载流程：
      1. 读 .secrets（key=value）
      2. 决定 provider
      3. 拿 3 个必填字段（API_KEY / BASE_URL / MODEL）
      4. 拿 3 个通用参数（MAX_TOKENS / TIMEOUT / TEMPERATURE）
      5. 缺 / 占位符 → None
    """
    secrets = parse_secrets_file(SECRETS_PATH)
    prov = provider or _resolve_provider(secrets)

    api_key  = _get_field("API_KEY",  prov, secrets)
    base_url = _get_field("BASE_URL", prov, secrets)
    model    = _get_field("MODEL",    prov, secrets)

    if _is_placeholder(api_key) or _is_placeholder(base_url) or _is_placeholder(model):
        return None

    try:
        max_tokens = int(_get_param("MAX_TOKENS", secrets, 2000))
        timeout    = int(_get_param("TIMEOUT",    secrets, 30))
        temperature = float(_get_param("TEMPERATURE", secrets, 0.3))
    except (TypeError, ValueError):
        max_tokens, timeout, temperature = 2000, 30, 0.3

    return LLMClient(
        provider=prov,
        base_url=base_url.strip(),
        model=model.strip(),
        api_key=api_key.strip(),
        max_tokens=max_tokens,
        timeout=timeout,
        temperature=temperature,
    )


# ============== CLI 调试 ==============

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="LLM 客户端调试")
    ap.add_argument("--provider", default=None, help="provider 覆盖")
    ap.add_argument("--prompt", default="用一句话介绍你自己。", help="user prompt")
    args = ap.parse_args()

    client = load_llm_client(args.provider)
    if client is None:
        print("[!] 未配置 LLM（.secrets 缺失或字段不全）")
    else:
        print(f"[i] 客户端：{client!r}")
        try:
            out = client.chat("你是一个简洁的助手。", args.prompt)
            print(f"[OK] 输出：\n{out}")
        except LLMError as e:
            print(f"[ERR] {e}")
