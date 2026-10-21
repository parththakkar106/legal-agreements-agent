"""A stand-in for `claude -p` so agent tests run instantly and offline."""

from legal_agent.llm import LLMResult


def result(data: dict) -> LLMResult:
    return LLMResult(data=data, input_tokens=100, output_tokens=10, cost_usd=0.001, seconds=0.1)


def review_answer(**overrides) -> dict:
    answer = {"need_context": [], "meaning": "m", "impact": "i", "decision": "accept",
              "rule_ids": ["payment-terms"], "gdpr_citations": [], "counter_proposal": "",
              "confidence": 0.9, "injection_suspected": False}
    return {**answer, **overrides}
