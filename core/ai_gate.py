# -*- coding: utf-8 -*-
"""门卫：判断一条群消息「值不值得回复」。

**要解决什么**：
  群里开着「接收所有消息」但关掉「需要 @ 才回复」时，机器人会对每条消息都回复 ——
  包括"哈哈哈""666""对"这类根本不需要接话的内容。全回会显得很傻，也不礼貌。
  这个模块用一个小模型先判断一次「该不该回」，再决定往不往下走。

**三种工作模式**（配置项 `ai_gate.mode`）：

  off      —— 完全不问（默认）。行为和以前一模一样。
  observe  —— **只记录，不拦截**。判断结果写日志和统计，但消息照常往下走。
              先这样跑几天，看判断准不准，再决定要不要真的拦。
  enforce  —— 真拦。判为"不回"就直接结束这条消息。

**两种判断后端**（配置项 `ai_gate.backend`）：

  llm      —— 用对话模型判断（填 ai_gate.base_url / model）。
              不需要训练数据，现在就能用；每次多一次模型调用。
  local    —— 用本地训好的分类器（tools/train_gate.py 的产物）。
              判得快（毫秒级）、不花钱；但需要自己标注数据训练。

**失败时怎么办**（配置项 `ai_gate.on_error`）：

  reply    —— 判断失败就当"该回"（默认）。模型挂了机器人还能正常工作，
              只是会多回几句 —— 这比"模型一挂机器人就哑了"要好得多。
  skip     —— 判断失败就当"不回"。只在你能接受机器人可能不吭声时用。
"""

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- 提示词
# 为什么这么写：
#   · 明确要求只输出 true/false —— 模型爱解释，解释多了就不好解析；
#   · 给几条判断依据 —— 比只给一句"你来判断"准得多；
#   · 强调"宁可该回" —— 误判成不回比多回一句伤害大（用户会觉得机器人坏了）。
DEFAULT_PROMPT = """你是群聊里的助手，负责判断一条消息需不需要你来接话。

判断为 true（需要回复）的情况：
- 有人在提问、求助、找答案（"这个怎么弄""有谁知道吗"）
- 有人在找助手、叫机器人、问功能（"机器人在吗"）
- 话题明显在等回应，不接会显得冷场

判断为 false（不需要回复）的情况：
- 纯情绪表达、附和、感叹（"哈哈""666""对""嗯""..."）
- 无意义的刷屏内容、乱码、纯表情
- 别人之间在聊、跟你无关的对话
- 自言自语、转发内容

拿不准时回答 true（宁可多接一句，也不要该说话时一声不吭）。

只输出一个单词：true 或 false。不要解释，不要加标点。"""

DEFAULT_MODEL_PROMPT = """以下是一段群聊记录，最后一行是刚刚收到的新消息。

{history}

新消息：{message}

这条新消息需要你接话吗？只回答 true 或 false。"""

# ---------------------------------------------------------------- 解析
_TRUE_WORDS = ("true", "yes", "1", "是", "需要", "应该", "该回", "回复")
_FALSE_WORDS = ("false", "no", "0", "否", "不需要", "不用", "不该", "不回")


def parse_decision(text: str) -> Optional[bool]:
    """把模型的输出解析成 True/False；认不出来返回 None。

    **为什么不能直接 `text == "true"`**：模型不会老实只输出一个词。
    实测常见输出有：
        true
        True.
        应该回复
        {"reply": true}
        可以回复，因为对方在提问。
    所以取开头若干字符，按关键词匹配；两边都能匹配时以**先出现的**为准。
    """
    if not text:
        return None
    lowered = str(text).strip().lower()[:200]
    if not lowered:
        return None

    # 先看是不是 JSON（有的模型会按 schema 输出）
    if lowered.startswith("{"):
        try:
            data = json.loads(str(text).strip())
            for key in ("reply", "should_reply", "result", "answer", "decision"):
                if key in data:
                    value = data[key]
                    if isinstance(value, bool):
                        return value
                    return parse_decision(str(value))
        except Exception:
            pass

    # 按"最先出现的那个词"决定
    first_true = len(lowered) + 1
    first_false = len(lowered) + 1
    for word in _TRUE_WORDS:
        pos = lowered.find(word)
        if pos != -1 and pos < first_true:
            first_true = pos
    for word in _FALSE_WORDS:
        pos = lowered.find(word)
        if pos != -1 and pos < first_false:
            first_false = pos

    if first_true == first_false == len(lowered) + 1:
        return None
    return first_true <= first_false


# ---------------------------------------------------------------- 本地分类器
class LocalGate:
    """加载 tools/train_gate.py 训出来的分类器。

    **懒加载**：只有真要用的时候才 import torch / sentence-transformers
    和加载 bge 模型（约 200MB 内存）。没配门卫的机器不该为它付代价。
    """

    def __init__(self, model_path: str, config_path: str, logger_obj=None):
        self.log = logger_obj or logging.getLogger("qqbot")
        self.model_path = model_path
        self.config_path = config_path
        self._model = None
        self._clf = None
        self._config: Dict[str, Any] = {}
        self._load_error = ""

    @staticmethod
    def _embedding_dim(model) -> Optional[int]:
        """读基座输出的向量维度，兼容新旧两套 API。

        sentence-transformers 把 `get_sentence_embedding_dimension()` 改名成了
        `get_embedding_dimension()`，旧名还能用但会打 FutureWarning。两个都试。
        """
        for name in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
            method = getattr(model, name, None)
            if callable(method):
                try:
                    value = method()
                    if value:
                        return int(value)
                except Exception:
                    continue
        return None

    def _ensure_loaded(self) -> bool:
        if self._model is not None:
            return True
        if self._load_error:
            return False
        try:
            import pickle
            with open(self.config_path, "r", encoding="utf-8") as fh:
                self._config = json.load(fh)
            with open(self.model_path, "rb") as fh:
                self._clf = pickle.load(fh)
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._config["base_model"])

            # **维度校验**：训练用的基座和推理用的基座必须一致。
            # 反例：用 bge-small（512 维）训了模型，后来配置里改成 bge-base（768 维），
            # 特征维度对不上 —— sklearn 会抛错，但如果没抛错而是静默出错更难查。
            # 模型权重里有 n_features_in_，直接跟基座的实际维度比。
            expected = self._config.get("dim")
            actual = self._embedding_dim(self._model)
            trained = getattr(self._clf, "n_features_in_", None)
            if expected and actual and int(expected) != int(actual):
                raise RuntimeError(
                    "基座维度对不上：模型是按 %s 维训练的，但 %s 输出 %s 维。"
                    "训练与推理必须用同一个基座模型。"
                    % (expected, self._config.get("base_model"), actual))
            if trained and actual and int(trained) != int(actual):
                raise RuntimeError(
                    "分类器维度对不上：分类器要求 %s 维，基座输出 %s 维。"
                    % (trained, actual))

            self.log.info("本地门卫模型已加载：%s（%s 维，准确率 %s）",
                          self._config.get("base_model"), actual,
                          (self._config.get("metrics") or {}).get("cv_accuracy"))
            return True
        except Exception as exc:
            self._load_error = str(exc)
            self.log.error("本地门卫模型加载失败：%s", exc)
            return False

    def decide(self, text: str) -> Optional[float]:
        """返回"该回"的概率（0~1）；失败返回 None。"""
        if not self._ensure_loaded():
            return None
        try:
            vec = self._model.encode([text], normalize_embeddings=True)
            prob = float(self._clf.predict_proba(vec)[0][1])
            return prob
        except Exception as exc:
            self.log.error("本地门卫判断失败：%s", exc)
            return None

    @property
    def threshold(self) -> float:
        try:
            return float(self._config.get("threshold", 0.5))
        except (TypeError, ValueError):
            return 0.5


# ---------------------------------------------------------------- 主类
class AIGate:
    """门卫：判断消息该不该回。

    每个机器人一个实例（配置是按机器人的）。
    """

    def __init__(self, config: Dict[str, Any] = None, logger_obj=None,
                 session=None):
        self.log = logger_obj or logging.getLogger("qqbot")
        self.config: Dict[str, Any] = dict(config or {})
        self.session = session
        self._local: Optional[LocalGate] = None
        self.last_error = ""
        self.stats = {"asked": 0, "yes": 0, "no": 0, "failed": 0}

    # ------------------------------------------------------------ 配置
    def update_config(self, config: Dict[str, Any]):
        self.config = dict(config or {})
        self._local = None          # 配置变了，本地模型要重新加载

    def _str(self, key: str, default: str = "") -> str:
        return str(self.config.get(key, default) or default).strip()

    def _bool(self, key: str, default: bool = False) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    @property
    def mode(self) -> str:
        mode = self._str("mode", "off").lower()
        return mode if mode in ("off", "observe", "enforce") else "off"

    @property
    def backend(self) -> str:
        backend = self._str("backend", "llm").lower()
        return backend if backend in ("llm", "local") else "llm"

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def on_error(self) -> str:
        value = self._str("on_error", "reply").lower()
        return value if value in ("reply", "skip") else "reply"

    # ------------------------------------------------------------ 可用性
    def usable(self) -> bool:
        """门卫是否真的能用（配好了）。"""
        if not self.enabled:
            return False
        if self.backend == "local":
            model_path = self._str("local_model")
            config_path = self._str("local_config")
            return bool(model_path and config_path and os.path.isfile(model_path))
        # llm 后端
        return bool(self._str("base_url") and self._str("model"))

    # ------------------------------------------------------------ 判断
    def judge(self, message: str, history: List[str] = None) -> Optional[bool]:
        """判断这条消息该不该回。

        返回 True（该回）/ False（不回）/ None（判断不了，调用方按 on_error 处理）。
        """
        if not self.enabled:
            return True             # 没开门卫 = 一律该回（老行为）
        if not message or not str(message).strip():
            return False            # 空消息不用回

        self.stats["asked"] += 1
        started = time.time()

        if self.backend == "local":
            result = self._judge_local(message, history)
        else:
            result = self._judge_llm(message, history)

        elapsed = (time.time() - started) * 1000

        if result is None:
            self.stats["failed"] += 1
            self.log.warning("门卫判断失败（%s 后端，耗时 %.0fms）：%s",
                             self.backend, elapsed, self.last_error or "未知原因")
            return None

        if result:
            self.stats["yes"] += 1
        else:
            self.stats["no"] += 1
        self.log.debug("门卫判断：%s（%s 后端，耗时 %.0fms）| %s",
                       "该回" if result else "不回", self.backend, elapsed,
                       str(message)[:40])
        return result

    # ------------------------------------------------------------ LLM 后端
    def _judge_llm(self, message: str, history: List[str]) -> Optional[bool]:
        base_url = self._str("base_url")
        model = self._str("model")
        api_key = self._str("api_key")
        if not base_url or not model:
            self.last_error = "没配 ai_gate.base_url / model"
            return None

        # 组装提示词：有上下文就带上下文
        prompt_template = self._str("prompt") or DEFAULT_PROMPT
        if history:
            history_text = "\n".join("- " + h for h in history if h)
            model_prompt = (self.config.get("model_prompt") or
                            DEFAULT_MODEL_PROMPT).format(history=history_text,
                                                         message=message)
        else:
            model_prompt = message

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt_template},
                {"role": "user", "content": model_prompt},
            ],
            "temperature": 0.0,          # 判断任务要稳定
            "max_tokens": 8,             # 只要一个词
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key

        endpoint = base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"

        try:
            import requests
            session = self.session or requests
            response = session.post(
                endpoint, headers=headers, json=payload,
                timeout=float(self._str("timeout_seconds", "10") or 10))
            if response.status_code != 200:
                self.last_error = "HTTP %s: %s" % (
                    response.status_code, (response.text or "")[:120])
                return None
            data = response.json()
            content = ((data.get("choices") or [{}])[0]
                       .get("message", {}) or {}).get("content", "")
            decision = parse_decision(content)
            if decision is None:
                self.last_error = "看不懂模型的回答：%r" % str(content)[:80]
            return decision
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            return None

    # ------------------------------------------------------------ 本地后端
    def _judge_local(self, message: str, history: List[str]) -> Optional[bool]:
        if self._local is None:
            self._local = LocalGate(self._str("local_model"),
                                    self._str("local_config"),
                                    logger_obj=self.log)
        # 本地模型训练时用的是"上下文 + 消息"拼起来的文本
        parts = [h for h in (history or []) if h]
        parts.append(message)
        text = "\n".join(parts)

        prob = self._local.decide(text)
        if prob is None:
            self.last_error = self._local._load_error or "本地模型判断失败"
            return None
        return prob >= self._local.threshold

    # ------------------------------------------------------------ 状态
    def status(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "backend": self.backend,
            "enabled": self.enabled,
            "usable": self.usable(),
            "on_error": self.on_error,
            "stats": dict(self.stats),
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------- 工厂
def load_gate_config(config) -> Dict[str, Any]:
    """从全局配置对象里取出 ai_gate 这一段。

    `Config` 只有 `get(*keys)`（返回整段），没有 `section()`。
    """
    try:
        section = config.get("ai_gate") if hasattr(config, "get") else {}
    except Exception:
        section = {}
    if not isinstance(section, dict):
        return {}
    return dict(section)
