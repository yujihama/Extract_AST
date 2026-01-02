# -*- coding: utf-8 -*-
import re

text = "(1) 当社の連結財務諸表は、「連結財務諸表の用語、様式及び作成方法に関する規則の一部を改正"
pattern = r"^\([0-9０-９]+\)\s*\S.+$"

print("Text:", text)
print("Pattern:", pattern)
print("Match:", bool(re.match(pattern, text)))

must_not = ["に従って", "に関わらず", "に至るまで", "に関する事項"]
for s in must_not:
    if s in text:
        print(f"Contains forbidden: '{s}' -> True")
    else:
        print(f"Contains forbidden: '{s}' -> False")

print("\nResult: Should be extracted?", bool(re.match(pattern, text)) and not any(s in text for s in must_not))

