def slugify(text: str) -> str:
    parts = []
    word = []
    for ch in text.lower():
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            word.append(ch)
        elif word:
            parts.append("".join(word))
            word = []
    if word:
        parts.append("".join(word))
    return "-".join(parts)
