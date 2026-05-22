import re

with open('src/prediction_bot/llm_analyst.py', 'r', encoding='utf-8') as f:
    text = f.read()

new_chain = '''def build_provider_chain(
    *,
    preferred: str | None = None,
    nvidia_api_key: str | None = None,
    nvidia_model: str | None = None,
    nvidia_temperature: float | None = None,
    nvidia_max_tokens: int | None = None,
) -> list[AnalystProvider]:
    """Build the provider chain in priority order: nvidia, stub.
    """
    nvidia_key = nvidia_api_key if nvidia_api_key is not None else os.getenv("NVIDIA_API_KEY", "").strip()
    nvidia_model_name = nvidia_model or os.getenv("NVIDIA_MODEL", "minimaxai/minimax-m2.7")
    try:
        nvidia_temp = float(nvidia_temperature if nvidia_temperature is not None else os.getenv("NVIDIA_TEMPERATURE", "0.6"))
    except (TypeError, ValueError):
        nvidia_temp = 0.6
    try:
        nvidia_max = int(nvidia_max_tokens if nvidia_max_tokens is not None else os.getenv("NVIDIA_MAX_TOKENS", "4096"))
    except (TypeError, ValueError):
        nvidia_max = 4096

    chain: list[AnalystProvider] = []
    if nvidia_key:
        chain.append(NvidiaProvider(
            api_key=nvidia_key,
            model=nvidia_model_name,
            temperature=nvidia_temp,
            max_tokens=nvidia_max,
        ))

    chain.append(StubProvider())

    if preferred:
        preferred = preferred.lower()
        for i, p in enumerate(chain):
            if p.name == preferred:
                chain.insert(0, chain.pop(i))
                break
    return chain'''

text = re.sub(r'def build_provider_chain\(.*?return chain', new_chain, text, flags=re.DOTALL)

with open('src/prediction_bot/llm_analyst.py', 'w', encoding='utf-8') as f:
    f.write(text)
