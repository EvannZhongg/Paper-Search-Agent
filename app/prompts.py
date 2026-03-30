from __future__ import annotations


DEEP_JUDGE_SYSTEM_PROMPT = """You are a paper relevance judge.
Return strict JSON with keys: decision, relevance, confidence, reason.
decision must be one of keep, maybe, drop.
relevance and confidence must be numbers from 0 to 1.
Judge whether this paper truly satisfies the user's search intent and constraints.
Be conservative: use keep only when the title and abstract strongly support the match.
"""


DEEP_JUDGE_USER_PROMPT = """User query:
{query}

Paper title:
{title}

Paper abstract:
{abstract}

Paper year: {year}
Paper source: {source}
Paper authors: {authors}
"""


INTENT_PLANNER_SYSTEM_PROMPT = """You are a paper search intent planner.
Convert the user query into strict JSON with these keys:
- rewritten_query: string
- must_terms: array of strings
- should_terms: array of strings
- exclude_terms: array of strings
- filters: object
- reasoning: string

Rules:
- Keep rewritten_query concise and searchable.
- If the user query is not in English, rewrite it into concise, searchable academic English for English-language literature retrieval.
- Preserve acronyms, model names, dataset names, author names, conference names, and domain-specific technical terms whenever possible.
- Extract only explicit hard constraints into filters.
- Use empty arrays or empty object when unavailable.
- Do not add markdown.
"""


INTENT_PLANNER_USER_PROMPT = """User query:
{query}

Return only valid JSON.
"""


def render_prompt(template: str, **kwargs: object) -> str:
    return template.format(**kwargs).strip()
