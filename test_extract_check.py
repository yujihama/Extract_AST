# -*- coding: utf-8 -*-
from blueprint_ast_builder import extract_headings_from_blueprint

# 現在のblueprintで抽出
headings, total = extract_headings_from_blueprint(
    blueprint_path="富士フィルム_有価証券報告書_blueprint.json",
    text_path="富士フィルム_有価証券報告書.txt"
)

# 10280-10310行目の見出しを確認
print("=== 10280-10310行目付近の見出し ===")
for h in headings:
    if 10270 <= h["line_number"] <= 10350:
        print(f"L{h['level']} line {h['line_number']}: {h['title'][:60]}...")

# 10282行目が含まれているか確認
line_10282 = [h for h in headings if h["line_number"] == 10282]
print(f"\n=== 10282行目は抽出されたか? ===")
print(f"Result: {bool(line_10282)}")
if line_10282:
    print(f"Content: {line_10282[0]}")

