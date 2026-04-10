from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """당신은 보수적인 한국 주식 매매 어시스턴트입니다.
주어진 종목 데이터(가격, RSI, 거래량 비율, 최근 일봉 OHLCV)를 바탕으로
buy / sell / hold 중 하나의 결정과 0~1 사이 confidence, 한국어 근거를 제시하세요.

원칙:
- 확실하지 않으면 hold 하고 confidence는 0.5 이하로 낮춰라.
- 오직 기술적 신호(RSI 극단, 거래량 급증, 추세 전환)만 판단 근거로 사용하라.
  뉴스·펀더멘털·소문 등 외부 정보는 절대 추측하지 마라.
- confidence 0.8 이상은 여러 신호가 동시에 맞아떨어지는 극단 상황에만 부여하라.
- reasoning은 한국어 2~4문장, 구체적인 수치를 반드시 인용하라.
- **쉬운 말로** 쓰세요. 일반 투자자도 바로 이해할 수 있게. 어려운 한자 용어 대신
  "구매/판매/과매도(RSI 30 미만)/과매수(RSI 70 초과)/거래량 급증" 같은 쉬운 표현을
  사용하세요. 'RSI' 'MACD' 같은 영어 지표 이름은 그대로 쓰되 괄호로 간단히 풀어 쓰세요.
- 반드시 emit_decision 도구로만 응답하라. 일반 텍스트 응답 금지.
"""


DECISION_TOOL: dict[str, Any] = {
    "name": "emit_decision",
    "description": "단일 종목에 대한 매매 결정을 제출한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["buy", "sell", "hold"],
                "description": "최종 매매 결정",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "결정에 대한 확신도 (0~1)",
            },
            "reasoning": {
                "type": "string",
                "description": "한국어 2~4문장 근거. 수치 인용 필수.",
            },
        },
        "required": ["decision", "confidence", "reasoning"],
    },
}


@dataclass
class LlmDecision:
    decision: str
    confidence: float
    reasoning: str
    input_tokens: int
    output_tokens: int
    model: str
    cost_usd: float


class ClaudeSignalClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        input_price_per_mtok: float,
        output_price_per_mtok: float,
        temperature: float = 0.0,
        max_tokens: int = 600,
    ):
        self.client = Anthropic(api_key=api_key)
        self.model = model
        self.input_price = input_price_per_mtok
        self.output_price = output_price_per_mtok
        self.temperature = temperature
        self.max_tokens = max_tokens

    def _build_user_message(
        self,
        features: dict[str, Any],
        side_hint: str,
        recent_ohlcv: list[dict[str, Any]],
    ) -> str:
        recent_lines: list[str] = []
        for c in recent_ohlcv[-10:]:
            recent_lines.append(
                f"- {c['date']}: 시가 {c['open']:.0f} 고가 {c['high']:.0f} "
                f"저가 {c['low']:.0f} 종가 {c['close']:.0f} 거래량 {c['volume']:.0f}"
            )
        recent_block = "\n".join(recent_lines)

        return (
            f"[종목]\n{features['name']} ({features['code']})\n\n"
            f"[현재 상태]\n"
            f"- 현재가: {features['current_price']:.0f}원\n"
            f"- 전일 종가: {features['prev_close']:.0f}원\n"
            f"- 전일 대비: {features['change_pct']:+.2f}%\n"
            f"- RSI(14): {features['rsi']:.1f}\n"
            f"- 거래량 비율(20일 평균 대비): {features['volume_ratio']:.2f}x\n\n"
            f"[사전필터 힌트]\n"
            f"룰베이스 엔진이 이 종목을 '{side_hint}' 후보로 뽑았습니다. "
            f"최종 판단은 당신이 스스로 합니다. 힌트에 얽매이지 말고 데이터만 보세요.\n\n"
            f"[최근 10일 일봉 OHLCV]\n{recent_block}\n"
        )

    def decide(
        self,
        features: dict[str, Any],
        side_hint: str,
        recent_ohlcv: list[dict[str, Any]],
    ) -> LlmDecision:
        user_msg = self._build_user_message(features, side_hint, recent_ohlcv)

        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=SYSTEM_PROMPT,
            tools=[DECISION_TOOL],
            tool_choice={"type": "tool", "name": "emit_decision"},
            messages=[{"role": "user", "content": user_msg}],
        )

        tool_input: dict[str, Any] | None = None
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "emit_decision":
                tool_input = dict(block.input)
                break
        if tool_input is None:
            raise RuntimeError(f"LLM이 emit_decision 도구를 사용하지 않았습니다: {resp}")

        input_tokens = int(resp.usage.input_tokens)
        output_tokens = int(resp.usage.output_tokens)
        cost = (
            (input_tokens / 1_000_000) * self.input_price
            + (output_tokens / 1_000_000) * self.output_price
        )

        return LlmDecision(
            decision=str(tool_input["decision"]),
            confidence=float(tool_input["confidence"]),
            reasoning=str(tool_input["reasoning"]),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=self.model,
            cost_usd=cost,
        )
