"""
ai.py — Claude AI allocation prompt and response parsing.
"""

import json
import time

import anthropic

from broker import ai_client
from config import MIN_ORDER_USD, TARGET_ALLOCATION, log


def ask_ai_for_allocation(portfolio: dict, new_cash: float) -> dict:
    """
    Ask Claude how to allocate new_cash to minimise drift from target weights.
    Retries up to 3 times with backoff on API overload (HTTP 529).
    """
    prompt = f"""You are managing a personal investment portfolio using a dollar-cost averaging strategy.
A new cash contribution of ${new_cash:.2f} has arrived and needs to be allocated.

Current portfolio state:
{json.dumps(portfolio, indent=2)}

Your job is to allocate the ${new_cash:.2f} across the target assets to bring the portfolio
closer to its target allocation. Prioritise the most underweight assets (most negative drift).

Rules:
- Only allocate to symbols in: {list(TARGET_ALLOCATION.keys())}
- Allocations must sum to exactly ${new_cash:.2f}
- Minimum order size is ${MIN_ORDER_USD}
- Briefly explain your reasoning

Respond ONLY with valid JSON — no markdown, no code fences:
{{
  "allocations": {{"SYMBOL": dollar_amount, ...}},
  "reasoning": "one or two sentences"
}}
"""
    for attempt in range(3):
        try:
            response = ai_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}]
            )
            break
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < 2:
                wait = 10 * (attempt + 1)
                log.warning(f"Anthropic overloaded — retrying in {wait}s")
                time.sleep(wait)
            else:
                raise

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    result = json.loads(raw.strip())
    log.info(f"AI reasoning: {result['reasoning']}")
    return result
