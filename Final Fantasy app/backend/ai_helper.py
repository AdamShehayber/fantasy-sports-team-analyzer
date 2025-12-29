import os
from openai import OpenAI

# Use your Windows environment variable API key
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

def explain_trade_report(trade_report: dict) -> str: # sends the trade details to OpenAI.
    """
    trade_report should contain:
        - other_roster (str)
        - give (list of names)
        - receive (list of names)
        - before_strength (float)
        - after_strength (float)
        - delta (float)
        - rationale (str)
    """

    give_names = ", ".join(trade_report.get("give", [])) or "None"
    receive_names = ", ".join(trade_report.get("receive", [])) or "None"

    prompt = f"""
You are a fantasy football trade advisor.

Here is a trade:

- Other roster: {trade_report.get("other_roster")}
- Give: {give_names}
- Receive: {receive_names}

Strength before trade: {trade_report.get("before_strength")}
Strength after trade: {trade_report.get("after_strength")}
Delta: {trade_report.get("delta")}
App decision: {trade_report.get("rationale")}

Write:
1) A simple explanation of whether this trade is good or bad.
2) Which positions improve or weaken.
3) 1–2 suggestions to fix weak areas.

Keep it under 150 words. Use simple language.
"""

    response = client.chat.completions.create(  # sends data
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "You explain fantasy football trades clearly and simply."},
            {"role": "user", "content": prompt},
        ],
    )

    return response.choices[0].message.content.strip()
